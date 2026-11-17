"""Compare completed 40 kB uploads on paired random links and a scheduling reference."""

import argparse
import csv
import json
import math
import sys
import tarfile
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from analyze import csv_write
from studies.cross_layer.analyze import read, tasks_for
from studies.cross_layer.config import TAIL_BACKHAUL
from studies.cross_layer.latency_reference import reference_tasks
from v3.plots import figure, plt, set_chinese_font, style

MEASURED = ("bbr", "radio")
MODES = (*MEASURED, "ideal")
LABELS = {"bbr": "原生 BBRv3", "radio": "仅空口快环", "ideal": "理想排程"}
COLORS = {"bbr": "#465771", "radio": "#943f63", "ideal": "#087b70"}
PROFILES = {
    "random-headroom": "回传 14–26 Mbps",
    "random-tight": "回传 6–18 Mbps",
    "random-middle": "回传 8–20 Mbps，每 40 ms 更新",
    "random-tight-fast": "回传 6–18 Mbps，每 20 ms 更新",
    "random-tight-100ms": "回传 6–18 Mbps，每 100 ms 更新",
    "random-tight-200ms": "回传 6–18 Mbps，每 200 ms 更新",
    "random-wide-100ms": "回传 10–30 Mbps，每 100 ms 更新",
}
METRICS = ("upload_ms", "e2e_ms")
TAIL_MARKS = (.90, .95, .99)


def read_runs(path):
    runs = defaultdict(dict)
    with tarfile.open(path, "r|xz") as archive:
        for member in archive:
            run, _, name = member.name.partition("/")
            if name == "plan.json":
                runs[run]["plan"] = json.load(archive.extractfile(member))
            elif name in ("client.csv", "server.csv"):
                with archive.extractfile(member) as stream:
                    runs[run][name] = list(csv.DictReader(line.decode("utf-8") for line in stream))
    return runs


def collect(folder):
    paired, tasks = {}, []
    for mode in MEASURED:
        runs = read_runs(folder / mode / "raw-evidence.tar.xz")
        meta = json.loads((folder / mode / "validation.json").read_text())
        expected = meta["tasks"]
        if (not meta["all_qlog_arrivals_match_capture"] or meta["runs"] != len(runs)
                or sum(len(t["plan"]["tasks"])-1 for t in runs.values()) != expected):
            raise ValueError(f"Packet verification failed for {mode}")
        if mode != "bbr" and runs.keys() != paired.keys():
            raise ValueError("Measured controllers must use the same paired runs")
        for name, trace in sorted(runs.items()):
            plan = trace["plan"]
            common = {k: v for k, v in plan.items() if k not in ("controller", "origin")}
            if mode == "bbr":
                paired[name] = common
                if plan["case"] not in TAIL_BACKHAUL or plan["gate"] != {"probability": .1, "wait_us": 5000}:
                    raise ValueError("CDF experiment requires both links to vary with wireless retry enabled")
                for row in reference_tasks(plan):
                    tasks.append(dict(profile=plan["case"], mode="ideal", repeat=plan["repeat"], task=row["task"],
                                      start_ms=row["start_ms"], upload_ms=row["scheduling_upload_ms"],
                                      e2e_ms=row["scheduling_reference_ms"]))
            elif common != paired[name]:
                raise ValueError(f"Controllers received different plans in {name}")
            for row in tasks_for(trace, mode, name):
                tasks.append({k: row[k] for k in ("profile", "mode", "repeat", "task", "start_ms", *METRICS)})
    return tasks


def ecdf_quantile(data, p):
    index = math.ceil(p*data.shape[-1])-1
    return np.take(np.partition(data, index, axis=-1), index, axis=-1)


