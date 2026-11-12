"""Analyze ACK timing and native recovery events in the rate-only experiments."""

import argparse
import csv
import gzip
import json
import sys
import tarfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from analyze import csv_write, quantile
from studies.pacing_loss.ack_deadlines import ACK, DELAY, LOSS, PTO, THRESHOLDS, key, measured, paired_events, read, summary
from v3.plots import figure, plt, set_chinese_font, style
from matplotlib.lines import Line2D
from matplotlib.ticker import NullFormatter


LABELS = {
    "fixed20": "固定 20 Mbps",
    "small_fast": "18–22 Mbps，每档 5 ms",
    "small_slow": "18–22 Mbps，每档 100 ms",
    "wide_fast": "10–30 Mbps，每档 5 ms",
    "wide_slow": "10–30 Mbps，每档 100 ms",
    "deep_fast": "0.5–39.5 Mbps，每档 5 ms",
    "deep_slow": "0.5–39.5 Mbps，每档 100 ms",
}
COLORS = ("#40444a", "#51905c", "#267746", "#5a90ba", "#1a5b89", "#a972bf", "#783c91")


def read_relay(archive_path, version):
    plans, checks = {}, []
    with tarfile.open(archive_path, "r|xz") as archive:
        for member in archive:
            if not member.name.endswith(("/plan.json", "/wire.csv")):
                continue
            run, name = member.name.split("/")
            content = archive.extractfile(member).read().decode("utf-8")
            if name == "plan.json":
                plan = plans[run] = json.loads(content)
                if plan["impairments"] or min(rate for _, rate in plan["capacity"]) <= 0:
                    raise ValueError(f"{run} is not a rate-only plan")
                continue
            rows = list(csv.DictReader(content.splitlines()))
            incoming = {r["id"]: r for r in rows if r["event"] == "in"}
            outgoing = [r for r in rows if r["event"] == "out"]
            if incoming.keys() != {r["id"] for r in outgoing}:
                raise ValueError(f"Relay failed to drain {version} {run}")
            forward = [r for r in outgoing if r["direction"] == "forward"]
            reverse = [r for r in outgoing if r["direction"] == "reverse"]
            if any(r["action"] != "direct" or r["due_us"] != incoming[r["id"]]["us"] for r in reverse):
                raise ValueError(f"Reverse traffic was scheduled in {version} {run}")
            if [int(r["id"]) for r in forward] != sorted(int(r["id"]) for r in forward):
                raise ValueError(f"Forward traffic left the relay out of order in {version} {run}")
            drops = [r for r in rows if r["event"] == "drop"]
            if any(r["direction"] != "forward" or r["action"] != "buffer_full" for r in drops):
                raise ValueError(f"Unexpected explicit drop in {version} {run}")
            lag = [int(r["us"]) - int(r["due_us"]) for r in forward]
            reverse_residence = [int(r["us"]) - int(incoming[r["id"]]["us"]) for r in reverse]
            checks.append(dict(version=version, run=run, forward_forwarded=len(forward), reverse_direct=len(reverse),
                               forward_buffer_drops=len(drops), release_lag_p99_us=quantile(lag, .99),
                               reverse_relay_p99_us=quantile(reverse_residence, .99),
                               reverse_relay_max_us=max(reverse_residence)))
    return plans, checks


def load(folder):
    with gzip.open(folder / "packets.csv.gz", "rt", encoding="utf-8") as stream:
        all_packets = list(csv.DictReader(stream))
    packets = [r for r in all_packets if measured(r)]
    event_file = folder / "loss-events.csv"
    recorded = read(event_file) if event_file.is_file() else []
    events = {key(r): r for r in recorded if measured(r)}
    if {key(r) for r in packets if r["declared_lost"] == "1"} != events.keys():
        raise ValueError(f"Event join failed for {folder.name}")
    send_times = {(r["run"], r["direction"], round(float(r["send_ms"]) * 1000)) for r in all_packets}
    for event in events.values():
        if event["loss_reason"] != "timer":
            continue
        age_us = int(event["native_deadline_us"]) - int(event["native_send_us"])
        timer_us = int(event["native_effective_timer_us"])
        tail_us = round(float(event["send_ms"]) * 1000) + age_us - timer_us
        if (event["native_timer_expired"] != "1" or int(event["ack_gap_at_loss"]) > 0
                or not timer_us <= age_us < 2 * timer_us
                or (event["run"], event["direction"], tail_us) not in send_times):
            raise ValueError(f"Could not validate the queue-tail timer for {folder.name} {key(event)}")
    return packets, events


