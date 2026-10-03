# Motion source reconstruction and independent feature

The stock 4.7.11 recorder's accelerometer conversion is now reconstructed in
portable C. A separate static ARM program summarizes native motion files and
can follow them as they grow. It requires neither a replacement kernel nor
access to the sensor bus. These are offline-verified components; no new program
has been installed or tested on the headset.

## Verified data path

The input is the exact `nano_core` identified in [source findings](source-findings.md).
Private Ghidra output helped locate functions; the findings below were checked
against ARM instructions. Extracted code and decompilations remain private.

| Address | Observed behavior |
| --- | --- |
| `0x0008fd94` | Initializes LIS2HH12 on bus 3, address `0x1e`; CTRL1 is `0xaf`, CTRL4 is `0x04` |
| `0x00090288` | Reads six bytes from register `0x28`; writes transaction status to byte 6 of an internal ring slot |
| `0x0002d6f0` | Converts signed little-endian XYZ counts to three rotated/scaled float32 values |
| `0x00027a08` | Writes exactly 12 bytes; reports failure on a short write |
| `0x0002eba8`, `0x0002ec54` | Recording path calls conversion, then writes through the accelerometer file handle |
| `0x00049c98`, `0x00049cbc` | Relaxation path uses the same conversion and writer |

[ST's LIS2HH12 datasheet](https://www.st.com/resource/en/datasheet/lis2hh12.pdf),
revision 5, sections 8.5 and 8.8, identifies that setup as high-resolution,
50 Hz, block-data-update enabled, all axes enabled, and a ±2 g range.
The recording/relaxation consumers also process one motion row per five EEG
rows: their countdown resets to four after a motion read. This supports a
**nominal 50 Hz** file cadence with 250 Hz EEG. It does not prove wall-clock
alignment, clock accuracy, absence of drops, or behavior on another firmware.

Each `accelerometer.data` row is **three little-endian IEEE-754 float32 values**,
12 bytes total, without a per-row timestamp or status byte. The values have
already been converted; do not apply the raw-count decoder to this file.
The internal sensor ring uses 16-byte slots, containing six raw count bytes
and a status byte. Neither that ring layout nor the raw I2C bytes are the file
format.

For raw signed counts `(x, y, z)`, the observed coordinate transform is:

```text
a0 = -y / 16384
a1 = (x*cos(20 degrees) + z*sin(20 degrees)) / 16384
a2 = (x*sin(20 degrees) - z*cos(20 degrees)) / 16384
```

The implementation pins the two observed double-precision rotation constants
and uses explicit fused multiply-add operations to preserve ARM rounding before
converting to float32. The output is in nominal g, consistent with ST's ±2 g
sensitivity. Physical calibration and the mapping of these recorder coordinates
to anatomical directions remain unverified. The transform alone cannot label
a sleeping posture.

In the recording path, a failed sensor read can replace the converted triple
with zeros before writing, with a separate health transition. Hardware-version
3 bypasses that status check. The relaxation path shown above does not perform
the same check. Consequently, a finite zero row is not sufficient evidence of
valid physical zero acceleration. The native motion file alone cannot recover
the missing validity information. The separate [recording-event monitor](algo-findings.md)
now decodes the reported health transitions; joining their counters to motion
rows across recovery boundaries remains unfinished.

## Build and use

```sh
sh development/build.sh
development/build/motion_quality.host /private/session/accelerometer.data
qemu-arm -cpu cortex-a7 development/build/motion_quality.arm \
  --follow-seconds 60 /private/session/accelerometer.data
python3 -m unittest tests.test_eeg_quality tests.test_motion_quality -v
```

The ARM binary is statically linked for Cortex-A7 hard-float. A later headset
trial can run it directly against an explicitly selected native file. The
command does not discover sessions, start recording, open a sensor device,
change configuration, or install a background service.

Each complete 50-row window emits one JSON line:

| Field | Meaning |
| --- | --- |
| `sample_start` | Zero-based row index, starting at the beginning of this invocation's file |
| `nominal_rate_hz`, `units` | `50` and nominal `g`; no absolute timestamp is inferred |
| `valid_rows`, `invalid_rows` | Triples with all finite numbers, or with at least one NaN/infinity; not hardware validity |
| `zero_rows` | Finite triples whose three components are zero; possible recorder placeholders |
| `mean_axes` | Three per-axis averages over finite triples |
| `vector_rms` | RMS vector magnitude, including gravity |
| `dynamic_rms` | RMS vector deviation from this window's mean; not a frequency filter or sleep-stage score |
| `step_pairs` | Adjacent finite pairs within this window; no pair crosses a nonfinite row or window boundary |
| `step_rms`, `max_step` | RMS and largest vector difference for those pairs, in g per sample step |

Unavailable metrics are JSON `null`. The final record reports consumed rows,
an unfinished window's row count, and remaining bytes. A partial row is retained
until completed while following. Removal, inode replacement, and observed
truncation below the consumed offset stop the program with an error. A
truncate-and-regrow operation entirely between observations cannot be detected
reliably; use append-only recording files and start a fresh invocation per
session. The reader starts at row zero, uses bounded memory and rejects a final
symlink, FIFO, directory or device node.

`motion_samples.h` separately exposes `dreem_motion_decode` for a future owner
of the raw sensor path. It consumes exactly six raw bytes and produces three
floats, without checking I2C status or accessing hardware. Callers must provide
appropriately sized buffers and validate the acquisition result themselves.

## Verification and remaining work

```sh
/private/work/venv/bin/python development/verify_motion_decoder.py \
  /private/work/inspection/nano_core > /private/work/motion-verification.json
```

On October 3, 2026 (America/New_York), both independent host and ARM builds
matched **4,439 synthetic records byte-for-byte** against the original
conversion, including all combinations of seven signed boundary/basis values
and 4,096 seeded random triples. Another 343 calls check already-initialized
state. Four writer cases check the 12-byte contract and short-write failures.
The emulator maps 376 bytes of selected code/literals, bounds execution, checks
input/output guards, and supplies only modeled `fwrite`, errno and logging
services. No original application process is launched. The fixture/result
SHA-256 is `b979cbcf1356e1559bf898e6833ad15ce3a2ac6b75314bce1d371f149fa84ad6`.

Six motion-feature tests pass on both host and ARM emulation, covering stationary
gravity, alternating motion, nonfinite gaps, zeros, incomplete rows/windows,
large finite values, growing files, replacement/removal/truncation and invalid
inputs. The three existing EEG-feature tests also pass. Input hashes remain
unchanged in the fixed-file comparison. These checks establish arithmetic,
format handling and file-follow behavior, not device-side resource use or
recording fidelity.

Device-side trials, health-event alignment to motion rows, clock/timestamp alignment,
physical orientation and calibration, and electrical qualification for an added
sensor remain. The existing [sensor and pad map](source-findings.md#existing-sensor-interfaces)
still supplies only candidate interfaces, not a verified expansion pinout.
