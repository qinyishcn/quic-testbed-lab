#!/usr/bin/env bash
set -euo pipefail
SRC=${1:?Pass the picoquic checkout path}
OUT=${2:?Pass the output executable path}
PIN=37bb5d28aaea417649a6d4facc15f54de7d55829
test "$(git -C "$SRC" rev-parse HEAD)" = "$PIN"
cmake -S "$SRC" -B "$SRC/build" -DPICOQUIC_FETCH_PTLS=ON -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build "$SRC/build" -j "${JOBS:-6}"
PTLS="$SRC/build/_deps/picotls-build"
EXTRA=()
if [[ ${3:-} == --trace-bbr ]]; then
  TRACE_SRC="$SRC/build/bbr1_trace.c"
  python3 "$(dirname "$0")/instrument_bbr1.py" "$SRC/picoquic/bbr1.c" "$TRACE_SRC"
  EXTRA=("$TRACE_SRC")
fi
cc -std=c11 -O2 -Wall -Wextra -I "$SRC/picoquic" "$(dirname "$0")/endpoint.c" \
  "${EXTRA[@]}" \
  "$SRC/build/libpicoquic-log.a" "$SRC/build/libpicoquic-core.a" \
  "$PTLS/libpicotls-openssl.a" "$PTLS/libpicotls-minicrypto.a" "$PTLS/libpicotls-core.a" \
  -lssl -lcrypto -lpthread -lm -o "$OUT"
