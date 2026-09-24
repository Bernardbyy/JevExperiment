"""Turns a results/<stamp>/ folder into summary.md plus charts.

Report only: reads saved results, makes no API calls, costs nothing. Rebuild a
report as often as you like after changing its format:

  python core/report.py                   most recent run
  python core/report.py results/2026-...  a specific one
  python core/report.py --demo            self-check the metric maths

Everything except the final "Takeaway" line is generated from the data.
"""
import csv
import io
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import prompt

ROOT = Path(__file__).resolve().parent.parent   # project root
CHECKS = 10_000

# dataviz reference palette, categorical slots 1 and 2 (validated adjacent pair)
BLUE, ORANGE = "#2a78d6", "#eb6834"
SURFACE, INK, MUTED = "#fcfcfb", "#0b0b0b", "#52514e"


# ----------------------------------------------------------------- loading

def latest_run():
    # by modification time, not name: "test_..." sorts after "2026-..." by name
    runs = sorted((ROOT / "results").glob("*/run.json"), key=lambda p: p.stat().st_mtime)
    if not runs:
        sys.exit("no runs found - python core/evaluate.py --run")
    return runs[-1].parent


def read(folder, model):
    path = folder / "csv" / f"{model}.csv"
    if not path.exists():
        return []
    # utf-8-sig and the id filter tolerate a CSV re-saved from Excel, which adds a
    # BOM and writes any formula cells below the data as id-less rows
    return [r for r in csv.DictReader(io.open(path, encoding="utf-8-sig")) if r.get("id")]


def scored(rows):
    return [r for r in rows if r["pred_decision"]]


