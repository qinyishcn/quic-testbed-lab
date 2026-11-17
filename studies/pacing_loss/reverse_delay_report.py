"""Build the combined forward-gate and reverse-delay report figures and tables."""

import argparse
import json
import tarfile
from collections import Counter
from pathlib import Path
from statistics import median

from mismatch_report import (BOUNDED_LABELS, BOUNDED_SPANS, CAP, CHANGE, OUT, PACE, ROOT, csv_write,
                             figure, load, plt, plot_pair, quantile, samples, set_chinese_font, step,
                             style, tables, windows)


COHORT = "pacing-mismatch-reverse"
LONG_RISE = "bounded_rise_long"
TRAIN_RISE = "bounded_rise_40kb_train"
MODES = ("bbr1-gate-reverse", "bbr3-gate-reverse")
TRAIN_MODES = ("bbr3-gate-reverse",)
CASES = tuple(BOUNDED_LABELS)
COLORS = ("#3a5c81", "#398079", "#ac4848", "#997223")


def timing_tables(runs, out):
    tasks, relay = [], []
    confirmations = {}
    for (mode, run), (raw, wire, rows) in sorted(runs.items()):
        case = raw["plan.json"]["case"]
        delays = wire["reverse_releases"]
        latest = reordered = 0
        for ident, _, _ in delays:
            reordered += ident < latest
            latest = max(latest, ident)
        draws = [draw / 1000 for _, draw, _ in delays]
        released = [actual / 1000 for _, _, actual in delays]
        lag = [(actual - draw) / 1000 for _, draw, actual in delays]
        relay.append(dict(mode=mode, run=run, case=case, reverse_datagrams=len(delays),
                          reordered_reverse_datagrams=reordered, draw_min_ms=min(draws),
                          draw_mean_ms=sum(draws)/len(draws), draw_max_ms=max(draws),
                          actual_min_ms=min(released), actual_p50_ms=median(released),
                          actual_p99_ms=quantile(released, .99), actual_max_ms=max(released),
                          release_lag_p99_ms=quantile(lag, .99), release_lag_max_ms=max(lag)))
        for task in rows["tasks"]:
            stream = str(4 * int(task["task"]))
            packets = [r for r in rows["packets"] if stream in r["stream_ids"].split("|")]
            acked = [r for r in packets if r["confirmation_ms"]]
            times = sorted({float(r["ack_receive_ms"]) for r in acked})
            spacing = [b-a for a, b in zip(times, times[1:])]
            delay = [float(r["confirmation_ms"]) for r in acked]
            confirmations.setdefault((mode, case, task["bytes"]), []).extend(delay)
            tasks.append(dict(mode=mode, run=run, case=case, task=task["task"],
                              request_bytes=task["bytes"], upload_ms=task["upload_ms"],
                              forward_packets=len(packets), packets_without_ack=len(packets)-len(acked),
                              ack_receive_timestamps=len(times), ack_gap_p50_ms=quantile(spacing, .5),
                              ack_gap_p99_ms=quantile(spacing, .99), confirmation_p50_ms=quantile(delay, .5),
                              confirmation_p99_ms=quantile(delay, .99),
                              forward_p99_ms=quantile([float(r["one_way_ms"]) for r in packets], .99),
                              receiver_ack_wait_p50_ms=quantile(
                                  [float(r["ack_send_ms"])-float(r["receive_ms"]) for r in acked], .5),
                              ack_reverse_p50_ms=quantile([float(r["ack_reverse_ms"]) for r in acked], .5)))
    csv_write(out / "ack-timing.csv", tasks)
    csv_write(out / "reverse-relay.csv", relay)
    return tasks, confirmations


