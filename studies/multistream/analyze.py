"""Count complete frames and the frame equivalents of the actually delivered byte budget."""

import argparse
import csv
import json
import sys
import tarfile
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from analyze import csv_write, quantile
from studies.cross_layer.path_breakdown import merge_interval, packet_events
from v3.plots import figure, plt, set_chinese_font, style

FRAME_BYTES = 40_000
STREAMS = 4
BUDGETS = (*range(10, 155, 5), 180, 200)


def read_archive(folder):
    runs = defaultdict(dict)
    with tarfile.open(folder/"raw-evidence.tar.xz", "r|xz") as archive:
        for item in archive:
            run, _, name = item.name.partition("/")
            if name == "plan.json":
                runs[run]["plan"] = json.load(archive.extractfile(item))
            elif name in ("client.csv", "server.csv", "wire.csv"):
                with archive.extractfile(item) as stream:
                    runs[run][name] = list(csv.DictReader(line.decode("utf-8") for line in stream))
            elif name.startswith("qlog-server/") and name.endswith(".qlog"):
                runs[run]["receiver"] = json.load(archive.extractfile(item))["traces"][0]
    return runs


def received_bytes(run, sizes):
    coverage, arrivals = defaultdict(list), defaultdict(list)
    for when, _, frames in packet_events(run["receiver"], "packet_received"):
        for f in frames:
            if f["frame_type"] != "stream":
                continue
            task, remainder = divmod(int(f["id"]), 4)
            if task == 0:
                continue
            lo = int(f.get("offset", 0))
            hi = lo+int(f["length"])
            if remainder or not 0 <= lo <= hi <= sizes[task]:
                raise ValueError("Unexpected stream data")
            earlier = coverage[task]
            merged = merge_interval(earlier, lo, hi)
            new = sum(b-a for a, b in merged)-sum(b-a for a, b in earlier)
            if new:
                cumulative = arrivals[task][-1][1] if arrivals[task] else 0
                arrivals[task].append((when, cumulative+new))
            coverage[task] = merged
    if any(coverage[t] != [(0, n)] for t, n in sizes.items()):
        raise ValueError("Incomplete QUIC stream in capture")
    return arrivals


def stream_rows(run_id, run):
    plan = run["plan"]
    sizes = {i: t["size"] for i, t in enumerate(plan["tasks"]) if not t["warmup"]}
    start = {int(r["task"]): int(r["us"]) for r in run["client.csv"] if r["event"] == "start" and int(r["task"])}
    done = {int(r["task"]): int(r["us"]) for r in run["server.csv"] if r["event"] == "upload" and int(r["task"])}
    if not start.keys() == done.keys() == sizes.keys():
        raise ValueError("A scheduled stream did not complete")
    arrivals = received_bytes(run, sizes)
    groups = defaultdict(list)
    for task in sorted(sizes):
        if sizes[task] != FRAME_BYTES or not start[task] <= arrivals[task][0][0] <= arrivals[task][-1][0] <= done[task]:
            raise ValueError("Request length or event order is invalid")
        batch = plan["tasks"][task]["batch"]
        groups[batch].append(task)
    if any(len(t) != STREAMS or len({start[x] for x in t}) != 1 for t in groups.values()):
        raise ValueError("The four streams did not start at a common timestamp")
    stream = []
    for batch, ids in groups.items():
        completion_order = {t: rank for rank, t in enumerate(sorted(ids, key=lambda t: (done[t], t)), 1)}
        for position, task in enumerate(ids, 1):
            stream.append(dict(run=run_id, repeat=plan["repeat"], batch=batch, stream=position,
                               quic_stream_id=4*task, completed_order=completion_order[task],
                               start_ms=(start[task]-plan["origin"])/1000,
                               upload_ms=(done[task]-start[task])/1000))
    deadlines = []
    for batch, ids in groups.items():
        previous = groups.get(batch-1)
        origin = start[ids[0]]
        if previous and max(done[t] for t in previous) >= origin:
            raise ValueError("Two batches overlap")
        for k in BUDGETS:
            cutoff = origin+1000*k
            by_stream = []
            for task in ids:
                times, volumes = zip(*arrivals[task])
                index = bisect_right(times, cutoff)
                by_stream.append(volumes[index-1] if index else 0)
            count = sum(done[t] <= cutoff for t in ids)
            subtotal = sum(by_stream)
            equivalent = subtotal//FRAME_BYTES
            if not 0 <= count <= equivalent <= STREAMS:
                raise ValueError("Byte count disagrees with completed streams")
            deadlines.append(dict(run=run_id, repeat=plan["repeat"], batch=batch, budget_ms=k,
                                  received_bytes=subtotal, completed=count, equivalent=equivalent,
                                  extra_frames=equivalent-count,
                                  partial_bytes=subtotal-count*FRAME_BYTES,
                                  **{f"stream_{j}_bytes": n for j, n in enumerate(by_stream, 1)}))
    return stream, deadlines


def wire_stats(runs):
    total = first = repeat = 0
    for run in runs.values():
        wait = Counter()
        forward = set()
        for r in run["wire.csv"]:
            if r["event"] == "drop":
                raise ValueError("An unexpected link drop invalidates the radio-only test")
            if r["direction"] == "forward" and r["event"] == "in":
                forward.add(r["id"])
            if r["event"] == "wait":
                if r["direction"] != "forward" or int(r["due_us"])-int(r["us"]) != 5000:
                    raise ValueError("Unexpected gate")
                wait[r["id"]] += 1
            if r["direction"] == "reverse" and r["event"] == "in" and int(r["due_us"]) != int(r["us"]):
                raise ValueError("Unexpected emulated delay on the return path")
        if not wait.keys() <= forward:
            raise ValueError("Gate event lacks a forward datagram")
        total += len(forward)
        first += len(wait)
        repeat += sum(n >= 2 for n in wait.values())
    return dict(forward_datagrams=total, waited_at_least_once=first, waited_at_least_twice=repeat)


