"""Build an isolated picoquic endpoint for the two-link ablation."""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bbr3.build import select_bbr3
from studies.pacing_loss.build import PIN, endpoint_source, loss_source, replace_once


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("picoquic", type=Path)
    parser.add_argument("libraries", type=Path, help="CMake build directory for the pinned source")
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    p, libs, out = (x.resolve() for x in (args.picoquic, args.libraries, args.out))
    if subprocess.check_output(["git", "-C", str(p), "rev-parse", "HEAD"], text=True).strip() != PIN:
        raise ValueError("picoquic commit differs from the BBRv3 baseline")
    out.mkdir(parents=True)
    code = endpoint_source(select_bbr3((ROOT / "v3/endpoint.c").read_text()))
    code = replace_once(code, "static int client, failed;",
                        'static int client, failed;\n#include "control.h"')
    code = replace_once(code, "    picoquic_set_default_congestion_algorithm(quic, picoquic_bbr_algorithm);",
                        "    if (client && setup_feedback()) return 2;\n"
                        "    picoquic_set_default_congestion_algorithm(quic,\n"
                        "        feedback_fd >= 0 ? &study_cc : picoquic_bbr_algorithm);")
    code = replace_once(code, "        if (client && now >= sample_due) {",
                        "        if (client) apply_feedback(path, now);\n"
                        "        if (client && now >= sample_due) {")
    code = replace_once(code, "    fclose(sent);", "    if (applied) fclose(applied);\n    fclose(sent);")
    endpoint, loss = out / "endpoint.c", out / "loss.c"
    endpoint.write_text(code)
    loss.write_text(loss_source((p / "picoquic/loss_recovery.c").read_text()))
    tls = libs / "_deps/picotls-build"
    subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-I", str(p / "picoquic"),
                    "-I", str(Path(__file__).resolve().parent), str(endpoint), str(loss),
                    str(libs / "libpicoquic-log.a"), str(libs / "libpicoquic-core.a"),
                    *[str(tls / f"libpicotls-{target}.a") for target in ("openssl", "minicrypto", "core")],
                    "-lssl", "-lcrypto", "-lpthread", "-lm", "-o", str(out / "endpoint")], check=True)


if __name__ == "__main__":
    main()
