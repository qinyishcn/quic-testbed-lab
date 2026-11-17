"""Verify causal feedback and measure both queues and each 40 kB request."""

import argparse
import csv
import gzip
import json
import random
import sys
import tarfile
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from analyze import csv_write, quantile
from studies.cross_layer.config import MODES, PROFILES, REVERSE_STEP, SLOW, START

TABLES = ("segments.csv", "wire.csv", "applied.csv", "feedback.csv",
          "client.csv", "server.csv", "samples.csv")
CONTROLS = {"bbr": (), "outer": (1,), "radio": (0,), "dual": (0, 1)}
NAMES = {"bbr": "原生 BBRv3", "outer": "仅慢环", "radio": "仅空口环", "dual": "双环"}
LABELS = {"steady": "恒速", "radio": "空口快变", "backhaul": "回传慢变",
          "mixed": "快慢叠加·过载", "mixed-clean": "过载·关闭重试等待",
          "mixed-feasible": "快慢叠加·可承载", "ack": "仅回程时延增加"}
LINK_STATS = ("retry_ms_p95", "radio_wait_ms_p95", "backhaul_wait_ms_p95",
              "active_pace_median_mbps", "air_pace_mae_mbps", "sender_unsent_p95_kB",
              "packet_ack_ms_p50", "packet_ack_ms_p95")


