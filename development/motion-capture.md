# Independent accelerometer capture

`motion_capture.c` turns the [source-built motion lifecycle](motion-lifecycle.md)
into a host/ARM command-line program. It configures an exclusively owned
LIS2HH12, records signed sensor counts with host read-time bounds, and attempts
checked power-down on completion, interruption or error. No original `nano_core`
source is needed. This is a runnable acquisition component; ownership transfer
from the vendor recorder and physical qualification remain unfinished.

## Build and integration contract

```sh
sh development/build_motion_capture.sh
development/build/motion_capture.host --help
qemu-arm -cpu cortex-a7 development/build/motion_capture.arm --help
python3 -m unittest tests.test_motion_capture -v
```

The program links the actual ST source, `motion_sensor.c` and `sensor_i2c.c`.
The ARM output is a static Cortex-A7 hard-float executable. Build outputs are
ignored by Git. The default build requires both host and ARM compilers; the
test suite also executes ARM cases when the cross-compiler and QEMU are present.

The parent must establish exclusive sensor ownership, prevent another consumer
from restarting, and pass an already-open read/write Linux i2c-dev descriptor
numbered at least three. The descriptor is checked as a character device with
major number 89, as described by the
[Linux userspace I2C interface](https://docs.kernel.org/i2c/dev-interface.html).
That type check establishes neither ownership nor correct physical routing.
The program does not open sensor devices, stop processes, change supervision,
or acquire an advisory lock that could be mistaken for vendor exclusion.

After the parent has fulfilled that contract, the command shape is:

```text
motion_capture --fd INHERITED_FD --address 0x1e --rate 100 --range 4 \
  --high-resolution 1 --duration-ms 10000 --max-samples 1000 \
  --output NEW_PRIVATE_FILE
```

This is not a recipe for taking over the running headset. In particular,
[recording idle and record-stop success do not prove ownership](sensor-ownership.md).
The parent must retain its exclusion after an uncertain stop and explicitly
recover the sensor before restoring the old consumer. Process exit closes the
child's descriptor references; it does not establish sensor power-down or release
the parent's responsibility. Concurrent use of the inherited open-file
description is outside the contract.

All eight options are required; duplicates and unsupported settings fail before
bus access. Rates are 10/50/100/200/400/800 Hz, ranges are 2/4/8 g, and high
resolution is zero or one. Both supported addresses (`0x1d`, `0x1e`) are accepted.
Duration is 1..3,600,000 ms and the output limit is 1..1,000,000 samples. The
selected profile determines sensor configuration, not a verified physical rate.

## Output and timing

Output is a newly created mode-600 regular file. An existing path, final symlink
or directory is rejected before any sensor access. Parent-directory trust,
available storage and exclusive ownership remain the integrating process's
responsibility. Output contains no device serial, account ID, recording path or
credentials, but the acquired motion itself can be personal data.

The newline-delimited JSON schema is `dreem.motion.capture.v1`:

| Record | Meaning |
| --- | --- |
| `request` | Requested address/profile, duration, sample limit, raw units, axes and clock; written before sensor start |
| `sample` | Zero-based output index, signed X/Y/Z counts, status bytes, continuity/overrun flags, and monotonic timestamps immediately before and after the checked read call |
| `end` | Capture stop reason, counts, signal, original error, final stop error, startup cleanup error and whether shutdown was confirmed |

Counts use sensor axes and are not the vendor recorder's transformed float
rows. No conversion to g, timestamps for the actual conversion, sleep-stage
interpretation or cross-device clock alignment is implied. `read_begin_ns` and
`read_end_ns` bracket the software read call, not the physical sample instant.
The clock is monotonic host time, not wall time. Backward/error/overflowing clock
results fail capture and initiate cleanup.

The checked lifecycle discards stale data and the first fresh settling row.
The program polls at a requested half-period interval, capped by the remaining
duration. Duration begins after successful startup. Calls that complete at or
after its deadline do not publish another sample. `not_ready` includes both
ordinary no-data results and the settling discard; it is not a lost-sample
count. `late_samples_discarded` counts otherwise successful reads withheld at
the deadline. A duration run can finish with zero samples.

Every published sample retains the continuity-unknown flag; observed overrun is
reported separately. Slow scheduling or file output may lose samples, even when
the process exits successfully. This polling implementation does not promise
every configured-rate sample, uninterrupted acquisition or hardware timestamps.

## Failure and shutdown

Invalid arguments or an unsuitable descriptor return 2 without sensor I/O.
Normal duration/sample-limit completion returns zero only after checked shutdown
and successful output write, `fsync` and close. Handled `SIGINT`/`SIGTERM` request
cleanup and otherwise return `128 + signal`; any acquisition, cleanup or output
failure returns 1. An initial identity failure performs no sensor writes and
does not claim shutdown was verified.

Start and reads can themselves attempt cleanup; their original error is
preserved while the final shutdown result is recorded separately. Failed reads
never publish their partially received sample. Failed shutdown reports
`UNKNOWN`/`shutdown_confirmed: false`, even when a modeled chip might actually
have stopped. A successful final retry does not erase an earlier capture error.

The small stderr summary reports exit status, sample count and errors without
printing samples or the output path. Consumers must check the process status
and final record together: output failure can leave no `end` record or a partial
line, and a later `fsync`/close failure can invalidate an otherwise complete file.
An `end` record alone is not proof of successful persistence.

Polling, output and I2C operations remain subject to kernel blocking and
scheduling. The duration is not a hard wall-clock execution limit. `SIGKILL`,
power loss and uninterruptible I/O cannot be cleaned up by this program. The
parent must supervise its child and retain recovery state after uncertain exit;
the generic fixture runner does not establish sensor ownership.

## Verification

On October 3, 2026 (America/New_York), all 13 connected tests passed on host and
ARM emulation, including host AddressSanitizer/UndefinedBehaviorSanitizer runs.
The tests link the unmodified capture program, checked lifecycle/transport and
ST driver. Only clock, descriptor, file-error and I2C boundaries are intercepted.
They cover all 72 profile/address combinations, settling/count semantics,
monotonic bounds, slow-output overrun, partial transfer rejection, separate
read/shutdown failures, interruption before/during acquisition, short output
writes, output persistence failures and no-I/O rejection of invalid inputs.

The register producer and timing are synthetic. These checks do not establish
native syscall compatibility, physical sample timing, signal fidelity, power
use or a functioning ownership handoff. No device-node access or headset
deployment was performed. A fresh desktop scan found 29 nearby BLE devices but
no Dreem/Rythm-named candidate; this does not establish the headset's power state.