def timing_stats(version, case, packets, events, tasks):
    stats = summary(version, case, packets, events)
    data = [r for r in packets if not case or r["case"] == case]
    requests = [r for r in tasks if not case or r["case"] == case]
    forward_residence = [float(r["relay_egress_ms"]) - float(r["relay_ingress_ms"]) for r in data if r["relay_egress_ms"]]
    stats.update(relay_dropped=sum(r["actual_relay_drop"] == "1" for r in data),
                 packets_received=sum(bool(r["one_way_ms"]) for r in data),
                 ack_p99_ms=quantile([float(r["confirmation_ms"]) for r in data if r["confirmation_ms"]], .99),
                 ack_reverse_p99_ms=quantile([float(r["ack_reverse_ms"]) for r in data if r["ack_reverse_ms"]], .99),
                 forward_relay_p99_ms=quantile(forward_residence, .99),
                 completed=sum(bool(r["upload_ms"]) for r in requests), tasks=len(requests),
                 completed20=sum(int(r["on_time20"]) for r in requests),
                 upload_p99_ms=quantile([float(r["upload_ms"]) for r in requests if r["upload_ms"]], .99),
                 repeated_stream_bytes=sum(int(r["repeated_stream_bytes"]) for r in data))
    return stats


def cdf(data, stats, out):
    fig, axes = plt.subplots(2, 2, figsize=(12.6, 6.9), sharex=True, sharey=True, layout="constrained")
    fig.suptitle("只改变前向速率时，逐包确认往返耗时的累积分布", fontsize=15, fontweight="bold")
    for i, (version, (packets, _)) in enumerate(data.items()):
        overview, detail = axes[i]
        for j, (case, label) in enumerate(LABELS.items()):
            values = [float(r["confirmation_ms"]) for r in packets if r["case"] == case and r["confirmation_ms"]]
            if not values:
                continue
            line = dict(color=COLORS[j], ls="--" if j in (1, 3, 5) else "-", lw=1.6, label=label)
            overview.ecdf(values, **line)
            row = stats[version, case]
            if not row["pto_events"] and case != "deep_slow":
                continue
            detail.ecdf(values, **line)
            if row["pto_events"]:
                low, mid, high = (row[f"pto_deadline_{x}_ms"] for x in ("min", "median", "max"))
                detail.axvspan(low, high, color=PTO, alpha=.15)
                detail.axvline(mid, color=PTO, ls=":", lw=1.5)
                tail = row["pto_all_ack_above_event_median"]
                detail.scatter(mid, 1-tail/len(values), color=PTO, s=25, zorder=3)
                note = f"期限中位数 {mid:.2f} ms\n参考右尾 {tail}/{len(values)}\n实际超时 {row['pto_events']}/{row['packets_sent']}"
                detail.text(.96, .14, note, transform=detail.transAxes, ha="right", va="bottom", fontsize=9,
                            bbox=dict(facecolor="white", edgecolor="none", alpha=.92, pad=2))
        if not any(row["pto_events"] for (v, _), row in stats.items() if v == version):
            detail.text(.96, .14, "全部速率条件均未触发探测超时", transform=detail.transAxes,
                        ha="right", va="bottom", fontsize=9)
        style(overview, f"{version}｜七种速率条件", "累计包比例")
        style(detail, f"{version}｜深度慢波动与探测超时期限", "")
        for ax in (overview, detail):
            ax.set_xscale("log")
            ticks = [.5, 1, 2, 5, 10, 20, 50, 100]
            ax.set_xticks(ticks, [str(x) for x in ticks])
            ax.xaxis.set_minor_formatter(NullFormatter())
            ax.set_yticks([0, .25, .5, .75, 1], ["0%", "25%", "50%", "75%", "100%"])
            ax.set_ylim(0, 1.045)
            if i == 1:
                ax.set_xlabel("从发出到收到该包确认的耗时（ms，对数刻度）")
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncol=4, fontsize=9, frameon=False)
    figure(out, "rate-ack-cdf", fig)


