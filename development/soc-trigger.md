# Direct audio trigger rollback

The research board's direct PCM link now unwinds a failed start, resume or
pause-release operation. A CPU or machine callback failure after platform DMA
starts requests DMA retirement before returning. Stop, suspend and pause-push
attempt all component callbacks even when an earlier callback fails.

## Implementation and scope

`apply_soc_trigger_overlay.py` applies after the board and PCM lifetime overlays.
Under `CONFIG_DREEM_WM8960`, it adds an explicit `dreem_trigger_rollback` field
to the DAI link. The board opts in only its direct link, index 0. The ASRC front
and back ends, other cards, and the legacy bespoke-trigger path retain their
previous dispatch behavior. This is an internal research-kernel configuration;
build its users together rather than mixing headers or modules from another
configuration.

The checked path preserves this old tree's codec, platform, CPU and machine
callback order. Start-like failures unwind attempted stages in reverse order,
including the failing stage because it may already have changed hardware.
The inverse commands are START → STOP, RESUME → SUSPEND, and PAUSE_RELEASE →
PAUSE_PUSH. Cleanup continues after an error, logs rollback errors, and retains
the original start error. Normal stop-like commands retain their first error
while attempting every stage. Invalid commands fail before any callback.
Missing operation tables or callbacks are skipped.

The inverse-command pairing and continued stop cleanup also appear in public
[Linux v6.6 ASoC source](https://github.com/torvalds/linux/blob/v6.6/sound/soc/soc-pcm.c).
This implementation adapts the older NXP interface and explicitly limits the
new behavior to the direct research link; it is not a backport of the modern
component/DPCM infrastructure.

The rollback invokes only atomic trigger callbacks. It does not flush DMA work
inside the PCM trigger. The existing [DMA retirement mechanism](pcm-lifetime.md)
retains descriptors while stopping and synchronizes before buffer reuse/free.
Pause and pause-capable suspend rollback leave the descriptor paused for a
later release/resume. A component that also fails its cleanup can still leave
hardware in an unknown state; attempting rollback is not a physical guarantee.

## Verification

Use the [research kernel build](audio-lifetime.md) and preserve the preceding
build from commit `6155af58682fa9b1aa4968f235b05bb8cc50e54b`:

```sh
/private/work/venv/bin/python development/verify_soc_trigger.py \
  /private/work/audio-kernel /private/work/pre-soc-trigger-kernel \
  > /private/work/audio-kernel/soc-trigger-verification.json
```

The verifier executes linked ARM ASoC dispatch, with modeled codec, CPU and
machine callbacks. Connected cases execute the actual PCM, virtual DMA, SDMA
control and retirement routines. Allocation, MMIO, work/tasklet scheduling
and the stop-settling interval remain modeled. The board opt-in check executes
the compiled probe both with and without an ASRC device.

Cases cover every trigger command and failing stage, rollback failures,
continued stop cleanup, optional callbacks/tables, invalid commands, unselected
links, both directions, all four sample widths, mono/stereo, cleanup/retry and
paused resume. Previous-kernel controls reproduce an active DMA channel after
a CPU start error and skipped DMA cleanup after a codec stop error.

Whole-object relocation and string comparisons include `soc-pcm.o`. The
comparison now binds MOVW/MOVT string-address halves by relocation symbol and
addend, so copying a low half to another register does not cause a false
rejection. Every observed half must agree, the complete string is checked,
and every relocated code byte must match. Two mutation controls change a
cross-register MOVT destination and its referenced string; both are rejected.
The existing seven codec comparison controls remain required.

Verified offline on October 3, 2026 (America/New_York): **190 cases**, including
the two previous-driver and two relocation mutation controls, passed. The
complete ASoC function/table comparison covers **18,864 bytes**. All twelve
earlier verifier reports also pass with the updated comparison code, and the
kernel/module build retains all 44 matching ADC imports.

The ASoC object SHA-256 is
`bb4477c3949fda14fcc2395d3317f82d2d390faa3836a0e4c0192212bbcdf0de`.
[Board integration](audio-lifetime.md) owns the kernel/board hashes; the existing
PCM and clock documents own their object hashes. The later
[SAI control verification](sai-control.md) records the current private qualification
manifest. This trigger verifier's dependency closure has 18 files.

With only the research audio option disabled, the complete ASoC object is
byte-identical to a previous-source build at the same source path:
`2be18893dfbb8bbafb31d258a257be871e0442483a76a6e24ee9e2150423404f`.
The private source was restored after that comparison.

## Remaining boundaries

The later [SAI control repair](sai-control.md) now reports real register failures
to this rollback path and adds connected checks through DMA retirement. Its
status-bit semantics, bounded stop and duplex handling have separate modeled
verification. Full ALSA linked-stream scheduling, DPCM/ASRC,
power management, independent unbind and physical audio remain unqualified.
No kernel was installed or flashed.
