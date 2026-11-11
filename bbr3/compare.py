"""Build Chinese charts and comparable BBRv1/BBRv3 tables from verified outputs."""

import argparse
import csv
import gzip
import io
import json
import shutil
import sys
import tarfile
from bisect import bisect_left
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from v3.plots import COLORS, figure, plt, set_chinese_font, style
from studies.cc_bsr.analyze import summarize_d1

VERSIONS = ("BBRv1", "BBRv3")
VERSION_COLORS = (COLORS[0], COLORS[2])
ONLINE_LABELS = {"steady20": "恒定 20 Mbps", "short10_30": "在 10–30 Mbps 间切换",
                 "high28_30": "在 28–30 Mbps 间切换"}


def rows(path):
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write(path, data):
    with path.open("w", encoding="utf-8", newline="") as handle:
        fields = list(dict.fromkeys(k for row in data for k in row))
        out = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        out.writeheader()
        out.writerows(data)


def grouped_bars(ax, labels, values, title, ylabel, label_format="%.1f"):
    for number, (version, color) in enumerate(zip(VERSIONS, VERSION_COLORS)):
        x = [j + (-.19 if number == 0 else .19) for j in range(len(labels))]
        bars = ax.bar(x, values[number], width=.35, label=version, color=color)
        ax.bar_label(bars, fmt=label_format, padding=3, fontsize=8)
    ax.set_xticks(range(len(labels)), labels)
    ax.margins(y=.23)
    ax.set_axisbelow(True)
    style(ax, title, ylabel)


def read_pair(out, name, old, new):
    comparison = []
    for version, source in zip(VERSIONS, (old, new)):
        comparison.extend({"algorithm": version, **row} for row in rows(source))
    write(out / name, comparison)
    return comparison


def burst_charts(out, baseline, new):
    result = read_pair(out, "burst-comparison.csv", baseline / "task-summary.csv", new / "task-summary.csv")
    keyed = {(r["algorithm"], r["case"], r["group"]): r for r in result}
    fig, axes = plt.subplots(2, 2, figsize=(12, 8.1), layout="constrained")
    cases = [
        (["A1_random", "A1_equal20", "A1_gold"], ["短时波动", "恒定 20 Mbps", "稳定高容量"],
         "all", "on_time", "甲　40 kB 周期请求的 20 ms 完成率", "按时完成率（%）"),
        (["B1_fixed", "B1_elastic", "B1_fast"], ["固定 20 Mbps", "按需分配", "按需分配并设置发送目标"],
         "200000", "upload_p50", "乙　200 kB 请求的上传耗时", "上传中位数（ms）"),
        (["C1_reactive", "C1_oracle", "C1_80pct"], ["超时后降档", "按已知容量选档", "按约八成准确率预测选档"],
         "10", "on_time", "丙　10 Mbps 下首次提交的 20 ms 完成率", "首次按时完成率（%）"),
        (["D1_native", "D1_equal", "D1_pattern"], ["算法自行调节", "固定 21 Mbps", "分段 28 → 14 Mbps"],
         "50000", "queue20_p50", "丁　50 kB 请求头 20 ms 的排队", "队列峰值中位数（kB）"),
    ]
    for ax, (keys, labels, group, field, title, ylabel) in zip(axes.flat, cases):
        values = []
        for version in VERSIONS:
            data = [keyed[version, key, group] for key in keys]
            values.append([100*int(r[field])/int(r["tasks"]) if field == "on_time"
                           else float(r[field])/(1000 if field == "queue20_p50" else 1) for r in data])
        grouped_bars(ax, labels, values, title, ylabel)
        if field == "on_time":
            ax.set_ylim(0, 112)
            ax.set_yticks(range(0, 101, 20))
    axes[0, 0].legend(loc="upper left", ncol=2)
    figure(out, "burst-comparison", fig)

    result = []
    for version, source in zip(VERSIONS, (baseline, new)):
        cases = defaultdict(list)
        for row in json.loads((source / "runs.json").read_text()):
            if row["case"].startswith("A1load_"):
                cases[row["case"]].append(row)
        for case, records in sorted(cases.items()):
            row = dict(algorithm=version, case=case, runs=len(records))
            for field in ("capacity_mbps", "sender_ip_mbps", "receiver_ip_mbps", "overshoot_pct"):
                row[field] = median(r[field] for r in records)
            for field in ("pace", "bw", "cwnd", "queue", "mismatch"):
                for index, label in enumerate(("p05", "p50", "p95", "p99")):
                    row[f"{field}_{label}"] = median(r[field][index] for r in records)
            result.append(row)
    write(out / "a1-saturated-comparison.csv", result)


