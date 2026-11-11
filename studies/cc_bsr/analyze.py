"""Recompute the A1 queue example and D1 service comparison from published traces."""

import argparse
import csv
import gzip
import io
import json
import sys
import tarfile
from collections import defaultdict
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from v3.plots import COLORS, figure, plt, set_chinese_font, style


STRATEGIES = {
    "D1_native": "BBRv1 自行调节",
    "D1_equal": "固定 21 Mbps",
    "D1_pattern": "分段 28 → 14 Mbps",
}
FIRST_WINDOW_MS = 10
NOMINAL_BUDGET_BYTES = 30 * FIRST_WINDOW_MS * 125


def summarize_a1(source):
    prefix = "A1load_slow__00/"
    with tarfile.open(source / "trace-evidence.tar.xz") as archive:
        with archive.extractfile(prefix + "plan.json") as stream:
            plan = json.load(stream)
        with io.TextIOWrapper(archive.extractfile(prefix + "wire.csv"), encoding="utf-8") as stream:
            packets = list(csv.DictReader(stream))

    start_us = next(offset for offset, rate in plan["capacity"] if offset >= 3_000_000 and rate == 10)
    start = plan["origin"] + start_us
    end = start + 5_000
    window = [row for row in packets if start <= int(row["us"]) < end]
    queue_before = int(next(row for row in reversed(packets) if int(row["us"]) < start)["queue"])
    return {
        "run": prefix.rstrip("/"),
        "start_offset_us": start_us,
        "end_offset_us": end - plan["origin"],
        "ingress_ip_bytes": sum(int(row["bytes"]) for row in window if row["event"] == "in"),
        "forwarded_ip_bytes": sum(int(row["bytes"]) for row in window if row["event"] == "out"),
        "dropped_ip_bytes": sum(int(row["bytes"]) for row in window if row["event"] == "drop"),
        "queue_before_bytes": queue_before,
        "queue_at_end_bytes": int(window[-1]["queue"]),
    }


def summarize_d1(source):
    delivered = defaultdict(lambda: defaultdict(dict))
    with gzip.open(source / "BD-curves.csv.gz", "rt", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            case = row["case"]
            ms = float(row["ms"])
            if case in STRATEGIES and row["size"] == "50000" and 0 <= ms < FIRST_WINDOW_MS:
                task = (row["repeat"], row["task"])
                bins = delivered[case][task]
                if ms in bins:
                    raise ValueError(f"Duplicate bin for {case} task {task} at {ms} ms")
                bins[ms] = int(row["rx"])

    with (source / "task-summary.csv").open(encoding="utf-8") as stream:
        summaries = {row["case"]: row for row in csv.DictReader(stream) if row["group"] == "50000"}

    rows = []
    for case in STRATEGIES:
        summary = summaries[case]
        tasks = delivered[case]
        expected_bins = set(range(FIRST_WINDOW_MS))
        if len(tasks) != int(summary["tasks"]) or any(set(bins) != expected_bins for bins in tasks.values()):
            raise ValueError(f"Incomplete 1 ms traces for {case}")

        actual = median(sum(bins.values()) for bins in tasks.values())
        rows.append({
            "case": case,
            "tasks": len(tasks),
            "first_10ms_nominal_ip_bytes": NOMINAL_BUDGET_BYTES,
            "first_10ms_median_forwarded_ip_bytes": actual,
            "first_10ms_median_share_of_nominal_pct": round(100 * actual / NOMINAL_BUDGET_BYTES, 2),
            "first_10ms_nominal_gap_ip_bytes": NOMINAL_BUDGET_BYTES - actual,
            "first_20ms_median_peak_queue_bytes": float(summary["queue20_p50"]),
            "median_upload_ms": round(float(summary["upload_p50"]), 4),
            "completed_within_20ms": int(summary["on_time"]),
        })
    return rows


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0], lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def plot(rows, output):
    fig, (service, queue) = plt.subplots(1, 2, figsize=(11.2, 4.2), layout="constrained")
    labels = [STRATEGIES[row["case"]] for row in rows]
    colors = [COLORS[0], COLORS[1], COLORS[2]]
    fig.suptitle("相同容量轨迹下，发包节奏改变了转发量和排队", fontweight="bold")
    forwarded = [row["first_10ms_median_forwarded_ip_bytes"] / 1000 for row in rows]
    queue_peaks = [row["first_20ms_median_peak_queue_bytes"] / 1000 for row in rows]

    bars = service.bar(labels, forwarded, color=colors)
    service.bar_label(bars, fmt="%.2f", padding=-15, color="white", fontweight="bold")
    budget = NOMINAL_BUDGET_BYTES / 1000
    service.axhline(budget, color="#333333", linestyle="--", label=f"模拟空口的名义预算 {budget:g} kB")
    service.legend(loc="upper right", frameon=False)
    service.set_ylim(0, 47)
    style(service, "（甲）头 10 ms 内的实际转发量", "转发量中位数（kB）")

    bars = queue.bar(labels, queue_peaks, color=colors)
    queue.bar_label(bars, fmt="%.2f", padding=3)
    queue.set_ylim(0, 23)
    style(queue, "（乙）头 20 ms 内的队列峰值", "队列峰值中位数（kB）")
    for ax in (service, queue):
        ax.set_xlabel("50 kB 请求的发包策略（每组 72 次）")
        ax.set_axisbelow(True)
    figure(output, "d1-service", fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, help="Path to a font with Simplified Chinese glyphs.")
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "cc-bsr")
    args = parser.parse_args()
    set_chinese_font(args.font)
    source = ROOT / "results" / "v3"
    rows = summarize_d1(source)
    args.output.mkdir(parents=True, exist_ok=True)
    write_csv(args.output / "a1-queue.csv", [summarize_a1(source)])
    write_csv(args.output / "d1-service.csv", rows)
    plot(rows, args.output)


if __name__ == "__main__":
    main()
