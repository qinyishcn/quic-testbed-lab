"""Compare delivery rate and available tandem service on a paired QUIC trace."""

import argparse
import json
import sys
import tarfile
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from analyze import csv_write
from studies.cross_layer.latency_reference import Service, scheduled_arrivals
from studies.cross_layer.path_breakdown import LABELS, MODES, merge_interval, packet_events
from v3.plots import figure, plt, set_chinese_font, style

BIN_US = 50_000
WINDOW_BINS = 50
PROPAGATION_US = 4_000
COLORS = {"bbr": "#465771", "radio": "#943f63", "ideal": "#087b70"}


def unique_deliveries(trace, plan):
    covered = defaultdict(list)
    events = []
    for when, _, frames in packet_events(trace, "packet_received"):
        size = 0
        for frame in frames:
            if frame["frame_type"] != "stream":
                continue
            stream = int(frame["id"])
            job, remainder = divmod(stream, 4)
            if job == 0:
                continue
            offset = int(frame.get("offset", 0))
            end = offset+int(frame["length"])
            if remainder or job >= len(plan["tasks"]) or not 0 <= offset <= end <= plan["tasks"][job]["size"]:
                raise ValueError("The receive trace disagrees with the request plan")
            before = covered[stream]
            after = merge_interval(before, offset, end)
            size += sum(b-a for a, b in after)-sum(b-a for a, b in before)
            covered[stream] = after
        if size:
            events.append((when-plan["origin"], size))
    for job, task in enumerate(plan["tasks"][1:], 1):
        if covered[4*job] != [(0, task["size"])]:
            raise ValueError(f"Measured delivery is incomplete for request {job}")
    return events


def read_arrivals(path):
    runs = defaultdict(dict)
    with tarfile.open(path, "r|xz") as archive:
        for member in archive:
            run, _, name = member.name.partition("/")
            if name == "plan.json":
                runs[run]["plan"] = json.load(archive.extractfile(member))
            elif name.startswith("qlog-server/") and name.endswith(".qlog"):
                trace = json.load(archive.extractfile(member))["traces"][0]
                runs[run]["arrivals"] = unique_deliveries(trace, runs[run]["plan"])
    return runs


def link_segments(service, start, end):
    i = bisect_right(service.times, start)
    j = bisect_right(service.times, end)
    return service.times[i:j]


def tandem_capacity(air, backhaul, start, end):
    """Bits per microsecond for a saturated sender and zero carry-in queue."""
    cuts = sorted({start, end, *link_segments(air, start, end), *link_segments(backhaul, start, end)})
    air_bits = queue = low_us = 0
    for a, b in zip(cuts, cuts[1:]):
        r_air, r_backhaul = air.rate(a), backhaul.rate(a)
        air_bits += (b-a)*r_air
        queue = max(0, queue+(b-a)*(r_air-r_backhaul))
        if r_backhaul < 15:
            low_us += b-a
    return (air_bits-queue)/(end-start), low_us/(end-start)


def cumulative(events, edges):
    times, sizes = zip(*sorted(events))
    prior = np.r_[0, np.cumsum(sizes)]
    return prior[np.searchsorted(times, edges, side="left")]


