# Source-built accelerometer acquisition

`motion_sensor.c/.h` integrate ST's public LIS2HH12 driver with the checked Linux
[I2C transport](sensor-transport.md). They add reset, explicit sampling profiles,
configuration readback, fresh-sample checks and verified power-down. This is
editable acquisition source for an exclusively owned sensor, not a deployed
replacement for Dreem's motion manager.

The [motion capture program](motion-capture.md) now provides a compiled host/ARM
caller with explicit profiles, host read-time bounds, private file output and
shutdown handling. Its parent must still establish and retain exclusive ownership.

## Public source and original behavior

The repository includes STMicroelectronics' driver and header at commit
[`09c28df1a67e2d85e4ea9f6447e6a9a983de2f0e`](https://github.com/STMicroelectronics/lis2hh12-pid/tree/09c28df1a67e2d85e4ea9f6447e6a9a983de2f0e),
with its BSD-3-Clause license and a [hash manifest](../third-party/source-snapshots/lis2hh12-pid/provenance.json).
Only CRLF-to-LF normalization was applied. The integration calls the actual ST
identity, reset, rate, scale, block-update and raw-acceleration functions. Other
upstream features are available as source, but are not qualified by these tests.
This is the component maker's source, not a claim that Dreem used this version.

`verify_motion_transport.py` executes the saved core's startup at `0x8fd94` and
sample reader at `0x90288`, including their original I2C helpers, with synthetic
system services. The core hash is pinned in [source findings](source-findings.md).
The observed successful startup opens bus 3, selects address `0x1e`, checks
WHO_AM_I for `0x41` and writes:

| Register | Value | Interpretation |
| --- | --- | --- |
| `0x1e` ACT_THS | `0x00` | Disable activity/inactivity switching |
| `0x20` CTRL1 | `0xaf` | High resolution, 50 Hz, BDU, all three axes |
| `0x21` CTRL2 | `0x00` | Default filter selection; no high-pass output |
| `0x22` CTRL3 | `0x00` | FIFO and INT1 routes disabled |
| `0x23` CTRL4 | `0x04` | ±2 g, auto bandwidth, address increment enabled |
| `0x24` CTRL5 | `0x00` | No self-test or decimation |

The first write's error label says `FUNC_CFG_ACCESS`; its ARM register arguments
and the [ST datasheet](https://www.st.com/resource/en/datasheet/lis2hh12.pdf)
identify ACT_THS instead. Startup waits 100 ms, with no configuration readback.
Wrong identity can cause five reads with 10-microsecond sleeps. A failed identity
transfer stops immediately. The isolated sample routine reads six bytes at
`0x28` without consulting data-ready/overrun status.

The original-code comparison passes 31 startup and 16 sample scenarios. Six
startup scenarios reproduce address-selection failure canceled by a short
register write, yielding a successful descriptor return. Sample checks also
reproduce partial destination updates and the same helper's false success.
These are synthetic failure paths, not claims that a saved recording suffered
those faults. The verifier maps 1,316 additional original instruction bytes;
its fixture SHA-256 is
`9de86dc04ce47b54789371d76e62ac4ad46bec6a2d8819537786223405edbeb3`.

## New interface and profile

The caller binds a descriptor and seven-bit address with
`dreem_motion_sensor_init`, then uses `start`, `read` and `stop`. Init performs
no I/O. Supported addresses are `0x1d` and `0x1e`; address selection does not
establish exclusive ownership. No function opens/closes a device, stops
`nano_core`, takes its locks or configures a service. Never use this interface
alongside the existing owner of that same sensor.

The [ownership investigation](sensor-ownership.md) verifies that record-stop
status and cleared thread flags do not establish this prerequisite. It also
replays the original motion cleanup: a software-reset request followed by close,
without reset polling or readback. Process retirement and confirmed chip state
are separate integration requirements.

The explicit profile supports six requested rates (10, 50, 100, 200, 400 and
800 samples/second), three ranges (±2, ±4 and ±8 g), and normal or high-resolution
mode. High-resolution mode retains the default ODR/50 low-pass selection;
normal mode uses automatic anti-alias bandwidth. These settings affect the
signal and power use; requested rates are not measured physical timestamps.
FIFO, interrupts, activity switching, self-test and decimation are disabled.

Start identifies the sensor before any writes, requests a destructive software
reset and polls it at most twenty times with one-millisecond waits. It then
confirms power-down, writes and verifies the profile while stopped, drains stale
output registers, activates the selected rate and verifies configuration again.
The first fresh sample is discarded, following the datasheet's power-down-to-
active settling rule for automatic bandwidth. A start on a running instance
returns `-EBUSY`; a new profile requires explicit stop/start and a new segment.
Reset polling does not bound kernel I/O, retries or total elapsed time.

Read verifies configuration, activity settings and FIFO bypass, then requires
data-ready status before fetching a six-byte sample. It reads status afterward
and rechecks control registers before publishing. `-EAGAIN` means no new sample
or the first fresh row was discarded. All error returns leave the caller's
output unchanged. Successful output contains signed 16-bit sensor-axis counts,
both status bytes and flags for unknown continuity and any observed overrun.
It is not the rotated float format in `accelerometer.data`. In particular,
the existing [recorder decoder](motion-findings.md) assumes the original ±2 g
profile; do not apply its scale unchanged to other ranges.

This polling/bypass interface can miss conversions. It does not provide FIFO
buffering, hardware timestamps, a dropped-sample count, contact/health judgment
or a guarantee of cross-axis simultaneity. Readback detects observed drift; it
cannot prove exclusive access or exclude reset/change-and-restore between reads.

## Failure and ownership

Hardware failures other than `-EAGAIN` withhold the sample and attempt power-down.
The primary error stays in `last_error`; `cleanup_error` separately records
whether that attempt failed. Power-down rejects an unfinished reset, disables
and verifies activity switching, clears ODR and checks the resulting mode.
`STOPPED` means those register conditions were observed, not that physical
current or stop timing was measured. Failed cleanup leaves `UNKNOWN` and the
caller-owned descriptor intact. Retain ownership until explicit recovery;
closing a file descriptor is not evidence that a sensor stopped. Stop can be
retried. Identity failures cause no writes to an unidentified target.

## Build and verification

Compile the integration with `sensor_i2c.c` and the captured `lis2hh12_reg.c`,
adding `development/` and `third-party/source-snapshots/lis2hh12-pid/` to the
include path. The connected tests do this for host and static Cortex-A7 ARM:

```sh
python3 -m unittest tests.test_motion_sensor tests.test_sensor_i2c -v
DREEM_MOTION_SANITIZE=1 python3 -m unittest tests.test_motion_sensor -v
/private/work/venv/bin/python development/verify_motion_transport.py \
  /private/work/inspection/nano_core
```

On October 3, 2026 (America/New_York), all 14 new tests passed, including compiled
host and ARM execution and snapshot provenance. The host execution also passed
AddressSanitizer and UndefinedBehaviorSanitizer.
The combined motion/transport suite passed 25 tests. The actual compiled ST
driver, integration and Linux transport execute against synthetic ioctl/sleep
services. Checks cover all 72 profile/address combinations, 252 startup fault
combinations, 35 read fault combinations, 12 stop fault combinations, identity,
stuck/interrupted reset, settling, status/overrun handling, configuration drift,
dual primary/cleanup failures and explicit stop/restart. Host and ARM outputs
agree. The register model does not emulate the complete analog/filter pipeline.

No physical adapter is opened in these tests. Manager ownership, sensor identity,
electrical validation, sustained bus timing, physical reset/power-down, filter
response and recording fidelity still require qualification on the headset.
