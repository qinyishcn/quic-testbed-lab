"""Compute a physical floor and a packet-scheduling reference from archived link plans."""

import argparse
import json
import random
import sys
import tarfile
from bisect import bisect_left, bisect_right
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from analyze import csv_write, quantile
from studies.cross_layer.analyze import phase_bounds, read
from v3.relay import service_finish

# A representative full data packet has 1,218 stream bytes in 1,280 IP bytes.
STREAM_BYTES = 1218
HEADER_BYTES = 62


class Service:
    def __init__(self, schedule):
        self.times, self.rates = zip(*schedule)

    def rate(self, t):
        return self.rates[bisect_right(self.times, t)-1]

    def finish(self, start, bits):
        return service_finish(self.times, self.rates, start, bits)

    def latest_start(self, end, bits):
        index = bisect_left(self.times, end)-1
        while bits > (end-self.times[index])*self.rates[index]:
            bits -= (end-self.times[index])*self.rates[index]
            end = self.times[index]
            index -= 1
            if index < 0:
                raise ValueError("Insufficient service before the reserved completion")
        return end-bits/self.rates[index]


def isolated_finish(air, backhaul, start, bits):
    """Earliest exit of an isolated request, allowing bit-level pipelining."""
    air_done = air.finish(start, bits)
    events = sorted({start, air_done} | {t for t in (*air.times, *backhaul.times) if start < t < air_done})
    queued = 0
    for a, b in zip(events, events[1:]):
        queued = max(0, queued+(b-a)*(air.rate(a)-backhaul.rate(a)))
    return backhaul.finish(air_done, queued)


def forward_arrival(finish, step):
    return finish+4000+(step[2] if step and step[0] <= finish < step[1] else 0)


def physical_floor(air, backhaul, task, plan):
    finish = isolated_finish(air, backhaul, task["t"], 8*task["size"])
    step = plan.get("forward_step")
    arrive = forward_arrival(finish, step)
    # At a downward propagation step, a packet sent later can arrive earlier.
    if step and step[0] <= finish < step[1]:
        arrive = min(arrive, forward_arrival(step[1], step))
    return (arrive+plan["reverse_delay"]["min_us"]-task["t"])/1000


def packets_ready(plan):
    retries = random.Random(plan["seed"])
    gate = plan["gate"]
    for job, task in enumerate(plan["tasks"]):
        for offset in range(0, task["size"], STREAM_BYTES):
            ready = task["t"]
            while gate and retries.random() < gate["probability"]:
                ready += gate["wait_us"]
            bits = 8*(min(STREAM_BYTES, task["size"]-offset)+HEADER_BYTES)
            yield ready, job, offset, bits


def scheduled_arrivals(air, backhaul, plan, paths=None):
    """Reserve earliest FIFO backhaul slots, then schedule the radio just in time."""
    received = defaultdict(float)
    air_free = backhaul_free = 0
    reservations = []
    for ready, job, offset, bits in sorted(packets_ready(plan)):
        air_free = air.finish(max(air_free, ready), bits)
        backhaul_start = max(backhaul_free, air_free)
        backhaul_free = backhaul.finish(backhaul_start, bits)
        delivered = forward_arrival(backhaul_free, plan.get("forward_step"))
        reservations.append((backhaul_start, ready, bits, job, offset, backhaul_free, delivered))
        received[job] = max(received[job], delivered)
    next_air_start = float("inf")
    for backhaul_start, ready, bits, job, offset, backhaul_end, delivered in reversed(reservations):
        air_end = min(backhaul_start, next_air_start)
        # Submit at air_start minus the pre-sampled retry wait. This places
        # discretionary waiting at the sender without delaying a backhaul slot.
        air_start = air.latest_start(air_end, bits)
        if air_start+1e-6 < ready or air.finish(air_start, bits) > air_end+1e-6:
            raise ValueError("Ideal send time cannot make the reserved service slot")
        if paths is not None and delivered == received[job] and job not in paths:
            release = plan["tasks"][job]["t"]
            send = air_start-(ready-release)
            paths[job] = (offset, (release, send, send, air_start, air_start, air_end,
                                   backhaul_start, backhaul_end, delivered, delivered))
        next_air_start = air_start
    return received


