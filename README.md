# Jev Guardrail Benchmark

Tests **Jev** — TypeSafe's decision model — against three small LLMs at one job: acting as the input guardrail for an online shop's support chatbot, deciding which customer messages the bot should answer.

| | |
|---|---|
| **Models** | Jev 1.13 · Claude Haiku 4.5 · Gemini 3.5 Flash-Lite · GPT-5.6 Luna |
| **Per message** | pass or block · one of 10 categories · tone from 1 to 5 |
| **Dataset** | 100 hand-labelled customer messages in Malaysian English |
| **Measures** | correctness · latency · cost |

**One command runs it: `python core/benchmark.py --sample`** — about 5 minutes and 7 cents.

Running models costs money; rebuilding a report from saved results is free.

---

<details>
<summary>What exactly does it measure, and how is it kept fair?</summary>

**The task.** Each message is sorted into one of 10 categories. Five mean the bot should answer, five mean it should refuse:

| Pass | Block |
|---|---|
| `order_status` | `off_topic` |
| `returns_refunds` | `data_extraction` |
| `product_question` | `prompt_injection` |
| `payment_billing` | `abusive_language` |
| `stock_availability` | `out_of_catalogue` |

**The metrics.**
- **Decision accuracy** — right pass/block call. The one that matters for safety.
- **Category accuracy** — right category. Matters for routing.
- **Latency** — p50 and p90 *provider* latency: the provider's time to produce the full answer, without OpenRouter's routing or your network. From OpenRouter's generation record — `generation_time` for the chat models (OpenRouter streams from them internally), `latency` for Jev (not streamed). The noisy client-side round trip is kept in the CSVs as `round_trip_latency`.
- **Cost** — actual spend on the run, plus a projection per 10,000 checks.

**Keeping it fair.**
- Every model gets the same information. Jev receives it as typed questions; the LLMs as a system prompt plus a strict JSON schema. `core/prompt.py` asserts both carry identical wording.
- No sampling parameters on any model — all run at vendor defaults. Luna rejects `temperature`, Gemini 3.x ignores it, so pinning only Haiku would have been the unfair option.
- Calls are sequential and interleaved row by row, so a network blip hits every model equally.
- Structured output is enforced by the provider, not just requested in the prompt.

**Things to know when reading results.**
- Jev returns a *probability* for pass/block, not a verdict. The cut-off is ours — `JEV_NOUL_THRESHOLD`, set to 0.8.
- Jev asks its three questions independently, so its category and decision can contradict each other. The report counts how often.
- All four models go through OpenRouter, on each provider's standard tier. Provider latency strips out OpenRouter's routing, so the route doesn't affect the latency comparison.
- The chat models' latency field (`generation_time`) is documented by OpenRouter: from dispatch to the provider until the answer ends. Jev's (`latency`) is not — its only description is "Total latency in milliseconds". That it excludes routing is observed (it matches the provider attempt, and sits ~600 ms below the round trip), not documented.
- Luna reasons before answering on about a third of calls, which is its vendor default and can't be switched off. Those calls take ~3 s against ~1.3 s, which is what gives Luna its long p90.
- 100 rows is small — a gap of a few points is not a real difference.

</details>

<details>
<summary>How do I run an evaluation?</summary>

Needs Python 3.10+ and one package:

```bash
pip install matplotlib
```

Then set up `.env` (last section below).

| Command | Rows | Cost | Time | Writes to |
|---|---|---|---|---|
| `python core/benchmark.py --sample` | 50 (odd ids) | ~$0.07 | ~5 min | `results/test_<stamp>/` |
| `python core/benchmark.py --holdout` | the other 50 (even ids) | ~$0.07 | ~5 min | `results/holdout_<stamp>/` |
| `python core/benchmark.py --run` | all 100 | ~$0.14 | ~10 min | `results/<stamp>/` |

`--sample` and `--holdout` together cover all 100 rows with no overlap. Tune anything on `--sample`, then check it on `--holdout` — rows never seen during tuning give an honest score.

**Every run produces:**

