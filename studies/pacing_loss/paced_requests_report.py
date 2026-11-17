"""Analyze the BBRv3 40 kB requests spaced 33 ms apart."""

import argparse
from collections import Counter, defaultdict
from pathlib import Path

from mismatch_report import (CAP, CHANGE, FLIGHT, LOSS, OUT, PACE, QUEUE, ROOT, WINDOW,
                             csv_write, figure, load, plt, quantile, range_points, samples,
                             set_chinese_font, step, style, windows)
from reverse_delay_report import TRAIN_MODES, train_flow_control


COHORT = "pacing-mismatch-reverse/train-33ms"
LABELS = {
    "bounded_fixed": "恒定 20 Mbps", "bounded_rise": "20→30 Mbps",
    "bounded_narrow": "降至 16 Mbps 后回升", "bounded_dip": "降至 10 Mbps 后回升",
    "rise": "20→90 Mbps", "drop": "降到 2 Mbps 后回到 90 Mbps",
    "collapse": "降到 0.12 Mbps 后直接回升", "staged": "降到 0.12 Mbps，经 1 Mbps 回升",
    "rebound": "降到 0.12 Mbps，经 4 Mbps 回升",
}
ENDS = dict(bounded_fixed=150, bounded_rise=150, bounded_narrow=170, bounded_dip=190,
            rise=150, drop=250, collapse=270, staged=430, rebound=490)
RECOVERY = dict(bounded_narrow=120, bounded_dip=140, drop=130, collapse=130, staged=280, rebound=360)


def timeline(changes):
    points = []
    current = 0
    for t, delta in sorted(changes.items()):
        current += delta
        if current < 0:
            raise ValueError("Negative backlog")
        points.append((t, current))
    if current != 0:
        raise ValueError("Backlog does not clear")
    return points


def queue_counts(raw):
    """Count waiting packets separately before the random gate and before serialization."""
    gate, fifo, total = (defaultdict(int) for _ in range(3))
    zero = raw["plan.json"]["origin"] + CHANGE * 1000
    previous_finish = 0
    for row in raw["wire.csv"]:
        if row["direction"] != "forward":
            continue
        t = int(row["us"])
        event = row["event"]
        if event == "in":
            gate[t] += 1
            total[t] += 1
        elif event == "admit":
            gate[t] -= 1
            fifo[t] += 1
            fifo[max(t, previous_finish)] -= 1
            previous_finish = int(row["due_us"])
        elif event == "out":
            total[t] -= 1
    return {name: [((t-zero)/1000, n) for t, n in timeline(points)]
            for name, points in (("gate", gate), ("fifo", fifo), ("total", total))}


def unsent_bytes(rows):
    changes = defaultdict(int)
    for task in rows["tasks"]:
        changes[round(float(task["start_ms"]) * 1000)] += int(task["bytes"])
    for packet in rows["packets"]:
        new = int(packet["stream_bytes"]) - int(packet["repeated_stream_bytes"])
        if new:
            changes[round(float(packet["send_ms"]) * 1000)] -= new
    return [(t/1000-CHANGE, n) for t, n in timeline(changes)]


