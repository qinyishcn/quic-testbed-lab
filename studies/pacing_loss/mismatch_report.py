"""Measure and plot paired pacing, queueing, and recovery traces."""

import argparse
import csv
import gzip
import json
import random
import sys
import tarfile
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from analyze import csv_write, quantile
from studies.pacing_loss.ack_deadlines import measured
from v3.plots import figure, plt, set_chinese_font, style
from matplotlib.lines import Line2D


MODES = ("bbr1-rate", "bbr1-gate", "bbr3-rate", "bbr3-gate")
SETS = {"initial": ("rise", "drop", "collapse"), "staged": ("staged",), "rebound": ("rebound",)}
CAP, PACE, OUT, WINDOW, FLIGHT, QUEUE, LOSS = "#3a424d", "#18768d", "#428853", "#76469b", "#7c899a", "#995921", "#ae3d46"
CHANGE = 1200


def records(path):
    if not path.is_file():
        return []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def raw_runs(path):
    runs = defaultdict(dict)
    with tarfile.open(path, "r|xz") as archive:
        for member in archive:
            run, _, name = member.name.partition("/")
            if name not in ("plan.json", "wire.csv", "samples.csv"):
                continue
            data = archive.extractfile(member).read().decode("utf-8")
            runs[run][name] = json.loads(data) if name == "plan.json" else list(csv.DictReader(data.splitlines()))
    return runs


def wire_state(raw):
    plan = raw["plan.json"]
    origin = plan["origin"]
    incoming, admitted, sent = {}, {}, {}
    reverse_in, reverse_out = {}, {}
    waits = Counter()
    next_check = {}
    rng = random.Random(plan["seed"])
    for r in raw["wire.csv"]:
        ident, event, now = r["id"], r["event"], int(r["us"])
        if r["direction"] == "reverse":
            if r["action"] != "direct":
                raise ValueError("Reverse path was shaped")
            (reverse_in if event == "in" else reverse_out)[ident] = now
            continue
        if event == "in":
            incoming[ident] = now
        elif event == "wait":
            if (not plan["gate"] or rng.random() >= plan["gate"]["probability"]
                    or now < next_check.get(ident, incoming[ident])):
                raise ValueError("Incorrect gate retry")
            waits[ident] += 1
            next_check[ident] = int(r["due_us"])
            if next_check[ident] != now + plan["gate"]["wait_us"]:
                raise ValueError("Incorrect retry interval")
        elif event == "admit":
            if (plan["gate"] and rng.random() < plan["gate"]["probability"]
                    or now < next_check.get(ident, incoming[ident])):
                raise ValueError("Packet admitted before retry")
            admitted[ident] = (now, int(r["due_us"]), int(r["udp_bytes"]) + 28)
        elif event == "out":
            sent[ident] = now
        else:
            raise ValueError(f"Unexpected dropped packet {ident}")
    if incoming.keys() != admitted.keys() or admitted.keys() != sent.keys() or reverse_in.keys() != reverse_out.keys():
        raise ValueError("Relay log does not conserve packets")
    if list(admitted) != list(sent):
        raise ValueError("Service queue released packets out of admission order")
    backlog = 0
    changes = []
    egress = []
    ingress = []
    queue_wait = []
    previous_due = 0
    for ident, (now, due, size) in admitted.items():
        start = max(now, previous_due)
        previous_due = due
        changes.extend(((now, size), (start, -size)))
        egress.append(((sent[ident]-origin)/1000-CHANGE, size))
        ingress.append(((incoming[ident]-origin)/1000-CHANGE, size))
        if incoming[ident]-origin >= (CHANGE-1)*1000:
            queue_wait.append(max(0, start-now)/1000)
    queue = []
    grouped = defaultdict(int)
    for now, delta in changes:
        grouped[now] += delta
    for now, delta in sorted(grouped.items()):
        backlog += delta
        queue.append(((now-origin)/1000-CHANGE, backlog))
    if backlog or min(y for _, y in queue) < 0:
        raise ValueError("Invalid queue accounting")
    return dict(queue=queue, egress=egress, ingress=ingress, wait_counts=Counter(waits.get(i, 0) for i in incoming),
                reverse_count=len(reverse_in), queue_wait_max_ms=max(queue_wait),
                max_release_lag_ms=max((sent[i]-admitted[i][1])/1000 for i in sent),
                reverse_relay_p99_us=quantile([reverse_out[i]-reverse_in[i] for i in reverse_in], .99))


