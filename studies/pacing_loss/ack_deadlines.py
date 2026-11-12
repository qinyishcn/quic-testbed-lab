"""Compare observed QUIC ACK return times with the sender's logged loss deadlines."""

import argparse
import csv
import gzip
import sys
from collections import Counter
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from analyze import csv_write
from studies.pacing_loss.plots import LABELS, read
from v3.plots import figure, plt, set_chinese_font, style
from matplotlib.lines import Line2D
from matplotlib.ticker import MultipleLocator, NullFormatter


SOURCES = {"BBRv1": "pacing-loss", "BBRv3": "bbr3/loss"}
CASES = ("one_delay5", "rack_tail35", "fifo_pause80", "ack_pause80")
TITLES = ("单包加 5 ms", "两包请求首包加 35 ms", "前向保序暂停 80 ms", "回程保序暂停 80 ms")
REASONS = ("packet_number", "rack_time", "timer", "timer")
ACK, DELAY, LOSS = "#225b85", "#668675", "#a1363e"
RACK, PTO = "#7656a5", "#b75e12"
THRESHOLDS = {"rack_time": ("RACK 期限", RACK, "D"), "timer": ("探测超时期限", PTO, "^")}


def measured(row):
    return row["direction"] == "forward" and row["measured"] == "1"


def key(row):
    return tuple(row[field] for field in ("run", "direction", "packet_type", "packet_number"))


def load(version):
    root = ROOT / "results" / SOURCES[version]
    with gzip.open(root / "packets.csv.gz", "rt", encoding="utf-8") as stream:
        packets = [row for row in csv.DictReader(stream) if measured(row)]
    events = {key(row): row for row in read(root / "loss-events.csv") if measured(row)}
    if {key(row) for row in packets if row["declared_lost"] == "1"} != events.keys():
        raise ValueError(f"Missing loss event for {version}")
    for row in packets:
        if bool(row["confirmation_ms"]) == (row["actual_relay_drop"] == "1"):
            raise ValueError(f"Unexpected ACK coverage for {version} {key(row)}")
        event = events.get(key(row))
        if event and event["loss_reason"] == "timer":
            elapsed_us = int(event["native_deadline_us"]) - int(event["native_send_us"])
            timer_us = int(event["native_effective_timer_us"])
            if (event["native_timer_expired"] != "1" or int(event["ack_gap_at_loss"]) > 0
                    or not timer_us <= elapsed_us < 2 * timer_us
                    or not float(row["confirmation_ms"]) > float(event["age_at_loss_ms"]) >= float(event["deadline_age_ms"])):
                raise ValueError(f"Cannot attribute probe timeout for {version} {key(row)}")
    return packets, events


def summary(version, case, packets, events):
    selected = [row for row in packets if not case or row["case"] == case]
    durations = [float(row["confirmation_ms"]) for row in selected if row["confirmation_ms"]]
    triggered = [events[key(row)] for row in selected if key(row) in events]
    counts = Counter(row["loss_reason"] for row in triggered)
    values = dict(version=version, case=case or "all", packets_sent=len(selected),
                  packets_with_ack=len(durations), relay_dropped=len(selected) - len(durations),
                  ack_median_ms=median(durations), ack_max_ms=max(durations),
                  packet_number_events=counts["packet_number"], rack_events=counts["rack_time"],
                  pto_events=counts["timer"], pto_share_of_sent_pct=100 * counts["timer"] / len(selected))
    for reason, label in (("rack_time", "rack"), ("timer", "pto")):
        group = [row for row in triggered if row["loss_reason"] == reason]
        deadlines = [float(row["deadline_age_ms"]) for row in group]
        with_ack = [row for row in group if row["confirmation_ms"]]
        middle = median(deadlines) if deadlines else None
        values.update({f"{label}_deadline_min_ms": min(deadlines, default=None),
                       f"{label}_deadline_median_ms": middle,
                       f"{label}_deadline_max_ms": max(deadlines, default=None),
                       f"{label}_all_ack_above_event_median": sum(d > middle for d in durations) if deadlines else None,
                       f"{label}_events_with_ack": len(with_ack),
                       f"{label}_paired_ack_after_deadline": sum(float(row["confirmation_ms"]) > float(row["deadline_age_ms"]) for row in with_ack)})
    return values


def paired_events(data):
    rows = []
    for version, (_, events) in data.items():
        for row in events.values():
            reason = row["loss_reason"]
            if reason not in THRESHOLDS:
                continue
            ack = float(row["confirmation_ms"]) if row["confirmation_ms"] else None
            deadline = float(row["deadline_age_ms"])
            rows.append(dict(version=version, run=row["run"], case=row["case"], reason=reason,
                             packet_number=row["packet_number"], send_ms=row["send_ms"],
                             original_arrival_ms=row["one_way_ms"], original_ack_ms=ack,
                             deadline_age_ms=deadline, decision_age_ms=row["age_at_loss_ms"],
                             ack_minus_deadline_ms=round(ack - deadline, 3) if ack is not None else None))
    return rows


