"""Replot congestion-window startup from the published rate-only QUIC runs."""

import argparse
import csv
import gzip
import json
import sys
import tarfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from analyze import csv_write
from v3.plots import figure, plt, set_chinese_font, style
from matplotlib.lines import Line2D
from matplotlib.patches import Patch


VERSIONS = {"BBRv1": "bbr1", "BBRv3": "bbr3"}
FILES = {"plan.json", "samples.csv", "client-sent.csv", "client.csv", "server.csv"}
WINDOW, INFLIGHT, ACK = "#78498e", "#637389", "#187689"
STAGES = {"BBRv1": {0: "启动", 1: "排空", 2: "带宽探测", 3: "最小往返时延探测", 4: "长时延启动"},
          "BBRv3": {0: "启动", 1: "排空", 2: "探测降速", 3: "巡航", 4: "补充在途量", 5: "提速",
                    6: "最小往返时延探测", 7: "长时延启动", 8: "恢复启动"}}


def run_files(folder):
    runs = defaultdict(dict)
    with tarfile.open(folder / "raw-evidence.tar.xz", "r|xz") as archive:
        for member in archive:
            run, _, name = member.name.partition("/")
            if name not in FILES:
                continue
            content = archive.extractfile(member).read().decode("utf-8")
            runs[run][name] = json.loads(content) if name == "plan.json" else list(csv.DictReader(content.splitlines()))
    return runs


