"""Archive all burst rounds with public plans and no local assets or keys."""

import argparse
import io
import json
import tarfile
from pathlib import Path


def archive(raw, output):
    environment = json.loads((raw / "environment.json").read_text())
    expected = {f"{case}__{repeat:02d}" for case in environment["cases"]
                for repeat in range(environment["repeats"])}
    runs = {p.parent.name: p.parent for p in raw.glob("*/plan.json")}
    if set(runs) != expected:
        raise ValueError("raw directories do not match the burst experiment manifest")
    with tarfile.open(output, "w:xz", preset=3) as bundle:
        for name in ("environment.json", "profiles.yaml", "traffic.sqlite"):
            bundle.add(raw / name, arcname=name, recursive=False)
        for name, work in sorted(runs.items()):
            plan = json.loads((work / "plan.json").read_text())
            for task in plan["tasks"]:
                task["file"] = Path(task["file"]).name
            data = json.dumps(plan, separators=(",", ":")).encode()
            header = tarfile.TarInfo(f"{name}/plan.json")
            header.size = len(data)
            bundle.addfile(header, io.BytesIO(data))
            files = [work / file for file in ("client.csv", "server.csv", "samples.csv", "wire.csv")]
            for role in ("client", "server"):
                files.extend(sorted((work / f"qlog-{role}").glob("*.qlog")))
            for file in files:
                bundle.add(file, arcname=file.relative_to(raw).as_posix(), recursive=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    archive(args.raw, args.output)


if __name__ == "__main__":
    main()