def compare_deadlines(rows):
    groups = defaultdict(list)
    for r in rows:
        groups[r["budget_ms"]].append(r)
    stats = []
    rng = np.random.default_rng(86100)
    runs = sorted({r["repeat"] for r in rows})
    draws = rng.integers(len(runs), size=(2000, len(runs)))
    for k, g in sorted(groups.items()):
        n = len(g)
        completed, ideal = (sum(r[key] for r in g) for key in ("completed", "equivalent"))
        per_run_gaps = [np.mean([r["extra_frames"] for r in g if r["repeat"] == seed]) for seed in runs]
        lower, upper = np.percentile(np.array(per_run_gaps)[draws].mean(axis=1), [2.5, 97.5])
        stats.append(dict(budget_ms=k, batches=n, completed_frames=completed, equivalent_frames=ideal,
                          completed_mean=completed/n, equivalent_mean=ideal/n, extra_mean=(ideal-completed)/n,
                          extra_low95=lower, extra_high95=upper,
                          extra_batched_pct=100*sum(r["extra_frames"]>0 for r in g)/n,
                          relative_gain_pct=100*(ideal-completed)/completed if completed else None,
                          **{f"{metric}_{x}_batches": sum(r[metric]==x for r in g)
                             for metric in ("completed", "equivalent") for x in range(STREAMS+1)}))
    return stats


def plots(folder, summaries, streams):
    fig, (left, right) = plt.subplots(1, 2, figsize=(12.1, 4.8), layout="constrained")
    k = [r["budget_ms"] for r in summaries]
    left.plot(k, [r["completed_mean"] for r in summaries], ".-", color="#465771", label="实际收齐")
    left.plot(k, [r["equivalent_mean"] for r in summaries], ".-", color="#087b70", label="按已收到字节折算")
    gap = [r["extra_mean"] for r in summaries]
    right.plot(k, gap, ".-", color="#943f63", label="每批可多完成的帧数")
    right.fill_between(k, [r["extra_low95"] for r in summaries], [r["extra_high95"] for r in summaries],
                       color="#943f63", alpha=.14, label="按连接重抽样的 95% 区间")
    style(left, "4 条流同时提交", "每批完成帧数")
    style(right, "已传数据重新集中后的完成量差距", "每批额外帧数")
    left.set(ylim=(0, 4.15), yticks=range(5))
    right.set_ylim(bottom=0)
    for ax in (left, right):
        ax.set_xlabel("上传时延预算 K（ms）")
        ax.legend(loc="best", fontsize=9)
    figure(folder, "deadline-completions", fig)

    fig, ax = plt.subplots(figsize=(8.7, 4.9), layout="constrained")
    for index, color in enumerate(("#465771", "#943f63", "#087b70", "#ad702c"), 1):
        x = np.sort([r["upload_ms"] for r in streams if r["stream"] == index])
        y = 100*np.arange(1,len(x)+1)/len(x)
        ax.step(np.r_[0, x, 1.015*max(r["upload_ms"] for r in streams)], np.r_[0, y, 100],
                where="post", color=color, lw=1.6, label=f"流 {index}")
    style(ax, "同一 QUIC 连接的四条流完成时延", "累计完成比例（%）")
    ax.set_xlabel("从同时提交到服务器收齐 40 kB（ms）")
    ax.set_ylim(0, 102)
    ax.legend(loc="lower right")
    figure(folder, "per-stream-upload-cdf", fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT/"results/quic-four-stream-deadline")
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    validation = json.loads((args.data/"validation.json").read_text())
    runs = read_archive(args.data)
    if not validation["all_qlog_arrivals_match_capture"] or len(runs) != validation["runs"]:
        raise ValueError("Unverified raw packet archives")
    full, deadlines = [], []
    for run_id, run in sorted(runs.items()):
        streams, limit = stream_rows(run_id, run)
        full.extend(streams)
        deadlines.extend(limit)
    if len(full) != validation["tasks"]:
        raise ValueError("Packet validation and frame sample counts differ")
    summaries = compare_deadlines(deadlines)
    csv_write(args.data/"stream-completions.csv", full)
    csv_write(args.data/"per-batch-deadlines.csv", deadlines)
    csv_write(args.data/"deadline-summary.csv", summaries)
    by_stream = []
    for i in range(1, STREAMS+1):
        group = [r for r in full if r["stream"] == i]
        times = [r["upload_ms"] for r in group]
        by_stream.append(dict(stream=i, samples=len(group), p50_ms=quantile(times, .5),
                              p95_ms=quantile(times, .95), p99_ms=quantile(times, .99), max_ms=max(times),
                              first_pct=100*sum(r["completed_order"] == 1 for r in group)/len(group)))
    csv_write(args.data/"stream-summary.csv", by_stream)
    (args.data/"radio-check.json").write_text(json.dumps(wire_stats(runs), indent=2)+"\n", encoding="utf-8", newline="\n")
    set_chinese_font(args.font)
    plots(args.data, summaries, full)


if __name__ == "__main__":
    main()