def timeline(bbr, radio):
    plan = bbr["plan"]
    common = lambda p: {k: v for k, v in p.items() if k not in ("origin", "controller")}
    if common(plan) != common(radio["plan"]) or plan.get("forward_step"):
        raise ValueError("Timeline requires paired plans with fixed forward propagation")
    air, backhaul = Service(plan["radio"]), Service(plan["backhaul"])
    ideal = []
    scheduled_arrivals(air, backhaul, plan, deliveries=ideal)
    arrivals = {"bbr": bbr["arrivals"], "radio": radio["arrivals"], "ideal": ideal}
    submitted = [(t["t"], t["size"]) for t in plan["tasks"] if not t["warmup"]]
    total = sum(n for _, n in submitted)
    if any(sum(n for _, n in a) != total for a in arrivals.values()):
        raise ValueError("Inconsistent application payload volume")
    start, last = submitted[0][0], submitted[-1][0]
    end = max(max(t for t, _ in v) for v in arrivals.values())
    edges = start + BIN_US*np.arange(int((end-start)//BIN_US)+2)
    demand = cumulative(submitted, edges)
    sent = {mode: cumulative(data, edges) for mode, data in arrivals.items()}
    pending = {mode: demand-a for mode, a in sent.items()}
    if min(min(a) for a in pending.values()) < 0 or any(a[-1] != 0 for a in pending.values()):
        raise ValueError("Arrivals precede submission or fail to drain")
    reference = [tandem_capacity(air, backhaul, a-PROPAGATION_US, b-PROPAGATION_US)
                 for a, b in zip(edges, edges[1:])]
    caps, lows = np.array(reference).T
    rows = []
    for i, (a, b) in enumerate(zip(edges, edges[1:])):
        row = dict(start_ms=a/1000, end_ms=b/1000, service_mbps=caps[i], low_backhaul_fraction=lows[i])
        for mode in MODES:
            row[mode+"_mbps"] = (sent[mode][i+1]-sent[mode][i])*8/BIN_US
            row[mode+"_pending_kB"] = pending[mode][i+1]/1000
        rows.append(row)
    best = None
    for hi in range(WINDOW_BINS, len(edges)):
        if edges[hi] > last or np.mean(lows[hi-WINDOW_BINS:hi]) < .25:
            continue
        means = {m: np.mean(pending[m][hi-WINDOW_BINS+1:hi+1])/1000 for m in MODES}
        score = min(means["bbr"], means["radio"])-means["ideal"]
        if best is None or score > best["score_kB"]:
            best = dict(score_kB=score, mean_pending_kB=means, start_ms=edges[hi-WINDOW_BINS]/1000,
                        end_ms=edges[hi]/1000, low_backhaul_fraction=np.mean(lows[hi-WINDOW_BINS:hi]))
    return rows, best


def plot(folder, rows, best):
    fig, axes = plt.subplots(3, 1, figsize=(12.6, 9), layout="constrained")
    fig.suptitle("40 kB 请求连续提交时，链路服务能力和服务器收到的业务量", fontsize=16, fontweight="bold")
    edge = [rows[0]["start_ms"], *(r["end_ms"] for r in rows)]
    lo, hi = best["start_ms"], best["end_ms"]
    for i, ax in enumerate(axes[:2]):
        ax.stairs([r["service_mbps"] for r in rows], edge, color="#575d65", ls="--", lw=1.15,
                  baseline=None, label="端到端链路服务参照")
        for mode in MODES:
            ax.stairs([r[mode+"_mbps"] for r in rows], edge, color=COLORS[mode], lw=1.45,
                      baseline=None, label=LABELS[mode])
        if i == 0:
            ax.axvspan(lo, hi, alpha=.1, color="#65879d", lw=0)
    for mode in MODES:
        axes[2].plot(edge[1:], [r[mode+"_pending_kB"] for r in rows], color=COLORS[mode], lw=1.45)
    for ax in axes[1:]:
        for r in rows:
            if r["low_backhaul_fraction"] > .5 and lo <= r["start_ms"] < hi:
                ax.axvspan(r["start_ms"], r["end_ms"], alpha=.09, color="#ad702c", lw=0)
        ax.set_xlim(lo, hi)
    style(axes[0], f"第 {best['repeat']+1} 轮全程 · 浅蓝区域为下方放大窗口", "50 ms 平均速率（Mbps）")
    style(axes[1], "放大的 2.5 s · 浅棕区域内回传大部分时间低于 15 Mbps", "50 ms 平均速率（Mbps）")
    style(axes[2], "同一窗口内已提交但服务器尚未收到的业务数据", "待交付数据（kB）")
    axes[0].set_xlim(edge[0], edge[-1])
    upper = 1.05*max(r[key] for r in rows for key in ("service_mbps", *(m+"_mbps" for m in MODES)))
    for ax in axes[:2]:
        ax.set_ylim(0, upper)
    axes[2].set_ylim(bottom=0)
    axes[-1].set_xlabel("相对实验起点的时间（ms）")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncol=4, frameon=False)
    figure(folder, "e2e-goodput-timeline", fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT/"results/cross-layer-backhaul-10-30")
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    bbr, radio = (read_arrivals(args.data/m/"raw-evidence.tar.xz") for m in ("bbr", "radio"))
    if bbr.keys() != radio.keys():
        raise ValueError("Measured runs are unpaired")
    best = None
    for run in sorted(bbr):
        rows, choice = timeline(bbr[run], radio[run])
        if choice is not None and (best is None or choice["score_kB"] > best["score_kB"]):
            best = dict(choice, repeat=bbr[run]["plan"]["repeat"], run=run)
            chosen = rows
    if best is None:
        raise ValueError("No qualifying 2.5 s window")
    csv_write(args.data/"e2e-goodput-timeline.csv", chosen)
    (args.data/"e2e-goodput-window.json").write_text(json.dumps(best, indent=2)+"\n", encoding="utf-8")
    set_chinese_font(args.font)
    plot(args.data, chosen, best)


if __name__ == "__main__":
    main()
