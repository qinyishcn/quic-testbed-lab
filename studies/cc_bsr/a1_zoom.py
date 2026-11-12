"""Plot native BBR control and the measured relay service around the A1 drop."""

import argparse
import csv
import gzip
import io
import json
import sys
import tarfile
from bisect import bisect_right
from pathlib import Path

from matplotlib.ticker import MultipleLocator

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from studies.cc_bsr.analyze import read_a1
from v3.pacing_changes import gain
from v3.plots import COLORS, figure, plt, set_chinese_font, style


PACE, WINDOW, BANDWIDTH = COLORS[2], COLORS[4], "#9a633b"
CAPACITY, FORWARDED, INFLIGHT = "#30343a", "#3a7e53", "#697789"
V1_PHASES = {0.75: "排队消减 ×0.75", 1: "巡航 ×1.00", 1.25: "加速探测 ×1.25"}
V3_PHASES = {2: "降速 ×0.90", 3: "巡航 ×1.00", 4: "补充在途 ×1.00", 5: "提速 ×1.25"}
WINDOWS = ((-60, 140), (-5, 25))


def rows_in_gzip(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def step_window(points, left, right):
    times, values = zip(*points)
    first, last = bisect_right(times, left), bisect_right(times, right)
    if not first:
        raise ValueError(f"Trace has no sample at or before {left} ms")
    return (left, *times[first:last], right), (values[first - 1], *values[first:last], values[last - 1])


def step(ax, points, left, right, **kwargs):
    times, values = step_window(points, left, right)
    ax.step(times, values, where="post", **kwargs)


def plot_drop(axes, plan, packets, samples, events=None):
    drop = next(t for t, rate in plan["capacity"] if t >= 3_000_000 and rate == 10)
    sample_time = lambda row: (int(row["us"]) - plan["origin"] - drop) / 1000
    capacity = [((t - drop) / 1000, rate) for t, rate in plan["capacity"]]
    inflight = [(sample_time(r), int(r["inflight"]) / 1000) for r in samples]
    if events is None:
        pace = [(sample_time(r), int(r["pacing_bps"]) / 1e6) for r in samples]
        bw = [(sample_time(r), int(r["btlbw_bps"]) / 1e6) for r in samples]
        cwnd = [(sample_time(r), int(r["cwnd"]) / 1000) for r in samples]
        phases = [(sample_time(r), gain(int(r["pacing_bps"]), int(r["btlbw_bps"])))
                  for r in samples if abs(sample_time(r)) < 160]
        phase_names = V1_PHASES
    else:
        pace = [(sample_time(r), int(r["pace_bps"]) / 1e6) for r in events]
        bw = [(sample_time(r), int(r["bw_bps"]) / 1e6) for r in events]
        cwnd = [(sample_time(r), int(r["cwnd_bytes"]) / 1000) for r in events]
        phases = [(sample_time(r), int(r["state"])) for r in events if abs(sample_time(r)) < 160]
        phase_names = V3_PHASES

    start, end = WINDOWS[0]
    forwarded = dict.fromkeys(range(start, end, 5), 0)
    for row in packets:
        t = sample_time(row)
        if row["event"] == "out" and start <= t < end:
            forwarded[int(t // 5) * 5] += int(row["bytes"])
    service = [(t, b * 8 / 5000) for t, b in forwarded.items()]
    service.append((end, service[-1][1]))

    for col, ((left, right), title) in enumerate(zip(WINDOWS, (
            "（甲）200 ms 概览", "（乙）30 ms 放大，最小刻度 1 ms"))):
        rates, windows, states = axes[:, col]
        step(rates, pace, left, right, color=PACE, lw=2, label="目标发包速率")
        step(rates, capacity, left, right, color=CAPACITY, ls="--", lw=1.5, label="模拟空口容量")
        step(rates, bw, left, right, color=BANDWIDTH, ls=":", lw=1.6, label="带宽估计")
        step(rates, service, left, right, color=FORWARDED, lw=1.2, alpha=.9, label="实际转发速率（每 5 ms）")
        rates.set(ylim=(0, 44), yticks=[0, 10, 20, 30, 40])
        style(rates, title, "速率（Mbps）")
        step(windows, cwnd, left, right, color=WINDOW, lw=2, label="拥塞窗口")
        step(windows, inflight, left, right, color=INFLIGHT, lw=1, alpha=.85, label="实际在途量")
        windows.set(ylim=(0, 42), yticks=[0, 10, 20, 30, 40])
        style(windows, "", "数据量（kB）")
        phase_index = {value: i for i, value in enumerate(phase_names)}
        t, active = step_window(phases, left, right)
        states.step(t, [phase_index[v] for v in active], where="post", color=PACE, lw=2)
        states.set(ylim=(-.5, len(phase_names) - .5),
                   xlabel="相对模拟空口降速时刻的时间（ms）")
        states.set_yticks(range(len(phase_names)), list(phase_names.values()))
        style(states, "", "带宽探测阶段")
        for ax in (rates, windows, states):
            ax.axvline(0, ls=":", lw=1.1, color=CAPACITY, alpha=.75)
            ax.set_xlim(left, right)
        states.xaxis.set_major_locator(MultipleLocator(25 if col == 0 else 5))
        if col == 1:
            states.xaxis.set_minor_locator(MultipleLocator(1))
        rates.tick_params(labelbottom=False)
        windows.tick_params(labelbottom=False)

    axes[0, 0].legend(loc="lower right", ncol=2, fontsize=9, framealpha=.95)
    axes[1, 0].legend(loc="lower right", ncol=2, fontsize=9, framealpha=.95)
    drop_rate = pace[bisect_right([t for t, _ in pace], 0) - 1][1]
    change_time, new_rate = next((t, rate) for t, rate in pace if t > 0 and rate != drop_rate)
    col = 1 if events is None else 0
    for ax in axes[:, col]:
        ax.axvline(change_time, ls=":", lw=1.1, color=PACE, alpha=.75)
    change_label = "首次采到" if events is None else "调至"
    axes[0, col].annotate(f"+{change_time:.3f} ms {change_label} {new_rate:.2f} Mbps",
                          xy=(change_time, new_rate), xytext=(8, 37) if col == 1 else (48, 41),
                          color=PACE, fontsize=9, arrowprops={"arrowstyle": "->", "color": PACE})


def read_rtt(path):
    with tarfile.open(path) as archive:
        prefix = "A1load_probe_rtt__00/"
        with archive.extractfile(prefix + "plan.json") as stream:
            origin = json.load(stream)["origin"]
        with io.TextIOWrapper(archive.extractfile(prefix + "bbr3-events.csv"), encoding="utf-8") as stream:
            return [dict(row, offset_us=int(row["us"]) - origin) for row in csv.DictReader(stream)]


def plot_extra(ax, events, bounds, title, spans, rate_limit, window_limit):
    left, right = bounds
    rates = [(int(r["offset_us"]) / 1000, int(r["pace_bps"]) / 1e6) for r in events]
    cwnd = [(int(r["offset_us"]) / 1000, int(r["cwnd_bytes"]) / 1000) for r in events]
    win_ax = ax.twinx()
    for start, stop, label, color in spans:
        ax.axvspan(start, stop, color=color, zorder=0)
        ax.text((start + stop) / 2, .93, label, transform=ax.get_xaxis_transform(),
                ha="center", va="top", fontsize=9)
    step(ax, rates, left, right, color=PACE, lw=1.7)
    step(win_ax, cwnd, left, right, color=WINDOW, lw=1.7)
    ax.set(xlim=(left, right), ylim=(0, rate_limit),
           xlabel="从各自实验计时起点开始计时（ms）")
    win_ax.set(ylim=(0, window_limit), ylabel="拥塞窗口（kB）")
    style(ax, title, "目标发包速率（Mbps）")
    win_ax.spines[["top", "left"]].set_visible(False)
    ax.tick_params(axis="y", colors=PACE)
    ax.yaxis.label.set_color(PACE)
    win_ax.tick_params(axis="y", colors=WINDOW)
    win_ax.yaxis.label.set_color(WINDOW)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, help="Path to a font with Simplified Chinese glyphs")
    parser.add_argument("--output", type=Path, default=ROOT / "results/cc-bsr")
    args = parser.parse_args()
    set_chinese_font(args.font)
    args.output.mkdir(parents=True, exist_ok=True)
    source = ROOT / "results"
    _, v1_plan, v1_wire, v1_samples = read_a1(source / "v3")
    bbr3 = source / "bbr3"
    v3_plan = json.loads((bbr3 / "bbr3-native-plan.json").read_text(encoding="utf-8"))
    v3_samples = rows_in_gzip(bbr3 / "bbr3-native-samples.csv.gz")
    v3_wire = rows_in_gzip(bbr3 / "bbr3-native-wire.csv.gz")
    v3_events = rows_in_gzip(bbr3 / "bbr3-native-events.csv.gz")
    if v1_plan["capacity"] != v3_plan["capacity"]:
        raise ValueError("A1 runs must use the same capacity schedule")

    fig, axes = plt.subplots(3, 2, figsize=(13, 7), sharex="col", layout="constrained",
                             height_ratios=(2, 1.65, 1.15))
    fig.suptitle("A1 实测：BBRv1 的目标发包速率、拥塞窗口和探测阶段", fontweight="bold", fontsize=16)
    plot_drop(axes, v1_plan, v1_wire, v1_samples)
    figure(args.output, "a1-bbrv1-zoom", fig)

    fig = plt.figure(figsize=(13, 10), layout="constrained")
    main, extra = fig.subfigures(2, 1, height_ratios=(7, 3), hspace=.05)
    main.suptitle("A1 实测：BBRv3 的目标发包速率、拥塞窗口和探测阶段", fontweight="bold", fontsize=16)
    axes = main.subplots(3, 2, sharex="col", height_ratios=(2, 1.65, 1.3))
    plot_drop(axes, v3_plan, v3_wire, v3_samples, v3_events)
    startup, probe = extra.subplots(1, 2)
    startup_events = [r for r in v3_events if 2_535_000 <= int(r["offset_us"]) < 2_590_000
                      and r["state_before"] != r["state"]]
    start, drain, cruise = [int(r["offset_us"]) / 1000 for r in startup_events]
    if [r["state"] for r in startup_events] != ["0", "1", "3"]:
        raise ValueError("Expected startup, drain and cruise in the A1 excerpt")
    plot_extra(startup, v3_events, (2535, 2590), "（丙）A1 同轮中的重新启动与排空",
               [(start, drain, "启动 ×2.77", "#e8ebf3"), (drain, cruise, "排空 ×0.50", "#f5e8d5")], 100, 50)
    rtt_events = read_rtt(source / "cc-bsr/a1-bbrv3-probe-rtt.tar.xz")
    enter = next(int(r["offset_us"]) / 1000 for r in rtt_events if r["state"] == "6")
    exit_time = next(int(r["offset_us"]) / 1000 for r in rtt_events if r["state_before"] == "6" and r["state"] != "6")
    plot_extra(probe, rtt_events, (enter - 35, exit_time + 35), "（丁）时延阶跃补测中的最小往返时延探测",
               [(enter, exit_time, "探测期间发包速率系数 ×1.00", "#e8ebf3")], 10, 25)
    figure(args.output, "a1-bbrv3-zoom", fig)


if __name__ == "__main__":
    main()
