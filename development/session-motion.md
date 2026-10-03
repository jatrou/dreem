# Health-aware motion summaries

`session_motion` combines native motion samples with recorded health events.
It checks counter and file consistency before aligning a completed, unrecovered
recording, then excludes reported-bad, unknown-health and nonfinite samples
from motion statistics. It is independently written C with host and static ARM
builds. No new program has been installed or qualified on the headset.

## Recovered timing rules

These observations use the pinned 4.7.11 `nano_core` from
[source findings](source-findings.md). Addresses identify evidence; extracted
instructions and decompiler output are not distributed.

| Evidence | Consequence |
| --- | --- |
| Acquisition reset, `0x19240`, clears 142 bytes at state offset `0x53f8` | The counter at `0x547e` starts at zero for a normal recording |
| Record-loop acquisition prefix, `0x2ea60` | Its decimator starts at zero, writes motion before EEG, resets to four and decrements on the next four iterations |
| Counter increment, `0x2ea50` | After successful intervening processing, each completed iteration advances the counter once |
| Motion-health call, `0x2ec14`, precedes motion write at `0x2ec54` | A health change applies to the motion row written in that iteration |
| Metadata writer, `0x2b57c` | Copies the 142-byte header and emits stop event 17 with the current counter and time |

For the normal, uninterrupted path, motion row `m` is therefore associated
with EEG counter `5*m`. `N` successfully recorded EEG rows require
`ceil(N/5)` motion rows, including a motion row on the first iteration.
The final motion window can be shorter than 50 rows. This is recorder ordering,
not a claim of simultaneous physical sensor sampling or precise wall-clock time.

The metadata header fields used here are:

| Byte offset | Bytes | Observed field |
| --- | --- | --- |
| 110 | 4 | Recording start time, matching event 16's payload |
| 114 | 4 | Metadata update/finalization time, matching final event 17's payload |
| 118 | 2 | Recovery flag; only zero is accepted for this alignment |
| 134 | 4 | Current recorder sample counter |

Integer fields are little-endian. The program checks time payloads for equality
but does not convert them to dates or infer sample timestamps. It does not print
the record/user identifiers elsewhere in the header, inspect EEG signal values,
or interpret the variable metadata body. This is a consistency check of selected
fields, not a complete metadata validator or authenticity check.

## Why recovery remains separate

As documented in [recording events](algo-findings.md#counter-and-recovery-boundaries),
recovery derives the next counter from the persisted EEG length and inserts
additional counter steps. The processor also restarts its motion decimator.
Consequently, a restarted segment's motion phase follows its starting counter,
which need not be divisible by five.

The inspected recovery path opens EEG and motion files independently in append
mode and does not record a persisted motion-row offset in recovery event 28.
Buffered writes and an interrupted final iteration can leave different tails
in the two files. The EEG-derived recovery counter alone does not establish the
motion-file boundary. The current command rejects nonzero recovery flags and
recovery events explicitly; it does not silently apply normal-recording offsets.
Recovering those boundaries needs further evidence or an independent checkpoint
scheme. The existing event and unfiltered motion readers remain usable for
inspection of such recordings.

## Build and use

```sh
sh development/build.sh
development/build/session_motion.host /private/completed-recording
qemu-arm -cpu cortex-a7 development/build/session_motion.arm /private/completed-recording
python3 -m unittest tests.test_session_motion -v
```

The directory must contain `eeg.data`, `accelerometer.data`, `algo.data`, and
`meta.data` from the same completed recording. The program requires:

- Complete EEG and motion rows, a complete 142-byte metadata header, and complete
  recognized event frames.
- Metadata counter equal to the EEG row count and motion count equal to
  `ceil(EEG rows/5)`.
- One start event at counter zero, matching the metadata start time, and one
  final stop event matching the EEG count and metadata finalization time.
- Nondecreasing event counters within that range, no recovery, and at most one
  motion-health event at each valid motion-sampling counter.

It first validates the complete event stream. Unknown codes, missing endpoints,
partial files, inconsistent counts and unsupported timing boundaries fail with
a diagnostic and no alignment report. A missing initial health event is allowed:
samples remain unknown until a usable health transition appears.

The first JSON line describes the accepted alignment basis. Each subsequent
window covers up to 50 motion rows, retaining reported-good/bad/unknown counts.
Only **reported-good rows whose three components are finite** contribute to
`mean_axes`, `vector_rms`, and `dynamic_rms`. Unknown values in health events
remain unknown. The report also counts nonfinite and all-zero reported-good
rows; a reported-good zero is not automatically reclassified as sensor failure.
Metrics without eligible rows are `null`. Dynamic RMS is variation about this
window's mean over included rows, not a time-derivative, frequency filter,
sleep-stage estimate or diagnostic. Nominal units are g.

Health reflects what the recorder reported. Hardware version 3 bypasses the
motion-status check, and failed event writes can lose transitions while sample
writes continue. These files cannot prove uninterrupted physical sensor validity.
Filtering based on the recorded state does not repair absent health evidence.

The final `end` line reports EEG/motion row counts and health coverage. Consumers
must require both a zero exit status and this end record: an input change or
I/O failure can invalidate a report after earlier windows have been emitted.

Only regular files are accepted. Final directory/file symlinks, FIFOs and device
nodes are rejected. The command checks inode, size, modification time and change
time before and after processing. It uses bounded memory and read-only handles.
These checks detect observed mutation; they do not provide an atomic snapshot
or defend against deliberately concealed changes. Use a stable completed copy,
not a directory that the recorder is actively updating.

## Verification

```sh
/private/work/venv/bin/python development/verify_recording_cadence.py \
  /private/work/inspection/nano_core > /private/work/cadence-verification.json
```

On October 3, 2026 (America/New_York), **18 normal-recording cases per host/ARM
build** matched original ARM acquisition ordering and health transitions,
including empty/short recordings, decimation boundaries and a motion-ring wrap.
Two additional cases cover a nonzero initial counter and counter wrap. The
comparison executes **6,824 acquisition-prefix iterations** and maps 1,356 bytes
of selected original code. Fixture SHA-256:
`babe9f4e41d4646c5a0e720dcf0c4dd18541813cdb3313b4f4f2e3fa9eb979ff`.

This is deliberately a bounded block comparison. It executes reset, the actual
acquisition/file-write prefix, and the original counter increment. The algorithm
work between prefix and increment is excluded and modeled as successful. Sensor
reads, sample conversion, synchronization, file services and Nerves callbacks
are modeled. It establishes the tested cadence and health-before-write behavior,
not successful execution of the whole recorder, durable storage, EEG-ring
progress, wall-clock accuracy or physical operation.

Seven new feature tests pass on host and ARM, covering filtering arithmetic,
partial windows, decimation endpoints, unknown/bad/nonfinite samples, malformed
or inconsistent input, recovery ambiguity, filesystem input types and mutation
during processing. All 16 existing EEG/motion/event feature tests also pass.

A privately retained native recording whose version file identifies firmware
4.6.9 and Nerves 3.3.5 also passes the consistency checks and produces identical
host/ARM reports. Two saved copies are byte-identical and count as **one**
independent recording. This is observed compatibility with that older recording;
it is not a physical 4.7.11 qualification or a claim about every firmware version.
Recording contents, identifiers, timestamps and generated reports remain private.

Recovery-segment alignment, live coherent snapshots, device-side resource use
and recording-fidelity checks, and electrical qualification of added sensors
remain open.