def plot_outcomes(tasks, confirmations, out):
    fig, axes = plt.subplots(1, 2, figsize=(13.7, 4.7), layout="constrained")
    fig.suptitle("双向随机时延条件下的逐包确认时间与上传完成时间", fontsize=16, fontweight="bold")
    cdf, completion = axes
    for version, mode in enumerate(MODES):
        for case, color in zip(CASES, COLORS):
            delays = sorted(confirmations[mode, case, "200000"])
            cdf.step([0, *delays], [0, *(100*i/len(delays) for i in range(1, len(delays)+1))],
                     where="post", color=color, ls=("--" if version == 0 else "-"), lw=1.5,
                     label=BOUNDED_LABELS[case] if version else None)
        for j, case in enumerate(CASES):
            values = [float(r["upload_ms"]) for r in tasks
                      if r["request_bytes"] == "200000" and r["mode"] == mode and r["case"] == case]
            x = j + (-.14 if version == 0 else .14)
            band = "#536887" if version == 0 else "#1d8090"
            completion.scatter([x]*len(values), values, color=band, alpha=.48, s=22)
            completion.scatter(x, median(values), color=band, marker="_", s=165,
                               label=f"BBRv{mode[3]}" if j == 0 else None)
    style(cdf, "按包统计的确认往返时延（虚线 BBRv1，实线 BBRv3）", "已获确认的业务包累计比例（%）")
    cdf.set_xlabel("从发包到收到对应确认（ms）")
    cdf.set_xlim(left=0)
    cdf.legend(fontsize=8, loc="lower right")
    style(completion, "同一容量曲线下的五轮上传完成时间", "200 kB 上传耗时（ms）")
    completion.set_xticks(range(len(CASES)), ["恒定 20\nMbps", "升至 30\nMbps",
                                            "降至 10 Mbps\n后回升", "降至 16 Mbps\n后回升"])
    completion.set_ylim(bottom=0)
    completion.legend(fontsize=9)
    figure(out, "delivery-and-ack", fig)


def plot_long_rise(runs, out, *, request_train=False):
    """Compare 2 ms observations at the step with 50 ms means during sustained demand."""
    modes = TRAIN_MODES if request_train else MODES
    fig, axes = plt.subplots(3, len(modes), figsize=(10.5 if request_train else 13, 9.3),
                             sharey="row", squeeze=False, layout="constrained")
    title = "每 8 ms 提交一笔 40 kB" if request_train else "3 MB 连续上传"
    fig.suptitle(f"{title}｜升速初期的失配与随后空口容量的利用", fontsize=16, fontweight="bold")
    early = list(range(-20 if request_train else 0, 81, 2))
    coarse = list(range(0, 801, 50))
    late = list(range(700, 751, 2)) if request_train else list(range(550, 601, 2))
    detail_repeat = "第三轮" if request_train else "第一轮"
    groups = [(0, 8), (0, 100), (100, 500), (500, 800)]
    table = []
    for j, mode in enumerate(modes):
        detail, overview, zoom = axes[:, j]
        for ax in ((overview, zoom) if request_train else axes[:, j]):
            ax.axhline(30, color=CAP, lw=1.4, ls="--", label="空口容量（30 Mbps）")
        by_window = {a: [] for a in coarse[:-1]}
        for (tag, run), (raw, wire, rows) in sorted(runs.items()):
            if tag != mode:
                continue
            completed = max(float(job["start_ms"]) + float(job["upload_ms"]) - CHANGE
                            for job in rows["tasks"])
            if completed < coarse[-1]:
                raise ValueError("Upload finished before the comparison window ended")
            if run.endswith("__02" if request_train else "__00"):
                if request_train:
                    capacity = [(t / 1000 - CHANGE, rate) for t, rate in raw["plan.json"]["capacity"]]
                    step(detail, capacity, early[0], early[-1], color=CAP, lw=1.4, ls="--", zorder=4,
                         label="空口容量（20→30 Mbps）")
                pacing = samples(raw, "pacing_bps", 1e6)
                for ax, edges in ((detail, early), (zoom, late)):
                    step(ax, pacing, edges[0], edges[-1], color=PACE, lw=1.5, label="目标发包速率")
                    rates = [sum(n for t, n in wire["egress"] if a <= t < b) * .008 / (b - a)
                             for a, b in zip(edges, edges[1:])]
                    ax.stairs(rates, edges, baseline=None, color=OUT, lw=1.1, label="实际出队速率（2 ms）")
            for a, b in [*groups, *zip(coarse, coarse[1:])]:
                values = windows(raw, wire, a, b)
                table.append(dict(mode=mode, run=run, start_ms=a, end_ms=b, upload_finished_ms=completed,
                                  capacity_mbps=30, capacity_used_pct=100 * values["egress_mbps"] / 30,
                                  **values))
                if b-a == 50:
                    by_window[a].append(values["egress_mbps"])
            overview.stairs([v[-1] for v in by_window.values()], coarse, baseline=None,
                            color="#91a1a8", alpha=.55, lw=.9)
        overview.stairs([median(v) for v in by_window.values()], coarse, baseline=None,
                        color=OUT, lw=2.1, label="实际出队速率（五轮中位数）")
        if request_train:
            overview.stairs([v[2] for v in by_window.values()], coarse, baseline=None,
                            color="#795093", lw=1.4, ls="--", label="第三轮（放大示例）")
        overview.axvspan(500, 800, color=OUT, alpha=.09, label="后段统计区间")
        overview.axvspan(late[0], late[-1], color=OUT, alpha=.13)
        detail_span = "升速前 20 ms 至升速后 80 ms" if request_train else "初期 80 ms"
        style(detail, f"BBRv{mode[3]}｜{detail_span}，{detail_repeat}", "速率（Mbps）" if j == 0 else "")
        style(overview, f"BBRv{mode[3]}｜每 50 ms 平均值，灰线为逐轮", "速率（Mbps）" if j == 0 else "")
        style(zoom, f"BBRv{mode[3]}｜放大 {late[0]}–{late[-1]} ms，{detail_repeat}",
              "速率（Mbps）" if j == 0 else "")
        for ax, bounds in zip((detail, overview, zoom), (early, coarse, late)):
            ax.set_xlim(bounds[0], bounds[-1])
            ax.set_ylim(bottom=0)
            ax.set_xlabel("距空口从 20 Mbps 升至 30 Mbps 的时间（ms）")
    for ax in axes[:2, 0]:
        ax.legend(loc="lower right", fontsize=8, framealpha=.95)
    csv_write(out / ("train-40kb-windows.csv" if request_train else "scale-windows.csv"), table)
    figure(out, "train-40kb-rise" if request_train else "sustained-rise", fig)


