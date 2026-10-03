# Optical recording monitor

`optical_quality.c` is an independent, read-only program for saved or growing
`pulse.data` files. It reports red/infrared count variation without taking
ownership of the sensor or replacing `nano_core`. It opens no sensor device,
issues no I2C transactions and does not control recording. Its source and tests
use Apache-2.0.

## Input and interpretation

The [reconstructed optical writer](sensor-transport.md#optical-data-and-register-evidence)
stores two little-endian unsigned 32-bit integers per eight-byte row: red then
infrared. The monitor decodes all 32 stored bits. The inspected configuration
requests 18-bit conversion, so values greater than `0x3ffff` are reported as
out of range, without masking the unexpected bits.

If either channel is out of range, the entire pair is excluded from statistics;
both channels therefore use the same accepted rows. Per-channel out-of-range
counts still identify which value exceeded the limit. In-range zero values are
retained. `zero_rows` counts pairs where both values are zero; per-channel zero
and exact-ceiling counts include only jointly in-range rows. A zero, ceiling
value or unexpected upper bit alone does not establish sensor failure, clipping
or its cause.

Each JSON `optical_window` covers 50 input rows, including any excluded pairs.
The last nonempty partial window is also emitted with `complete_window: false`
and its actual `rows`. `sample_start` is a zero-based file row index. Windows are
not assigned a duration or sample frequency: the original acquisition cadence
and possible FIFO loss do not justify deriving physical time from file offsets.

For each channel, the program reports minimum, maximum, mean, AC RMS (the square
root of population variance), peak-to-peak range and AC/DC ratio (AC RMS divided
by mean). It also reports Pearson red/infrared correlation. Means, variance and
covariance use an incremental calculation with bounded memory. Undefined
metrics are JSON `null`: all statistics when no pairs are accepted, AC/DC when
the mean is zero, and correlation with fewer than two accepted pairs or either
channel's variance zero.

Every window reports `units: "adc_counts"`, `sensor_health: "unverified"` and
`continuous_acquisition_verified: false`. These are descriptive file statistics,
not heart rate, oxygen saturation, contact detection or a clinical assessment.
The monitor does not align rows to the separate recorded-health events.

## Build and use

With the host and ARM cross-compilers installed:

```sh
sh development/build.sh
python3 -m unittest tests.test_optical_quality -v
development/build/optical_quality.host /private/path/pulse.data
qemu-arm -cpu cortex-a7 development/build/optical_quality.arm /private/path/pulse.data
```

The ARM program is statically linked for Cortex-A7 hard-float. To observe a
growing file for a finite interval, starting at row zero:

```sh
development/build/optical_quality.host --follow-seconds 60 /private/path/pulse.data
```

Default mode analyzes only the byte extent observed when the file was opened;
appended bytes are left for another invocation. This bounds the input extent,
but does not make an atomic copy of the contents. Follow mode polls every
100 ms when no complete row is available and rereads incomplete trailing bytes
when more data arrives. It accepts finite durations greater than zero and at
most 86,400 seconds. Blocking file/output I/O or scheduling can delay completion;
the duration is not a hard wall-clock guarantee.

The final `end` record includes `rows_consumed`, `bytes_remaining`,
`initial_bytes` and `follow`. Remaining bytes are the final observed file size
minus the consumed offset. They can include complete appended rows as well as
an incomplete trailing row, especially after a follow deadline. Only complete
eight-byte rows contribute to statistics.

Only regular files are accepted. The final pathname component must not be a
symlink; FIFOs and device nodes are rejected. Before reads and at completion,
the reader checks the open file and pathname for removal, inode/device changes
and observed shrinkage. Detection fails the run; already emitted windows remain
partial output. In-place edits and shrink/regrow events between observations
cannot reliably be detected. Use a new invocation after a recording is replaced.
Exit status is 0 on successful analysis, 1 on detected I/O/timeline/output
failure and 2 for invalid arguments.

## Verification and remaining work

On October 3, 2026 (America/New_York), all eight monitor tests passed on the host
and under ARM emulation. They cover independently calculated statistics,
constants, correlation, all-32-bit range checks, null metrics, partial windows,
growth, partial rows, the initial-extent boundary, replacement/removal/truncation,
input restrictions and output failure. All eight also passed host AddressSanitizer
and UndefinedBehaviorSanitizer checks. The combined feature suite passed 42 tests.

Host and ARM results also agreed on one private native optical recording. A
separate two-pass calculation verified means, AC RMS, extrema, range/zero/ceiling
counts, ratios and correlation. Every complete input row was accounted for,
including the final partial window; the local input hash remained unchanged.
Two identical archived copies represent one recording, not independent evidence.
Its metadata identifies firmware 4.6.9, while the analyzed writer is from 4.7.11.
The file fits the recovered format; this does not prove every older firmware
version has identical semantics or establish physical timing and sensor health.
The recording and derived statistics remain outside the public repository.

The [ARM fixture trial](feature-trial.md) includes this monitor among its six
cases, using only synthetic input. Its full emulated run passed. Neither that
trial nor the native-file comparison qualifies concurrent operation on a headset:
target resource use, recording fidelity and physical acquisition remain untested.
