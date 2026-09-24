"""Evaluate, then report - the one command for a normal run.

  python core/benchmark.py --sample     50 rows  + summary.md
  python core/benchmark.py --holdout    other 50 + summary.md
  python core/benchmark.py --run        100 rows + summary.md

Takes the same flags as evaluate.py. The two steps stay separate so a report
can be changed and rebuilt (python core/report.py) without paying to re-run.
"""
import report
import evaluate

if __name__ == "__main__":
    outdir = evaluate.main()
    if outdir:  # only the run modes produce results; --smoke / --dry-run don't
        print(f"wrote {report.build(outdir)}")
