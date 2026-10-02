#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Build a comparison kernel. This script does not install or flash anything.
set -eu
if [ "$#" -ne 4 ]; then
    echo 'Usage: build_kernel_baseline.sh NXP_SOURCE NEW_OUTPUT STOCK_CONFIG COMPILER_PREFIX' >&2
    exit 2
fi
kernel_source=$(realpath "$1")
kernel_output=$(realpath -m "$2")
stock_config=$(realpath "$3")
compiler_prefix=$4
expected_revision=30278abfe0977b1d2f065271ce1ea23c0e2d1b6e
test "$(git -C "$kernel_source" rev-parse HEAD)" = "$expected_revision"
test -z "$(git -C "$kernel_source" status --porcelain)"
if [ -e "$kernel_output" ] || [ -L "$kernel_output" ]; then
    echo 'Output must be a new directory' >&2
    exit 2
fi
mkdir -m 700 "$kernel_output"
cp "$stock_config" "$kernel_output/.config"
"${compiler_prefix}gcc" --version > "$kernel_output/compiler-version.txt"
# Old dtc has tentative duplicate yylloc definitions; -fcommon restores its
# historical host-compiler behavior without changing the NXP source tree.
make -C "$kernel_source" O="$kernel_output" ARCH=arm \
    CROSS_COMPILE="$compiler_prefix" HOSTCFLAGS='-O2 -fcommon' olddefconfig
make -C "$kernel_source" O="$kernel_output" ARCH=arm \
    CROSS_COMPILE="$compiler_prefix" HOSTCFLAGS='-O2 -fcommon' -j8 vmlinux
sha256sum "$stock_config" "$kernel_output/.config" "$kernel_output/vmlinux"