def first_request(packets, case):
    return sorted((row for row in packets if row["case"] == case and row["repeat"] == "0"
                   and "4" in row["stream_ids"].split("|")), key=lambda row: float(row["send_ms"]))


def example(version, case, reason, packets, events):
    row = next(events[key(row)] for row in first_request(packets, case)
               if key(row) in events and events[key(row)]["loss_reason"] == reason)
    sent = float(row["send_ms"])
    loss_age = float(row["age_at_loss_ms"])
    deadline = float(row["deadline_age_ms"]) if reason in THRESHOLDS else None
    later_ack = (int(row["native_highest_ack_receive_us"]) - int(row["native_send_us"])) / 1000
    base = float(row["base_timer_at_loss_ms"])
    effective = float(row["effective_timer_at_loss_ms"])
    rack_candidate = None
    if reason == "rack_time":
        delta = int(row["native_latest_acked_send_us"]) - int(row["native_send_us"])
        rack_candidate = later_ack + (min(int(row["native_srtt_us"]) // 4, 5000) - delta
                                      + int(row["native_max_ack_delay_us"])) / 1000
        if abs(min(base, rack_candidate) - deadline) > .001:
            raise ValueError(f"RACK deadline differs from recorded inputs for {version}")
    return dict(version=version, case=case, run=row["run"], reason=reason, packet_number=row["packet_number"],
                send_ms=sent, original_arrival_ms=float(row["one_way_ms"]), original_ack_ms=float(row["confirmation_ms"]),
                ack_return_path_ms=float(row["ack_reverse_ms"]), decision_age_ms=loss_age, deadline_age_ms=deadline,
                highest_acked_packet=row["native_highest_ack"], ack_gap=row["ack_gap_at_loss"],
                later_ack_ms=later_ack if reason != "timer" else None,
                base_timer_ms=base, effective_timer_ms=effective, rack_candidate_ms=rack_candidate,
                pto_tail_offset_ms=round(deadline - effective, 3) if reason == "timer" else None,
                first_repeat_packet=row["repeat_packet_numbers"].split("|")[0],
                repeat_send_age_ms=round(float(row["first_repeat_send_ms"]) - sent, 3),
                repeat_arrival_age_ms=round(float(row["first_repeat_receive_ms"]) - sent, 3))


def legend_handle(label, color, marker):
    return Line2D([], [], label=label, color=color, marker=marker, linestyle="None")


def cdf(data, summaries, out):
    fig, axes = plt.subplots(2, 4, figsize=(14.5, 6), sharey=True, layout="constrained")
    fig.suptitle("原包确认往返耗时的累积分布与实测判丢期限", fontsize=16, fontweight="bold")
    for i, (version, (packets, _)) in enumerate(data.items()):
        for j, (case, title, reason) in enumerate(zip(CASES, TITLES, REASONS)):
            ax = axes[i, j]
            row = summaries[version, case]
            times = sorted(float(r["confirmation_ms"]) for r in packets if r["case"] == case and r["confirmation_ms"])
            ax.ecdf(times, color=ACK, lw=2, zorder=3)
            if reason in THRESHOLDS:
                _, color, _ = THRESHOLDS[reason]
                label = "rack" if reason == "rack_time" else "pto"
                low, middle, high = (row[f"{label}_deadline_{n}_ms"] for n in ("min", "median", "max"))
                ax.axvspan(low, high, color=color, alpha=.14)
                ax.axvline(middle, color=color, ls="--", lw=1.4)
                tail = row[f"{label}_all_ack_above_event_median"]
                ax.scatter([middle], [1 - tail / len(times)], s=22, color=color, zorder=4)
                note = f"期限中位数 {middle:.2f} ms\n参考右尾 {tail}/{len(times)}"
            else:
                note = "按包号触发\n此组无计时判丢事件"
            ax.text(.96, .12, note, ha="right", va="bottom", transform=ax.transAxes, fontsize=9,
                    bbox=dict(facecolor="white", edgecolor="none", alpha=.9, pad=2))
            ax.set_xscale("log")
            ticks = [2, 5, 10, 20] if j == 0 else [2, 5, 10, 20, 40, 80]
            ax.set_xticks(ticks, [str(x) for x in ticks])
            ax.xaxis.set_minor_formatter(NullFormatter())
            ax.set(xlim=(1.7, 23 if j == 0 else 100), ylim=(0, 1.045))
            ax.set_yticks([0, .25, .5, .75, 1], ["0%", "25%", "50%", "75%", "100%"])
            style(ax, f"{version}｜{title}", "累计包比例" if j == 0 else "")
            if i == 1:
                ax.set_xlabel("确认往返耗时（ms，对数刻度）")
    figure(out, "ack-cdf-deadlines", fig)


def by_packet(data, out):
    fig, axes = plt.subplots(2, 4, figsize=(14.5, 6), layout="constrained")
    fig.suptitle("每个原包的确认往返耗时与它自己的判丢期限", fontsize=16, fontweight="bold")
    for i, (version, (packets, events)) in enumerate(data.items()):
        for j, (case, title) in enumerate(zip(CASES, TITLES)):
            ax = axes[i, j]
            rows = first_request(packets, case)
            origin = float(rows[0]["send_ms"])
            for row in rows:
                x = float(row["send_ms"]) - origin
                y = float(row["confirmation_ms"])
                ax.scatter(x, y, color=ACK, s=19, alpha=.83, zorder=3)
                event = events.get(key(row))
                if not event:
                    continue
                reason = event["loss_reason"]
                if reason in THRESHOLDS:
                    _, color, marker = THRESHOLDS[reason]
                    deadline = float(event["deadline_age_ms"])
                    ax.vlines(x, deadline, y, color=color, lw=1.1, alpha=.75)
                    ax.scatter(x, deadline, color=color, marker=marker, s=37, zorder=4)
                else:
                    deadline, color = float(event["age_at_loss_ms"]), LOSS
                    ax.scatter(x, deadline, color=color, marker="x", s=42, zorder=4)
                label_y = y if reason == "packet_number" else deadline
                ax.annotate(f"包 {row['packet_number']}", (x, label_y), xytext=(7, -10),
                            textcoords="offset points", fontsize=8, color=color)
            ax.set(xlim=(-1, 20 if j < 2 else 95), ylim=(0, (13, 48, 95, 95)[j]))
            ax.xaxis.set_major_locator(MultipleLocator(5 if j < 2 else 20))
            style(ax, f"{version}｜{title}", "耗时或期限（ms）" if j == 0 else "")
            if i == 1:
                ax.set_xlabel("从该请求首包发出开始计时（ms）")
    items = [legend_handle("原包确认往返耗时", ACK, "o"), legend_handle("包号判丢发生的耗时", LOSS, "x")]
    items.extend(legend_handle(label, color, marker) for label, color, marker in THRESHOLDS.values())
    fig.legend(handles=items, loc="outside lower center", ncol=4, frameon=False)
    figure(out, "ack-by-packet", fig)


def timelines(examples, out):
    fig, axes = plt.subplots(2, 2, figsize=(12.5, 5.6), layout="constrained")
    fig.suptitle("三种判丢机制的逐包时间线", fontsize=16, fontweight="bold")
    symbols = (("original_arrival_ms", "原包到达", DELAY, "o"), ("later_ack_ms", "后发包的确认到达", "#226466", "v"),
               ("deadline_age_ms", "计时期限", PTO, "|"), ("decision_age_ms", "发送端采取恢复动作", LOSS, "x"),
               ("repeat_arrival_age_ms", "含相同流字节的新包到达", RACK, "D"),
               ("original_ack_ms", "原包的确认到达", ACK, "s"))
    for j, (ax, case, title) in enumerate(zip(axes.flat, CASES, TITLES)):
        group = [r for r in examples if r["case"] == case]
        for y, row in zip((1, 0), group):
            ax.hlines(y, 0, row["original_ack_ms"], color="#c9d3db", lw=1.1)
            ax.scatter(0, y, marker="|", color="#333333", s=45)
            for offset, (field, _, color, marker) in zip((.09, -.09, -.13, .13, -.04, .09), symbols):
                x = row[field]
                if x is not None:
                    ax.scatter(x, y + offset, marker=marker, color=color, s=55, zorder=3)
        ax.set(ylim=(-.35, 1.35), xlim=(-.3 if j == 0 else -1, (12, 45, 90, 90)[j]),
               xlabel="从各自原包发出开始计时（ms）")
        ax.set_yticks([0, 1], [f"{r['version']} 包 {r['packet_number']}" for r in reversed(group)])
        style(ax, title, "")
    fig.legend(handles=[legend_handle(label, color, marker) for _, label, color, marker in symbols],
               loc="outside lower center", ncol=3, frameon=False)
    figure(out, "ack-event-timelines", fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, help="Path to a font with Simplified Chinese glyphs")
    parser.add_argument("--output", type=Path, default=ROOT / "results/ack-deadlines")
    args = parser.parse_args()
    set_chinese_font(args.font)
    args.output.mkdir(parents=True, exist_ok=True)
    data = {version: load(version) for version in SOURCES}
    summaries = {(version, case): summary(version, case, packets, events)
                 for version, (packets, events) in data.items() for case in (*LABELS, None)}
    examples = [example(version, case, reason, packets, events)
                for version, (packets, events) in data.items() for case, reason in zip(CASES, REASONS)]
    csv_write(args.output / "summary.csv", list(summaries.values()))
    csv_write(args.output / "examples.csv", examples)
    csv_write(args.output / "paired-time-events.csv", paired_events(data))
    timelines(examples, args.output)
    cdf(data, summaries, args.output)
    by_packet(data, args.output)


if __name__ == "__main__":
    main()