def selected_task(data):
    groups = defaultdict(lambda: defaultdict(list))
    counts = defaultdict(int)
    for version, (packets, events) in data.items():
        for r in packets:
            if r["case"] != "deep_slow":
                continue
            for stream in set(r["stream_ids"].split("|")) - {"0"}:
                group = r["run"], int(stream) // 4
                groups[group][version].append(r)
                event = events.get(key(r))
                counts[group] += bool(event and event["loss_reason"] == "timer")
    common = [group for group, versions in groups.items() if versions.keys() == data.keys()]
    run, task = max(common, key=lambda g: (counts[g], -int(g[0].split("__")[1]), -g[1]))
    return run, task, groups[run, task]


def packet_plot(data, plans, out):
    run, task, groups = selected_task(data)
    fig, axes = plt.subplots(2, 2, figsize=(12.7, 6.7), sharex=True, sharey="row", layout="constrained",
                             height_ratios=(1, 2.6))
    fig.suptitle(f"深度慢波动的逐包时序｜第 {int(run.split('__')[1])+1} 轮，第 {task} 笔请求",
                 fontsize=15, fontweight="bold")
    origin = plans["BBRv1"][run]["tasks"][task]["t"] / 1000
    transitions = [t / 1000 - origin for t, rate in plans["BBRv1"][run]["capacity"]
                   if t / 1000 > origin and rate == 39.5]
    right = max(transitions[0] + 8, max(float(r["send_ms"]) - origin for rows in groups.values() for r in rows) + 7)
    examples = []
    for col, (version, (_, events)) in enumerate(data.items()):
        upper, ax = axes[:, col]
        cap = [(t/1000-origin, rate) for t, rate in plans[version][run]["capacity"]]
        initial = next(rate for t, rate in reversed(cap) if t <= -5)
        inside = [(t, rate) for t, rate in cap if -5 < t <= right]
        end_rate = inside[-1][1] if inside else initial
        t, rate = zip(*[(-5, initial), *inside, (right, end_rate)])
        upper.step(t, rate, where="post", color="#343a47", lw=1.8)
        upper.set(ylim=(0, 43), xlim=(-5, right))
        for t, rate in inside:
            upper.axvline(t, color="#858b96", ls=":", lw=1)
            ax.axvline(t, color="#858b96", ls=":", lw=1)
        upper.text(.03, .2, f"提交时容量 {initial:g} Mbps", transform=upper.transAxes, fontsize=9)
        style(upper, version, "前向容量（Mbps）" if col == 0 else "")
        example_events = []
        for r in groups[version]:
            x = float(r["send_ms"]) - origin
            if not r["confirmation_ms"]:
                continue
            y = float(r["confirmation_ms"])
            ax.scatter(x, y, color=ACK, s=22, zorder=3)
            residence = float(r["relay_egress_ms"]) - float(r["relay_ingress_ms"])
            ax.scatter(x, residence, color=DELAY, s=16, marker="+", zorder=4)
            event = events.get(key(r))
            if not event:
                continue
            reason = event["loss_reason"]
            if reason in THRESHOLDS:
                _, color, marker = THRESHOLDS[reason]
                h = float(event["deadline_age_ms"])
                ax.vlines(x, h, y, color=color, lw=1.15)
                ax.scatter(x, h, color=color, s=50, marker=marker, zorder=5)
                ax.annotate(f"包 {r['packet_number']}，门限 {h:.3f} ms", (x, h), xytext=(16, -12),
                            textcoords="offset points", color=color, fontsize=9)
                example_events.append(event)
            else:
                ax.scatter(x, float(event["age_at_loss_ms"]), color=LOSS, marker="x", s=38, zorder=5)
        style(ax, "", "确认耗时或期限（ms）" if col == 0 else "")
        ax.set(xlabel="包的发送时刻（相对本次请求计划提交时刻，ms）", ylim=(0, None))
        for event in example_events:
            row = {k: event[k] for k in ("run", "case", "packet_number", "send_ms", "one_way_ms", "confirmation_ms",
                                         "ack_reverse_ms", "relay_ingress_ms", "relay_egress_ms", "loss_reason",
                                         "deadline_age_ms", "age_at_loss_ms", "effective_timer_at_loss_ms",
                                         "first_repeat_send_ms", "first_repeat_receive_ms")}
            examples.append(dict(version=version, task=task, **row))
    handles = [Line2D([], [], color=c, marker=m, ls="None", label=l) for c, m, l in (
        (ACK, "o", "原包确认往返耗时"), (DELAY, "+", "前向中继停留时间"),
        (PTO, "^", "该原包的探测超时门限"))]
    fig.legend(handles=handles, loc="outside lower center", ncol=3, frameon=False)
    figure(out, "rate-ack-by-packet", fig)
    return examples