def packet_times(folder):
    first_ack, first_warmup_send = {}, {}
    with gzip.open(folder / "packets.csv.gz", "rt", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            if row["direction"] != "forward":
                continue
            stream_ids = row["stream_ids"].split("|")
            if "0" in stream_ids and int(row["stream_bytes"]) > 0:
                send_us = round(float(row["send_ms"]) * 1000)
                previous = first_warmup_send.get(row["run"])
                if previous is None or send_us < previous[0]:
                    first_warmup_send[row["run"]] = (send_us, row["sha256"])
            if not row["ack_receive_ms"]:
                continue
            time_us = round(float(row["ack_receive_ms"]) * 1000)
            for stream_id in stream_ids:
                if stream_id:
                    key = row["run"], int(stream_id) // 4
                    first_ack[key] = min(first_ack.get(key, time_us), time_us)
    return first_ack, first_warmup_send


def analyze(version, run, files, acks, first_warmup_send):
    plan = files["plan.json"]
    schedule = [(t["warmup"], t["size"]) for t in plan["tasks"]]
    if schedule != [(True, 300_000)] + [(False, 40_000)] * 6:
        raise ValueError(f"Unexpected warm-up or request schedule for {version} {run}")
    origin = plan["origin"]
    starts = {int(r["task"]): int(r["us"]) - origin for r in files["client.csv"] if r["event"] == "start"}
    ends = {int(r["task"]): int(r["us"]) - origin for r in files["server.csv"] if r["event"] == "upload"}
    if not starts.keys() == ends.keys() == set(range(7)):
        raise ValueError(f"Incomplete requests for {version} {run}")
    samples = [dict(t=int(r["us"]) - origin, cwnd=int(r["cwnd"]), inflight=int(r["inflight"]),
                    pace=int(r["pacing_bps"]), state=int(r["state"]) & 255) for r in files["samples.csv"]]
    sends = [dict(t=int(r["us"]) - origin, cwnd=int(r["cwnd"])) for r in files["client-sent.csv"]]
    warmup_send_us, digest = first_warmup_send
    send_matches = [r for r in files["client-sent.csv"] if int(r["us"]) == origin + warmup_send_us and r["sha256"] == digest]
    if len(send_matches) != 1 or not starts[0] <= warmup_send_us < acks[run, 0]:
        raise ValueError(f"Could not match the first warm-up data send for {version} {run}")
    first_data = send_matches[0]
    before = max((r for r in samples if r["t"] <= starts[0]), key=lambda r: r["t"])
    first_exit = next((r for r in samples if r["t"] >= starts[0] and r["state"] != 0), None)
    startup_end = min(ends[0]+1, first_exit["t"]) if first_exit else ends[0]+1
    peak = max((r for r in samples if starts[0] <= r["t"] < startup_end), key=lambda r: r["cwnd"])
    probe_states = {2} if version == "BBRv1" else {2, 3, 4, 5}
    first_probe = next((r for r in samples if r["t"] >= starts[0] and r["state"] in probe_states), None)
    startup = dict(version=version, run=run, case=plan["case"], initial_send_window_bytes=sends[0]["cwnd"],
                   warmup_bytes=plan["tasks"][0]["size"], warmup_start_ms=starts[0]/1000,
                   warmup_first_data_after_submit_ms=(warmup_send_us-starts[0])/1000,
                   warmup_first_data_window_bytes=int(first_data["cwnd"]),
                   warmup_first_data_udp_bytes=int(first_data["udp_bytes"]),
                   warmup_first_data_inflight_bytes=int(first_data["inflight"]),
                   warmup_first_ack_ms=(acks[run, 0]-starts[0])/1000,
                   warmup_baseline_bytes=before["cwnd"], startup_peak_bytes=peak["cwnd"],
                   startup_peak_ms=(peak["t"]-starts[0])/1000, startup_growth_ratio=round(peak["cwnd"]/before["cwnd"], 5),
                   first_non_startup_ms=(first_exit["t"]-starts[0])/1000 if first_exit else None,
                   first_non_startup_state=STAGES[version][first_exit["state"]] if first_exit else None,
                   first_non_startup_window_bytes=first_exit["cwnd"] if first_exit else None,
                   first_probe_ms=(first_probe["t"]-starts[0])/1000 if first_probe else None,
                   warmup_upload_ms=(ends[0]-starts[0])/1000, first_formal_after_warmup_ms=(starts[1]-ends[0])/1000)
    tasks = []
    for task in range(1, 7):
        start, end = starts[task], ends[task]
        before = next(r for r in reversed(samples) if r["t"] <= start)
        inside = [r for r in samples if start <= r["t"] <= end]
        tasks.append(dict(version=version, run=run, case=plan["case"], task=task,
                          first_ack_ms=(acks[run, task]-start)/1000,
                          window_before_bytes=before["cwnd"], stage_before=STAGES[version][before["state"]],
                          startup_sample_during_upload=int(any(r["state"] in ({0, 4} if version == "BBRv1" else {0, 7, 8}) for r in inside)),
                          upload_ms=(end-start)/1000))
    return startup, tasks, samples, starts, ends, sends


def plot(examples, out):
    fig, axes = plt.subplots(3, 3, figsize=(13.5, 8.9), layout="constrained",
                             width_ratios=(1.25, 1, 1))
    fig.suptitle("固定 20 Mbps 实测：拥塞窗口在连接预热时增长", fontsize=16, fontweight="bold")
    picks = (("BBRv1", "fixed20__00"), ("BBRv1", "fixed20__01"), ("BBRv3", "fixed20__00"))
    for i, (version, run) in enumerate(picks):
        info, tasks, samples, starts, ends, sends = examples[version, run]
        repeat = int(run.split("__")[1]) + 1
        views = ((0, -5, 135, "300 kB 预热全程"), (0, -1, 16, "预热开始后 16 ms"),
                 (1, -1, 19, "同连接首笔 40 kB 请求"))
        for j, (task, left, right, title) in enumerate(views):
            ax = axes[i, j]
            origin = starts[task]
            data = [((r["t"]-origin)/1000, r) for r in samples if left-3 <= (r["t"]-origin)/1000 <= right+3]
            send_rows = [((r["t"]-origin)/1000, r["cwnd"]/1000) for r in sends if left <= (r["t"]-origin)/1000 <= right]
            ax.step([t for t, _ in data], [r["cwnd"]/1000 for _, r in data], where="post", color=WINDOW, lw=1.9)
            ax.step([t for t, _ in data], [r["inflight"]/1000 for _, r in data], where="post", color=INFLIGHT, lw=1.1)
            ax.scatter(*zip(*send_rows), s=7, color=WINDOW, alpha=.43, zorder=4)
            ax.axvline(0, color="#46525c", ls=":", lw=1)
            ack = info["warmup_first_ack_ms"] if not task else tasks[0]["first_ack_ms"]
            ax.axvline(ack, color=ACK, ls="--", lw=1.1)
            if not task:
                exit_ms = info["first_non_startup_ms"]
                ax.axvspan(0, right if exit_ms is None else exit_ms, color="#dcecee", alpha=.7)
                if exit_ms is not None and info["first_probe_ms"] is not None:
                    ax.axvspan(exit_ms, info["first_probe_ms"], color="#f5e5ca", alpha=.72)
            elif tasks[0]["stage_before"] == "启动" and info["first_non_startup_ms"] is None:
                ax.axvspan(0, right, color="#dcecee", alpha=.7)
            if j == 0 or task:
                ax.axvline((ends[task]-origin)/1000, color="#46525c", ls=":", lw=1)
            ax.set(xlim=(left, right), ylim=(0, None), xlabel="距该请求提交的时间（ms）" if i == 2 else "")
            style(ax, f"{version} 第 {repeat} 轮｜{title}" if j == 0 else title, "数据量（kB）" if j == 0 else "")
    handles = [Line2D([], [], color=WINDOW, lw=2, label="拥塞窗口"),
               Line2D([], [], color=INFLIGHT, lw=1.3, label="实际在途数据"),
               Line2D([], [], color=WINDOW, ls="None", marker="o", ms=4, label="每次准备发包时的窗口"),
               Line2D([], [], color=ACK, ls="--", label="该请求首份确认到达"),
               Patch(facecolor="#dcecee", label="启动"), Patch(facecolor="#f5e5ca", label="排空")]
    fig.legend(handles=handles, loc="outside lower center", ncol=3, frameon=False)
    figure(out, "startup-window-traces", fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    set_chinese_font(args.font)
    source, out = ROOT / "results/rate-only-loss", ROOT / "results/startup-windows"
    out.mkdir(parents=True, exist_ok=True)
    windows, tasks, traces, examples = [], [], [], {}
    for version, name in VERSIONS.items():
        folder = source / name
        acks, warmup_sends = packet_times(folder)
        for run, files in sorted(run_files(folder).items()):
            result = analyze(version, run, files, acks, warmup_sends[run])
            summary, request_rows, samples, starts, _, _ = result
            windows.append(summary)
            tasks.extend(request_rows)
            if run.startswith("fixed20"):
                for task, limit in ((0, 135), (1, 19)):
                    for r in samples:
                        ms = (r["t"]-starts[task])/1000
                        if -5 <= ms <= limit:
                            traces.append(dict(version=version, run=run, task=task, time_ms=ms, cwnd_bytes=r["cwnd"],
                                               inflight_bytes=r["inflight"], pacing_bps=r["pace"], stage=STAGES[version][r["state"]]))
            if run in ("fixed20__00", "fixed20__01"):
                examples[version, run] = result
    csv_write(out / "startup-runs.csv", windows)
    csv_write(out / "formal-request-states.csv", tasks)
    csv_write(out / "fixed20-samples.csv", traces)
    plot(examples, out)


if __name__ == "__main__":
    main()