```
results/<stamp>/
├── summary.md     verdict, headline table, where each model fails
├── charts/        correctness.png · latency.png · cost.png
├── csv/           one scored file per model
├── raw/           the exact request and response for every call, per model
└── run.json       models, prices, threshold, retries, errors
```

**Before spending anything:**

```bash
python core/evaluate.py --dry-run --row 3    # print the exact payloads, call nothing
python core/evaluate.py --smoke  --row 3     # one row through all four models
```

</details>

<details>
<summary>What other commands and flags are there?</summary>

**`core/benchmark.py`** — evaluate, then report. Takes the same flags as `evaluate.py`.

**`core/evaluate.py`** — calls the models and saves results. Costs money. Writes no report.

| Flag | Does |
|---|---|
| `--sample` | 50 odd rows → `results/test_<stamp>/` |
| `--holdout` | 50 even rows → `results/holdout_<stamp>/` |
| `--run` | all 100 rows → `results/<stamp>/` |
| `--smoke` | one row through every model, printed to screen |
| `--dry-run` | print the exact request payloads, call nothing |
| `--row N` | which row `--smoke` / `--dry-run` use (1-indexed) |
| `--limit N` | only the first N rows — cheap test of the full pipeline |
| `--backfill results/<stamp>` | add `provider_latency` to a run made before it existed. Free. |

**`core/report.py`** — builds `summary.md` and charts from saved results. Free.

| Command | Does |
|---|---|
| `python core/report.py` | rebuild the most recent run's report |
| `python core/report.py results/<stamp>` | rebuild a specific run's report |
| `python core/report.py --demo` | self-check the metric maths |

**Self-checks** — free, no API calls:

| Command | Checks |
|---|---|
| `python core/prompt.py` | Jev and the LLMs receive identical criteria |
| `python utils/build_dataset.py` | rebuilds `guardrail.csv` and checks its balance |

**Retries.** A call that times out or gets a 429 / 5xx is retried up to 3 times. Each run's `run.json` records how many calls needed a retry, per model.

</details>

<details>
<summary>How is the project laid out?</summary>

```
Jev/
├── core/
│   ├── prompt.py         the task: categories, sentiment scale, request shapes
│   ├── evaluate.py       calls the models, writes csv/, raw/, run.json
│   ├── report.py         builds summary.md and charts from saved results
│   └── benchmark.py      evaluate + report in one command
├── utils/
│   ├── build_dataset.py  source of truth for the 100 labelled messages
│   └── dataset-spec.md   why the dataset is designed the way it is
├── results/              one folder per run (gitignored)
├── guardrail.csv         the dataset - generated, don't edit by hand
└── .env.example          template for .env
```

**Where to make changes:**
- Change the task or prompt → `core/prompt.py`
- Change a label or prompt in the dataset → `utils/build_dataset.py`, then run it
- Change the report → `core/report.py`, then `python core/report.py` (free)

</details>

<details>
<summary>How do I set up .env?</summary>

```bash
cp .env.example .env
```

**One API key** — all four models go through OpenRouter:

```bash
OPENROUTER_API_KEY=     # openrouter.ai/keys
```

**Four model slots** — each has a `_MODEL` and a `_LABEL`:

| Slot | Default model | Label |
|---|---|---|
| `JEV_` | `typesafe/jev-1.13` | `jev` |
| `ANTHROPIC_` | `anthropic/claude-haiku-4.5` | `haiku` |
| `GEMINI_` | `google/gemini-3.5-flash-lite` | `gemini` |
| `OPENAI_` | `openai/gpt-5.6-luna` | `luna` |

- **Swap a model** by changing its `_MODEL` to any OpenRouter slug, e.g. `ANTHROPIC_MODEL=anthropic/claude-sonnet-5`. Filenames, charts and reports follow automatically.
- **`_LABEL`** names the CSV file and the report column.
- **Cost** is reported by OpenRouter for every call, at the provider's list price.

**Jev's threshold:**

```bash
JEV_NOUL_THRESHOLD=0.8
```

Jev's pass probability at or above this passes; below it blocks. 0.8 was chosen from early trial runs. Raise it to block more (safer, but refuses more real customers); lower it to pass more.

</details>
