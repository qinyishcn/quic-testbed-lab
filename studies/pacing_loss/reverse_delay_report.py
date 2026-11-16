"""Build the combined forward-gate and reverse-delay report figures and tables."""

import argparse
from pathlib import Path
from statistics import median

from mismatch_report import (BOUNDED_LABELS, BOUNDED_SPANS, ROOT, csv_write, figure, load, plt,
                             plot_pair, quantile, set_chinese_font, style, tables)


COHORT = "pacing-mismatch-reverse"
MODES = ("bbr1-gate-reverse", "bbr3-gate-reverse")
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "results")
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    set_chinese_font(args.font)
    out = args.data / COHORT
    runs = load(args.data, {COHORT: CASES}, MODES)
    tables(runs, out, BOUNDED_SPANS)
    tasks, confirmations = timing_tables(runs, out)
    plot_outcomes(tasks, confirmations, out)
    plot_pair(runs, "bounded_rise", (-4, 48), out, modes=MODES,
              caption="双向随机时延条件下的升速响应｜20 Mbps 升至 30 Mbps", bin_ms=2)
    plot_pair(runs, "bounded_dip", (-4, 164), out, modes=MODES,
              caption="双向随机时延条件下的降速与回升｜20 Mbps 降至 10 Mbps", bin_ms=4)


if __name__ == "__main__":
    main()
