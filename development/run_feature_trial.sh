#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Run only the fixture cases bundled by build_feature_trial.py.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
LC_ALL=C
export LC_ALL
umask 077
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$base"
mode=native
qemu=
if [ "$#" -ne 0 ]; then
    if [ "$#" -ne 2 ] || [ "$1" != --emulated ]; then
        echo 'Usage: run_feature_trial.sh [--emulated /absolute/path/to/qemu-arm]' >&2
        exit 2
    fi
    mode=emulated
    qemu=$2
    case "$qemu" in /*) ;; *) echo 'QEMU path must be absolute' >&2; exit 2 ;; esac
    [ -x "$qemu" ] || { echo 'QEMU executable missing' >&2; exit 2; }
fi
sha256sum -c SHA256SUMS >/dev/null
results=$(mktemp -d "$base/results.XXXXXX")
printf 'Results: %s\n' "$results"
printf '%s\n' "$mode" > "$results/mode.txt"
uname -m > "$results/architecture.txt"
uname -r > "$results/kernel-release.txt"
core_pid=
core_start=
preserved=null
if [ "$mode" = native ]; then
    case "$(uname -m)" in armv7*) ;; *) echo 'Native trial requires an ARMv7 headset' >&2; exit 1 ;; esac
    [ -f /usr/bin/nano_core ] || { echo 'Headset core is missing' >&2; exit 1; }
    IFS= read -r expected_core < CORE_SHA256
    actual_core=$(sha256sum /usr/bin/nano_core | awk '{print $1}')
    [ "$actual_core" = "$expected_core" ] || { echo 'Headset core differs from the reviewed firmware' >&2; exit 1; }
    core_pid=$(pidof nano_core)
    case "$core_pid" in ''|*[!0-9]*) echo 'Expected one recorder process' >&2; exit 1 ;; esac
    core_start=$(awk '{print $22}' "/proc/$core_pid/stat")
    [ -n "$core_start" ] || { echo 'Cannot identify recorder process start' >&2; exit 1; }
fi

run_case() {
    name=$1
    program=$2
    input=$3
    expected_status=$4
    status=0
    if [ "$mode" = emulated ]; then
        "$qemu" -cpu cortex-a7 ./trial_exec.arm 20 "$results/$name.metrics.json" -- \
            "$qemu" -cpu cortex-a7 "$base/$program.arm" "$base/$input" \
            > "$results/$name.stdout" 2> "$results/$name.stderr" || status=$?
    else
        ./trial_exec.arm 20 "$results/$name.metrics.json" -- "$base/$program.arm" "$base/$input" \
            > "$results/$name.stdout" 2> "$results/$name.stderr" || status=$?
    fi
    [ "$status" -eq "$expected_status" ] || { echo "Unexpected result for $name" >&2; return 1; }
    for field in '"timed_out":false' '"interrupted":false' '"monitor_error":false' '"exec_errno":null'; do
        grep -F "$field" "$results/$name.metrics.json" >/dev/null || return 1
    done
    grep -F "\"exit_code\":$expected_status," "$results/$name.metrics.json" >/dev/null
    cmp "expected/$name.stdout" "$results/$name.stdout"
    cmp "expected/$name.stderr" "$results/$name.stderr"
    sha256sum -c SHA256SUMS >/dev/null
}

run_case eeg_quality eeg_quality fixtures/normal/eeg.data 0
run_case motion_quality motion_quality fixtures/normal/accelerometer.data 0
run_case algo_health algo_health fixtures/normal/algo.data 0
run_case session_motion session_motion fixtures/normal 0
run_case recovered_session session_motion fixtures/recovered 1
if [ "$mode" = native ]; then
    [ "$(pidof nano_core)" = "$core_pid" ] &&
    [ "$(awk '{print $22}' "/proc/$core_pid/stat")" = "$core_start" ] || {
        echo 'Recorder process changed during trial' >&2
        exit 1
    }
    preserved=true
fi
printf '{"mode":"%s","fixture_cases":5,"recorder_process_preserved":%s,"native_recording_fidelity_tested":false}\n' \
    "$mode" "$preserved" > "$results/result.json"
echo 'All five fixture cases and input-integrity checks passed.'