def summarise(tasks):
    groups = defaultdict(list)
    for row in tasks:
        groups[row["profile"], row["mode"]].append(row)
    results, uploads = [], {}
    rng = np.random.default_rng(69811)
    available = {profile for profile, _ in groups}
    for profile in (p for p in PROFILES if p in available):
        runs = sorted({row["repeat"] for row in groups[profile, "bbr"]})
        if not runs:
            raise ValueError(f"No completed requests for {profile}")
        count = len(groups[profile, "bbr"])//len(runs)
        expected = [(run, task) for run in runs for task in range(1, count+1)]
        ordered = {}
        for mode in MODES:
            rows = sorted(groups[profile, mode], key=lambda r: (r["repeat"], r["task"]))
            if [(r["repeat"], r["task"]) for r in rows] != expected:
                raise ValueError(f"Unpaired requests or missing completions in {profile}/{mode}")
            ordered[mode] = rows
        draws = rng.integers(len(runs), size=(2000, len(runs)))
        for metric in METRICS:
            values = {m: np.array([r[metric] for r in ordered[m]]).reshape(len(runs), count) for m in MODES}
            resampled = {m: v[draws].reshape(len(draws), -1) for m, v in values.items()}
            tails = {m: ecdf_quantile(v, .99) for m, v in resampled.items()}
            ideal_percentiles = {p: ecdf_quantile(values["ideal"].ravel(), p) for p in TAIL_MARKS}
            ideal_bootstrap = {p: tails["ideal"] if p == .99 else ecdf_quantile(resampled["ideal"], p)
                               for p in TAIL_MARKS}
            for mode in MODES:
                flat = values[mode].ravel()
                p99 = ecdf_quantile(flat, .99)
                lo, hi = np.quantile(tails[mode], [.025, .975])
                gap_lo, gap_hi = np.quantile(tails[mode]-tails["ideal"], [.025, .975])
                row = dict(profile=profile, metric=metric, mode=mode, runs=len(runs), tasks=flat.size,
                           p50_ms=ecdf_quantile(flat, .5), p90_ms=ecdf_quantile(flat, .9),
                           p95_ms=ecdf_quantile(flat, .95), p99_ms=p99, p99_lo_ms=lo, p99_hi_ms=hi,
                           max_ms=max(flat), p99_gap_to_ideal_ms=p99-ideal_percentiles[.99],
                           gap_lo_ms=gap_lo, gap_hi_ms=gap_hi)
                for p in TAIL_MARKS:
                    key = f"p{round(100*p)}"
                    observed = row[f"{key}_ms"]
                    bootstrap = tails[mode] if p == .99 else ecdf_quantile(resampled[mode], p)
                    pct = 100*(bootstrap-ideal_bootstrap[p])/bootstrap
                    low, high = np.quantile(pct, [.025, .975])
                    row[f"{key}_gap_to_ideal_pct"] = 100*(observed-ideal_percentiles[p])/observed
                    row[f"{key}_gap_pct_lo"] = low
                    row[f"{key}_gap_pct_hi"] = high
                results.append(row)
                if metric == "upload_ms":
                    uploads[profile, mode] = np.sort(flat)
    return results, uploads


def plot(folder, uploads, *, labels=PROFILES, title="空口与回传随机波动时，完整 40 kB 上传时延分布",
         filename="upload-latency-cdf", tail_floor=.90, marks=TAIL_MARKS):
    profiles = [(p, label) for p, label in labels.items() if (p, "bbr") in uploads]
    fig, axes = plt.subplots(len(profiles), 2, figsize=(12.8, 4*len(profiles)),
                             squeeze=False, layout="constrained")
    fig.suptitle(title, fontsize=16, fontweight="bold")
    for (profile, label), (full, tail) in zip(profiles, axes):
        data = [uploads[profile, mode] for mode in MODES]
        left = .95*min(v[0] for v in data)
        right = 1.025*max(v[-1] for v in data)
        for mode, v in zip(MODES, data):
            x = np.r_[left, v, right]
            y = np.r_[0, 100*np.arange(1, len(v)+1)/len(v), 100]
            for panel in (full, tail):
                panel.step(x, y, where="post", color=COLORS[mode], lw=1.65, label=LABELS[mode])
            points = [ecdf_quantile(v, p) for p in marks]
            tail.scatter(points, [100*p for p in marks], color=COLORS[mode], s=29, zorder=5,
                         edgecolor="white", linewidth=.35)
            if marks == (.99,):
                tail.annotate(f"{points[0]:.2f} ms", (points[0], 99), xytext=(14, -23),
                              textcoords="offset points", color=COLORS[mode], fontsize=9,
                              arrowprops=dict(arrowstyle="-", color=COLORS[mode], lw=.7, shrinkB=4))
        style(full, f"{label} · 全分布", "累计完成比例（%）")
        style(tail, f"{label} · 尾部放大", "累计完成比例（%）")
        full.set(xlim=(left, right), ylim=(0, 101))
        tail.set(xlim=(.95*min(ecdf_quantile(v, tail_floor) for v in data), right),
                 ylim=(100*tail_floor-.5, 100.15),
                 yticks=[y for y in (90, 92, 94, 95, 96, 98, 99, 100) if y >= 100*tail_floor])
        for i, p in enumerate(marks):
            tail.axhline(100*p, ls="--", lw=.75, color="#525b63",
                         label="、".join(f"{100*q:.0f}%" for q in marks) if i == 0 else None)
        for panel in (full, tail):
            panel.set_xlabel("从提交到服务器收齐 40 kB（ms）")
    handles, labels = axes[0, 1].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncol=4, frameon=False)
    figure(folder, filename, fig)


