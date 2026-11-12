"""Reduce native MsQuic ACK logs and Testbed queue/grant traces."""

import argparse
import bisect
import csv
import gzip
import json
import math
import tarfile
from collections import defaultdict
from pathlib import Path
from statistics import median

FIELDS = ("ack_us", "packet", "packet_bytes", "bandwidth_Bps", "delivery_Bps", "cwnd_bytes",
          "inflight_bytes", "srtt_us", "min_rtt_us", "state", "marker", "ratio_milli",
          "ratio_duplicate", "gain_milli", "ratio_pace_Bps", "ratio_cwnd_bytes",
          "sample_rtt_us", "raw_ratio_milli", "seq", "fresh", "base_rtt_us")


def read(path):
    with path.open(encoding="utf-8", newline="") as file:
        yield from csv.DictReader(file)


def write(path, records):
    open_file = gzip.open if path.suffix == ".gz" else open
    with open_file(path, "wt", encoding="utf-8", newline="") as file:
        columns = list(dict.fromkeys(key for row in records for key in row))
        writer = csv.DictWriter(file, columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(records)


def percentile(values, p):
    values = sorted(values)
    point = (len(values)-1)*p
    lo, hi = math.floor(point), math.ceil(point)
    return values[lo]+(point-lo)*(values[hi]-values[lo])


def native(path):
    previous = None
    with path.open(encoding="utf-8") as file:
        for line in file:
            values = line.rstrip().split(",")
            if len(values) != len(FIELDS) or values[10] != "739":
                continue
            row = dict(zip(FIELDS, map(int, values)))
            if previous is not None and row["ack_us"] < previous:
                raise ValueError(f"native ACK time went backwards: {path}")
            if row["ack_us"] != previous:
                yield row
            previous = row["ack_us"]


def artifact_gain(ratio):
    def sigmoid(x):
        return 1/(1+math.exp(-x))

    if ratio <= .2:
        return .99+.01*(sigmoid(25*ratio)-.5)/(sigmoid(5)-.5)
    return 1+.01*(sigmoid(25*(ratio-.4))-sigmoid(-5))/(sigmoid(40)-sigmoid(-5))


def signal_validation(ack, grants, wire, origin):
    sequences = {0: (1., 1.)}
    smoothed = 1.
    for row in grants:
        if row["socket_sent"] == "1":
            raw = float(row["ratio"])
            smoothed = .6*min(1., raw)+.4*smoothed
            sequences[len(sequences)] = (raw, smoothed)
    error, gain_error, window_error, rate_error, sequences_used = [], [], [], [], set()
    for previous, row in zip(ack, ack[1:]):
        _, smooth = sequences[row["seq"]]
        error.append(abs(row["ratio_milli"]/1000-smooth))
        gain_error.append(abs(row["gain_milli"]/1000-artifact_gain(smooth)))
        if row["ratio_pace_Bps"] and row["base_rtt_us"]:
            expected = 2*row["ratio_pace_Bps"]*min(row["base_rtt_us"], 500000)/1e6
            window_error.append(abs(row["ratio_cwnd_bytes"]-int(expected)))
        if row["fresh"]:
            sequences_used.add(row["seq"])
            if previous["ratio_pace_Bps"]:
                expected = min(100_000_000, max(100_000, previous["ratio_pace_Bps"]*artifact_gain(smooth)))
                rate_error.append(abs(row["ratio_pace_Bps"]-expected))
    gap_start, gap_end = 2_080_000, 2_270_000
    gap_ack = [r for r in ack if gap_start <= r["offset_us"] < gap_end]
    gap_wire = [int(r["ip_bytes"]) for r in read(wire)
                if r["stage"] == "radio" and r["event"] == "in"
                and gap_start <= int(r["us"])-origin < gap_end]
    return dict(sent=len(sequences)-1, consumed=len(sequences_used), last_seen=ack[-1]["seq"],
                smoothing_error_p99=percentile(error, .99), gain_error_p99=percentile(gain_error, .99),
                pace_step_error_p99_Bps=percentile(rate_error, .99), window_error_max_bytes=max(window_error),
                gap_start_us=gap_start, gap_end_us=gap_end, gap_ack_samples=len(gap_ack),
                gap_sequences=sorted({r["seq"] for r in gap_ack}),
                gap_pacing_targets_Bps=sorted({r["ratio_pace_Bps"] for r in gap_ack}),
                gap_radio_ip_packets=len(gap_wire), gap_radio_ip_bytes=sum(gap_wire))


def measure(work, suite):
    plan = json.loads((work / "plan.json").read_text())
    result = json.loads((work / "result.json").read_text())
    grants = list(read(work / "grants.csv"))
    ack = list(native(work / "native.log"))
    if len(ack) < 10 or max(row["seq"] for row in ack) <= 0:
        raise ValueError(f"missing observed telemetry: {work}")
    origin = plan["origin_us"]
    times, backlogs = defaultdict(list), defaultdict(list)
    delivered, slips, drops = defaultdict(int), [], defaultdict(int)
    first_wire = None
    for row in read(work / "wire.csv"):
        stage, event = row["stage"], row["event"]
        timestamp = int(row["us"])
        offset = timestamp-origin
        if stage == "radio" and event == "in" and first_wire is None:
            first_wire = timestamp
        times[stage].append(offset)
        backlogs[stage].append(int(row["queue_bytes"]))
        if event == "out":
            slips.append(max(0, timestamp-int(row["due_us"])))
            if stage == "backhaul":
                delivered[offset//50000] += int(row["ip_bytes"])
        elif event == "drop":
            drops[stage] += 1
    if any(values != sorted(values) for values in times.values()) or first_wire is None:
        raise ValueError(f"invalid wire clock: {work}")

    def backlog(stage, offset):
        index = bisect.bisect_right(times[stage], offset)-1
        return backlogs[stage][index] if index >= 0 else 0

    # The first radio ingress is a proxy for connection start. Broad phase
    # windows leave 200 ms of headroom at both ends.
    anchor = first_wire-origin
    for row in ack:
        row["offset_us"] = anchor+row["ack_us"]
    first_ack = [r for r in ack if r["srtt_us"]]
    phase = [r for r in first_ack if 2_200_000 <= r["offset_us"] < 5_800_000]
    outer_path = work / "outer.csv"
    outer = list(read(outer_path)) if outer_path.exists() else []
    window = range(2_200_000, 5_800_000, 5000)
    observed = dict(suite=suite, profile=plan["profile"], mode=plan["mode"], repeat=plan["repeat"],
        **result, ack_samples=len(ack), active_ack_samples=len(phase),
        native_fresh_updates=sum(r["fresh"] for r in ack),
        rtt_p50_ms=median(r["srtt_us"] for r in first_ack)/1000,
        rtt_p95_ms=percentile((r["srtt_us"] for r in first_ack), .95)/1000,
        phase_rtt_p50_ms=median(r["srtt_us"] for r in phase)/1000 if phase else None,
        phase_rtt_p95_ms=percentile((r["srtt_us"] for r in phase), .95)/1000 if phase else None,
        phase_radio_queue_p95_kB=percentile((backlog("radio", t) for t in window), .95)/1000,
        phase_backhaul_queue_p95_kB=percentile((backlog("backhaul", t) for t in window), .95)/1000,
        phase_radio_ratio_p50=median(r["ratio_milli"] for r in phase)/1000 if phase else None,
        phase_ip_mbps=sum(delivered[i] for i in range(44, 116))*8/3_600_000,
        radio_drops=drops["radio"], backhaul_drops=drops["backhaul"],
        service_slip_p99_ms=percentile(slips, .99)/1000,
        outer_entries=sum(r["action"] == "enter" for r in outer),
        outer_cuts=sum(r["action"] != "exit" for r in outer))

    timeline = []
    offsets = [r["offset_us"] for r in ack]
    for offset in range(0, min(9_000_000, max(times["backhaul"])), 50000):
        index = bisect.bisect_right(offsets, offset)-1
        point = ack[index] if index >= 0 else None
        timeline.append(dict(suite=suite, profile=plan["profile"], mode=plan["mode"], repeat=plan["repeat"],
            offset_ms=offset/1000, delivered_ip_mbps=delivered[offset//50000]*8/50000,
            radio_queue_kB=backlog("radio", offset)/1000, backhaul_queue_kB=backlog("backhaul", offset)/1000,
            rtt_ms=point["srtt_us"]/1000 if point else None,
            ratio=point["ratio_milli"]/1000 if point else None))
    for row in grants:
        row["offset_ms"] = (int(row["sent_us"])-origin)/1000
    for row in outer:
        row.update(suite=suite, profile=plan["profile"], repeat=plan["repeat"],
                   offset_ms=(int(row["monotonic_us"])-origin)/1000)
    return observed, timeline, ack, grants, outer


def archive(output, examples):
    with tarfile.open(output, "w:xz", preset=3) as tar:
        for suite, work in examples:
            for name in ("plan.json", "result.json", "wire.csv", "grants.csv", "native.log",
                         "sender.log", "receiver.log", "outer.csv"):
                path = work / name
                if path.is_file():
                    tar.add(path, arcname=f"{suite}/{work.name}/{name}", recursive=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formal", type=Path, required=True)
    parser.add_argument("--outer", type=Path)
    parser.add_argument("--signal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    all_runs, timelines, outer_actions, examples = [], [], [], []
    for suite in ("formal", "outer", "signal"):
        source = getattr(args, suite)
        if source is None:
            continue
        environment = json.loads((source / "environment.json").read_text())
        expected = {f"{p}--{m}--{r:02d}" for p in environment["profiles"]
                    for m in environment["modes"] for r in range(environment["repeats"])}
        paths = {p.parent.name: p.parent for p in source.glob("*/result.json")}
        if paths.keys() != expected:
            raise ValueError(f"incomplete run set in {suite}")
        (args.output / f"{suite}-environment.json").write_text(json.dumps(environment, indent=2)+"\n")
        for name, work in sorted(paths.items()):
            row, curve, ack, grants, outer = measure(work, suite)
            all_runs.append(row)
            timelines.extend(curve)
            outer_actions.extend(outer)
            if name.endswith("--00"):
                examples.append((suite, work))
            if suite == "signal" and row["mode"] == "gbr":
                write(args.output / "signal-ack.csv.gz", ack)
                write(args.output / "signal-grants.csv", grants)
                origin = json.loads((work / "plan.json").read_text())["origin_us"]
                result = signal_validation(ack, grants, work / "wire.csv", origin)
                (args.output / "signal-validation.json").write_text(json.dumps(result, indent=2)+"\n")
    groups = defaultdict(list)
    for row in all_runs:
        groups[row["suite"], row["profile"], row["mode"]].append(row)
    summary = []
    for (suite, profile, mode), group in sorted(groups.items()):
        numeric = (key for key in group[0] if key not in ("suite", "profile", "mode", "repeat"))
        medians = {key: median(v) for key in numeric if (v := [r[key] for r in group if r[key] is not None])}
        summary.append(dict(suite=suite, profile=profile, mode=mode, rounds=len(group), **medians))
    write(args.output / "rounds.csv", all_runs)
    write(args.output / "summary.csv", summary)
    write(args.output / "timeline.csv.gz", timelines)
    if outer_actions:
        write(args.output / "outer-actions.csv", outer_actions)
    archive(args.output / "representative-traces.tar.xz", examples)
    print(f"verified {len(all_runs)} completed uploads")


if __name__ == "__main__":
    main()
