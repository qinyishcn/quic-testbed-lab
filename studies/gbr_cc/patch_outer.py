"""Apply the isolated outer-loop reconstruction to a separate upstream worktree."""

import argparse
import shutil
import subprocess
from pathlib import Path

PIN = "53cb31a862ca75bd4ee4a500db4161a9b739348b"


def once(source, old, new):
    if source.count(old) != 1:
        raise ValueError(f"unexpected source anchor: {old!r}")
    return source.replace(old, new)


def patch(worktree):
    revision = subprocess.check_output(["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True).strip()
    if revision != PIN:
        raise ValueError(f"outer-loop source requires upstream {PIN}")
    core = worktree / "sender/src/core"
    path = core / "bbr.c"
    source = path.read_text()
    source = once(source, "static int g_BbrLogFileInitialized = 0;",
                  '#include "study_outer.h"\n\nstatic int g_BbrLogFileInitialized = 0;')
    old = """            if (cellularRatioFresh) {
                UpdateRatioPacingRate(cellularRatio, EstimatedBandwidth);
            }"""
    new = """            int studyOuterActive = StudyOuterUpdate(AckEvent->TimeNow, cellularRatioFresh,
                cellularRatio, TrendlineEstimatorGetSlope(&Bbr->TrendlineEstimator), SmoothedRtt, MinRtt);
            if (cellularRatioFresh && !studyOuterActive) {
                UpdateRatioPacingRate(cellularRatio, EstimatedBandwidth);
            }"""
    source = once(source, old, new)
    shutil.copyfile(Path(__file__).with_name("outer.h"), core / "study_outer.h")
    path.write_text(source)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worktree", type=Path)
    patch(parser.parse_args().worktree)