def timeout_timeline(data, out):
    events = [(version, e) for version, (_, rows) in data.items() for e in rows.values()
              if e["loss_reason"] == "timer"]
    if not events:
        return
    fig, ax = plt.subplots(figsize=(11.2, 3.9), layout="constrained")
    symbols = ((PTO, "|", "实际超时门限"), (LOSS, "x", "发送端启动恢复"), (DELAY, "o", "原包到达接收端"),
               (ACK, "s", "原包确认到达发送端"), ("#7656a5", "D", "携带相同数据的新包到达"))
    labels = []
    for i, (version, event) in enumerate(events):
        labels.append(f"{version} 第 {int(event['repeat'])+1} 轮，包 {event['packet_number']}")
        send = float(event["send_ms"])
        markers = [float(event[f]) for f in ("deadline_age_ms", "age_at_loss_ms", "one_way_ms", "confirmation_ms")]
        repeat = event["first_repeat_receive_ms"]
        markers.append(float(repeat) - send if repeat else None)
        ax.hlines(i, 0, max(x for x in markers if x is not None), color="#ccd3dd", lw=1.3)
        for x, offset, (color, marker, _) in zip(markers, (-.16, .15, -.09, .11, -.15), symbols):
            if x is not None:
                ax.scatter(x, i + offset, color=color, marker=marker, s=62, zorder=3)
    ax.set(xlim=(0, None), ylim=(-.4, len(events)-.6), xlabel="从各原包发送开始计时（ms）")
    ax.set_yticks(range(len(events)), labels)
    ax.invert_yaxis()
    style(ax, f"仅靠前向低速产生的 {len(events)} 次探测超时", "")
    handles = [Line2D([], [], color=c, marker=m, ls="None", label=l) for c, m, l in symbols]
    fig.legend(handles=handles, loc="outside lower center", ncol=3, fontsize=9, frameon=False)
    figure(out, "rate-timeout-timeline", fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "results/rate-only-loss")
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    set_chinese_font(args.font)
    data, stats, plans, audits = {}, {}, {}, []
    for version, dirname in (("BBRv1", "bbr1"), ("BBRv3", "bbr3")):
        folder = args.data / dirname
        data[version] = packets, events = load(folder)
        plans[version], checks = read_relay(folder / "raw-evidence.tar.xz", version)
        audits.extend(checks)
        tasks = read(folder / "tasks.csv")
        for case in (*LABELS, None):
            stats[version, case] = timing_stats(version, case, packets, events, tasks)
    for run, plan in plans["BBRv1"].items():
        other = plans["BBRv3"][run]
        if any(plan[k] != other[k] for k in ("capacity", "tasks", "impairments", "buffer_packets")):
            raise ValueError(f"Unpaired rate or workload for {run}")
    csv_write(args.data / "summary.csv", list(stats.values()))
    csv_write(args.data / "relay-audit.csv", audits)
    csv_write(args.data / "paired-time-events.csv", paired_events(data))
    cdf(data, stats, args.data)
    csv_write(args.data / "examples.csv", packet_plot(data, plans, args.data))
    timeout_timeline(data, args.data)


if __name__ == "__main__":
    main()
