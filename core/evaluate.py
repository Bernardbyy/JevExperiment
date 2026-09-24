"""Evaluation only: calls the models and writes csv/, raw/ and run.json.

  python core/evaluate.py --sample      50 rows  -> results/test_<stamp>/
  python core/evaluate.py --holdout     other 50 -> results/holdout_<stamp>/
  python core/evaluate.py --run         100 rows -> results/<stamp>/
  python core/evaluate.py --smoke       one row against all four models
  python core/evaluate.py --dry-run     print the exact payloads, call nothing

  python core/evaluate.py --backfill results/<stamp>
                                        add provider_latency to a run made before it
                                        existed (free - lookups only)

This costs money. It writes no report - use core/benchmark.py for both steps, or
core/report.py to rebuild a report from saved results for free.

Two latencies per call, both in ms:
  round_trip_latency  our stopwatch, this machine -> model -> back. Noisy.
  provider_latency    the provider's time to produce the full answer - the official
                      figure. From OpenRouter's generation record, looked up after
                      the run: generation_time for streamed calls (the chat models),
                      latency for non-streamed ones (Jev).
"""
import argparse
import csv
import io
import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import prompt

ROOT = Path(__file__).resolve().parent.parent   # project root
CSV_PATH = ROOT / "guardrail.csv"

OR_DECISIONS = "https://openrouter.ai/api/alpha/decisions"
OR_CHAT = "https://openrouter.ai/api/v1/chat/completions"
OR_GENERATION = "https://openrouter.ai/api/v1/generation?id="

# Four model slots, each with a swappable slug, all routed through OpenRouter -
# one key, one billing source, and every call has a generation record for cost
# and provider latency. Slugs and column labels come from .env so a newer
# lightweight model can be dropped in without touching code.
SLOTS = {
    "jev":        {"env": "JEV",        "default": "typesafe/jev-1.13"},
    "anthropic":  {"env": "ANTHROPIC",  "default": "anthropic/claude-haiku-4.5"},
    "gemini":     {"env": "GEMINI",     "default": "google/gemini-3.5-flash-lite"},
    "openai":     {"env": "OPENAI",     "default": "openai/gpt-5.6-luna"},
}
DEFAULT_LABELS = {"jev": "jev", "anthropic": "haiku",
                  "gemini": "gemini", "openai": "luna"}


def noul_threshold():
    """Jev's noul is P(pass); at or above this it passes. 0.5 is the untuned
    default - set JEV_NOUL_THRESHOLD to trade false blocks against missed ones."""
    return float(os.environ.get("JEV_NOUL_THRESHOLD") or 0.5)


# Display names for chart labels. Taken from the slug's "vendor/" prefix where it
# has one, so swapping in a new model relabels itself; OpenAI's direct slugs have
# no prefix, so each slot also carries a fallback.
VENDOR_NAMES = {"typesafe": "TypeSafe", "anthropic": "Anthropic", "google": "Google",
                "openai": "OpenAI", "meta-llama": "Meta", "mistralai": "Mistral",
                "qwen": "Qwen", "deepseek": "DeepSeek", "x-ai": "xAI"}
SLOT_VENDOR = {"jev": "TypeSafe", "anthropic": "Anthropic",
               "gemini": "Google", "openai": "OpenAI"}


def vendor(slot, slug):
    prefix = slug.split("/")[0] if "/" in slug else None
    return VENDOR_NAMES.get(prefix, prefix.title()) if prefix else SLOT_VENDOR[slot]


def slot_config():
    """{slot: {slug, label}} resolved from .env with defaults."""
    return {slot: {"slug": os.environ.get(f"{spec['env']}_MODEL") or spec["default"],
                   "label": os.environ.get(f"{spec['env']}_LABEL") or DEFAULT_LABELS[slot]}
            for slot, spec in SLOTS.items()}


# ------------------------------------------------------------------ transport

RETRYABLE = {429, 500, 502, 503, 504}


