# Recording events and sensor health

The independent `algo_events.c` parser and `algo_health` monitor decode the
saved 4.7.11 recorder's event stream, including accelerometer and optical-sensor
health transitions. Host and static ARM builds are verified against isolated
original ARM routines and synthetic files. No headset installation or device-side
trial has been performed for this feature.

## Observed format

Addresses below refer only to the `nano_core` hash in
[source findings](source-findings.md#identity-and-source-matching). Decompiled
analysis and extracted instructions remain outside this repository.

| Routine or call | Address | Evidence |
| --- | --- | --- |
| Generic event writer | `0x22bcc` | Writes four counter bytes, one code byte, then the caller's payload; checks each write |
| Recording wrapper | `0x28038` | Writes through the `algo.data` handle using the recorder counter at state offset `0x547e` |
| Replay reader | `0x281b4` | Selects payload sizes by event code and applies sensor-health updates |
| Recording motion-health event | `0x2ec14` | Emits code 30 when the motion read's reported health changes |

Each event is a **little-endian uint32 sample counter**, a **uint8 code**, and
a code-dependent payload. There is no stored payload length. The replay reader
recognizes these 28 codes:

| Payload bytes | Codes, decimal |
| --- | --- |
| 0 | 2, 3, 15, 23, 28, 32, 33 |
| 1 | 18, 19, 24, 34 |
| 4 | 13, 14, 16, 17, 20, 21, 22, 25, 26, 30, 31, 35, 36 |
| 8 | 27, 29 |
| 12 | 37 |
| 16 | 1 |

Only the meanings below are interpreted by the new monitor. Other recognized
codes retain their raw payload bytes. Unsupported codes stop parsing with an
error and byte offset: guessing a payload size would misalign subsequent events.
This intentionally differs from the original replay reader's warning-and-continue
behavior for an unknown code. The generic writer itself accepts arbitrary codes;
the recognized set is not proof that every recording uses only these codes.

Code **30** carries motion health and code **31** carries optical-sensor health,
each as a four-byte integer. The recording path emits `0` for bad and `1` for
good. The original replay reader treats zero as a failed sensor and any nonzero
value as good. The independent monitor interprets only `0` and `1`; other values
are retained but reported as `unknown`. This is a conservative interpretation,
not an exact reproduction of the original nonzero test.

These are **reported acquisition states**, not proof of signal quality or
physical calibration. In particular, the recording motion path bypasses its
status check for hardware version 3. A motion failure can produce finite zero
triples in `accelerometer.data`; see [motion findings](motion-findings.md).

## Counter and recovery boundaries

The monitor preserves `sample_counter` without converting it to an absolute
time or native file row index. The record loop advances this counter for EEG
samples, but recovery also changes it independently of writing a new EEG row.

In the inspected initialization recovery branch, the recorder derives a count
from the EEG file size divided by 16, increments it, emits **code 28** with no
payload, then increments it again. This establishes a recovery marker and
invalidates a blanket assumption that every counter equals a zero-based EEG
file row. Motion-row alignment across such boundaries remains unfinished.

The new monitor starts both sensor states as `unknown`. Code 28 or an observed
counter decrease starts a new numbered `segment` and clears both states. A
health event on that same record then updates its own sensor. A decrease is
reported as a discontinuity, without claiming whether it was recovery, wrap,
corruption, or another cause. Equal counters are permitted. No wall-clock time,
missing duration, sensor freshness interval, or cross-file offset is inferred.

A separate two-word writer at `0x27a90` has a time-data diagnostic, but a direct
ARM branch-target scan found no callers in the executable. Indirect calls have
not been excluded; the stored values and cadence are unproven. The similar
routine at `0x27b44` has audio-thread callers. Neither observation establishes
`timestamp.data` as an EEG or motion wall-clock source.

## Build and use

```sh
sh development/build.sh
development/build/algo_health.host /private/session/algo.data
qemu-arm -cpu cortex-a7 development/build/algo_health.arm \
  --follow-seconds 60 /private/session/algo.data
python3 -m unittest tests.test_algo_health tests.test_eeg_quality tests.test_motion_quality -v
```

The ARM build is statically linked for Cortex-A7. It reads an explicitly chosen
regular file from byte zero, using bounded memory; it never replays stimulation
events, opens sensor devices, or changes recordings. JSON lines contain:

- `type`: `event`, `recovery`, `motion_health`, or `optical_health`.
- `byte_offset`, original `sample_counter`, decimal `code`, and `payload_hex`.
- `segment` and `counter_decreased`, identifying the monitor's state boundaries.
- `motion_health` and `optical_health`: last reported `good`/`bad` state within
  this segment, or `unknown`; not a continuously verified live health status.
- `health_value` on sensor-health events, preserving even an unexpected integer.

The final `end` line reports complete events and bytes consumed, plus bytes
remaining. A partial trailing record is retained while following; at exit its
bytes remain explicitly unconsumed. When a follow duration expires, remaining
bytes can also include complete events that have not yet been read. Empty files
produce an end line with zero counts. Consumers should check both exit status
and the end record before treating input as fully processed.

The reader rejects a final symlink, FIFO, directory or device node. Removal,
inode replacement, or an observed decrease in file size is an error, including
a decrease affecting only an unconsumed partial record. Same-size overwrites
and truncate-and-regrow changes entirely between observations cannot reliably
be detected. Use append-only files and a fresh invocation for each session.

The portable `dreem_algo_decode` API returns one event's consumed byte count,
zero for incomplete input, or minus one for an unsupported code. It leaves
output unchanged on incomplete/unsupported input. Callers retain unconsumed
bytes; no I/O, allocation, device access or health-state interpretation occurs
inside the parser.

## Verification and remaining work

```sh
/private/work/venv/bin/python development/verify_algo_events.py \
  /private/work/inspection/nano_core > /private/work/algo-verification.json
```

On October 3, 2026 (America/New_York), the isolated original ARM writer emitted
**112 fixtures** covering all 28 supported codes and four counter boundaries.
Both the shipped host and ARM monitors decoded them exactly. **36 original
reader cases** verify payload consumption, health-flag changes and modeled
stimulation callbacks; **240 short-read cases** and **three short-write cases**
verify the original error paths. The private firmware hash is pinned before
emulation. Only 2,276 bytes of selected instructions/literals are mapped,
execution is bounded, and recorder-state mutations are checked. File services,
logging and stimulation callbacks are synthetic; no original application or
hardware runs. Fixture SHA-256:
`37ed7f7994b50947cec1284deaf1a0a3d87735fed0eef82278307464e8ef5c28`.

Seven new feature tests pass on host and ARM: all payload sizes across read
buffer boundaries, every incomplete prefix of the supported frames, all 228
unsupported codes, health transitions and invalid values, recovery/counter
boundaries, partial appends, file replacement/removal/shrink, and invalid
inputs. The nine existing EEG/motion feature tests also pass. These are offline
format and file-handling checks, not physical sensor or recording qualification.

The monitor exposes health events but does not yet join them to EEG/motion
rows. Cross-file alignment, wall-clock reconstruction, device-side resource
and recording-fidelity checks, additional code semantics, and physical sensor
qualification remain open.
