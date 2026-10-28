#!/usr/bin/env python3
"""Analyze independently recorded dev-pipeline and baseline runs.

Input JSONL rows represent COMPLETED real runs. This instrument measures outcomes;
it does not run agents, invent token use, or infer success from review counts.
"""
import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

REQUIRED = {"task_id", "variant", "tier", "elapsed_s", "input_tokens",
            "output_tokens", "success", "regressions", "review_rounds"}
TIERS = {"T0", "T1", "T2", "T3"}
VARIANTS = {"baseline", "pipeline"}
# Instrument-field cap. Numbers at or above 2**53 lose float-exact arithmetic in
# the report and a token count this large is a recording error, not a measurement.
MAX_NUM = 10 ** 15


def validate(row):
    if not isinstance(row, dict) or not REQUIRED <= set(row):
        raise ValueError("missing required fields")
    if row["tier"] not in TIERS or row["variant"] not in VARIANTS:
        raise ValueError("invalid tier or variant")
    if not isinstance(row["task_id"], str) or not row["task_id"].strip():
        raise ValueError("invalid task_id")
    if type(row["success"]) is not bool:
        raise ValueError("success must be boolean")
    for key in ("elapsed_s", "input_tokens", "output_tokens", "regressions",
                "review_rounds"):
        val = row[key]
        if type(val) not in (int, float) or (isinstance(val, float)
                                             and not math.isfinite(val)) or val < 0:
            raise ValueError(f"{key} must be finite nonnegative number")
        if val > MAX_NUM:
            raise ValueError(f"{key} exceeds instrument maximum {MAX_NUM}")
    if any(int(row[key]) != row[key] for key in
           ("input_tokens", "output_tokens", "regressions", "review_rounds")):
        raise ValueError("token, regression and review counts must be integers")
    return row


def summarize(rows):
    paired = defaultdict(dict)
    for row in rows:
        row = validate(row)
        key = (row["task_id"], row["tier"])
        if row["variant"] in paired[key]:
            raise ValueError(f"duplicate run: {key} {row['variant']}")
        paired[key][row["variant"]] = row
    complete = [v for v in paired.values() if len(v) == 2]
    if not complete:
        raise ValueError("no paired baseline/pipeline task records")
    result = {"paired_tasks": len(complete), "unpaired_tasks": len(paired)-len(complete),
              "tiers": {}}
    tiers = sorted({v["baseline"]["tier"] for v in complete})
    for tier in tiers:
        items = [v for v in complete if v["baseline"]["tier"] == tier]
        metrics = {}
        for variant in ("baseline", "pipeline"):
            runs = [v[variant] for v in items]
            metrics[variant] = {
                "success_rate": statistics.mean(r["success"] for r in runs),
                "mean_elapsed_s": statistics.mean(r["elapsed_s"] for r in runs),
                "mean_tokens": statistics.mean(r["input_tokens"] + r["output_tokens"] for r in runs),
                "mean_regressions": statistics.mean(r["regressions"] for r in runs),
                "mean_review_rounds": statistics.mean(r["review_rounds"] for r in runs),
            }
        result["tiers"][tier] = {
            "pairs": len(items), **metrics,
            "pipeline_minus_baseline": {
                k: metrics["pipeline"][k] - metrics["baseline"][k]
                for k in metrics["baseline"]
            },
        }
    return result


def read_rows(jsonl):
    raw = Path(jsonl).read_text(encoding="utf-8").splitlines()
    rows = []
    for lineno, line in enumerate(raw, start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {lineno}: invalid JSON ({exc})") from exc
    return rows


def render(result):
    """Serialize the report as STRICT JSON; non-finite values fail closed."""
    try:
        return json.dumps(result, indent=2, allow_nan=False)
    except ValueError as exc:
        raise ValueError(f"report contains a non-serializable value: {exc}") from exc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl", type=Path, help="Recorded runs; never commit secrets/transcripts")
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    rows = read_rows(args.jsonl)
    result = render(summarize(rows))
    print(result)
    if args.output:
        args.output.write_text(result+"\n", encoding="utf-8")


if __name__ == "__main__":
    main()
