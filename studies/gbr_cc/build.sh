#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir "${1:?Give a new WSL directory for the reproduction}"
WORK="$(realpath "$1")"
PIN=53cb31a862ca75bd4ee4a500db4161a9b739348b
OPENSSL=3638fd45af31a5982d6c279c37afb58799201277

git init -q "$WORK/upstream"
git -C "$WORK/upstream" fetch --depth=1 https://github.com/NEMO-LAB-repo/GBR-CC.git "$PIN"
git -C "$WORK/upstream" checkout --detach FETCH_HEAD
curl -fsSL --retry 2 "https://codeload.github.com/openssl/openssl/tar.gz/$OPENSSL" -o "$WORK/openssl.tar.gz"
tar -xzf "$WORK/openssl.tar.gz" -C "$WORK"
git -C "$WORK/upstream" worktree add --detach "$WORK/dual-src" "$PIN"
for tree in upstream/sender upstream/receiver dual-src/sender; do
    ln -s "$WORK/openssl-$OPENSSL" "$WORK/$tree/submodules/openssl"
done
python3 "$HERE/patch_outer.py" "$WORK/dual-src"

build() {
    local name="$1" source="$2"
    shift 2
    cmake -G Ninja -S "$WORK/$source" -B "$WORK/build-$name" -DCMAKE_BUILD_TYPE=Release \
        -DQUIC_BUILD_PERF=ON -DQUIC_ENHANCED_PACKET_LOGGING=ON -DQUIC_TLS_LIB=openssl "$@"
    cmake --build "$WORK/build-$name" --target secnetperf -j "${JOBS:-8}"
    sha256sum "$WORK/build-$name/bin/Release/secnetperf" "$WORK/build-$name/bin/Release/libmsquic.so.2"
}

build sender upstream/sender
build receiver upstream/receiver \
    "-DCMAKE_C_FLAGS=-Wno-error=unused-variable -Wno-error=unused-but-set-variable -Wno-error=unused-parameter -Wno-error=stringop-truncation"
build dual dual-src/sender