def read(path):
    with (gzip.open if path.suffix == ".gz" else open)(path, "rt", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def archives(folder):
    runs = defaultdict(dict)
    with tarfile.open(folder / "raw-evidence.tar.xz", "r|xz") as archive:
        for member in archive:
            run, _, name = member.name.partition("/")
            if name == "plan.json":
                runs[run]["plan"] = json.load(archive.extractfile(member))
            elif name in TABLES:
                with archive.extractfile(member) as f:
                    runs[run][name] = list(csv.DictReader(line.decode("utf-8") for line in f))
            elif name.startswith("qlog-client/") and name.endswith(".qlog"):
                trace = json.load(archive.extractfile(member))["traces"][0]["events"]
                blocked = sum("blocked" in f["frame_type"]
                              for _, _, event, d in trace if event == "packet_sent" for f in d.get("frames", []))
                credit = next(d for _, _, event, d in trace if event == "parameters_set"
                              and d.get("owner") == "remote" and "initial_max_data" in d)
                if (blocked or credit["initial_max_data"] != 8*1024*1024
                        or credit["initial_max_stream_data_bidi_remote"] != 512*1024):
                    raise ValueError(f"Unexpected flow-control limit in {run}")
    return runs


def stage_records(raw):
    plan = raw["plan"]
    reverse_rng = random.Random(plan["reverse_delay"]["seed"])
    entered, released, waits = {}, {}, Counter()
    for r in raw["wire.csv"]:
        ident, event, now = r["id"], r["event"], int(r["us"])
        if event == "in":
            entered[ident] = r
            if r["direction"] == "reverse":
                delay = reverse_rng.randint(10000, 20000)
                step = plan["reverse_step"]
                if step and step[0] <= now-plan["origin"] < step[1]:
                    delay += step[2]
                if int(r["due_us"]) != now+delay:
                    raise ValueError("Reverse jitter differs from the configured draw")
        elif event == "wait" and plan["gate"] and int(r["due_us"])-now == 5000:
            waits[ident] += 1
        elif event == "out" and now >= int(r["due_us"]):
            released[ident] = r
        else:
            raise ValueError("Invalid relay event")
    if entered.keys() != released.keys() or any(entered[i]["sha256"] != released[i]["sha256"] for i in entered):
        raise ValueError("Packets were lost or altered by the relay")
    stages = {k: {} for k in ("radio", "backhaul")}
    exits = {k: {} for k in stages}
    for r in raw["segments.csv"]:
        stage, i = r["segment"], r["id"]
        if r["event"] == "in":
            stages[stage][i] = r
        else:
            exits[stage][i] = int(r["us"])
    forward = {i for i, row in entered.items() if row["direction"] == "forward"}
    if any(forward != d.keys() for d in (*stages.values(), *exits.values())):
        raise ValueError("Packet does not traverse both queues")
    result = []
    for i in forward:
        a, b = stages["radio"][i], stages["backhaul"][i]
        t0, t1, t2 = int(entered[i]["us"]), int(a["us"]), int(b["us"])
        sa, sb = int(a["start_us"]), int(b["start_us"])
        if (t1-t0 < 5000*waits[i] or not t1 <= sa <= exits["radio"][i] == t2 <= sb <= exits["backhaul"][i]
                or int(released[i]["us"]) < exits["backhaul"][i]+4000):
            raise ValueError("Invalid chronology across forwarding stages")
        result.append((t0-plan["origin"], (t1-t0)/1000, (sa-t1)/1000, (sb-t2)/1000))
    return result


def feedback_records(raw, mode):
    received = raw.get("applied.csv", [])
    if not CONTROLS[mode]:
        if received or raw["feedback.csv"]:
            raise ValueError("Native BBR received a side channel")
        return [0, 0]
    sent = {(r["kind"], r["sample_us"]): r for r in raw["feedback.csv"] if r["delivered"] == "1"}
    rates = [2500000, 3750000]
    counts = Counter()
    for r in received:
        kind = int(r["kind"])
        mark = sent[r["kind"], r["source_us"]]
        rates[kind] = int(mark["cap_Bps"])
        effective = min(rates[0] if 0 in CONTROLS[mode] else 2500000,
                        rates[1] if 1 in CONTROLS[mode] else 3750000)
        delay = int(mark["ready_us"])-int(mark["sample_us"])
        if (delay != (1000 if kind == 0 else 20000) or int(mark["sent_us"]) < int(mark["ready_us"])
                or int(r["reported_Bps"]) != rates[kind] or int(r["effective_Bps"]) != effective):
            raise ValueError("Endpoint pacing does not match causal control input")
        counts[kind] += 1
    if set(counts) != set(CONTROLS[mode]):
        raise ValueError("A control input was never consumed")
    return [counts[0], counts[1]]


def tasks_for(raw, mode, name):
    plan = raw["plan"]
    origin = plan["origin"]
    def event_times(file, event):
        return {r["task"]:int(r["us"]) for r in raw[file] if r["event"] == event and r["task"] != "0"}
    start = event_times("client.csv", "start")
    upload = event_times("server.csv", "upload")
    token = event_times("client.csv", "token")
    if not start.keys() == upload.keys() == token.keys() or len(start) != len(plan["tasks"])-1:
        raise ValueError(f"Unfinished or missing task {mode}/{name}")
    a, b = REVERSE_STEP[:2] if plan["case"] == "ack" else SLOW
    result = []
    for job, t in start.items():
        if not t <= upload[job] <= token[job]:
            raise ValueError("Task timestamps overlap incorrectly")
        phase = "pre" if t-origin < a else "stress" if t-origin < b else "post"
        result.append(dict(profile=plan["case"], mode=mode, run=name, repeat=plan["repeat"], task=int(job),
                           start_ms=(t-origin)/1000, phase=phase, upload_ms=(upload[job]-t)/1000,
                           reply_ms=(token[job]-upload[job])/1000, e2e_ms=(token[job]-t)/1000))
    return result


def pacing_state(raw, tasks, packets):
    origin = raw["plan"]["origin"]
    changes = defaultdict(int)
    for r in tasks:
        changes[round(1000*r["start_ms"])] += 40_000
    for r in packets:
        changes[round(1000*float(r["send_ms"]))] -= int(r["stream_bytes"])-int(r["repeated_stream_bytes"])
    events = iter(sorted(changes.items()))
    t, delta = next(events)
    backlog = 0
    samples = []
    for s in raw["samples.csv"]:
        sample = int(s["us"])-origin
        if sample < START:
            continue
        while t <= sample:
            backlog += delta
            if backlog < 0:
                raise ValueError("Send log moved ahead of submitted bytes")
            t, delta = next(events, (float("inf"), 0))
        samples.append((sample, int(s["pacing_bps"])/1e6, int(s["cwnd"]), int(s["inflight"]), backlog))
    if t != float("inf") or backlog:
        raise ValueError("Sender did not transmit all requested byte ranges")
    return samples


def capacity(schedule, t):
    return schedule[bisect_right(schedule, [t, float("inf")])-1][1]


def task_metrics(out, tasks):
    out["tasks"] = len(tasks)
    for name in ("upload_ms", "e2e_ms", "reply_ms"):
        for p in (.5, .95, .99):
            out[f"{name}_p{int(p*100)}"] = quantile([t[name] for t in tasks], p)
    for bound in (60, 80, 120):
        out[f"within_{bound}ms_pct"] = 100*sum(t["e2e_ms"] <= bound for t in tasks)/len(tasks)
    return out


def summary_row(key, tasks, segments, states, confirmations, plan):
    out = task_metrics(dict(key), tasks)
    for index, name in enumerate(LINK_STATS[:3], 1):
        out[name] = quantile([r[index] for r in segments], .95)
    active = [s for s in states if s[-1] > 0]
    out["active_samples"] = len(active)
    out["active_pace_median_mbps"] = quantile([s[1] for s in active], .5)
    out["air_pace_mae_mbps"] = mean(abs(s[1]-capacity(plan["radio"], s[0])) for s in active) if active else None
    out["sender_unsent_p95_kB"] = quantile([s[-1]/1000 for s in states], .95)
    out["packet_ack_ms_p50"] = quantile(confirmations, .5)
    out["packet_ack_ms_p95"] = quantile(confirmations, .95)
    return out


def series(raw, states, profile, mode):
    origin = raw["plan"]["origin"]
    queues = defaultdict(list)
    for r in raw["segments.csv"]:
        t = int(r["us"])-origin
        queues[r["segment"]].append((t, int(r["queue_bytes"])/1000))
    sample_index = [s[0] for s in states]
    queue_index = {k: [r[0] for r in v] for k, v in queues.items()}
    for t in range(START, 3_350_000, 2000):
        at = max(0, bisect_right(sample_index, t)-1)
        _, rate, cwnd, inflight, pending = states[at]
        depth = {k: v[max(0, bisect_right(queue_index[k], t)-1)][1] for k, v in queues.items()}
        yield dict(profile=profile, mode=mode, repeat=0, ms=t/1000, pacing_mbps=rate,
                   cwnd_kB=cwnd/1000, inflight_kB=inflight/1000, pending_kB=pending/1000,
                   radio_kB=depth["radio"], backhaul_kB=depth["backhaul"],
                   radio_mbps=capacity(raw["plan"]["radio"], t), backhaul_mbps=capacity(raw["plan"]["backhaul"], t))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "results/cross-layer-loops")
    args = parser.parse_args()
    folder = args.data
    all_tasks, metrics, curves = [], [], []
    paired = {}
    for mode in MODES:
        raw = archives(folder / mode)
        by_run, losses = defaultdict(list), defaultdict(Counter)
        for p in read(folder / mode / "packets.csv.gz"):
            if p["direction"] == "forward" and p["measured"] == "1":
                by_run[p["run"]].append(p)
        for p in read(folder / mode / "loss-events.csv"):
            if p["direction"] == "forward" and p["measured"] == "1":
                losses[p["run"]][p["loss_reason"]] += 1
        for name, trace in sorted(raw.items()):
            plan = trace["plan"]
            profile = plan["case"]
            inputs = tuple(plan[k] for k in ("radio", "backhaul", "reverse_delay", "reverse_step", "tasks", "gate", "seed"))
            if inputs != paired.setdefault(name, inputs):
                raise ValueError("Controller groups use different exogenous profiles")
            trial_tasks = tasks_for(trace, mode, name)
            all_tasks.extend(trial_tasks)
            segments, states = stage_records(trace), pacing_state(trace, trial_tasks, by_run[name])
            confirmations = [(1000*float(p["send_ms"]), float(p["confirmation_ms"])) for p in by_run[name]
                             if p["confirmation_ms"]]
            fast, slow = feedback_records(trace, mode)
            if plan["repeat"] == 0:
                curves.extend(series(trace, states, profile, mode))
            a, b = REVERSE_STEP[:2] if profile == "ack" else SLOW
            phases = {"pre": (START, a), "stress": (a, b), "post": (b, 3_210_000)}
            for phase, (lo, hi) in phases.items():
                key = dict(profile=profile, mode=mode, run=name, repeat=plan["repeat"], phase=phase)
                metrics.append(summary_row(key, [t for t in trial_tasks if lo <= 1000*t["start_ms"] < hi],
                                           [s for s in segments if lo <= s[0] < hi],
                                           [s for s in states if lo <= s[0] < hi],
                                           [delay for t, delay in confirmations if lo <= t < hi], plan))
            metrics.append(dict(summary_row(dict(profile=profile, mode=mode, run=name, repeat=plan["repeat"], phase="all"),
                                             trial_tasks, [s for s in segments if s[0] >= START], states,
                                             [delay for _, delay in confirmations], plan),
                                packet_threshold=losses[name]["packet_number"], time_threshold=losses[name]["rack_time"],
                                pto=losses[name]["timer"], radio_feedback_applied=fast, slow_feedback_applied=slow))
    aggregates = []
    for profile in PROFILES:
        for mode in MODES:
            for phase in ("all", "pre", "stress", "post"):
                group = [r for r in metrics if (r["profile"], r["mode"], r["phase"]) == (profile, mode, phase)]
                tasks = [t for t in all_tasks if t["profile"] == profile and t["mode"] == mode
                         and (phase == "all" or t["phase"] == phase)]
                row = task_metrics(dict(profile=profile, mode=mode, phase=phase, runs=len(group)), tasks)
                for stat in LINK_STATS:
                    row[stat] = median(r[stat] for r in group if r[stat] is not None)
                aggregates.append(row)
    csv_write(folder / "summary.csv", aggregates)
    csv_write(folder / "runs.csv", metrics)
    csv_write(folder / "tasks.csv", all_tasks)
    with gzip.open(folder / "timeline.csv.gz", "wt", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(curves[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(curves)


if __name__ == "__main__":
    main()