def post(url, body, headers, timeout=20, attempts=3):
    """POST with retry on transient failures, applied to every model alike.

    Gemini intermittently returns 503 or hangs outright - successful calls finish
    in ~1-2s, so a 20s timeout still leaves 10x headroom. Latency is that of the
    attempt that succeeded, so a retry doesn't count against the model's speed;
    the attempt count is recorded so reliability stays visible.
    """
    data = json.dumps(body).encode("utf-8")
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:      # before URLError: it's a subclass
            detail = e.read().decode("utf-8", "replace")[:600]
            if e.code in RETRYABLE and attempt < attempts:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"HTTP {e.code} from {url} after {attempt} "
                               f"attempt(s)\n{detail}") from None
        except (TimeoutError, urllib.error.URLError) as e:
            if attempt < attempts:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"{e} after {attempt} attempt(s)") from None
        ms = (time.perf_counter() - t0) * 1000
        LAST_CALL.update(url=url, request=body, response=payload,
                         round_trip_latency=round(ms), attempts=attempt)
        return payload, ms


# Filled by post() on every call so the raw request/response can be written to
# results/<run>/raw/<model>.jsonl without threading it through every adapter.
LAST_CALL = {}


def lookup_provider_latency(gen_id, attempts=5):
    """The provider's time to produce the full answer for one OpenRouter call, in ms.

    From OpenRouter's generation record, so OpenRouter's routing and your network
    are excluded. Which field holds it depends on how OpenRouter talked to the
    provider:
      streamed      generation_time. OpenRouter streams from chat providers even
                    when we send stream=false, and for those `latency` stops when
                    the answer starts arriving - only generation_time covers it all.
      not streamed  latency. Jev's Decisions endpoint; generation_time is 0 there,
                    and latency equals the provider attempt that returned 200.
    Records can take a few seconds to appear, so 404s are retried. None if it
    never resolves.
    """
    req = urllib.request.Request(OR_GENERATION + gen_id,
                                 headers={"Authorization": f"Bearer {_env('OPENROUTER_API_KEY')}"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))["data"]
            if data.get("streamed"):
                return data.get("generation_time") or None
            return data.get("latency")
        except urllib.error.HTTPError as e:
            if e.code not in (404, *RETRYABLE):
                return None
        except (TimeoutError, urllib.error.URLError):
            pass
        time.sleep(2 ** attempt)
    return None


def fill_provider_latency(raw_log, collected):
    """Look up provider_latency for every OpenRouter call and write it into the rows.

    raw_log / collected are {label: [...]}, joined on the dataset id. Lookups run
    in parallel - they are free and their own timing is irrelevant. Returns the
    number of successful calls per model that still have no figure.
    """
    jobs = []
    for label, lines in raw_log.items():
        rows_by_id = {r["id"]: r for r in collected[label]}
        for line in lines:
            gen_id = (line.get("response") or {}).get("id")
            if gen_id and "openrouter.ai" in line.get("url", "") and line["id"] in rows_by_id:
                jobs.append((rows_by_id[line["id"]], gen_id))
    with ThreadPoolExecutor(max_workers=8) as pool:
        found = pool.map(lookup_provider_latency, [g for _, g in jobs])
        for (row, _), ms in zip(jobs, found):
            row["provider_latency"] = "" if ms is None else ms
    return {label: sum(1 for r in collected[label] if r.get("pred_decision")
                       and r.get("provider_latency") in ("", None))
            for label in collected}


def _env(name):
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} not set - check .env")
    return v


def load_env():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def record(**kw):
    """Every adapter returns this shape."""
    kw.setdefault("cached_tokens", 0)
    return kw


# --------------------------------------------------------------------- models

def call_jev(text):
    raw, ms = post(OR_DECISIONS, prompt.jev(text, slot_config()["jev"]["slug"]),
                   {"Authorization": f"Bearer {_env('OPENROUTER_API_KEY')}",
                    "Content-Type": "application/json"})
    a, u = raw["answers"], raw.get("usage", {})
    noul = a["decision"]["noul"]
    return record(
        decision="pass" if noul >= noul_threshold() else "block",
        category=a["category"]["choice"],
        sentiment=a["sentiment"]["score"] + 1,   # 0-indexed levels -> 1-5
        confidence=max(noul, 1 - noul),          # noul carries no confidence field
        noul=noul,
        cat_confidence=a["category"].get("confidence"),
        sent_confidence=a["sentiment"].get("confidence"),
        probabilities=a["category"].get("probabilities"),
        round_trip_latency=ms,
        provider_latency=None,                   # looked up after the run
        cost=u.get("cost"),
        tok_in=u.get("input_tokens"), tok_out=u.get("output_tokens"),
        cached_tokens=u.get("cached_tokens", 0),
        raw=raw,
    )