def range_points(points, left, right):
    times, values = zip(*points)
    start, end = bisect_right(times, left), bisect_right(times, right)
    if start == 0:
        raise ValueError("Timeline starts before first observation")
    return [left, *times[start:end], right], [values[start-1], *values[start:end], values[end-1]]


def step(ax, points, left, right, **kwargs):
    t, y = range_points(points, left, right)
    ax.step(t, y, where="post", **kwargs)


def samples(raw, field, scale):
    origin = raw["plan.json"]["origin"]
    return [((int(r["us"])-origin)/1000-CHANGE, int(r[field])/scale) for r in raw["samples.csv"]]


def windows(raw, wire, left, right):
    pace = [value for t, value in samples(raw, "pacing_bps", 1e6) if left <= t < right]
    cwnd = [value for t, value in samples(raw, "cwnd", 1000) if left <= t < right]
    flight = [value for t, value in samples(raw, "inflight", 1000) if left <= t < right]
    t, q = range_points(wire["queue"], left, right)
    return dict(pacing_median_mbps=median(pace), cwnd_median_kb=median(cwnd), inflight_median_kb=median(flight),
                ingress_mbps=sum(n for t, n in wire["ingress"] if left <= t < right)*.008/(right-left),
                egress_mbps=sum(n for t, n in wire["egress"] if left <= t < right)*.008/(right-left),
                queue_max_kb=max(q)/1000,
                queue_empty_pct=100*sum(b-a for a, b, n in zip(t, t[1:], q) if not n)/(right-left))


def load(source):
    runs, pairs = {}, {}
    for cohort, cases in SETS.items():
        for mode in MODES:
            folder = source / cohort / mode
            by_run = defaultdict(lambda: defaultdict(list))
            for name, file in (("packets", "packets.csv.gz"), ("loss", "loss-events.csv"), ("tasks", "tasks.csv")):
                for row in records(folder / file):
                    if name == "tasks" or measured(row):
                        by_run[row["run"]][name].append(row)
            for run, raw in raw_runs(folder / "raw-evidence.tar.xz").items():
                plan = raw["plan.json"]
                if plan["case"] not in cases:
                    raise ValueError("Unexpected experiment in archive")
                pair = (plan["capacity"], plan["tasks"], plan["seed"])
                if pairs.setdefault(run, pair) != pair:
                    raise ValueError(f"Unpaired plans for {run}")
                runs[mode, run] = raw, wire_state(raw), by_run[run]
    return runs


def tables(runs, out):
    stats, events, time_windows = [], [], []
    for (mode, run), (raw, wire, rows) in sorted(runs.items()):
        tasks = {int(r["task"]): r for r in rows["tasks"]}
        packets, losses = rows["packets"], rows["loss"]
        if len(tasks) != 2 or any(not r["upload_ms"] for r in tasks.values()) or any(not r["one_way_ms"] for r in packets):
            raise ValueError(f"Incomplete workload or packets for {mode}/{run}")
        reasons = Counter(r["loss_reason"] for r in losses)
        counts = wire["wait_counts"]
        stats.append(dict(mode=mode, run=run, case=raw["plan.json"]["case"], packets=len(packets),
                          packet_threshold=reasons["packet_number"], rack=reasons["rack_time"], pto=reasons["timer"],
                          original_arrivals=sum(bool(r["one_way_ms"]) for r in losses),
                          observed_repeats=sum(bool(r["first_repeat_send_ms"]) for r in losses),
                          repeated_stream_bytes=sum(int(r["repeated_stream_bytes"]) for r in packets),
                          forward_p99_ms=quantile([float(r["one_way_ms"]) for r in packets], .99),
                          ack_p99_ms=quantile([float(r["confirmation_ms"]) for r in packets if r["confirmation_ms"]], .99),
                          queue_wait_max_ms=wire["queue_wait_max_ms"],
                          max_release_lag_ms=wire["max_release_lag_ms"], reverse_direct=wire["reverse_count"],
                          reverse_relay_p99_us=wire["reverse_relay_p99_us"],
                          upload_200kb_ms=tasks[1]["upload_ms"], upload_40kb_ms=tasks[2]["upload_ms"],
                          gate_0=counts[0], gate_1=counts[1], gate_2=counts[2], gate_3plus=sum(n for k, n in counts.items() if k >= 3)))
        for r in losses:
            keys = ("packet_number", "loss_reason", "send_ms", "receive_ms", "one_way_ms", "confirmation_ms", "loss_ms",
                    "age_at_loss_ms", "deadline_age_ms", "repeat_packet_numbers", "first_repeat_send_ms", "first_repeat_receive_ms",
                    "relay_ingress_ms", "relay_egress_ms", "ack_gap_at_loss")
            events.append(dict(mode=mode, run=run, **{k: r[k] for k in keys}))
        spans = {"rise": [(0, 8)], "drop": [(0, 12), (132, 137)], "collapse": [(0, 12), (132, 137)],
                 "staged": [(0, 12), (282, 287)], "rebound": [(0, 12), (250, 300), (340, 360), (362, 367), (367, 375)]}
        for a, b in spans[raw["plan.json"]["case"]]:
            sent = sum(int(r["stream_bytes"])-int(r["repeated_stream_bytes"]) for r in packets
                       if r["stream_ids"] == "4" and float(r["send_ms"])-CHANGE < b)
            time_windows.append(dict(mode=mode, run=run, start_ms=a, end_ms=b,
                                     first_request_new_bytes_sent_to_end=sent, **windows(raw, wire, a, b)))
    csv_write(out / "runs.csv", stats)
    csv_write(out / "events.csv", events)
    csv_write(out / "windows.csv", time_windows)
    return stats


