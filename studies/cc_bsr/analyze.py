"""Recreate the A1 drop example and D1 service comparison from published traces."""

import argparse
import csv
import gzip
import io
import json
import sys
import tarfile
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path
from statistics import median

from matplotlib.ticker import MultipleLocator

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


def read_a1(source):
    prefix = "A1load_slow__00/"
    with tarfile.open(source / "trace-evidence.tar.xz") as archive:
        with archive.extractfile(prefix + "plan.json") as stream:
            plan = json.load(stream)
        with io.TextIOWrapper(archive.extractfile(prefix + "wire.csv"), encoding="utf-8") as stream:
            packets = list(csv.DictReader(stream))
        with io.TextIOWrapper(archive.extractfile(prefix + "samples.csv"), encoding="utf-8") as stream:
            samples = list(csv.DictReader(stream))
    return prefix.rstrip("/"), plan, packets, samples


def summarize_a1(run, plan, packets):
    start_us = next(offset for offset, rate in plan["capacity"] if offset >= 3_000_000 and rate == 10)
    start = plan["origin"] + start_us
    end = start + 5_000
    window = [row for row in packets if start <= int(row["us"]) < end]
    queue_before = int(next(row for row in reversed(packets) if int(row["us"]) < start)["queue"])
    return {
        "run": run,
        "start_offset_us": start_us,
        "end_offset_us": end - plan["origin"],
        "ingress_ip_bytes": sum(int(row["bytes"]) for row in window if row["event"] == "in"),
        "forwarded_ip_bytes": sum(int(row["bytes"]) for row in window if row["event"] == "out"),
        "dropped_ip_bytes": sum(int(row["bytes"]) for row in window if row["event"] == "drop"),
        "queue_before_bytes": queue_before,
        "queue_at_end_bytes": int(window[-1]["queue"]),
    }


def plot_a1(plan, samples, drop_us, output):
    pacing_times = [int(row["us"]) - plan["origin"] for row in samples]
    pacing_rates = [int(row["pacing_bps"]) / 1e6 for row in samples]
    capacity_times, capacity_rates = zip(*plan["capacity"])

    def rates_in_window(left, right):
        times = sorted({left, right} | {t for t in pacing_times if left < t < right}
                       | {t for t in capacity_times if left < t < right})
        pacing = [pacing_rates[bisect_right(pacing_times, t) - 1] for t in times]
        capacity = [capacity_rates[bisect_right(capacity_times, t) - 1] for t in times]
        return times, pacing, capacity

    left, right = drop_us - 5_000, drop_us + 15_000
    fig, (overview, zoom) = plt.subplots(2, 1, figsize=(11.5, 6.3), layout="constrained", sharey=True)
    fig.suptitle("A1 长时波动补测中的目标发包速率与网络速率", fontweight="bold", fontsize=15)
    overview.axvspan(left / 1e6, right / 1e6, color="#bdcddd", label="乙图放大区间")
    times, pacing, capacity = rates_in_window(3_000_000, 6_000_000)
    overview.step([t / 1e6 for t in times], pacing, where="post", color=COLORS[2], lw=1.5,
                  label="BBRv1 目标发包速率")
    overview.step([t / 1e6 for t in times], capacity, where="post", color="#333333", lw=1.3,
                  ls="--", label="网络速率（模拟空口容量）")
    overview.set(xlim=(3, 6), xlabel="从实验计时起点开始计时（s）")
    overview.legend(loc="lower right", ncol=3, fontsize=9)
    style(overview, "（甲）第 3 s 至第 6 s 的完整统计区间", "速率（Mbps）")

    times, pacing, capacity = rates_in_window(left, right)
    ms = [(t - drop_us) / 1000 for t in times]
    drop_index = times.index(drop_us)
    pacing_at_drop = pacing[drop_index]
    after_drop = bisect_right(pacing_times, drop_us)
    first_lower = next(i for i in range(after_drop, len(pacing_times)) if pacing_rates[i] < pacing_at_drop)
    change_ms = (pacing_times[first_lower] - drop_us) / 1000
    changed_rate = pacing_rates[first_lower]
    low_capacity = capacity[drop_index]
    zoom.fill_between(ms[drop_index:], capacity[drop_index:],
                      [max(p, c) for p, c in zip(pacing[drop_index:], capacity[drop_index:])],
                      step="post", color="#f5d8ad", zorder=1)
    zoom.axvline(0, color="#637183", lw=1, ls=":")
    zoom.step(ms, pacing, where="post", color=COLORS[2], lw=2)
    zoom.step(ms, capacity, where="post", color="#333333", lw=1.5, ls="--")
    zoom.scatter(change_ms, changed_rate, color=COLORS[2], s=20, zorder=4)
    zoom.annotate(f"+{change_ms:.3f} ms 首次采到 {changed_rate:.2f} Mbps",
                  xy=(change_ms, changed_rate), xytext=(8.6, 32), va="center", fontsize=10,
                  arrowprops={"arrowstyle": "->", "color": COLORS[2]})
    zoom.text(change_ms / 2, (pacing_at_drop + low_capacity) / 2,
              f"高出 {pacing_at_drop - low_capacity:.2f} Mbps", ha="center", va="center")
    zoom.text((change_ms + ms[-1]) / 2, (changed_rate + low_capacity) / 2,
              f"高出 {changed_rate - low_capacity:.2f} Mbps", ha="center", va="center")
    zoom.set(xlim=(ms[0], ms[-1]), ylim=(0, 43), xlabel="相对模拟空口降速时刻的时间（ms）")
    zoom.xaxis.set_major_locator(MultipleLocator(1))
    zoom.set_axisbelow(True)
    style(zoom, "（乙）按 1 ms 刻度放大 30 Mbps 降至 10 Mbps 的前后", "速率（Mbps）")
    figure(output, "a1-slow-rates-ms", fig)


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
    run, plan, packets, samples = read_a1(source)
    a1 = summarize_a1(run, plan, packets)
    rows = summarize_d1(source)
    args.output.mkdir(parents=True, exist_ok=True)
    write_csv(args.output / "a1-queue.csv", [a1])
    plot_a1(plan, samples, a1["start_offset_us"], args.output)
    write_csv(args.output / "d1-service.csv", rows)
    plot(rows, args.output)


if __name__ == "__main__":
    main()