def _parse(text, ms, cost, tok_in, tok_out, cached, raw):
    d = json.loads(text)
    return record(
        decision=d["decision"], category=d["category"],
        sentiment=d["sentiment"], confidence=d["confidence"],
        noul=None, cat_confidence=None, sent_confidence=None, probabilities=None,
        round_trip_latency=ms, provider_latency=None,   # looked up after the run
        cost=cost, tok_in=tok_in, tok_out=tok_out,
        cached_tokens=cached, raw=raw,
    )


def _openrouter_chat(slot, text):
    """Any OpenAI-compatible model via OpenRouter. require_parameters restricts
    routing to endpoints that honour json_schema, so no upstream silently drops it."""
    body = dict(prompt.chat(text, slot_config()[slot]["slug"]),
                provider={"require_parameters": True})
    raw, ms = post(OR_CHAT, body,
                   {"Authorization": f"Bearer {_env('OPENROUTER_API_KEY')}",
                    "Content-Type": "application/json"})
    u = raw.get("usage", {})
    return _parse(raw["choices"][0]["message"]["content"], ms, u.get("cost"),
                  u.get("prompt_tokens"), u.get("completion_tokens"),
                  u.get("cached_tokens", 0), raw)


CALLERS = {"jev": call_jev,
           "anthropic": lambda text: _openrouter_chat("anthropic", text),
           "gemini": lambda text: _openrouter_chat("gemini", text),
           "openai": lambda text: _openrouter_chat("openai", text)}


def models():
    """{label: caller} - label is what appears in filenames and reports."""
    cfg = slot_config()
    return {cfg[slot]["label"]: CALLERS[slot] for slot in CALLERS}


def jev_label():
    return slot_config()["jev"]["label"]



# ------------------------------------------------------------------------ cli

def rows():
    return list(csv.DictReader(io.open(CSV_PATH, encoding="utf-8")))


def dry_run(text):
    def show(title, url, body):
        print(f"\n{'=' * 78}\n{title}\n  POST {url}\n{'=' * 78}")
        print(json.dumps(body, indent=2, ensure_ascii=False))

    print(f"\n{'#' * 78}\n# SYSTEM PROMPT (shared by the three LLMs)\n{'#' * 78}")
    print(prompt.SYSTEM)
    c = slot_config()
    show(f"JEV  {c['jev']['slug']}", OR_DECISIONS,
         prompt.jev(text, c["jev"]["slug"]))
    for slot in ("anthropic", "gemini", "openai"):
        show(f"{slot.upper()}  {c[slot]['slug']}  (via OpenRouter)", OR_CHAT,
             dict(prompt.chat(text, c[slot]["slug"]), provider={"require_parameters": True}))


def smoke(row):
    print(f"\nprompt   {row['prompt']}")
    print(f"truth    {row['decision']} / {row['category']} / sentiment {row['sentiment']}\n")
    print(f"  {'model':<7} {'decision':<8} {'category':<19} {'sent':>5} {'conf':>5} "
          f"{'provider':>9} {'round trip':>11} {'cost':>11} {'tok i/o':>9}")
    for name, fn in models().items():
        try:
            LAST_CALL.clear()
            r = fn(row["prompt"])
            prov = lookup_provider_latency(r["raw"]["id"])
            prov = f"{prov}ms" if prov is not None else "-"
            print(f"  {name:<7} {r['decision']:<8} {r['category']:<19} "
                  f"{r['sentiment']:>5.2f} {r['confidence']:>5.2f} "
                  f"{prov:>9} {r['round_trip_latency']:>9.0f}ms ${r['cost']:>10.6f} "
                  f"{r['tok_in']:>4}/{r['tok_out']:<4}")
        except Exception as e:
            print(f"  {name:<7} FAILED: {e}")


