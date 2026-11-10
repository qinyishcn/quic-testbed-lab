"""Attribute the visible A1 pacing changes and export the native verification events."""

import argparse
import csv
import io
import json
import sys
import tarfile
from bisect import bisect_right
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyze import csv_write


START, END = 3_000_000, 6_000_000
GAINS = (.75, 1., 1.25)
RUN = "A1load_slow__00"


def gain(rate, bandwidth):
    # The exported pacing rate is an integer number of bytes per second.
    matches = [factor for factor in GAINS if abs(rate-bandwidth*factor) < 8]
    if len(matches) != 1:
        raise ValueError(f"cannot identify gain for pacing={rate}, bandwidth={bandwidth}")
    return matches[0]


def cause(gain_before, gain_after, bw_before, bw_after):
    if gain_before != gain_after:
        return "gain_and_bandwidth" if bw_before != bw_after else "gain_only"
    if bw_before == bw_after:
        raise ValueError("pacing changed without a change in gain or bandwidth")
    return "bandwidth_only"


def capacity_at(plan):
    times, rates = zip(*plan["capacity"])
    return lambda offset: rates[bisect_right(times, offset)-1]


def sampled_changes(evidence):
    with tarfile.open(evidence) as archive:
        with archive.extractfile(RUN+"/plan.json") as stream:
            plan = json.load(stream)
        with io.TextIOWrapper(archive.extractfile(RUN+"/samples.csv"), encoding="utf-8") as stream:
            samples = [{k:int(v) for k,v in row.items()} for row in csv.DictReader(stream)]
    cap = capacity_at(plan)
    for row in samples:
        if START <= row["us"]-plan["origin"] < END:
            if row["state"] != 2:
                raise ValueError("expected bandwidth probing in the A1 analysis interval")
            gain(row["pacing_bps"],row["btlbw_bps"])
    rows = []
    for before, after in zip(samples, samples[1:]):
        t = after["us"]-plan["origin"]
        if not START <= t < END or before["pacing_bps"] == after["pacing_bps"]:
            continue
        if before["state"] != 2 or after["state"] != 2:
            raise ValueError("expected bandwidth probing in the A1 analysis interval")
        before_t = before["us"]-plan["origin"]
        g0 = gain(before["pacing_bps"], before["btlbw_bps"])
        g1 = gain(after["pacing_bps"], after["btlbw_bps"])
        rows.append(dict(event=len(rows)+1, previous_sample_us=before_t, sample_us=t,
            capacity_before_mbps=cap(before_t), capacity_after_mbps=cap(t),
            pacing_before_bps=before["pacing_bps"], pacing_after_bps=after["pacing_bps"],
            bandwidth_before_bps=before["btlbw_bps"], bandwidth_after_bps=after["btlbw_bps"],
            gain_before=g0, gain_after=g1, cause=cause(g0,g1,before["btlbw_bps"],after["btlbw_bps"]),
            latest_delivery_sample_bps=after["delivery_sample_bps"],
            cwnd_before_bytes=before["cwnd"], cwnd_after_bytes=after["cwnd"],
            retransmissions_before=before["lost"], retransmissions_after=after["lost"]))
    return rows


def native_changes(raw):
    plan = json.loads((raw/"plan.json").read_text(encoding="utf-8"))
    cap = capacity_at(plan)
    with (raw/"bbr-native.csv").open(encoding="utf-8") as stream:
        log = list(csv.DictReader(stream))
    times = [int(row["us"]) for row in log]
    if not log or times != sorted(times):
        raise ValueError("missing or unsorted native pacing log")
    rows = []
    for row in log:
        t = int(row["us"])-plan["origin"]
        if not START <= t < END:
            continue
        g0,g1 = float(row["gain_before"]),float(row["gain_after"])
        bw0,bw1 = int(row["bw_before_bps"]),int(row["bw_after_bps"])
        if (row["notification"] != "ack" or row["state_before"] != "2" or row["state_after"] != "2"
            or gain(int(row["rate_before_bps"]),bw0) != g0 or gain(int(row["rate_after_bps"]),bw1) != g1):
            raise ValueError("unexpected state, notification or gain in native pacing log")
        detail = {key:value for key,value in row.items() if key!="us"}
        rows.append(dict(event=len(rows)+1, offset_us=t, capacity_mbps=cap(t),
                         cause=cause(g0,g1,bw0,bw1), **detail))
    matched = 0
    n = 0
    with (raw/"samples.csv").open(encoding="utf-8") as stream:
        for sample in csv.DictReader(stream):
            t = int(sample["us"])
            if START <= t-plan["origin"] < END:
                i = bisect_right(times,t)-1
                matched += i >= 0 and log[i]["rate_after_bps"] == sample["pacing_bps"]
                n += 1
    if not rows or not n or matched/n < .99:
        raise ValueError("native updates do not reconstruct the endpoint samples")
    return rows, dict(samples=n, samples_reconstructed=matched,
                      environment=json.loads((raw.parent/"environment.json").read_text(encoding="utf-8")))


def probe_rtt_observations(out, roots):
    runs = json.loads((out/"runs.json").read_text(encoding="utf-8"))
    rows = []
    for run in runs:
        name = f"{run['case']}__{run['repeat']:02d}"
        work = next((root/name for root in roots if (root/name/"plan.json").is_file()),None)
        if work is None:
            raise ValueError(f"formal raw trace missing for {name}")
        plan = json.loads((work/"plan.json").read_text(encoding="utf-8"))
        with (work/"samples.csv").open(encoding="utf-8") as stream:
            samples = [row for row in csv.DictReader(stream) if row["state"]=="3"]
        windows = [int(row["cwnd"]) for row in samples]
        rows.append(dict(case=run["case"], repeat=run["repeat"], probe_rtt_samples=len(samples),
            first_probe_rtt_us=int(samples[0]["us"])-plan["origin"] if samples else None,
            last_probe_rtt_us=int(samples[-1]["us"])-plan["origin"] if samples else None,
            min_cwnd_bytes=min(windows,default=None), max_cwnd_bytes=max(windows,default=None)))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--native", type=Path, help="directory of the separate instrumented run")
    parser.add_argument("--formal", type=Path, nargs="+", help="formal raw roots, replacements first")
    args = parser.parse_args()
    sampled = sampled_changes(args.out/"trace-evidence.tar.xz")
    csv_write(args.out/"A1-pacing-changes.csv",sampled)
    print("Original sampled changes",len(sampled),dict(Counter(r["cause"] for r in sampled)))
    if args.native:
        native, meta = native_changes(args.native)
        csv_write(args.out/"A1-native-pacing-updates.csv",native)
        (args.out/"A1-native-pacing-metadata.json").write_text(json.dumps(meta,indent=2)+"\n",encoding="utf-8")
        print("Native updates",len(native),dict(Counter(r["cause"] for r in native)))
        print("Endpoint samples reconstructed",meta["samples_reconstructed"],"of",meta["samples"])
    if args.formal:
        probes = probe_rtt_observations(args.out,args.formal)
        csv_write(args.out/"BBRv1-probe-rtt-observations.csv",probes)
        print("Formal runs with observed ProbeRTT",sum(r["probe_rtt_samples"]>0 for r in probes),"of",len(probes))


if __name__ == "__main__":
    main()
