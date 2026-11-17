"""Split upload P90 into stages by tracing the data that completes each QUIC stream."""

import argparse
import csv
import json
import math
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from analyze import csv_write
from studies.cross_layer.analyze import read
from studies.cross_layer.latency_reference import Service, scheduled_arrivals
from studies.pacing_loss.analyze import packet_key
from v3.plots import figure, plt, set_chinese_font, style

MODES = ("bbr", "radio", "ideal")
LABELS = {"bbr": "原生 BBRv3", "radio": "仅空口快环", "ideal": "理想排程"}
STAGES = {
    "sender_wait": ("提交到决定包发出", "#526788"),
    "ingress": ("发送端到空口中继", "#b0b9c6"),
    "retry": ("空口入队前随机等待", "#b87831"),
    "radio_queue": ("空口排队", "#a64669"),
    "radio_service": ("空口发送", "#d38db1"),
    "backhaul_queue": ("回传排队", "#137868"),
    "backhaul_service": ("回传发送", "#7bb6a7"),
    "propagation": ("回传传播", "#7853a2"),
    "receiver": ("传播预算结束到收齐回调", "#b7a4c5"),
}


def read_targets(path, runs, with_wire):
    wanted = {"plan.json"}
    if with_wire:
        wanted.update(("wire.csv", "segments.csv", "client-sent.csv"))
    found = {name: {} for name in runs}
    with tarfile.open(path, "r|xz") as archive:
        for member in archive:
            run, _, name = member.name.partition("/")
            if run not in found:
                continue
            if name in wanted:
                with archive.extractfile(member) as stream:
                    found[run][name] = (json.load(stream) if name.endswith(".json")
                                        else list(csv.DictReader(line.decode("utf-8") for line in stream)))
            elif with_wire and name.startswith(("qlog-server/", "qlog-client/")) and name.endswith(".qlog"):
                found[run][name.split("/")[0]] = json.load(archive.extractfile(member))["traces"][0]
    return found


def packet_events(trace, event_name):
    if trace["configuration"]["time_units"] != "us":
        raise ValueError("Expected microsecond QLOG timestamps")
    base = int(trace["common_fields"]["reference_time"])
    for delta, _, event, data in trace["events"]:
        if event == event_name:
            yield base+delta, packet_key(data), data.get("frames", [])


def merge_interval(ranges, lo, hi):
    merged = []
    for a, b in sorted((*ranges, (lo, hi))):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def decisive_packet(trace, stream, size):
    covered, fin = [], False
    for when, packet, frames in packet_events(trace, "packet_received"):
        for f in frames:
            if f["frame_type"] != "stream" or int(f["id"]) != stream:
                continue
            lo = int(f.get("offset", 0))
            hi = lo+int(f["length"])
            if hi > size or (f.get("fin") and hi != size):
                raise ValueError("The receive trace disagrees with the 40 kB request")
            covered = merge_interval(covered, lo, hi)
            fin = fin or f.get("fin", False)
            if fin and covered == [(0, size)]:
                return when, packet, lo
    raise ValueError("The receive trace never reaches full coverage and FIN")


def measured_path(trace, job, submitted, completed):
    stream = 4*job
    seen, key, offset = decisive_packet(trace["qlog-server"], stream, trace["plan.json"]["tasks"][job]["size"])
    sent = next(t for t, pkt, _ in packet_events(trace["qlog-client"], "packet_sent") if pkt == key)
    digest, = (r["sha256"] for r in trace["client-sent.csv"] if int(r["us"]) == sent)
    wire = {r["event"]: r for r in trace["wire.csv"] if r["sha256"] == digest and r["event"] != "wait"}
    stages = {(r["segment"], r["event"]): r for r in trace["segments.csv"] if r["id"] == wire["in"]["id"]}
    air_in, bh_in = (stages[link, "in"] for link in ("radio", "backhaul"))
    air_out, bh_out = (int(stages[link, "out"]["us"]) for link in ("radio", "backhaul"))
    if int(bh_in["us"]) != air_out or not int(wire["out"]["due_us"]) <= int(wire["out"]["us"]) <= seen <= completed:
        raise ValueError("QLOG coverage, relay log and upload callback do not agree")
    bounds = (submitted, sent, int(wire["in"]["us"]), int(air_in["us"]), int(air_in["start_us"]), air_out,
              int(bh_in["start_us"]), bh_out, int(wire["out"]["due_us"]), completed)
    return offset, key[1], bounds