LLM_COLUMNS = ["id", "prompt", "decision", "pred_decision", "category", "pred_category",
               "sentiment", "pred_sentiment", "round_trip_latency", "provider_latency",
               "cost"]
JEV_COLUMNS = LLM_COLUMNS + ["noul", "decision_conf", "category_conf",
                             "category_probabilities", "sentiment_conf"]


def to_row(row, r):
    """One CSV row. Blank predictions when the call failed."""
    out = {c: "" for c in JEV_COLUMNS}
    out.update(id=row["id"], prompt=row["prompt"], decision=row["decision"],
               category=row["category"], sentiment=row["sentiment"])
    if r is None:
        return out
    out.update(pred_decision=r["decision"], pred_category=r["category"],
               pred_sentiment=round(r["sentiment"], 2),
               round_trip_latency=round(r["round_trip_latency"]),
               provider_latency="" if r["provider_latency"] is None else r["provider_latency"],
               cost=repr(r["cost"]))
    if r["noul"] is not None:  # jev
        out.update(noul=r["noul"],
                   decision_conf=round(r["confidence"], 3),
                   category_conf=r["cat_confidence"],
                   category_probabilities=json.dumps(r["probabilities"]),
                   sentiment_conf=r["sent_confidence"])
    return out


def sample():
    """Every other row - one from each (category, sentiment) pair, 50 rows.

    Pairs are consecutive in the CSV, so the stride picks one of each while
    keeping all 10 categories and the full sentiment spread. Half the cost,
    which is what makes iterating on the methodology affordable.
    """
    return rows()[::2]


def holdout():
    """The other 50 rows - even ids, the complement of sample().

    Tune anything (e.g. Jev's noul threshold) on --sample, then check it here.
    These rows were never looked at while tuning, so the score is honest.
    """
    return rows()[1::2]


SPLITS = {"full": (rows, ""), "sample": (sample, "test_"), "holdout": (holdout, "holdout_")}