def write_tables(runs, out):
    phases, summaries, events = [], [], []
    observed = {}
    for case in LABELS:
        group = [(name, raw, wire, rows) for (_, name), (raw, wire, rows) in sorted(runs.items())
                 if raw["plan.json"]["case"] == case]
        packets = [p for _, _, _, rows in group for p in rows["packets"]]
        losses = [loss for _, _, _, rows in group for loss in rows["loss"]]
        tasks = [t for _, _, _, rows in group for t in rows["tasks"]]
        if not group or any(not t["upload_ms"] for t in tasks):
            raise ValueError(f"An upload was incomplete in {case}")
        reasons = Counter(loss["loss_reason"] for loss in losses)
        queue_peaks = []
        for name, raw, wire, rows in group:
            counts = queue_counts(raw)
            unsent = [(-CHANGE-1, 0), *unsent_bytes(rows)]
            observed[name] = counts, unsent
            queue_peaks.append(max(n for t, n in counts["fifo"] if t >= -2))
            events.extend(dict(loss, case=case) for loss in rows["loss"])
            spans = [(0, 8), (0, 12), (0, 33), (0, 130)]
            if case in RECOVERY:
                at = RECOVERY[case]
                spans += [(at, at+8), (at+8, at+20)]
            for a, b in spans:
                _, pending = range_points(unsent, a, b)
                _, fifo = range_points(counts["fifo"], a, b)
                phases.append(dict(case=case, run=name, start_ms=a, end_ms=b,
                                   unsent_at_end_bytes=pending[-1], peak_unsent_bytes=max(pending),
                                   peak_fifo_packets=max(fifo), **windows(raw, wire, a, b)))
        summaries.append(dict(case=case, runs=len(group), tasks=len(tasks), forward_packets=len(packets),
                              upload_p50_ms=quantile([float(t["upload_ms"]) for t in tasks], .5),
                              upload_p95_ms=quantile([float(t["upload_ms"]) for t in tasks], .95),
                              forward_p99_ms=quantile([float(p["one_way_ms"]) for p in packets if p["one_way_ms"]], .99),
                              confirmation_p99_ms=quantile([float(p["confirmation_ms"])
                                                           for p in packets if p["confirmation_ms"]], .99),
                              peak_fifo_packets=max(queue_peaks), packet_number=reasons["packet_number"],
                              rack_time=reasons["rack_time"], timer=reasons["timer"],
                              originals_arrived=sum(bool(p["receive_ms"]) for p in losses),
                              repeated_stream_bytes=sum(int(p["repeated_stream_bytes"]) for p in packets)))
    csv_write(out / "summary.csv", summaries)
    csv_write(out / "time-windows.csv", phases)
    csv_write(out / "forward-loss-events.csv", events)
    return observed


