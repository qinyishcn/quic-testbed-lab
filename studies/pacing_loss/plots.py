"""Render Chinese packet-delay and loss-recovery figures from the published tables."""

import argparse
import csv
import gzip
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from analyze import csv_write
from v3.plots import figure, plt, set_chinese_font, style


LABELS = {
    "steady":"恒定链路",
    "one_delay5":"单个前向包加 5 ms",
    "one_delay80":"单个前向包加 80 ms",
    "jitter5_fifo":"0–5 ms 抖动，保序",
    "jitter5_reorder":"0–5 ms 抖动，允许乱序",
    "rack_tail35":"2.4 kB 请求，首包加 35 ms",
    "fifo_pause80":"前向保序暂停 80 ms",
    "ack_pause80":"回程保序暂停 80 ms",
    "true_drop":"真正丢弃一个前向包",
}


def read(path):
    with path.open(newline="",encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def delay_cdf(output):
    samples = defaultdict(list)
    with gzip.open(output / "packets.csv.gz", "rt", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            delay = row["one_way_ms"]
            if not delay:
                continue
            direction = row["direction"]
            if direction == "forward" and row["measured"] == "1":
                samples[direction, row["case"]].append(float(delay))
            elif direction == "reverse" and row["packet_type"] == "1RTT" and float(row["send_ms"]) >= 1200:
                samples[direction, row["case"]].append(float(delay))

    colors = {
        "steady": "#303941",
        "one_delay5": "#b66a15",
        "one_delay80": "#70538f",
        "jitter5_fifo": "#117e8b",
        "jitter5_reorder": "#4163aa",
        "rack_tail35": "#8c641e",
        "fifo_pause80": "#b54445",
        "ack_pause80": "#a2477d",
        "true_drop": "#567847",
    }
    fig, axes = plt.subplots(2, 2, figsize=(12.6, 8.3), layout="constrained")
    fig.suptitle("BBRv1 逐包单向时延的累积分布", fontweight="bold", fontsize=15)
    short = ("steady", "one_delay5", "jitter5_fifo", "jitter5_reorder", "ack_pause80", "true_drop")
    tail = ("steady", "one_delay80", "rack_tail35", "fifo_pause80")
    reverse_focus = ("steady", "ack_pause80")
    panels = (
        ("forward", short, (0, 11), "甲｜前向数据包，时延较短的六组"),
        ("forward", tail, (0, 85), "乙｜前向数据包，长延迟条件与基线"),
        ("reverse", reverse_focus, (1, 2.1), "丙｜回程包，1–2.1 ms 放大"),
        ("reverse", reverse_focus, (0, 85), "丁｜回程包，完整时延范围"),
    )
    for ax, (direction, cases, limits, title) in zip(axes.flat, panels):
        def curve(case, **kwargs):
            ordered = sorted(samples[direction, case])
            n = len(ordered)
            if not n:
                raise ValueError(f"no arrived packets for {direction}/{case}")
            x = [0, *ordered, max(limits[1], ordered[-1])]
            y = [0, *(100*i/n for i in range(1, n+1)), 100]
            ax.step(x, y, where="post", **kwargs)

        if direction == "reverse":
            other_cases = [case for case in LABELS if case not in cases]
            for index, case in enumerate(other_cases):
                curve(case, color="#a3adb6", alpha=.52, linewidth=.9,
                      label="其余七种条件" if index == 0 else None)
        for case in cases:
            n = len(samples[direction, case])
            curve(case, color=colors[case], linewidth=1.9, label=f"{LABELS[case]}（{n} 包）",
                  linestyle="--" if case in ("steady", "true_drop") else "-")
        ax.set(xlim=limits, ylim=(-2, 104), xlabel="单向时延（ms）")
        ax.set_yticks((0, 25, 50, 75, 100))
        style(ax, title, "不超过横轴时延的包占比（%）")
        ax.legend(loc="lower right", fontsize=8.4, framealpha=.95)
    figure(output, "delay-cdf", fig)


def delay_figure(output, summary, losses):
    fig,(ax,comparison) = plt.subplots(1,2,figsize=(14.8,6.0),layout="constrained")
    ordered = [summary[case] for case in LABELS]
    positions = list(range(len(ordered)))
    for field, label, marker, color in (
        ("delay_p50","前向中位数","o","#1f7889"),
        ("delay_p99","前向第 99 百分位数","s","#63858e"),
        ("delay_max","前向最大值","D","#b95141"),
        ("reverse_delay_max","回程最大值","^","#63518b"),
    ):
        ax.scatter([float(row[field]) for row in ordered],positions,s=40,marker=marker,color=color,label=label)
    for y,row in enumerate(ordered):
        ax.hlines(y,float(row["delay_p50"]),float(row["delay_max"]),color="#b6c2c7",lw=1,zorder=0)
    ax.set_yticks(positions,[LABELS[case] for case in LABELS])
    ax.invert_yaxis()
    ax.set_xlabel("逐包单向时延（ms）")
    ax.set_xlim(-3,91)
    style(ax,"甲｜各网络条件的单向时延","")
    ax.legend(loc="lower right",frameon=False,ncol=2,fontsize=9)

    line = (0,88)
    comparison.plot(line,line,"--",color="#777777",lw=1,label="原包到达与判丢同时")
    for reason,label,marker,color in (
        ("packet_number","包号差达到 3","o","#1f7889"),
        ("rack_time","基于确认时序的计时","s","#ab7541"),
        ("timer","探测超时","^","#9c456a"),
    ):
        data = [row for row in losses if row["loss_reason"] == reason and row["one_way_ms"]]
        comparison.scatter([float(row["one_way_ms"]) for row in data],
                           [float(row["age_at_loss_ms"]) for row in data],
                           marker=marker,color=color,s=27,alpha=.66,label=label)
    comparison.set(xlim=(-3,88),ylim=(-3,88),xlabel="原包前向单向时延（ms）")
    comparison.set_aspect("equal",adjustable="box")
    style(comparison,"乙｜每次判丢时，原包是否已经到达","从原包发出到发送端判丢（ms）")
    comparison.legend(loc="upper left",frameon=False,fontsize=9)
    figure(output,"delay-and-loss",fig)


def timelines(output, rows):
    selected = []
    for case,reason in (("one_delay5","packet_number"),("rack_tail35","rack_time"),
                        ("fifo_pause80","timer"),("ack_pause80","timer")):
        selected.append(next(row for row in rows if row["case"] == case and row["loss_reason"] == reason
                             and row["first_repeat_send_ms"] and row["ack_receive_ms"]))
    csv_write(output / "examples.csv",selected)
    fig,ax = plt.subplots(figsize=(12,4.7),layout="constrained")
    event_types = (
        ("receive_ms","接收端收到原包","o","#1f7889",-.16),
        ("ack_send_ms","接收端发出确认原包的 ACK","s","#617ebb",-.05),
        ("loss_ms","发送端判丢或触发超时恢复","X","#bc5140",.07),
        ("first_repeat_send_ms","发送端发出含相同数据的新包","^","#986a1c",.19),
        ("first_repeat_receive_ms","接收端收到补发包","v","#489065",.29),
        ("ack_receive_ms","原包的 ACK 到达发送端","D","#6b548b",-.25),
    )
    for y,row in enumerate(selected):
        start = float(row["send_ms"])
        finish = max(float(row[field])-start for field,*_ in event_types if row[field])
        ax.hlines(y,0,finish,color="#c9c9c9",lw=1)
        for field,label,marker,color,shift in event_types:
            if row[field]:
                ax.scatter(float(row[field])-start,y+shift,marker=marker,color=color,s=58,
                           label=label if y == 0 else None,zorder=4)
    ax.set_yticks(range(len(selected)),[LABELS[row["case"]] for row in selected])
    ax.set_ylim(-.55,len(selected)-.38)
    ax.invert_yaxis()
    ax.set_xlabel("从对应原包发出开始计时（ms）")
    style(ax,"四个逐包例子中的原包到达、判丢、补发与确认","")
    ax.legend(loc="upper center",bbox_to_anchor=(.5,-.15),ncol=3,frameon=False,fontsize=9)
    figure(output,"event-timelines",fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results",type=Path)
    parser.add_argument("--font",type=Path)
    args = parser.parse_args()
    set_chinese_font(args.font)
    summary = {row["case"]:row for row in read(args.results / "summary.csv")}
    losses = [row for row in read(args.results / "loss-events.csv")
              if row["direction"] == "forward" and row["measured"] == "1"]
    delay_cdf(args.results)
    delay_figure(args.results,summary,losses)
    timelines(args.results,losses)


if __name__ == "__main__":
    main()