def run(limit=None, split="full"):
    pick, prefix = SPLITS[split]
    data = pick()[:limit]
    subset = split != "full"
    stamp = time.strftime("%Y-%m-%d-%H%M")
    outdir = ROOT / "results" / f"{prefix}{stamp}"
    outdir.mkdir(parents=True, exist_ok=True)

    active = models()
    collected = {m: [] for m in active}
    raw_log = {m: [] for m in active}
    errors, providers = [], {}

    # Interleaved and sequential: a network dip hits every model equally
    # rather than poisoning whichever one happened to be running.
    for i, row in enumerate(data, 1):
        for name, fn in active.items():
            LAST_CALL.clear()
            err = None
            try:
                r = fn(row["prompt"])
                providers.setdefault(name, r["raw"].get("provider"))
            except Exception as e:
                r, err = None, str(e)[:300]
                errors.append({"id": row["id"], "model": name, "error": err})
            collected[name].append(to_row(row, r))
            raw_log[name].append({"id": row["id"], "model": name,
                                  **LAST_CALL, **({"error": err} if err else {})})
        print(f"\r  {i}/{len(data)}  errors={len(errors)}", end="", flush=True)
    print()

    print("  looking up provider latency from OpenRouter ...", flush=True)
    missing = fill_provider_latency(raw_log, collected)

    rawdir = outdir / "raw"
    rawdir.mkdir(exist_ok=True)
    for name, lines in raw_log.items():
        with io.open(rawdir / f"{name}.jsonl", "w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")

    csvdir = outdir / "csv"
    csvdir.mkdir(exist_ok=True)
    for name, recs in collected.items():
        cols = JEV_COLUMNS if name == jev_label() else LLM_COLUMNS
        with io.open(csvdir / f"{name}.csv", "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(recs)

    cfg = slot_config()
    meta = {
        "run": stamp,
        "subset": subset,
        "split": split,
        "rows": len(data),
        "jev_label": cfg["jev"]["label"],
        "jev_noul_threshold": noul_threshold(),
        "models": {cfg[s]["label"]: cfg[s]["slug"] for s in cfg},
        "vendors": {cfg[s]["label"]: vendor(s, cfg[s]["slug"]) for s in cfg},
        "upstream_providers": providers,
        "cost_source": "reported by OpenRouter for every model",
        "notes": [
            "provider_latency is the provider's time to produce the full answer, from "
            "OpenRouter's generation record: generation_time for streamed calls, "
            "latency for non-streamed (Jev). round_trip_latency is our stopwatch.",
            "All four models go through OpenRouter.",
            "No sampling parameters are set - every model runs at vendor defaults, "
            "so none is guaranteed deterministic.",
        ],
        # calls that needed more than one attempt - reliability, per model
        "retried": {m: sum(1 for l in lines if l.get("attempts", 1) > 1)
                    for m, lines in raw_log.items()},
        # successful calls with no provider_latency - the lookup never resolved
        "provider_latency_missing": missing,
        "errors": errors,
    }
    io.open(outdir / "run.json", "w", encoding="utf-8").write(
        json.dumps(meta, indent=2))

    print(f"wrote {outdir}")
    return outdir


def backfill(outdir):
    """Add provider_latency to a run made before it existed, and rename the old
    latency_ms column. OpenRouter models only: their generation IDs are in raw/.
    Luna's figure came from a response header that was never saved, so it stays
    blank. Free - lookups don't run any model."""
    outdir = Path(outdir)
    raw_log, collected, cols = {}, {}, {}
    for f in sorted((outdir / "csv").glob("*.csv")):
        label = f.stem
        with io.open(f, encoding="utf-8-sig") as fh:
            rdr = csv.DictReader(fh)
            header = ["round_trip_latency" if c == "latency_ms" else c for c in rdr.fieldnames]
            rows_ = [r for r in rdr if r.get("id")]
        for r in rows_:
            if "latency_ms" in r:
                r["round_trip_latency"] = r.pop("latency_ms")
        if "provider_latency" not in header:
            header.insert(header.index("round_trip_latency") + 1, "provider_latency")
        collected[label], cols[label] = rows_, header
        raw_file = outdir / "raw" / f"{label}.jsonl"
        raw_log[label] = ([json.loads(l) for l in io.open(raw_file, encoding="utf-8")]
                          if raw_file.exists() else [])
    missing = fill_provider_latency(raw_log, collected)
    for label, rows_ in collected.items():
        with io.open(outdir / "csv" / f"{label}.csv", "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols[label], extrasaction="ignore")
            w.writeheader()
            w.writerows(rows_)
    meta_path = outdir / "run.json"
    meta = json.loads(io.open(meta_path, encoding="utf-8").read())
    meta["provider_latency_missing"] = missing
    meta["provider_latency_backfilled"] = True
    io.open(meta_path, "w", encoding="utf-8").write(json.dumps(meta, indent=2))
    print(f"backfilled {outdir}  missing={missing}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--run", action="store_true", help="all 100 rows -> results/<stamp>/")
    p.add_argument("--sample", action="store_true",
                   help="50 odd rows, one per (category, sentiment) -> results/test_<stamp>/")
    p.add_argument("--holdout", action="store_true",
                   help="the other 50 even rows, for checking what --sample tuned "
                        "-> results/holdout_<stamp>/")
    p.add_argument("--row", type=int, default=1, help="1-indexed row")
    p.add_argument("--limit", type=int, help="only the first N rows")
    p.add_argument("--backfill", metavar="RUN_DIR",
                   help="add provider_latency to an existing run (free)")
    a = p.parse_args()

    load_env()
    if a.backfill:
        return backfill(a.backfill)
    if a.run or a.sample or a.holdout:
        return run(a.limit, "sample" if a.sample else "holdout" if a.holdout else "full")
    elif a.dry_run:
        dry_run(rows()[a.row - 1]["prompt"])
    elif a.smoke:
        smoke(rows()[a.row - 1])
    else:
        p.error("pass --dry-run, --smoke, --sample, --holdout, --run or --backfill")


if __name__ == "__main__":
    main()
