"""Build figures from the paired two-link measurements."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from studies.cross_layer.analyze import LABELS, NAMES, phase_bounds, read
from studies.cross_layer.config import MODES, PROFILES, REVISION_MODES, REVISION_PROFILES, SLOW
from v3.plots import figure, plt, set_chinese_font, style

COLORS = dict(bbr="#505e78", outer="#ad702c", radio="#943f63", dual="#187c77", bbr_radio="#187c77")


def bar_comparison(data, out, modes, profiles):
    fig, axes = plt.subplots(2, 2, figsize=(14, 8.2), layout="constrained")
    fig.suptitle("控制方案在同一批 40 kB 请求下的表现", fontsize=17, fontweight="bold")
    for i, (ax, field, title, ylabel) in enumerate(zip(axes.flat,
            ("e2e_ms_p95", "within_80ms_pct", "radio_wait_ms_p95", "backhaul_wait_ms_p95"),
            ("从提交请求到终端收到回复", "80 ms 内收到回复的请求比例",
             "上行空口队列等待", "上行回传队列等待"),
            ("第 95 百分位数（ms）", "比例（%）", "等待第 95 百分位数（ms）", "等待第 95 百分位数（ms）"))):
        width = .76 / len(modes)
        for j, mode in enumerate(modes):
            y = [float(data[profile, mode, "stress"][field]) for profile in profiles]
            ax.bar([k+(j-(len(modes)-1)/2)*width for k in range(len(profiles))], y, width=width-.01,
                   color=COLORS[mode], label=NAMES[mode])
        style(ax, title, ylabel)
        ax.set_xticks(range(len(profiles)), [LABELS[p].replace("·", "\n").replace("时延增加", "\n时延增加")
                                           for p in profiles], fontsize=9)
        if i == 1:
            ax.set_ylim(0, 105)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncol=len(modes), frameon=False)
    figure(out, "comparison", fig)


def radio_trace(rows, out, modes):
    nrows, ncols = (3, 1) if "bbr_radio" in modes else (2, 2)
    fig, axes = plt.subplots(nrows, ncols, figsize=(12.7, 7.8), sharex=True, sharey=True, layout="constrained")
    fig.suptitle("目标发包速率如何跟踪 10–30 Mbps 的空口变化",
                 fontsize=16, fontweight="bold")
    for ax, mode in zip(axes.flat, modes):
        data = rows["radio", mode]
        focus = [r for r in data if 1_340 <= float(r["ms"]) <= 1_480]
        t = [float(r["ms"]) for r in focus]
        ax.step(t, [float(r["radio_mbps"]) for r in focus], where="post", color="#333842",
                ls="--", lw=1.2, label="空口容量")
        ax.step(t, [float(r["pacing_mbps"]) for r in focus], where="post", color=COLORS[mode],
                lw=1.5, label="目标发包速率")
        if mode == "bbr_radio":
            ax.step(t, [float(r["bbr_outer_mbps"]) for r in focus], where="post", color="#ad702c",
                    ls=":", lw=1.4, label="BBRv3 原始发包目标")
        for sample in focus[:-1]:
            if float(sample["pending_kB"]) > 0:
                x = float(sample["ms"])
                ax.axvspan(x, x+2, alpha=.07, color="#187c77", lw=0)
        style(ax, NAMES[mode], "速率（Mbps）")
        ax.set_ylim(bottom=0)
    for ax in (axes.flat[0], axes.flat[-1]):
        ax.legend(loc="upper right", framealpha=.95, fontsize=9)
    fig.supxlabel("相对实验起点的时间（ms），浅绿色表示应用还有数据待发")
    figure(out, "radio-tracking", fig)


def mixed_trace(rows, tasks, out, modes):
    fields = ("pacing_mbps", "radio_kB", "backhaul_kB", "pending_kB")
    labels = ("目标速率（Mbps）", "空口待转发（kB）", "回传待转发（kB）", "发送端待发（kB）")
    if "bbr_radio" in modes:
        fields += ("cwnd_kB",)
        labels += ("拥塞窗口（kB）",)
    fig, axes = plt.subplots(len(fields)+1, 1, figsize=(12.2, 2.25*(len(fields)+1)),
                             sharex=True, layout="constrained")
    fig.suptitle("快慢叠加并过载时，等待在哪里累积", fontsize=16, fontweight="bold")
    for mode in modes:
        data = rows["mixed", mode]
        x = [float(r["ms"]) for r in data]
        for ax, field in zip(axes, fields):
            ax.plot(x, [float(r[field]) for r in data], color=COLORS[mode], lw=1.12, label=NAMES[mode])
        points = [r for r in tasks if r["profile"] == "mixed" and r["mode"] == mode and r["repeat"] == "0"]
        axes[-1].plot([float(r["start_ms"]) for r in points], [float(r["e2e_ms"]) for r in points],
                      color=COLORS[mode], marker="o", markersize=2.6, lw=1)
    axes[0].step(x, [float(r["backhaul_mbps"]) for r in data], where="post",
                 color="#333842", ls="--", label="回传容量")
    if "bbr_radio" in modes:
        hybrid = rows["mixed", "bbr_radio"]
        axes[0].plot([float(r["ms"]) for r in hybrid], [float(r["bbr_outer_mbps"]) for r in hybrid],
                     color="#ad702c", ls=":", lw=1.2, label="联合方案的 BBRv3 原始目标")
    axes[-1].axhline(80, color="#333842", ls="--", lw=1, label="80 ms 参考期限")
    for ax, label in zip(axes, (*labels, "请求到回复（ms）")):
        ax.axvspan(*(t/1000 for t in SLOW), color="#ad702c", alpha=.08, lw=0)
        style(ax, "", label)
        ax.set_ylim(bottom=0)
    axes[0].legend(loc="upper right", ncol=3, fontsize=8, framealpha=.95)
    axes[-1].legend(loc="upper right", fontsize=8)
    axes[-1].set_xlabel("相对实验起点的时间（ms），最后一行按各请求的提交时刻绘制")
    figure(out, "mixed-queue-timeline", fig)


def path_delay(rows, out, modes, profile):
    fig, axes = plt.subplots(1, 2, figsize=(12.4, 4.4), layout="constrained")
    for ax, field, title in zip(axes, ("upload_ms", "reply_ms"),
                               ("终端提交到服务器收齐", "服务器收齐到终端收到回复")):
        for mode in modes:
            group = [r for r in rows if r["profile"] == profile and r["mode"] == mode and r["repeat"] == "0"]
            ax.plot([float(r["start_ms"]) for r in group], [float(r[field]) for r in group],
                    label=NAMES[mode], color=COLORS[mode], marker="o", markersize=2, lw=1.05)
        ax.axvspan(*(t/1000 for t in phase_bounds(profile)), color="#ad702c", alpha=.09, lw=0)
        style(ax, title, "耗时（ms）")
        ax.set_xlabel("请求提交时刻（ms）")
        ax.set_ylim(bottom=0)
    axes[1].legend(loc="upper right", fontsize=8, ncol=2)
    figure(out, "forward-delay" if profile == "forward-delay" else "return-step", fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", action="store_true")
    parser.add_argument("--data", type=Path)
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    set_chinese_font(args.font)
    folder = args.data or ROOT / "results" / ("cross-layer-bbr-radio" if args.revision else "cross-layer-loops")
    modes, profiles = (REVISION_MODES, REVISION_PROFILES) if args.revision else (MODES, PROFILES)
    summary = {(r["profile"], r["mode"], r["phase"]): r for r in read(folder / "summary.csv")}
    timeline = {(p, m): [] for p in profiles for m in modes}
    for r in read(folder / "timeline.csv.gz"):
        timeline[r["profile"], r["mode"]].append(r)
    tasks = read(folder / "tasks.csv")
    bar_comparison(summary, folder, modes, profiles)
    radio_trace(timeline, folder, modes)
    mixed_trace(timeline, tasks, folder, modes)
    path_delay(tasks, folder, modes, profiles[-1])


if __name__ == "__main__":
    main()