def percentile(values, q):
    """Linear interpolation between the closest ranks - the same definition as
    Excel's PERCENTILE.INC / MEDIAN and numpy's default, so every figure can be
    checked in a spreadsheet with the built-in functions."""
    if not values:
        return 0
    v = sorted(values)
    pos = q * (len(v) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (pos - lo) * (v[hi] - v[lo])


def headline(rows):
    ok = scored(rows)
    if not ok:
        return None
    # provider_latency is the official latency: the model provider's own time,
    # without routing or network. Blank where it was never captured (e.g. Luna on
    # runs made before it existed), so p50/p90 are None rather than a guess.
    lat = [float(r["provider_latency"]) for r in ok if r.get("provider_latency")]
    return {
        "n": len(ok),
        "failed": len(rows) - len(ok),
        "decision": 100 * sum(r["pred_decision"] == r["decision"] for r in ok) / len(ok),
        "category": 100 * sum(r["pred_category"] == r["category"] for r in ok) / len(ok),
        "p50": percentile(lat, 0.50) if lat else None,
        "p90": percentile(lat, 0.90) if lat else None,
        "total": sum(float(r["cost"]) for r in ok),
        "cost": sum(float(r["cost"]) for r in ok) / len(ok) * CHECKS,
    }


# ------------------------------------------------------------- jev analysis

def implied(category):
    return "pass" if category in prompt.PASS_CATEGORIES else "block"


def raw_noul(row):
    """Jev's raw P(pass). Stored directly since the threshold became configurable;
    older runs only have decision_conf, which reconstructs correctly only because
    they all used a 0.5 threshold."""
    if row.get("noul"):
        return float(row["noul"])
    c = float(row["decision_conf"])
    return c if row["pred_decision"] == "pass" else 1 - c


def contradictions(rows):
    """Rows where Jev's own choice answer disagrees with its noul answer."""
    return [r for r in scored(rows) if implied(r["pred_category"]) != r["pred_decision"]]


def threshold_sweep(rows, steps=(0.5, 0.6, 0.7, 0.8, 0.9), active=None):
    ok = scored(rows)
    steps = sorted(set(steps) | ({active} if active is not None else set()))
    out = []
    for t in steps:
        acc = sum(("pass" if raw_noul(r) >= t else "block") == r["decision"] for r in ok)
        blocked = sum(raw_noul(r) < t and r["decision"] == "pass" for r in ok)
        missed = sum(raw_noul(r) >= t and r["decision"] == "block" for r in ok)
        out.append((t, 100 * acc / len(ok), blocked, missed))
    return out


# ----------------------------------------------------------------- charts

def _style(ax, ylabel):
    ax.set_facecolor(SURFACE)
    ax.figure.patch.set_facecolor(SURFACE)
    ax.set_ylabel(ylabel, color=MUTED, fontsize=9)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.grid(axis="y", color="#e6e5e1", linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#d8d7d3")


def _label(ax, bars, fmt):
    for b in bars:
        ax.annotate(fmt.format(b.get_height()),
                    (b.get_x() + b.get_width() / 2, b.get_height()),
                    ha="center", va="bottom", fontsize=8.5, color=INK,
                    xytext=(0, 2), textcoords="offset points")


# Last-resort vendor guess for old runs whose slugs carry no "vendor/" prefix
_GUESS = {"gpt": "OpenAI", "gemini": "Google", "claude": "Anthropic", "jev": "TypeSafe"}


def _ticks(models, slugs, vendors):
    """Vendor on top, bare model slug underneath - e.g. Google / gemini-3.5-flash-lite.

    Older runs predate the "vendors" field in run.json, so fall back to the
    slug's own prefix, then to the label.
    """
    out = []
    for m in models:
        slug = slugs.get(m, "")
        bare = slug.split("/")[-1]
        name = (vendors.get(m)
                or (slug.split("/")[0].title() if "/" in slug else None)
                or next((v for k, v in _GUESS.items() if bare.startswith(k)), m))
        out.append(name + "\n" + slug.split("/")[-1])
    return out


def _grouped(path, title, ylabel, models, a_vals, a_name, b_vals, b_name, fmt,
             top_pad, slugs, vendors, cap=None):
    fig, ax = plt.subplots(figsize=(7, 3.4))
    x = range(len(models))
    w = 0.38
    ba = ax.bar([i - w / 2 - 0.01 for i in x], a_vals, w, label=a_name, color=BLUE)
    bb = ax.bar([i + w / 2 + 0.01 for i in x], b_vals, w, label=b_name, color=ORANGE)
    _label(ax, ba, fmt)
    _label(ax, bb, fmt)
    ax.set_xticks(list(x))
    ax.set_xticklabels(_ticks(models, slugs, vendors), fontsize=8)
    top = max(max(a_vals), max(b_vals)) * top_pad
    ax.set_ylim(0, min(top, cap) if cap else top)
    ax.set_title(title, color=INK, fontsize=11, loc="left", pad=10)
    _style(ax, ylabel)
    ax.legend(frameon=False, fontsize=9, labelcolor=MUTED, ncol=2,
              loc="upper right", bbox_to_anchor=(1, 1.16))
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def charts(folder, stats, MODELS, slugs, vendors):
    models = [m for m in MODELS if stats.get(m)]
    folder = folder / "charts"
    folder.mkdir(exist_ok=True)

    _grouped(folder / "correctness.png", "Correctness (higher is better)", "% correct",
             models, [stats[m]["decision"] for m in models], "decision",
             [stats[m]["category"] for m in models], "category", "{:.0f}%", 1.25,
             slugs, vendors, cap=108)

    timed = [m for m in models if stats[m]["p50"] is not None]
    if timed:
        _grouped(folder / "latency.png", "Provider latency (lower is better)", "milliseconds",
                 timed, [stats[m]["p50"] for m in timed], "p50",
                 [stats[m]["p90"] for m in timed], "p90", "{:.0f}", 1.25, slugs, vendors)

    fig, ax = plt.subplots(figsize=(7, 3.4))
    vals = [stats[m]["cost"] for m in models]
    bars = ax.bar(range(len(models)), vals, 0.55, color=BLUE)
    ax.set_xticks(range(len(models)))
    ax.set_xticklabels(_ticks(models, slugs, vendors), fontsize=8)
    _label(ax, bars, "${:.2f}")
    ax.set_ylim(0, max(vals) * 1.2)
    ax.set_title(f"Cost per {CHECKS:,} checks (lower is better)",
                 color=INK, fontsize=11, loc="left", pad=10)
    _style(ax, "USD")
    fig.tight_layout()
    fig.savefig(folder / "cost.png", dpi=160)
    plt.close(fig)


# ----------------------------------------------------------------- report

def _ms(v):
    return "-" if v is None else f"{v:.0f}ms"


def best(stats, key, lowest=False):
    pool = {m: s[key] for m, s in stats.items() if s}
    return min(pool, key=pool.get) if lowest else max(pool, key=pool.get)


def tied(stats, key):
    """All models sharing the top value - a single 'winner' misleads on a tie."""
    top = max(s[key] for s in stats.values())
    return [m for m, s in stats.items() if round(s[key]) == round(top)]


def verdict(stats, jev_rows, jev_label="jev", threshold=0.5):
    out = ["## 1. Verdict", ""]
    if not stats:
        return out + ["No scored rows.", ""]

    acc, cheap = best(stats, "decision"), best(stats, "cost", True)
    timed = {m: s for m, s in stats.items() if s["p50"] is not None}
    fast = best(timed, "p50", True) if timed else None
    out += [
        f"- **Most accurate:** {', '.join(f'`{m}`' for m in tied(stats, 'decision'))} "
        f"at {stats[acc]['decision']:.0f}% decision accuracy"
        f"{' (tied)' if len(tied(stats, 'decision')) > 1 else ''}.",
        (f"- **Fastest:** `{fast}` at {stats[fast]['p50']:.0f}ms p50 provider latency."
         if fast else "- **Fastest:** no provider latency recorded."),
        f"- **Cheapest:** `{cheap}` at ${stats[cheap]['cost']:.2f} per {CHECKS:,} checks "
        f"({stats[best(stats, 'cost')]['cost'] / stats[cheap]['cost']:.0f}x less than the dearest).",
    ]

    if jev_rows:
        contra = contradictions(jev_rows)
        sweep = threshold_sweep(jev_rows, active=threshold)
        active = next(row for row in sweep if row[0] == threshold)
        derived = 100 * sum(implied(r["pred_category"]) == r["decision"]
                            for r in scored(jev_rows)) / len(scored(jev_rows))
        out += [
            "",
            f"- **`{jev_label}` contradicts itself on {len(contra)}/{len(scored(jev_rows))} rows.** Its three "
            "questions are evaluated independently, so `choice` can answer "
            "`prompt_injection` while `noul` answers *pass*. An LLM emitting one JSON "
            "object cannot do this.",
            f"  - Deriving the decision from `category` instead scores **{derived:.0f}%** "
            f"(vs {stats[jev_label]['decision']:.0f}% from the noul at threshold {threshold}).",
            f"- **At threshold {threshold}:** {active[2]} false blocks, {active[3]} "
            "missed blocks.",
        ]

    out += ["", "**Takeaway:** <!-- one line, written by a human -->", ""]
    return out


def build(folder):
    meta = json.loads(io.open(folder / "run.json", encoding="utf-8").read())
    MODELS = list(meta["models"])          # whatever this run actually used
    JEV = meta.get("jev_label", "jev")     # only that slot has the extra columns
    THRESHOLD = meta.get("jev_noul_threshold", 0.5)
    data = {m: read(folder, m) for m in MODELS}
    stats = {m: headline(rows) for m, rows in data.items() if rows}
    stats = {m: s for m, s in stats.items() if s}
    charts(folder, stats, MODELS, meta["models"], meta.get("vendors", {}))

    out = [f"# Guardrail benchmark - {meta['run']}", ""]
    out += verdict(stats, data.get(JEV), JEV, THRESHOLD)

    out += ["## 2. Headline", "",
            "![Correctness](charts/correctness.png)", "",
            "![Latency](charts/latency.png)", "",
            "![Cost](charts/cost.png)", "",
            "| model | decision acc | category acc | p50 latency | p90 latency | cost | "
            f"cost / {CHECKS:,} |",
            "|---|---|---|---|---|---|---|"]
    for m in MODELS:
        s = stats.get(m)
        out.append(f"| {m} | {s['decision']:.0f}% | {s['category']:.0f}% | "
                   f"{_ms(s['p50'])} | {_ms(s['p90'])} | ${s['total']:.4f} | ${s['cost']:.2f} |"
                   if s else f"| {m} | - | - | - | - | - | - |")
    out.append("")

    out += ["- " + " · ".join(f"**{m}** `{meta['models'][m]}`" for m in MODELS), ""]

    failed = {m: s["failed"] for m, s in stats.items() if s["failed"]}
    if failed:
        out += [f"- Failed calls excluded: {failed}. See `run.json`.", ""]

    if data.get(JEV):
        out += [f"**{JEV} decision threshold** (raw `noul` is stored, so this is free to re-tune):", "",
                "| threshold | accuracy | false blocks | missed blocks |", "|---|---|---|---|"]
        for t, acc, fb, mb in threshold_sweep(data[JEV], active=THRESHOLD):
            mark = " *(this run)*" if t == THRESHOLD else ""
            out.append(f"| {t}{mark} | {acc:.0f}% | {fb} | {mb} |")
        out.append("")

    out += ["## 3. Where each model fails", "",
            "- Each cell is **correct / total** for that category. Anything short of "
            "total is bolded.",
            "- Decision errors are safety failures; category errors are routing failures.", ""]
    for title, pred, truth in (("Decision (pass / block)", "pred_decision", "decision"),
                               ("Category", "pred_category", "category")):
        out += [f"**{title}**", "",
                "| category | " + " | ".join(MODELS) + " |",
                "|---|" + "---|" * len(MODELS)]
        for category in prompt.CATEGORIES:
            cells = []
            for m in MODELS:
                rows = [r for r in scored(data.get(m, [])) if r["category"] == category]
                if not rows:
                    cells.append("-")
                    continue
                hit = sum(r[pred] == r[truth] for r in rows)
                cell = f"{hit}/{len(rows)}"
                cells.append(f"**{cell}**" if hit < len(rows) else cell)
            marker = "" if category in prompt.PASS_CATEGORIES else " *(block)*"
            out.append(f"| `{category}`{marker} | " + " | ".join(cells) + " |")
        out.append("")

    path = folder / "summary.md"
    io.open(path, "w", encoding="utf-8").write("\n".join(out))
    return path


def demo():
    # matches Excel: MEDIAN(1..10) = 5.5, PERCENTILE.INC(1..10, 0.9) = 9.1
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.5) == 5.5
    assert abs(percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9) - 9.1) < 1e-9
    # vendor from run.json wins; old runs fall back to the slug prefix, then label
    assert _ticks(["g"], {"g": "google/gemini-x"}, {"g": "Google"}) == ["Google\ngemini-x"]
    assert _ticks(["g"], {"g": "google/gemini-x"}, {}) == ["Google\ngemini-x"]
    assert _ticks(["l"], {"l": "gpt-5.6-luna"}, {}) == ["OpenAI\ngpt-5.6-luna"]
    assert _ticks(["x"], {"x": "mystery-1"}, {}) == ["x\nmystery-1"]
    assert percentile([5], 0.9) == 5 and percentile([], 0.5) == 0
    assert implied("order_status") == "pass" and implied("off_topic") == "block"
    assert raw_noul({"decision_conf": "0.8", "pred_decision": "pass"}) == 0.8
    # stored noul wins - reconstruction would be wrong at a non-0.5 threshold
    assert raw_noul({"noul": "0.7", "decision_conf": "0.7", "pred_decision": "block"}) == 0.7
    # a noul of 0.7 passes at 0.5 but blocks at 0.8, and the active
    # threshold always appears in the sweep even if it isn't a default step
    row = {"pred_decision": "block", "decision": "block", "noul": "0.7"}
    sweep = {t: acc for t, acc, *_ in threshold_sweep([row], active=0.75)}
    assert 0.75 in sweep and sweep[0.5] == 0 and sweep[0.8] == 100, sweep
    assert abs(raw_noul({"decision_conf": "0.8", "pred_decision": "block"}) - 0.2) < 1e-9

    rows = [{"pred_decision": "pass", "decision": "pass", "pred_category": "order_status",
             "category": "order_status", "pred_sentiment": "3.5", "sentiment": "3",
             "round_trip_latency": "900", "provider_latency": "100",
             "cost": "0.0001", "decision_conf": "0.9"}]
    h = headline(rows)
    assert h["decision"] == 100
    assert abs(h["cost"] - 1.0) < 1e-9
    assert contradictions(rows) == []
    # choice says block, noul says pass -> a contradiction
    bad = dict(rows[0], pred_category="off_topic")
    assert len(contradictions([bad])) == 1
    print("demo ok")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--demo" in sys.argv:
        demo()
    else:
        print(f"wrote {build(Path(args[0]) if args else latest_run())}")
