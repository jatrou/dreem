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
${HOST_CC:-cc} -std=c11 -O2 -Wall -Wextra -Werror -o build/motion_quality.host motion_quality.c -lm
${ARM_CC:-arm-linux-gnueabihf-gcc} -std=c11 -O2 -Wall -Wextra -Werror \
    -marm -mcpu=cortex-a7 -mfpu=neon-vfpv4 -mfloat-abi=hard -static \
    -Wl,--build-id=sha1 -o build/motion_quality.arm motion_quality.c -lm
arm-linux-gnueabihf-readelf -h build/motion_quality.arm
sha256sum build/motion_quality.host build/motion_quality.arm
${HOST_CC:-cc} -std=c11 -O2 -Wall -Wextra -Werror \
    -o build/algo_health.host algo_health.c algo_events.c
${ARM_CC:-arm-linux-gnueabihf-gcc} -std=c11 -O2 -Wall -Wextra -Werror \
    -marm -mcpu=cortex-a7 -mfpu=neon-vfpv4 -mfloat-abi=hard -static \
    -Wl,--build-id=sha1 -o build/algo_health.arm algo_health.c algo_events.c
arm-linux-gnueabihf-readelf -h build/algo_health.arm
sha256sum build/algo_health.host build/algo_health.arm
${HOST_CC:-cc} -std=c11 -O2 -Wall -Wextra -Werror \
    -o build/session_motion.host session_motion.c algo_events.c -lm
${ARM_CC:-arm-linux-gnueabihf-gcc} -std=c11 -O2 -Wall -Wextra -Werror \
    -marm -mcpu=cortex-a7 -mfpu=neon-vfpv4 -mfloat-abi=hard -static \
    -Wl,--build-id=sha1 -o build/session_motion.arm session_motion.c algo_events.c -lm
arm-linux-gnueabihf-readelf -h build/session_motion.arm
sha256sum build/session_motion.host build/session_motion.arm
${HOST_CC:-cc} -std=c11 -O2 -Wall -Wextra -Werror -o build/trial_exec.host trial_exec.c
${ARM_CC:-arm-linux-gnueabihf-gcc} -std=c11 -O2 -Wall -Wextra -Werror \
    -marm -mcpu=cortex-a7 -mfpu=neon-vfpv4 -mfloat-abi=hard -static \
    -Wl,--build-id=sha1 -o build/trial_exec.arm trial_exec.c
arm-linux-gnueabihf-readelf -h build/trial_exec.arm
sha256sum build/trial_exec.host build/trial_exec.arm