def plot_experience_rate(folder, uploads, labels, filename):
    fig, axes = plt.subplots(len(labels), 2, figsize=(12.8, 4.1*len(labels)), squeeze=False,
                             layout="constrained")
    fig.suptitle("低回传容量时，40 kB 上传的等价体验速率", fontsize=16, fontweight="bold")
    for (profile, label), (full, tail) in zip(labels.items(), axes):
        rates = {m: np.sort(320 / uploads[profile, m]) for m in MODES}
        left = .85*min(v[0] for v in rates.values())
        right = 1.05*max(v[-1] for v in rates.values())
        for mode, v in rates.items():
            x = np.r_[left, v, right]
            percent = 100*np.arange(1, len(v)+1)/len(v)
            full.step(x, np.r_[0, percent, 100], where="post", color=COLORS[mode], lw=1.65)
            tail.step(x, np.r_[100, percent[::-1], 0], where="pre",
                      color=COLORS[mode], lw=1.65, label=LABELS[mode])
            # Use the same request rank as the P99 latency to keep the two figures comparable.
            guaranteed = 320/ecdf_quantile(uploads[profile, mode], .99)
            covered = 100*np.mean(v >= guaranteed)
            tail.scatter(guaranteed, covered, color=COLORS[mode], s=29, zorder=5,
                         edgecolor="white", linewidth=.35)
            tail.annotate(f"{guaranteed:.2f} Mbps", (guaranteed, covered), xytext=(10, 9),
                          textcoords="offset points", color=COLORS[mode], fontsize=9,
                          arrowprops=dict(arrowstyle="-", color=COLORS[mode], lw=.7, shrinkB=4))
        style(full, f"{label} · 全分布", "速率不高于横轴值的请求比例（%）")
        style(tail, "低速尾部 · 99% 请求能达到的速率", "达到横轴速率的请求比例（%）")
        full.set(xlim=(left, right), ylim=(0, 101))
        tail.set(xlim=(left, 1.1*max(ecdf_quantile(v, .05) for v in rates.values())),
                 ylim=(94.5, 100.2), yticks=[95, 96, 97, 98, 99, 100])
        tail.axhline(99, ls="--", lw=.75, color="#525b63", label="99% 达标")
        for panel in (full, tail):
            panel.set_xlabel("40 kB 除以完整上传时延（Mbps）")
    handles, names = axes[0, 1].get_legend_handles_labels()
    fig.legend(handles, names, loc="outside lower center", ncol=4, frameon=False)
    figure(folder, filename, fig)


def plot_submit_backhaul_band(folder, low, high, experience_rate=False):
    runs = read_runs(folder / "bbr" / "raw-evidence.tar.xz")
    plans = {(run["plan"]["case"], run["plan"]["repeat"]): run["plan"] for run in runs.values()}
    selected = set()
    for (profile, repeat), plan in plans.items():
        times, rates = zip(*plan["backhaul"])
        for job, task in enumerate(plan["tasks"]):
            if not task["warmup"] and low <= rates[bisect_right(times, task["t"])-1] < high:
                selected.add((profile, repeat, job))
    uploads = defaultdict(list)
    cohorts = defaultdict(set)
    for row in read(folder / "tasks.csv"):
        key = (row["profile"], int(row["repeat"]), int(row["task"]))
        if key in selected:
            group = (key[0], row["mode"])
            uploads[group].append(float(row["upload_ms"]))
            cohorts[group].add(key)
    profiles = {key[0] for key in selected}
    if not profiles or any(cohorts[profile, mode] != {key for key in selected if key[0] == profile}
                           or len(uploads[profile, mode]) != len(cohorts[profile, mode])
                           for profile in profiles for mode in MODES):
        raise ValueError("No complete paired cohort in the selected backhaul band")
    band = f"{low:g}–{high:g} Mbps"
    labels = {p: f"提交时回传 {band}（{len(cohorts[p, 'radio'])} 笔）" for p in PROFILES if p in profiles}
    ordered = {key: np.sort(values) for key, values in uploads.items()}
    if experience_rate:
        plot_experience_rate(folder, ordered, labels, f"experience-rate-cdf-backhaul-{low:g}-{high:g}")
    else:
        plot(folder, ordered, labels=labels, title="低回传容量时，40 kB 上传时延分布",
             filename=f"upload-latency-cdf-backhaul-{low:g}-{high:g}", tail_floor=.95, marks=(.99,))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "results/cross-layer-random-cdf")
    parser.add_argument("--font", type=Path)
    parser.add_argument("--submit-backhaul-band", nargs=2, type=float, metavar=("LOW", "HIGH"),
                        help="plot an existing paired cohort with LOW <= backhaul at nominal submit time < HIGH")
    parser.add_argument("--experience-rate", action="store_true", help="convert the selected cohort to 40 kB upload rates")
    args = parser.parse_args()
    if args.experience_rate and not args.submit_backhaul_band:
        parser.error("--experience-rate requires --submit-backhaul-band")
    set_chinese_font(args.font)
    if args.submit_backhaul_band:
        low, high = args.submit_backhaul_band
        if not 0 <= low < high or not math.isfinite(high):
            parser.error("the backhaul band must have finite, increasing nonnegative bounds")
        plot_submit_backhaul_band(args.data, low, high, args.experience_rate)
        return
    tasks = collect(args.data)
    rows, uploads = summarise(tasks)
    csv_write(args.data / "tasks.csv", tasks)
    csv_write(args.data / "p99-summary.csv", rows)
    plot(args.data, uploads)


if __name__ == "__main__":
    main()
