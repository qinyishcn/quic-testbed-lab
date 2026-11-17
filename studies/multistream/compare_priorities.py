"""Compare the default FIFO run with the same four streams at priority 10."""

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from analyze import csv_write
from studies.multistream.analyze import FRAME_BYTES, STREAMS, read_archive, received_bytes
from v3.plots import figure, plt, set_chinese_font, style


COLORS = ("#465771", "#943f63", "#087b70", "#ad702c")


def read_rows(path):
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def batch_key(row):
    return row["run"], int(row["batch"]), int(row["budget_ms"])


def compare_deadlines(baseline, round_robin):
    by_budget = defaultdict(list)
    for a, b in zip(baseline, round_robin, strict=True):
        if batch_key(a) != batch_key(b):
            raise ValueError("Budget rows are not paired by connection and batch")
        by_budget[int(a["budget_ms"])].append((a, b))
    for k, group in sorted(by_budget.items()):
        summary = dict(budget_ms=k, batches=len(group))
        for priority, rows in zip((9, 10), zip(*group)):
            for metric in ("completed", "equivalent", "extra_frames"):
                summary[f"p{priority}_{metric}"] = sum(int(r[metric]) for r in rows)
            summary[f"p{priority}_zero_to_three"] = sum(
                r["completed"] == "0" and r["equivalent"] == "3" for r in rows)
        yield summary


def plot_overview(folder, comparisons, completions):
    fig, (left, right) = plt.subplots(1, 2, figsize=(12.3, 4.8), layout="constrained")
    k = [r["budget_ms"] for r in comparisons]
    for priority, color in zip((9, 10), ("#465771", "#943f63")):
        equivalent_line = ":" if priority == 9 else "--"
        for metric, line, label in (("completed", "-", "实际收齐"), ("equivalent", equivalent_line, "按字节折算")):
            left.plot(k, [r[f"p{priority}_{metric}"]/r["batches"] for r in comparisons],
                      line, color=color, label=f"优先级 {priority}，{label}")
    style(left, "两种调度的时限内完成帧数", "每批帧数")
    end_ms = 1.02*max(float(r["upload_ms"]) for rows in completions.values() for r in rows)
    left.set(xlabel="上传时延预算 K（ms）", yticks=range(5), ylim=(0, 4.15),
             xlim=(min(k), 5*np.ceil(end_ms/5)))
    for (priority, rows), line in zip(completions.items(), ("--", "-")):
        for stream, color in enumerate(COLORS, 1):
            x = np.sort([float(r["upload_ms"]) for r in rows if int(r["stream"]) == stream])
            right.step(np.r_[0, x, end_ms], np.r_[0, 100*np.arange(1, len(x)+1)/len(x), 100],
                       where="post", color=color, ls=line, label=f"流 {stream}，优先级 {priority}")
    style(right, "四条流各自的完成时延", "累计完成比例（%）")
    right.set(xlabel="从共同提交到服务器收齐一帧（ms）", ylim=(0, 102))
    left.legend(fontsize=8, loc="lower right")
    right.legend(fontsize=8, loc="upper center", bbox_to_anchor=(.5, -.18), ncols=4)
    figure(folder, "priority-comparison", fig)


def plot_example(folder, runs_by_priority, example):
    run_id, batch, budget = batch_key(example)
    fig, axes = plt.subplots(1, 2, figsize=(12.3, 4.7), sharex=True, sharey=True, layout="constrained")
    recorded = []
    for ax, (priority, runs) in zip(axes, runs_by_priority.items()):
        run = runs[run_id]
        tasks = run["plan"]["tasks"]
        arrivals = received_bytes(run, {i: t["size"] for i, t in enumerate(tasks) if not t["warmup"]})
        start = {int(r["task"]): int(r["us"]) for r in run["client.csv"] if r["event"] == "start"}
        ids = [i for i, t in enumerate(tasks) if t.get("batch") == batch]
        for position, (task, color) in enumerate(zip(ids, COLORS), 1):
            values = [(0, 0)] + [((t-start[task])/1000, n) for t, n in arrivals[task]]
            recorded.extend(dict(priority=priority, run=run_id, batch=batch, stream=position,
                                 elapsed_ms=t, unique_bytes=n) for t, n in values)
            x, y = zip(*values)
            ax.step(x, np.array(y)/1000, where="post", color=color, label=f"流 {position}")
        ax.axvline(budget, ls="--", color="#585858", label=f"截止时间 {budget} ms")
        ax.axhline(FRAME_BYTES/1000, ls=":", color="#585858")
        style(ax, f"优先级 {priority}", "服务器累计收到的有效字节（kB）")
        ax.set(xlabel="从四条流共同提交起计时（ms）", ylim=(0, 42))
        ax.legend(loc="upper center", bbox_to_anchor=(.5, -.18), ncols=3, fontsize=8)
    csv_write(folder/"priority-example-arrivals.csv", recorded)
    figure(folder, "priority-example-arrivals", fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("round_robin", type=Path)
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    paths = {9: args.baseline, 10: args.round_robin}
    runs = {p: read_archive(path) for p, path in paths.items()}
    if runs[9].keys() != runs[10].keys():
        raise ValueError("The two priority groups do not contain the same connections")
    for run_id in runs[9]:
        original, repeat = (runs[p][run_id]["plan"] for p in paths)
        ignored = {"origin", "stream_priority"}
        if repeat.get("stream_priority") != 10 or original.get("stream_priority", 9) != 9 or (
            {k: v for k, v in original.items() if k not in ignored} !=
            {k: v for k, v in repeat.items() if k not in ignored}
        ):
            raise ValueError(f"Unexpected plan difference in {run_id}")
    deadlines = {p: read_rows(path/"per-batch-deadlines.csv") for p, path in paths.items()}
    comparisons = list(compare_deadlines(deadlines[9], deadlines[10]))
    csv_write(args.round_robin/"priority-comparison.csv", comparisons)
    completions = {p: read_rows(path/"stream-completions.csv") for p, path in paths.items()}
    set_chinese_font(args.font)
    plot_overview(args.round_robin, comparisons, completions)
    candidates = [r for r in deadlines[10] if r["completed"] == "0" and r["equivalent"] == "3"
                  and all(int(r[f"stream_{i}_bytes"]) < FRAME_BYTES for i in range(1, STREAMS+1))]
    if candidates:
        closest = max(candidates, key=lambda r: min(int(r[f"stream_{i}_bytes"]) for i in range(1, STREAMS+1)))
        print(closest)
        plot_example(args.round_robin, runs, closest)


if __name__ == "__main__":
    main()