def plot_trace(case, name, raw, wire, rows, counts, unsent, out, suffix=""):
    left, right = -10, ENDS[case]
    fig, axes = plt.subplots(5, 1, figsize=(11.8, 10.1), sharex=True, layout="constrained")
    fig.suptitle(f"BBRv3｜{LABELS[case]}｜第 {int(name.rsplit('__', 1)[-1])+1} 轮",
                 fontsize=16, fontweight="bold")
    rate, control, queue, backlog, delay = axes
    capacity = [(t/1000-CHANGE, value) for t, value in raw["plan.json"]["capacity"]]
    step(rate, capacity, left, right, color=CAP, lw=1.6, ls="--", zorder=4, label="空口配置容量")
    step(rate, samples(raw, "pacing_bps", 1e6), left, right, color=PACE, lw=1.5, label="目标发包速率")
    edges = list(range(left, right + 1, 2))
    rates = [sum(n for t, n in wire["egress"] if a <= t < b) * .004 for a, b in zip(edges, edges[1:])]
    rate.stairs(rates, edges, baseline=None, color=OUT, lw=.95, label="实际出队速率（2 ms）")
    if case not in ("bounded_fixed", "bounded_rise", "bounded_dip", "bounded_narrow"):
        rate.set_yscale("symlog", linthresh=.12)
        rate.set_yticks([0, .12, 1, 2, 4, 10, 30, 90], ["0", "0.12", "1", "2", "4", "10", "30", "90"])
    step(control, samples(raw, "cwnd", 1000), left, right, color=WINDOW, lw=1.4, label="拥塞窗口")
    step(control, samples(raw, "inflight", 1000), left, right, color=FLIGHT, lw=1.1, label="已发未确认")
    for label, field, line, color in (("中继待转发总量", "total", "-", QUEUE),
                                     ("等候空口服务", "fifo", "--", PACE),
                                     ("等待抽签通过", "gate", ":", WINDOW)):
        step(queue, counts[field], left, right, color=color, lw=1.25, ls=line, label=label)
    step(backlog, [(t, n / 1000) for t, n in unsent], left, right, color=CAP, lw=1.35)
    submissions = [float(t["start_ms"])-CHANGE for t in rows["tasks"]]
    backlog.scatter([t for t in submissions if left <= t <= right],
                    [0 for t in submissions if left <= t <= right], color=PACE, marker="|", s=95,
                    clip_on=False, label="提交 40 kB 请求")

    for field, label, color in (("one_way_ms", "前向单包时延", PACE),
                                ("confirmation_ms", "从发包到收到首次确认", "#586274")):
        points = [(float(p["send_ms"])-CHANGE, float(p[field])) for p in rows["packets"] if p[field]
                  and left <= float(p["send_ms"])-CHANGE < right]
        if points:
            x, y = zip(*points)
            delay.scatter(x, y, color=color, s=8, alpha=.46, label=label)
    for reason, label, marker, color in (("packet_number", "包号阈值判丢", "x", LOSS),
                                         ("rack_time", "时间阈值判丢", "D", WINDOW)):
        points = [(float(e["send_ms"])-CHANGE, float(e["confirmation_ms"])) for e in rows["loss"]
                  if e["loss_reason"] == reason and e["confirmation_ms"]
                  and left <= float(e["send_ms"])-CHANGE < right]
        if points:
            x, y = zip(*points)
            delay.scatter(x, y, color=color, marker=marker, s=18, label=label, zorder=4)
    timers = [e for e in rows["loss"] if e["loss_reason"] == "timer"
              and left <= float(e["send_ms"])-CHANGE < right]
    if timers:
        delay.scatter([float(e["send_ms"])-CHANGE for e in timers],
                      [float(e["deadline_age_ms"]) for e in timers],
                      color=LOSS, marker="^", s=35, label="实际探测超时门限")
        for e in timers:
            at = float(e["loss_ms"])-CHANGE
            if left < at < right:
                for ax in axes:
                    ax.axvline(at, color=LOSS, ls=":", lw=.9, alpha=.6)
    for t, _ in capacity[1:]:
        if left < t < right:
            for ax in axes:
                ax.axvline(t, color=CAP, lw=.7, ls="--", alpha=.35)
    for ax, label in zip(axes, ("速率（Mbps）", "数据量（kB）", "数据报数量（个）",
                               "应用待发字节（kB）", "时延（ms）")):
        style(ax, "", label)
        ax.set_xlim(left, right)
        ax.set_ylim(bottom=0)
        if ax.get_legend_handles_labels()[0]:
            ax.legend(loc="upper right", fontsize=8, ncol=3, framealpha=.95)
    delay.set_xlabel("距首次容量变化的时间（ms）｜恒速组以同一预设时刻为 0 ms")
    figure(out, case + suffix, fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "results")
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    set_chinese_font(args.font)
    out = args.data / COHORT
    runs = load(args.data, {COHORT: tuple(LABELS)}, TRAIN_MODES)
    train_flow_control(out, out, runs, filename="flow-control.csv")
    observed = write_tables(runs, out)
    for case in LABELS:
        candidates = [(name, raw, wire, rows) for (_, name), (raw, wire, rows) in sorted(runs.items())
                      if raw["plan.json"]["case"] == case]
        example = next((r for r in candidates if any(e["loss_reason"] == "timer" for e in r[3]["loss"])),
                       candidates[0])
        plot_trace(case, *example, *observed[example[0]], out)
        if case == "staged":
            rack = next((r for r in candidates if any(e["loss_reason"] == "rack_time" for e in r[3]["loss"])),
                        None)
            if rack:
                plot_trace(case, *rack, *observed[rack[0]], out, suffix="-rack")


if __name__ == "__main__":
    main()