def reference_tasks(plan):
    air, backhaul = Service(plan["radio"]), Service(plan["backhaul"])
    received = scheduled_arrivals(air, backhaul, plan)
    without_retry = scheduled_arrivals(air, backhaul, dict(plan, gate=None)) if plan["gate"] else received
    reverse = plan["reverse_delay"]
    reply_rng = random.Random(reverse["seed"])
    a, b = phase_bounds(plan["case"])
    for job, task in enumerate(plan["tasks"]):
        reply_delay = reply_rng.randint(reverse["min_us"], reverse["max_us"])
        if task["warmup"]:
            continue
        release = task["t"]
        phase = "pre" if release < a else "stress" if release < b else "post"
        floor = physical_floor(air, backhaul, task, plan)
        replay = (received[job]+reply_delay-release)/1000
        no_retry = (without_retry[job]+reply_delay-release)/1000
        if min(replay, no_retry) < floor-1e-8:
            raise ValueError("Packet replay finished faster than the physical lower bound")
        yield dict(profile=plan["case"], repeat=plan["repeat"], phase=phase, task=job,
                   start_ms=release/1000, physical_floor_ms=floor, scheduling_reference_ms=replay,
                   scheduling_no_retry_ms=no_retry, scheduling_upload_ms=(received[job]-release)/1000)


def plot_comparison(folder, reference, font):
    from v3.plots import figure, plt, set_chinese_font, style

    set_chinese_font(font)
    measured = {(r["profile"], r["mode"]): float(r["e2e_ms_p95"])
                for r in read(folder / "summary.csv") if r["phase"] == "stress"}
    replay = {r["profile"]: r for r in reference if r["phase"] == "stress"}
    fig, axes = plt.subplots(1, 2, figsize=(12, 5), layout="constrained")
    fig.suptitle("真实 QUIC 请求时延与理想排程参照", fontsize=16)
    cohorts = (
        ("回传可承载平均业务量", ("radio", "mixed-feasible"), ("空口快变\n回传 45 Mbps", "空口快变\n回传 12 Mbps")),
        ("回传低于平均业务量", ("mixed", "mixed-clean"), ("有随机等待\n回传 7.5 Mbps", "无随机等待\n回传 7.5 Mbps")),
    )
    for ax, (title, profiles, labels) in zip(axes, cohorts):
        for index, (mode, name, color) in enumerate((
            ("bbr", "原生 BBRv3", "#505e78"), ("radio", "仅空口快环", "#943f63"),
            ("bbr_radio", "BBRv3 加空口快环", "#187c77"),
        )):
            ax.bar([x+(index-1)*.21 for x in range(len(profiles))],
                   [measured[p, mode] for p in profiles], width=.2, color=color, label=name)
        ax.scatter(range(len(profiles)), [replay[p]["scheduling_reference_p95_ms"] for p in profiles],
                   marker="D", color="#ad702c", edgecolor="white", s=65, zorder=3, label="理想排程")
        ax.scatter(range(len(profiles)), [replay[p]["physical_floor_p95_ms"] for p in profiles],
                   marker="_", color="#232b33", linewidths=2, s=120, zorder=3, label="单笔物理下界")
        style(ax, title, "请求时延第 95 百分位数（ms）")
        ax.set_xticks(range(len(profiles)), labels)
        ax.set_ylim(bottom=0)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncol=5, frameon=False)
    figure(folder, "latency-reference", fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "results/cross-layer-bbr-radio")
    parser.add_argument("--plot", action="store_true")
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    tasks = []
    with tarfile.open(args.data / "bbr" / "raw-evidence.tar.xz", "r|xz") as archive:
        for member in archive:
            if member.name.endswith("/plan.json"):
                tasks.extend(reference_tasks(json.load(archive.extractfile(member))))
    summary = []
    for profile in sorted({t["profile"] for t in tasks}):
        for phase in ("all", "pre", "stress", "post"):
            group = [t for t in tasks if t["profile"] == profile and (phase == "all" or t["phase"] == phase)]
            floor = [t["physical_floor_ms"] for t in group]
            replay = [t["scheduling_reference_ms"] for t in group]
            summary.append(dict(profile=profile, phase=phase, tasks=len(group),
                                physical_floor_p95_ms=quantile(floor, .95),
                                scheduling_reference_p95_ms=quantile(replay, .95),
                                scheduling_no_retry_p95_ms=quantile([t["scheduling_no_retry_ms"] for t in group], .95),
                                scheduling_reference_within_80ms_pct=100*sum(x <= 80 for x in replay)/len(replay)))
    csv_write(args.data / "latency-reference-tasks.csv", tasks)
    csv_write(args.data / "latency-reference-summary.csv", summary)
    if args.plot:
        plot_comparison(args.data, summary, args.font)


if __name__ == "__main__":
    main()