def trace_chart(out, trace):
    plan = json.loads((trace / "plan.json").read_text())
    shutil.copyfile(trace / "plan.json", out / "bbr3-native-plan.json")
    shutil.copyfile(trace.parent / "environment.json", out / "bbr3-native-environment.json")
    events = rows(trace / "bbr3-events.csv")
    if not events:
        raise ValueError("missing native BBRv3 trace")
    for row in events:
        row["offset_us"] = int(row["us"])-plan["origin"]
    with gzip.open(out / "bbr3-native-events.csv.gz", "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(events[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(events)
    samples = rows(trace / "samples.csv")
    with gzip.open(out / "bbr3-native-samples.csv.gz", "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(samples[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(samples)
    changes = [r for r in events if r["pace_bps"] != r["pace_before_bps"]]
    active = [r for r in changes if 3_000_000 <= r["offset_us"] < 6_000_000]
    counts = Counter((r["notification"], r["gain"] != r["gain_before"],
                      r["bw_bps"] != r["bw_before_bps"]) for r in active)
    write(out / "bbr3-native-change-counts.csv", [
        dict(notification=notification, gain_changed=int(gain), bandwidth_changed=int(bandwidth), count=count)
        for (notification, gain, bandwidth), count in sorted(counts.items())
    ])
    wire = rows(trace / "wire.csv")
    with (trace / "wire.csv").open("rb") as source, gzip.open(out / "bbr3-native-wire.csv.gz", "wb") as target:
        shutil.copyfileobj(source, target)
    dip = next(t for t, rate in plan["capacity"] if t >= 3_000_000 and rate == 10)
    sample_after = [r for r in samples if dip <= int(r["us"])-plan["origin"] < dip+5000]
    wire_after = [r for r in wire if dip <= int(r["us"])-plan["origin"] < dip+5000]
    prior = next(r for r in reversed(wire) if int(r["us"])-plan["origin"] < dip)
    write(out / "a1-drop-window.csv", [dict(
        start_offset_us=dip, end_offset_us=dip+5000, sample_count=len(sample_after),
        minimum_pace_bps=min(int(r["pacing_bps"]) for r in sample_after),
        maximum_pace_bps=max(int(r["pacing_bps"]) for r in sample_after),
        ingress_bytes=sum(int(r["bytes"]) for r in wire_after if r["event"] == "in"),
        forwarded_bytes=sum(int(r["bytes"]) for r in wire_after if r["event"] == "out"),
        dropped_bytes=sum(int(r["bytes"]) for r in wire_after if r["event"] == "drop"),
        queue_before_bytes=int(prior["queue"]), queue_after_bytes=int(wire_after[-1]["queue"]))])

    start, end = 3.0, 3.70
    switch_x, switch_y = zip(*[(t/1e6, r) for t, r in plan["capacity"] if (start-.25)*1e6 <= t <= end*1e6])
    filtered = [(int(r["us"])-plan["origin"], r) for r in samples
                if start*1e6 <= int(r["us"])-plan["origin"] <= end*1e6]
    times = [t/1e6 for t, _ in filtered]
    fig, (pace, window) = plt.subplots(2, 1, sharex=True, figsize=(11, 6.6), layout="constrained")
    pace.step([*switch_x, end], [*switch_y, switch_y[-1]], where="post", label="模拟空口容量",
              color="#333333", linestyle="--")
    pace.step(times, [int(r["pacing_bps"])/1e6 for _, r in filtered], where="post",
              label="BBRv3 目标发包速率", color=COLORS[2])
    pace.step(times, [int(r["btlbw_bps"])/1e6 for _, r in filtered], where="post",
              label="算法用于控制的带宽估计", color=COLORS[3], alpha=.85)
    pace.legend(ncol=3, loc="upper center")
    style(pace, "甲　模拟空口容量、目标发包速率和带宽估计", "速率（Mbps）")
    window.step(times, [int(r["cwnd"])/1000 for _, r in filtered], where="post",
                label="拥塞窗口", color=COLORS[2])
    window.step(times, [int(r["inflight"])/1000 for _, r in filtered], where="post",
                label="已发出、尚未确认的数据", color=COLORS[0], alpha=.75)
    window.legend(ncol=2)
    style(window, "乙　拥塞窗口和在途数据量", "数据量（kB）")
    for ax in (pace, window):
        ax.axvspan(dip/1e6, (dip+5000)/1e6, color="#f2d29e", alpha=.65)
        ax.set_xlim(start, end)
    window.set_xlabel("从实验计时起点开始的时间（s）")
    figure(out, "a1-bbr3-trace", fig)


def loss_chart(out, result):
    keyed = {(r["algorithm"], r["case"]): r for r in result}
    keys = ["jitter5_fifo", "jitter5_reorder", "fifo_pause80", "ack_pause80", "true_drop"]
    labels = ["抖动保序", "抖动乱序", "前向暂停", "回程暂停", "真实丢包"]
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.2), layout="constrained")
    for ax, field, title, ylabel in (
        (axes[0], "declared", "甲　发送端记录的判丢或超时事件", "事件数"),
        (axes[1], "upload_p50", "乙　服务器收齐请求的耗时", "上传中位数（ms）"),
    ):
        values = [[float(keyed[version, k][field]) for k in keys] for version in VERSIONS]
        grouped_bars(ax, labels, values, title, ylabel, "%.0f" if field == "declared" else "%.1f")
    axes[0].legend(ncol=2)
    figure(out, "loss-comparison", fig)