def train_flow_control(source, out, runs):
    """Verify the negotiated limits and look for credit exhaustion on the sender."""
    results = []
    for mode in TRAIN_MODES:
        with tarfile.open(source / mode / "raw-evidence.tar.xz", "r|xz") as archive:
            for member in archive:
                run, _, path = member.name.partition("/")
                if not (path.startswith("qlog-client/") and path.endswith(".qlog")):
                    continue
                events = json.load(archive.extractfile(member))["traces"][0]["events"]
                remote = next(d for _, _, name, d in events
                              if name == "parameters_set" and d["owner"] == "remote"
                              and "initial_max_stream_data_bidi_remote" in d)
                expected = runs[mode, run][0]["plan.json"]["server_flow_control"]
                if (remote["initial_max_stream_data_bidi_remote"] != expected["stream_bytes"]
                        or remote["initial_max_data"] != expected["connection_bytes"]):
                    raise ValueError(f"Negotiated flow-control limits differ from the plan in {mode}/{run}")
                frames = Counter(f["frame_type"] for _, _, name, d in events
                                 if name == "packet_sent" for f in d.get("frames", []))
                results.append(dict(mode=mode, run=run, **expected,
                                    stream_data_blocked=frames["stream_data_blocked"],
                                    data_blocked=frames["data_blocked"],
                                    streams_blocked=sum(n for f, n in frames.items() if f.startswith("streams_blocked"))))
    if len(results) != len(runs) or len({(r["mode"], r["run"]) for r in results}) != len(runs):
        raise ValueError("Missing client qlog for a 40 kB train run")
    csv_write(out / "train-40kb-flow-control.csv", sorted(results, key=lambda r: (r["mode"], r["run"])))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "results")
    parser.add_argument("--font", type=Path)
    parser.add_argument("--train-40kb", action="store_true", help="Regenerate the BBRv3-only burst train report")
    args = parser.parse_args()
    set_chinese_font(args.font)
    out = args.data / COHORT
    if args.train_40kb:
        train_cohort = f"{COHORT}/train-40kb"
        train = load(args.data, {train_cohort: (TRAIN_RISE,)}, TRAIN_MODES)
        train_flow_control(args.data / train_cohort, out, train)
        plot_long_rise(train, out, request_train=True)
        return
    runs = load(args.data, {COHORT: CASES}, MODES)
    tables(runs, out, BOUNDED_SPANS)
    tasks, confirmations = timing_tables(runs, out)
    plot_outcomes(tasks, confirmations, out)
    plot_pair(runs, "bounded_rise", (-4, 120), out, modes=MODES, mark_upload=True,
              caption="双向随机时延条件下的升速响应｜200 kB 上传全过程", bin_ms=2)
    plot_pair(runs, "bounded_dip", (-4, 164), out, modes=MODES,
              caption="双向随机时延条件下的降速与回升｜20 Mbps 降至 10 Mbps", bin_ms=4)
    long_cohort = f"{COHORT}/sustained"
    long_runs = load(args.data, {long_cohort: (LONG_RISE,)}, MODES)
    plot_long_rise(long_runs, out)


if __name__ == "__main__":
    main()
