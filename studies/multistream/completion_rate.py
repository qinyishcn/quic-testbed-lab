"""Plot the mean complete-frame delivery rate within each latency budget."""

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from analyze import csv_write
from v3.plots import figure, plt, set_chinese_font, style


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT/"results/quic-four-stream-10kb-priority10")
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    environment = json.loads((args.data/"environment.json").read_text())
    frame_bytes = environment["frame_bytes"]
    with (args.data/"deadline-summary.csv").open(encoding="utf-8", newline="") as handle:
        summary = list(csv.DictReader(handle))
    rows = []
    for item in summary:
        budget = int(item["budget_ms"])
        batches = int(item["batches"])
        completed = int(item["completed_frames"])
        equivalent = int(item["equivalent_frames"])
        if budget <= 0 or batches <= 0 or not 0 <= completed <= equivalent <= batches*environment["streams_per_batch"]:
            raise ValueError("Invalid deadline completion counts")
        rows.append(dict(budget_ms=budget, batches=batches, frame_bytes=frame_bytes,
                         completed_frames=completed, equivalent_frames=equivalent,
                         baseline_mbps=completed*frame_bytes*8/(batches*budget*1000),
                         completeness_bound_mbps=equivalent*frame_bytes*8/(batches*budget*1000)))
    csv_write(args.data/"completion-rate.csv", rows)
    set_chinese_font(args.font)
    fig, ax = plt.subplots(figsize=(8.6, 4.9), layout="constrained")
    k = [r["budget_ms"] for r in rows]
    ax.plot(k, [r["baseline_mbps"] for r in rows], "o-", color="#465771", label="基线方案")
    ax.plot(k, [r["completeness_bound_mbps"] for r in rows], "s--", color="#087b70", label="完整性上界")
    style(ax, f"优先级 {environment['stream_priority']}，每帧 {frame_bytes/1000:g} kB",
          "完整帧交付速率（Mbps）")
    ax.set(xlabel="时延预算 K（ms）", xticks=k, xlim=(min(k), max(k)), ylim=(0, None))
    ax.legend(loc="best")
    figure(args.data, "completion-rate", fig)


if __name__ == "__main__":
    main()
