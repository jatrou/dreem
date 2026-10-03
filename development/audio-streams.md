# Research audio stream integration

This records the `b58392d` integration milestone. The current research build
adds [clock planning and codec retry repairs](audio-clocking.md); use that
document for current behavior and verification.

At this milestone, the experimental kernel selected the complete [source-matched WM8960
codec](audio-findings.md) together with the [repaired board driver](audio-lifetime.md).
Board stream startup and parameter failures no longer publish ownership before
their operations succeed. The codec still reproduces the saved firmware's
behavior, including clock-selection and error-state defects described below.
This is an offline research build, not a qualified firmware replacement.

## Selection and board changes

`build_sdma_kernel.py --wm8960-board --hardware-version` enables both audio
overlays through `CONFIG_DREEM_WM8960`. The option defaults to disabled. The
board checks Femto identity before binding; the codec selection is at compile
time and has no separate per-device runtime gate. Generated codec source comes
from the existing pinned NXP reconstruction recipe, retaining upstream notices.

`kernel/wm8960_streams.inc` changes the board's stream callbacks:

- Startup checks CPU/board ownership, applies slave-mode rate constraints,
  and acquires MCLK before publishing the opened flag. A failure can be retried;
  duplicate startup does not acquire another clock reference.
- Shutdown releases only an owned clock reference and clears stream state.
  Repeated shutdown does not disable an unowned clock.
- Parameters require an opened stream, a rate within 8–48 kHz and a supported
  sample width. Simultaneous directions must share rate and width before any
  clock or format reprogramming. The active flag is committed after board
  configuration succeeds. Zero MCLK is rejected before PLL programming.
- Free clears the direction's parameters and propagates a codec format error
  when returning the last active direction to slave mode.

These repairs preserve the original PLL target policy: sample rate multiplied
by 768 for 24-bit samples, or by 512 otherwise. They do not fix the codec's own
failure state or prove CPU-DAI cleanup.

## Connected execution and remaining clock limits

`verify_wm8960_streams.py` executes the linked board and codec ARM instructions
together. Its DAI wrappers dispatch to the actual codec routines, including the
fifth PLL argument on the stack. Integer division helpers also execute as ARM
code. SAI, clock services, codec register access and PCM fixtures are modeled.

With the documented 19.2 MHz input and codec-master mode, both saved and research
implementations produce this **software configuration result**:

| Sample rate (Hz) | Widths accepted by both board and codec |
| --- | --- |
| 8,000 | None |
| 11,025 | None |
| 12,000 | None |
| 16,000 | None |
| 22,050 | 24 |
| 24,000 | 24 |
| 32,000 | 16, 24, 32 |
| 44,100 | 16, 32 |
| 48,000 | 16, 32 |

Each combination was compared for playback/capture and mono/stereo. Mono uses
two serial slots in this codec. The saved device tree configures the ASRC
backend for 48 kHz, 16 bits; that combination succeeds in the modeled trace.
This does not establish the currently selected route on a physical device.

The source-matched codec has an inverted availability check in
`wm8960_set_dai_pll`: it rejects a target when `is_pll_freq_available` returns
true. That helper also omits the input predivider used by the actual PLL-factor
routine. The 48 kHz/16-bit target happens to pass the first check and then use
the predivider successfully. The resulting nominal SYSCLK is 12.288 MHz and
BCLK is 1.536 MHz. Conversely, 48 kHz/24-bit fails at the board's PLL call.
At 48 kHz/20-bit the board succeeds, but the codec's clock search fails and
leaves its own active flag set. The public NXP `soc_pcm_hw_params` error path
does not call `hw_free` for the codec whose parameter callback just failed.

These are software restrictions, not a claim that the chip lacks those formats.
The manufacturer's [WM8960 revision 4.4 datasheet, page 65](https://mm.digikey.com/Volume0/opasdata/d220001/medias/docus/307/WM8960.pdf)
shows 24-bit, 48 kHz operation using a 3.072 MHz bit clock, or 64 clocks per
stereo frame. A revised clock plan needs coordinated codec/SAI slot widths and
device measurements. It is not implemented here.

## Reproduction and evidence

To reproduce this historical milestone, first select its implementation in an
isolated checkout:

```sh
git worktree add --detach /private/work/stream-reference b58392d
cd /private/work/stream-reference
```

Build the kernel using that checkout's [board integration](audio-lifetime.md), and build
the separate matched board reference as shown in [source reconstruction](audio-findings.md).
Then run:

```sh
/private/work/venv/bin/python development/verify_wm8960_streams.py \
  /private/work/inspection/kernel.elf \
  /private/work/audio-kernel/kernel/vmlinux \
  /private/work/audio-kernel/kernel/sound/soc/fsl/imx-wm8960.o \
  /private/work/audio-kernel/kernel/sound/soc/codecs/wm8960.o \
  /private/work/wm8960-board-reference/kernel/sound/soc/fsl/imx-wm8960.o \
  > /private/work/audio-kernel/stream-verification.json
```

Verified offline on October 3, 2026 (America/New_York): **188 scenarios pass**.
They cover 144 stock/research register-trace comparisons, 20 boundary/ownership/
free-error checks, ten startup rollback/retry cases, nine board-parameter error
cases, duplex conflict isolation and balanced shutdown, zero MCLK, two stock
startup negative controls, and the remaining codec active-flag defect.

Before running scenarios, the verifier compares all 16,796 file-backed codec
bytes against both the saved kernel and the new linked kernel, with relocation
and string validation. The separate complete source verifier and its seven
negative controls also pass. The board-lifetime, identity, EEG pipeline,
bus-frequency and DDR checks pass on this build as recorded in
[board integration](audio-lifetime.md).

The enabled codec object SHA-256 is
`e48186273e13aaa98178de047938b2a7dc510722aab23642eb3bd62e3b56ac5c`.
With the option disabled, both full audio objects are byte-identical to the
unmodified NXP baseline; the codec object's SHA-256 is
`52a93f5850097a3bac35c9731a451d725454aa439ea46b2a5979bf6c3ffd2e8e`.
Private reports pin input artifacts, source dependencies and verifier versions.

ALSA core execution, concurrent scheduling, CPU-DAI failure cleanup, power
management, PLL settling, register-I/O failures, analogue output and recording
fidelity remain unqualified. The codec's inherited removed PLL delay is still
present in the matching recipe. No kernel or module was installed or flashed;
the headset's known SSH endpoint timed out during this verification.