def trace_rows(folder):
    measured = read(folder / "tasks.csv")
    ordered = {m: sorted((r for r in measured if r["mode"] == m),
                         key=lambda r: (float(r["upload_ms"]), int(r["repeat"]), int(r["task"]))) for m in MODES}
    if len({r["profile"] for r in measured}) != 1:
        raise ValueError("Use a single profile for the P90 path comparison")
    p90 = {m: rows[math.ceil(.90*len(rows))-1] for m, rows in ordered.items()}
    paired_key = p90["radio"]["repeat"], p90["radio"]["task"]
    chosen = [("p90", m, row) for m, row in p90.items()]
    for m in ("bbr", "ideal"):
        chosen.append(("paired_to_radio_p90", m, next(r for r in ordered[m] if (r["repeat"], r["task"]) == paired_key)))

    def name(row):
        return f'{row["profile"]}__{int(row["repeat"]):02d}'

    logs = {}
    for m in ("bbr", "radio"):
        logs[m] = read_targets(folder / m / "raw-evidence.tar.xz",
                               {name(r) for _, mode, r in chosen if mode == m or (m == "bbr" and mode == "ideal")}, True)
    results = []
    for comparison, mode, r in chosen:
        job = int(r["task"])
        trace = logs["bbr" if mode == "ideal" else mode][name(r)]
        plan = trace["plan.json"]
        if mode == "ideal":
            paths = {}
            scheduled_arrivals(Service(plan["radio"]), Service(plan["backhaul"]), plan, paths)
            offset, bounds = paths[job]
            packet = None
        else:
            submitted = plan["origin"]+round(1000*float(r["start_ms"]))
            completed = submitted+round(1000*float(r["upload_ms"]))
            offset, packet, bounds = measured_path(trace, job, submitted, completed)
        parts = {k+"_ms": (b-a)/1000 for k, a, b in zip(STAGES, bounds, bounds[1:])}
        if min(parts.values()) < -0.001 or not math.isclose(sum(parts.values()), float(r["upload_ms"]), abs_tol=1e-7):
            raise ValueError(f"Path stages do not add to upload latency in {mode}/{name(r)}/{job}")
        results.append(dict(comparison=comparison, mode=mode, repeat=r["repeat"], task=job, stream=4*job,
                            packet=packet, decisive_offset=offset, upload_ms=float(r["upload_ms"]), **parts))
    return results


def plot(folder, rows):
    fig, axes = plt.subplots(2, 1, figsize=(13.2, 6.8), sharex=True, layout="constrained")
    fig.suptitle("收齐 40 kB 的决定包走了多久", fontsize=16, fontweight="bold")
    groups = ("p90", "paired_to_radio_p90")
    for i, (ax, selected) in enumerate(zip(axes, groups)):
        data = [r for r in rows if r["comparison"] == selected
                or (i and r["comparison"] == "p90" and r["mode"] == "radio")]
        data.sort(key=lambda r: MODES.index(r["mode"]))
        y = list(range(len(data)))
        left = [0.] * len(data)
        for key, (label, color) in STAGES.items():
            width = [r[key+"_ms"] for r in data]
            ax.barh(y, width, left=left, color=color, label=label)
            left = [a+b for a, b in zip(left, width)]
        for pos, t in enumerate(left):
            ax.annotate(f"{t:.2f} ms", (t, pos), xytext=(5, 0), textcoords="offset points", va="center")
        ax.set_yticks(y, [f'{LABELS[r["mode"]]} · 第 {int(r["repeat"])+1} 轮第 {r["task"]} 笔' for r in data])
        ax.invert_yaxis()
        style(ax, ("各方法各自的 P90 请求", "三种方法对照仅空口快环的同一笔请求")[i], "")
    axes[-1].set_xlim(0, 1.12*max(r["upload_ms"] for r in rows))
    axes[-1].set_xlabel("应用提交到服务器收齐（ms）")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncol=3, frameon=False)
    figure(folder, "p90-path-breakdown", fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "results/cross-layer-backhaul-10-30")
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    rows = trace_rows(args.data)
    csv_write(args.data / "p90-path-breakdown.csv", rows)
    set_chinese_font(args.font)
    plot(args.data, rows)


if __name__ == "__main__":
    main()
