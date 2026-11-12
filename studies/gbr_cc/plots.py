"""Draw Chinese charts for the GBR-CC reproduction."""

import argparse
import csv
import gzip
import math
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from studies.gbr_cc.analyze import artifact_gain, read
from v3.plots import figure, plt, set_chinese_font, style

MODES = {"bbr": ("原生 BBR", "#657186"), "gbr": ("公开 GBR-CC", "#147b8e"),
         "dual": ("补建双环", "#b65e43")}
PROFILES = {"steady": "恒定无线容量", "radio_step": "无线降至 10 Mbps",
            "radio_fast": "无线每 5–10 ms 切换", "backhaul_step": "回传降至 10 Mbps"}


def gain_chart(output):
    def paper_gain(ratio, beta):
        if ratio <= .2:
            return .98+.02/(1+math.exp(-beta*ratio))
        return 1+.01/(1+math.exp(-beta*(ratio-.4)))

    fig, ax = plt.subplots(figsize=(8.4, 4.3), layout="constrained")
    x = [i/1000 for i in range(1001)]
    ax.plot(x, [paper_gain(r, 35) for r in x], color="#147b8e", lw=2, label="论文通用传输公式")
    ax.plot(x, [paper_gain(r, 50) for r in x], color="#657186", lw=1.8, linestyle="--",
            label="论文视频通话公式")
    ax.plot(x, [artifact_gain(r) for r in x], color="#b65e43", lw=1.8, label="公开 MsQuic 代码")
    ax.axvline(.2, color="#777777", ls=":", label="目标比值 0.2")
    ax.set(xlabel="授权字节量与上报待发字节量的比值", xlim=(0, 1), ylim=(.9885, 1.0115))
    style(ax, "一次更新使用多大的速率乘数", "速率乘数")
    ax.legend(loc="lower right")
    figure(output, "gain-equation", fig)


def signal_chart(output):
    grants = [r for r in read(output / "signal-grants.csv") if r["socket_sent"] == "1"]
    with gzip.open(output / "signal-ack.csv.gz", "rt", encoding="utf-8") as file:
        rows = [r for r in csv.DictReader(file) if 0 <= float(r["offset_us"]) < 3_100_000]
    times = [float(r["offset_us"])/1e6 for r in rows]
    fig, axes = plt.subplots(3, 1, figsize=(10.2, 7.6), sharex=True, layout="constrained")
    tx = [(float(r["offset_ms"])/1000, float(r["ratio"])) for r in grants if float(r["offset_ms"]) < 3100]
    axes[0].scatter(*zip(*tx), marker="|", s=29, color="#657186", alpha=.72, label="发送给原版端点的样本")
    axes[0].step(times, [float(r["ratio_milli"])/1000 for r in rows], where="post",
                 color="#147b8e", lw=1.7, label="端点使用的平滑比值")
    axes[0].legend(loc="center right")
    axes[1].step(times, [int(r["ratio_pace_Bps"])*8/1e6 for r in rows], where="post", color="#147b8e")
    axes[2].step(times, [int(r["cwnd_bytes"])/1000 for r in rows], where="post", color="#147b8e")
    for ax, title, ylabel in zip(axes, ("甲　注入的比值与端点平滑值", "乙　比值控制器的目标发包速率",
                                     "丙　发送端的实际拥塞窗口"),
                                ("授权与待发量之比", "目标速率（Mbps）", "拥塞窗口（kB）")):
        ax.axvspan(2, 2.3, color="#f2dca9", alpha=.7)
        ax.set_xlim(0, 3.1)
        style(ax, title, ylabel)
    axes[-1].set_xlabel("从测试开始计时（s）")
    figure(output, "signal-replay", fig)


def bulk_chart(output, runs):
    fig, axes = plt.subplots(1, 2, figsize=(11.8, 4.4), layout="constrained")
    cases = list(PROFILES)
    for ax, field, title, ylabel in (
        (axes[0], "goodput_kbps", "甲　完整上传的有效吞吐量", "中位数（Mbps）"),
        (axes[1], "rtt_p50_ms", "乙　发送端平滑往返时延", "逐轮中位数的中位数（ms）"),
    ):
        scale = 1000 if field == "goodput_kbps" else 1
        for mode, shift in (("bbr", -.18), ("gbr", .18)):
            label, color = MODES[mode]
            x = [i+shift for i in range(len(cases))]
            values = [[float(r[field])/scale for r in runs if r["suite"] == "formal"
                       and r["profile"] == case and r["mode"] == mode] for case in cases]
            ax.bar(x, [median(v) for v in values], width=.32, color=color, alpha=.72, label=label)
            for pos, group in zip(x, values):
                points = [pos + (i-(len(group)-1)/2)*.035 for i in range(len(group))]
                ax.scatter(points, group, color=color, edgecolor="white", linewidth=.35, zorder=4)
        ax.set_xticks(range(len(cases)), [PROFILES[c] for c in cases], fontsize=8.5)
        ax.margins(y=.2)
        style(ax, title, ylabel)
        ax.legend(loc="upper center", ncol=2)
    figure(output, "bulk-comparison", fig)


def outer_chart(output):
    curves = defaultdict(list)
    with gzip.open(output / "timeline.csv.gz", "rt", encoding="utf-8") as file:
        for r in csv.DictReader(file):
            if r["suite"] == "outer" and r["profile"] == "backhaul_step" and r["rtt_ms"]:
                offset = float(r["offset_ms"])/1000
                if offset <= 8:
                    for key in ("rtt_ms", "backhaul_queue_kB"):
                        curves[key, r["mode"], offset].append(float(r[key]))
    fig, axes = plt.subplots(2, 1, sharex=True, figsize=(10.2, 6), layout="constrained")
    for ax, key, title, unit in (
        (axes[0], "rtt_ms", "甲　回传降速时发送端的平滑往返时延", "平滑往返时延（ms）"),
        (axes[1], "backhaul_queue_kB", "乙　回传中继的排队数据量", "队列深度（kB）"),
    ):
        for mode, (label, color) in MODES.items():
            points = sorted((t, median(v)) for (field, m, t), v in curves.items()
                            if field == key and m == mode and len(v) == 5)
            ax.plot(*zip(*points), label=label, color=color, lw=1.8)
        ax.axvspan(2, 6, color="#f2dca9", alpha=.56)
        ax.set_xlim(0, 8)
        style(ax, title, unit)
    axes[0].legend(ncol=3, loc="upper left")
    axes[1].set_xlabel("从测试开始计时（s）")
    figure(output, "outer-backhaul", fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    set_chinese_font(args.font)
    gain_chart(args.output)
    signal_chart(args.output)
    bulk_chart(args.output, list(read(args.output / "rounds.csv")))
    if (args.output / "outer-environment.json").exists():
        outer_chart(args.output)
