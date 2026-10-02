#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p build
${HOST_CC:-cc} -std=c11 -O2 -Wall -Wextra -Werror -o build/eeg_quality.host eeg_quality.c -lm
${ARM_CC:-arm-linux-gnueabihf-gcc} -std=c11 -O2 -Wall -Wextra -Werror \
    -marm -mcpu=cortex-a7 -mfpu=neon-vfpv4 -mfloat-abi=hard -static \
    -Wl,--build-id=sha1 -o build/eeg_quality.arm eeg_quality.c -lm
arm-linux-gnueabihf-readelf -h build/eeg_quality.arm
sha256sum build/eeg_quality.host build/eeg_quality.arm
