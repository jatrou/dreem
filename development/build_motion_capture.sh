#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p build
st=../third-party/source-snapshots/lis2hh12-pid
${HOST_CC:-cc} -std=c11 -O2 -Wall -Wextra -Werror -I"$st" \
    motion_capture.c motion_sensor.c sensor_i2c.c "$st/lis2hh12_reg.c" \
    -o build/motion_capture.host
${ARM_CC:-arm-linux-gnueabihf-gcc} -std=c11 -O2 -Wall -Wextra -Werror -I"$st" \
    -marm -mcpu=cortex-a7 -mfpu=neon-vfpv4 -mfloat-abi=hard -static \
    -Wl,--build-id=sha1 motion_capture.c motion_sensor.c sensor_i2c.c \
    "$st/lis2hh12_reg.c" -o build/motion_capture.arm
arm-linux-gnueabihf-readelf -h build/motion_capture.arm
sha256sum build/motion_capture.host build/motion_capture.arm
