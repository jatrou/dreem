# Research audio clock and retry repairs

The experimental audio profile now configures all nine advertised sample rates
and all four PCM sample widths through the connected board, codec and SAI setup
callbacks. The earlier [matching codec](audio-findings.md) remains an unchanged
reference; the active codec deliberately repairs its clock and error-state bugs.
These are compiled ARM/model results, not physical playback or recording proof.

## Behavior

In codec-master mode, the board requests two 32-bit I2S slots and a BCLK/sample
rate ratio of 64. Logical PCM widths remain 16, 20, 24 or 32 bits; this does not
claim 32-bit analogue precision. The SAI overlay honors an explicitly requested
slot width while receiving external clocks. Without an explicit request it
retains the original sample-width behavior. The compile option selects these
changes for the research profile; it is not a per-SAI-device runtime gate.

The board supplies MCLK to the codec's automatic clock planner instead of
choosing PLL output from sample width. Planning checks the complete SYSCLK,
ADC/DAC, BCLK and class-D divider combination before clock-register changes.
PLL candidates use the manufacturer's preferred 90–100 MHz internal VCO band;
class-D switching stays within 700–800 kHz. The physical references are the
[WM8960 revision 4.4 datasheet, pages 59–65](https://mm.digikey.com/Volume0/opasdata/d220001/medias/docus/307/WM8960.pdf).

The codec now advertises the nonstandard-rate bit and applies an explicit
nine-rate constraint list at startup, with 8–48 kHz bounds. In this older ALSA
version, `SNDRV_PCM_RATE_8000_48000` alone excludes 12 and 24 kHz. The verifier
checks the emitted mask, bounds, callback registration and constraint data;
actual ALSA negotiation remains outside its model.

For the documented 19.2 MHz input, nominal outcomes are:

| Rate (Hz) | SYSCLK (MHz) | BCLK (MHz) | Class-D clock (kHz) |
| --- | --- | --- | --- |
| 8,000 | 11.264 | 0.512 | 704 |
| 11,025 | 11.2896 | 0.7056 | 705.6 |
| 12,000 | 12.288 | 0.768 | 768 |
| 16,000 | 12.288 | 1.024 | 768 |
| 22,050 | 11.2896 | 1.4112 | 705.6 |
| 24,000 | 12.288 | 1.536 | 768 |
| 32,000 | 12.288 | 2.048 | 768 |
| 44,100 | 11.2896 | 2.8224 | 705.6 |
| 48,000 | 12.288 | 3.072 | 768 |

The verifier derives these rates independently from the resulting register
values using rational arithmetic, allowing less than 1 ppm of PLL quantization
error. It also checks matching ADC/DAC divisors, codec word size, ALC rate,
SAI frame size, slot width and first-bit position. The saved 48 kHz/16-bit ASRC
backend now uses 64 serial clocks per frame; its former configuration used 32.

PLL setup shares one checked factor calculation for availability and programming,
including the input predivider, exact odd-frequency arithmetic and rounding
carry into the integer divider. The inverted availability rejection is removed.
The temporary factor object is local rather than shared static state. The public
upstream 250 ms settling interval is restored before selecting the powered PLL;
the model checks call ordering, not actual lock time.

Codec parameters publish the active flag only after configuration succeeds.
Matching duplex setup reuses the shared clock; a rate/width conflict returns
`-EBUSY` before changing codec registers. Board conflicts still return `-EINVAL`.
Invalid PLL arguments and zero MCLK return errors. Setup propagates register
errors and attempts to stop the PLL on failure, preserving the original error
even when cleanup also fails. It cannot promise electrical rollback on a failed
control bus. CPU-master operation retains its prior clock policy and is not
covered by the expanded clock matrix.

## Write-only register cache

The public NXP `regmap.c` updates its cache before sending a write to the bus.
Its ordinary `update_bits` can therefore skip a needed retry after a bus error.
The cache-aware model detects successful-looking retries that leave hardware
at the old value.

`regmap_force_dreem.inc` adds an internal built-in helper that reads the cached
value and always sends the resulting write while holding the existing regmap
lock. Codec clock setup and rate-dependent filter updates use it. This avoids
an unlocked read/write pair and preserves unrelated bits. The helper is compiled
only with `CONFIG_DREEM_WM8960`, is not exported to modules, and leaves existing
regmap APIs unchanged. Its return convention matches ALSA update operations:
negative error, zero unchanged, one changed.

The test separates cached and modeled hardware values, including failed writes
that already updated the cache. It injects one-shot and persistent errors at
every codec setup read/write, checks released locks and inactive stream state,
and requires a subsequent successful retry to agree with modeled hardware.
Regmap cache/bus internals remain models; the new helper's ARM instructions and
its lock/unlock callbacks execute in the test.

## Build and verification

Build with the command in [board integration](audio-lifetime.md). Also build
the unchanged codec reference using [source reconstruction](audio-findings.md).
All generated artifacts remain outside the repository.

```sh
/private/work/venv/bin/python development/verify_wm8960_clocking.py \
  /private/work/audio-kernel/kernel/vmlinux \
  /private/work/audio-kernel/kernel/sound/soc/fsl/imx-wm8960.o \
  /private/work/audio-kernel/kernel/sound/soc/codecs/wm8960.o \
  /private/work/audio-kernel/kernel/sound/soc/fsl/fsl_sai.o \
  /private/work/audio-kernel/kernel/drivers/base/regmap/regmap.o \
  /private/work/inspection/kernel.elf \
  /private/work/wm8960-reference/kernel/sound/soc/codecs/wm8960.o \
  /private/work/kernel-build-gcc7/vmlinux \
  /private/work/kernel-build-gcc7/sound/soc/fsl/fsl_sai.o \
  > /private/work/audio-kernel/clock-verification.json
```

Verified offline on October 3, 2026 (America/New_York): **305 scenarios** cover
144 rate/width/channel/direction combinations, 100 codec I/O failure/retry cases,
ten board startup rollback/retry cases, fourteen DAI callback errors, zero MCLK,
duplex isolation, sixteen independent PLL-factor checks including odd inputs,
three rounding boundaries, five invalid PLL pairs, explicit/default SAI slots,
five rate-constraint/registration checks and three original-driver negative controls.

The verifier compares 17,340 codec bytes and 6,644 SAI bytes against their linked
kernel contents with relocation/string validation. It separately verifies and
executes the 140-byte forced-write helper. Negative controls reproduce the old
codec's valid-PLL rejection and failed-parameter active flag, and the old SAI's
ignored explicit slot width. The matching reference codec is still compared
against the saved firmware.

The complete kernel and ADC module build, and all 44 ADC imports match the
rebuilt kernel. The 58 PCM, 58 SAI lifetime, 50 board-lifetime, 43 identity,
76 connected EEG, 617 bus-frequency and 58 DDR preparation checks also pass on
this build. The earlier SAI milestone verified that, with the audio option
disabled, board/codec/SAI objects are byte-identical to the existing NXP baseline.
Its disabled regmap object is byte-identical to a clean
NXP control compiled at the same source path; its warning strings embed
`__FILE__`, so a different source-directory path changes the full object.

The [board integration record](audio-lifetime.md) owns the kernel and board
hashes. The enabled codec SHA-256 is
`5ce5fc4d3d30831fa048fb97f6758ea98564194ca52b134f5cd7828d9ed0f840`;
SAI is `11753a7c76bed9a9cb3fe1337818e542c1e45cbf328ef83b74a77c2c5275b5af`;
regmap is `59c939288e154b6e1950367791a082514fa2960b7354217212edd69684ebb5ad`.
Private manifests pin the source inputs, artifacts and verifier dependencies.

[SAI startup and shutdown](sai-lifetime.md) now have separate resource rollback
and connected callback tests on this kernel. The [PCM cyclic-path verification](pcm-dma.md)
corrects this record's earlier packed-20 claim: PCM requests a three-byte bus
width, and cyclic SDMA preparation accepts it. The rejection previously cited
belongs to a different scatter/gather path. Actual sample packing/transfer,
ALSA negotiation/unwind internals, SAI parameter/trigger register-error handling,
power management, concurrent scheduling, physical clock timing, analogue output
and recording fidelity remain unqualified. These callback tests do not establish
complete `aplay`/`arecord` support.
No kernel was flashed;
the known headset SSH endpoint timed out during this work.