def ack_pause_evidence(out, baseline, new):
    records = []
    for version, source in zip(VERSIONS, (baseline, new)):
        packets = defaultdict(list)
        with gzip.open(source / "packets.csv.gz", "rt", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row["case"] == "ack_pause80" and row["direction"] == "forward" and row["measured"] == "1":
                    for stream in row["stream_ids"].split("|"):
                        packets[row["run"], stream].append(row)
        with tarfile.open(source / "raw-evidence.tar.xz") as bundle:
            sends = {}
            for member in bundle.getmembers():
                if member.name.startswith("ack_pause80__") and member.name.endswith("/client-sent.csv"):
                    with io.TextIOWrapper(bundle.extractfile(member), encoding="utf-8") as handle:
                        sends[member.name.split("/")[0]] = {r["sha256"]: r for r in csv.DictReader(handle)}
        for task in rows(source / "tasks.csv"):
            if task["case"] != "ack_pause80":
                continue
            matching = packets[task["run"], str(int(task["task"])*4)]
            first_sent = min(matching, key=lambda r: float(r["send_ms"]))
            start = float(task["start_ms"])
            upload = float(task["upload_ms"])
            first_ack = min(float(r["ack_receive_ms"]) for r in matching if r["ack_receive_ms"]) - start
            records.append(dict(algorithm=version, run=task["run"], task=task["task"],
                                upload_ms=upload, first_matching_ack_ms=round(first_ack, 3),
                                initial_cwnd_bytes=sends[task["run"]][first_sent["sha256"]]["cwnd"],
                                upload_before_first_matching_ack=int(upload < first_ack)))
    write(out / "ack-pause-requests.csv", records)


def online_chart(out, online):
    source = online / "online-requests.csv"
    requests = rows(source)
    shutil.copyfile(source, out / source.name)
    shutil.copyfile(online / "environment.json", out / "online-environment.json")
    profiles = [p for p in ONLINE_LABELS if any(r["profile"] == p for r in requests)]
    summary = []
    transport = []
    for version in VERSIONS:
        for profile in profiles:
            group = [r for r in requests if r["algorithm"] == version and r["profile"] == profile]
            if len(group) != 12 or len({r["sha256"] for r in group}) != 12:
                raise ValueError(f"unexpected online denominator in {version}/{profile}")
            work = online / f"{profile}_{version}__00"
            starts = {int(r["task"]): int(r["us"]) for r in rows(work / "client.csv") if r["event"] == "start"}
            uploads = {int(r["task"]): int(r["us"]) for r in rows(work / "server.csv")}
            samples = rows(work / "samples.csv")
            timestamps = [int(r["us"]) for r in samples]
            probe_state = 3 if version == "BBRv1" else 6
            ordered = sorted(group, key=lambda r: (int(r["video"]), int(r["question"])))
            for index, request in enumerate(ordered):
                lo, hi = (bisect_left(timestamps, t) for t in (starts[index], uploads[index]))
                during_upload = samples[lo:hi]
                transport.append(dict(algorithm=version, profile=profile, video=request["video"],
                                      question=request["question"], upload_ms=request["upload_ms"],
                                      endpoint_samples=len(during_upload),
                                      probe_rtt_samples=sum((int(r["state"]) & 255) == probe_state for r in during_upload),
                                      cwnd_min_bytes=min(int(r["cwnd"]) for r in during_upload),
                                      pacing_p50_mbps=median(int(r["pacing_bps"]) for r in during_upload)/1e6))
            metrics = ["upload_ms", "model_dispatch_ms", "model_first_text_ms",
                       "reply_first_text_ms", "first_text_ms", "completion_ms"]
            summary.append(dict(algorithm=version, profile=profile, tasks=len(group),
                                correct=sum(int(r["correct"]) for r in group),
                                **{f"{k}_p50": median(float(r[k]) for r in group) for k in metrics}))
    write(out / "online-summary.csv", summary)
    write(out / "online-transport-state.csv", transport)
    paired = []
    for profile in profiles:
        keyed_requests = {v: {r["sha256"]: r for r in requests if r["profile"] == profile
                              and r["algorithm"] == v} for v in VERSIONS}
        if set(keyed_requests["BBRv1"]) != set(keyed_requests["BBRv3"]):
            raise ValueError(f"online requests were not paired in {profile}")
        for digest, old in keyed_requests["BBRv1"].items():
            new = keyed_requests["BBRv3"][digest]
            paired.append(dict(profile=profile, video=old["video"], question=old["question"], sha256=digest,
                               **{f"{k}_v3_minus_v1": round(float(new[k])-float(old[k]), 3) for k in metrics}))
    write(out / "online-paired.csv", paired)
    keyed = {(r["algorithm"], r["profile"]): r for r in summary}
    fig, axes = plt.subplots(1, 2, figsize=(11.4, 4.4), layout="constrained")
    for ax, field, title in (
        (axes[0], "upload_ms_p50", "甲　服务器收齐四张图和题目的耗时"),
        (axes[1], "first_text_ms_p50", "乙　客户端收到模型首段可见文本的耗时"),
    ):
        values = [[keyed[v, p][field] for p in profiles] for v in VERSIONS]
        grouped_bars(ax, [ONLINE_LABELS[p] for p in profiles], values, title, "中位数（ms）")
    axes[0].legend(ncol=2)
    figure(out, "online-comparison", fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bursts", "loss", "trace", "out"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--online", type=Path)
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    set_chinese_font(args.font)
    args.out.mkdir(parents=True, exist_ok=True)
    burst_charts(args.out, ROOT / "results/v3", args.bursts)
    baseline_loss = ROOT / "results/pacing-loss"
    result = read_pair(args.out, "loss-comparison.csv", baseline_loss / "summary.csv", args.loss / "summary.csv")
    loss_chart(args.out, result)
    ack_pause_evidence(args.out, baseline_loss, args.loss)
    write(args.out / "d1-first-window.csv", [
        {"algorithm": version, **row} for version, source in zip(VERSIONS, (ROOT / "results/v3", args.bursts))
        for row in summarize_d1(source)
    ])
    trace_chart(args.out, args.trace / "A1load_slow__00")
    if args.online:
        online_chart(args.out, args.online)


if __name__ == "__main__":
    main()