def plot_pair(runs, case, bounds, out, suffix=""):
    fig, axes = plt.subplots(4, 2, figsize=(13.4, 9), sharex="col", layout="constrained")
    title = "容量突升时的供给不足" if case == "rise" else "速率深跌、探测超时与容量回升"
    fig.suptitle(title + ("后的毫秒放大" if suffix else ""), fontsize=16, fontweight="bold")
    for j, mode in enumerate(("bbr3-rate", "bbr3-gate")):
        raw, wire, rows = runs[mode, case+"__00"]
        rate, control, queue, delay = axes[:, j]
        left, right = bounds
        capacity = [(t/1000-CHANGE, value) for t, value in raw["plan.json"]["capacity"]]
        step(rate, capacity, left, right, color=CAP, lw=1.8, ls="--", label="配置容量")
        step(rate, samples(raw, "pacing_bps", 1e6), left, right, color=PACE, lw=1.7, label="目标发包速率")
        size = 1 if suffix else (2 if case == "rise" else 5)
        bins = dict.fromkeys(range(left, right, size), 0)
        for t, n in wire["egress"]:
            if left <= t < right:
                bins[left+int((t-left)//size)*size] += n
        flow = [(t, n*.008/size) for t, n in bins.items()]
        rate.step([t for t, _ in flow]+[right], [y for _, y in flow]+[flow[-1][1]], where="post",
                  color=OUT, lw=1.1, label=f"实际出队（每 {size} ms）")
        rate.set_yscale("symlog", linthresh=1)
        rate.set_yticks([0, 1, 10, 90, 300], ["0", "1", "10", "90", "300"])
        rate.set_ylim(0, 450)
        step(control, samples(raw, "cwnd", 1000), left, right, color=WINDOW, lw=1.6, label="拥塞窗口")
        step(control, samples(raw, "inflight", 1000), left, right, color=FLIGHT, lw=1.1, label="实际在途量")
        step(queue, [(t, b/1000) for t, b in wire["queue"]], left, right, color=QUEUE, lw=1.5)
        lookup = {r["packet_number"]: r for r in rows["loss"]}
        for r in rows["packets"]:
            t = float(r["send_ms"])-CHANGE
            if not left <= t <= right:
                continue
            y = float(r["one_way_ms"])
            delay.scatter(t, y, s=7, color=PACE, alpha=.62)
            e = lookup.get(r["packet_number"])
            if e and e["loss_reason"] == "timer":
                deadline = float(e["deadline_age_ms"])
                ack = float(e["confirmation_ms"])
                delay.scatter(t, deadline, color=LOSS, marker="^", s=38, zorder=4)
                delay.scatter(t, ack, facecolors="none", edgecolors=LOSS, marker="D", s=30, zorder=4)
                delay.vlines(t, deadline, ack, color=LOSS, lw=.9)
                at = float(e["loss_ms"])-CHANGE
                if left <= at <= right:
                    for ax in (rate, control, queue):
                        ax.axvline(at, color=LOSS, lw=.85, ls=":", alpha=.8)
            elif e:
                delay.scatter(t, y, s=18, color=LOSS, marker="x")
        for t, _ in capacity[1:]:
            for ax in axes[:, j]:
                if left <= t <= right:
                    ax.axvline(t, color=CAP, alpha=.55, lw=.8, ls="--")
        title = "BBRv3｜仅速率" if not j else "BBRv3｜另加逐包随机等待"
        for ax, label in zip(axes[:, j], ("速率（Mbps）", "数据量（kB）", "等待服务的队列（kB）", "时延或期限（ms）")):
            style(ax, title if ax is rate else "", label if j == 0 else "")
            ax.set_xlim(left, right)
        delay.set_xlabel("距首次容量变化的时间（ms）")
    axes[0, 0].legend(loc="upper left", fontsize=8, framealpha=.94)
    axes[1, 0].legend(loc="upper left", fontsize=8, framealpha=.94)
    handles = [Line2D([], [], color=color, marker=marker, ls=ls, label=label, markerfacecolor=fill)
               for color, marker, ls, label, fill in (
                   (PACE, "o", "None", "前向单向时延", PACE),
                   (LOSS, "^", "None", "探测超时门限", LOSS),
                   (LOSS, "D", "None", "原包确认往返耗时", "none"),
                   (LOSS, "x", "None", "其他判丢的原包", LOSS),
                   (LOSS, "None", ":", "发送端触发探测超时", LOSS))]
    fig.legend(handles=handles, loc="outside lower center", ncol=5, fontsize=9, frameon=False)
    for ax1, ax2 in axes[1:]:
        high = max(ax1.get_ylim()[1], ax2.get_ylim()[1])
        ax1.set_ylim(0, high)
        ax2.set_ylim(0, high)
    figure(out, f"{case}-trace{suffix}", fig)


def comparison(stats, out):
    fig, axes = plt.subplots(2, 2, figsize=(13.2, 6.5), layout="constrained")
    fig.suptitle("容量时序和逐包随机等待怎样改变交付结果", fontsize=16, fontweight="bold")
    labels = ["直接升速", "2 Mbps 后回升", "0.12 Mbps 后回升", "经 1 Mbps 回升", "经 4 Mbps 回升"]
    cases = [case for values in SETS.values() for case in values]
    columns = ("upload_200kb_ms", "forward_p99_ms", "pto", "packet_threshold")
    titles = ("200 kB 完整上传耗时", "前向单包时延第 99 百分位数", "探测超时事件", "包号阈值判丢事件")
    names = ("BBRv1 仅速率", "BBRv1 加随机等待", "BBRv3 仅速率", "BBRv3 加随机等待")
    for i, (ax, field, title) in enumerate(zip(axes.flat, columns, titles)):
        for j, (mode, name) in enumerate(zip(MODES, names)):
            values = [[float(r[field]) for r in stats if r["case"] == case and r["mode"] == mode] for case in cases]
            height = [median(a) if i < 2 else sum(a) for a in values]
            ax.bar([k+(j-1.5)*.18 for k in range(len(cases))], height, width=.17,
                   color=("#8794a4", "#4e5968", "#60a2b1", "#1b6176")[j], label=name)
        ax.set_xticks(range(len(cases)), labels, fontsize=9)
        style(ax, title, "耗时（ms）" if i < 2 else "三轮累计事件数")
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncol=4, frameon=False)
    figure(out, "outcomes", fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path)
    parser.add_argument("--data", type=Path, default=ROOT / "results/pacing-mismatch")
    args = parser.parse_args()
    set_chinese_font(args.font)
    runs = load(args.data)
    stats = tables(runs, args.data)
    plot_pair(runs, "rise", (-4, 50), args.data)
    plot_pair(runs, "rebound", (-5, 405), args.data)
    plot_pair(runs, "rebound", (354, 381), args.data, "-zoom")
    comparison(stats, args.data)


if __name__ == "__main__":
    main()
