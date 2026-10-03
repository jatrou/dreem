# Research WM8960 board integration

The reconstructed WM8960 board driver now builds into the experimental kernel
with repaired jack publication and resource ownership. This is an implementation
derived from the [matched board reference](audio-findings.md), not a claim that
the original defects are acceptable or that physical audio is qualified.

## Selection and interface

`build_sdma_kernel.py --wm8960-board --hardware-version` selects
`CONFIG_DREEM_WM8960=y`, selecting the board, repaired codec, explicit SAI slots,
checked forced-write helper, [SAI parameter ownership](sai-parameters.md),
[PCM cyclic-path repairs](pcm-dma.md), [submission/control checks](pcm-trigger.md)
and [DMA retirement](pcm-lifetime.md), with [direct-link trigger rollback](soc-trigger.md).
The option defaults to disabled and requires built-in
`SND_SOC_IMX_WM8960` and the checked hardware-identity API. When selected, this
board driver is active at probe; unlike the experimental SDMA and bus-frequency
paths, it does not require a second runtime switch. It replaces the NXP board
implementation in that configuration and accepts only the Femto machine with
a valid nonzero hardware identity. Identity errors, including deferred OTP
initialization, propagate without creating the jack node.

The node is still `/dev/jack`, with plain ioctl commands 5 and 6 retaining the
observed active-low speaker/headphone routing and ignored argument. It now uses
a misc device with a dynamic minor and initial permissions 0600, rather than
the original separately allocated character-device major/class. Programs must
open the node by path rather than rely on the old major or class. Unsupported
commands return `-ENOTTY`; unavailable or stale handles return `-ENODEV`.

The driver permits a single owner. Each binding receives a monotonically
increasing epoch, captured when a file opens. An ioctl holds the jack mutex
while checking readiness and the epoch and while using sound-card pointers.
An old descriptor cannot operate on a subsequent binding. Epoch exhaustion
returns `-EOVERFLOW` rather than wrapping.

## Startup and cleanup

The implementation registers the sound card, control, jacks, GPIOs and sysfs
attributes before publishing the misc device and finally setting readiness.
An open arriving inside misc-device publication returns `-ENODEV`. Required
resources and registration errors stop probe and unwind its completed stages.
In particular, failed `snd_ctl_add` is treated as having freed its argument.
The codec late-probe register update now propagates a negative error.

Removal first withdraws readiness under the mutex, waiting for an active
ioctl to finish. It then deregisters the misc device without that mutex held:
the kernel's `misc_open` takes the misc lock before entering our open method,
so the opposite ordering during deregistration could deadlock. Sysfs readers
are drained, then jack GPIO IRQs/work are freed synchronously while their
sound card and controls still exist. Only then is the sound card unregistered.
Pointers are cleared and ownership released after cleanup.

Clock and device references belong to this board binding. The original codec
device's devres lifetime no longer owns the board's clock reference. Each
instance also has its own DAI link array, copied from the NXP template before
ASoC can mutate it. Cleanup attempts to restore the previous masked audio GPR
field and reports an error if that hardware operation fails. The current
implementation requires the headphone-detect GPIO present in the reviewed
device tree; an absent microphone GPIO is supported, as are separate and
shared microphone/headphone GPIOs.

## Build and verification

```sh
/private/work/venv/bin/python development/build_sdma_kernel.py \
  /private/work/linux-imx /private/work/kernel-build-gcc7 \
  /private/work/inspection/kernel.elf /private/work/audio-kernel \
  /private/toolchain/bin/arm-linux- \
  --busfreq-policy --hardware-version --wm8960-board

/private/work/venv/bin/python development/verify_wm8960_lifetime.py \
  /private/work/audio-kernel/kernel/vmlinux \
  /private/work/audio-kernel/kernel/sound/soc/fsl/imx-wm8960.o \
  > /private/work/audio-kernel/lifetime-verification.json
```

Verified offline on October 3, 2026 (America/New_York). The complete research
kernel and ADC module build, with all 44 ADC imports matching the actual kernel
exports. The new verifier executes the linked ARM board routines and passes
**50 scenarios**:

- 28 injected startup failures, each followed by resource checks, rejected
  opens, successful retry and removal with modeled pending GPIO work.
- Three hardware-identity errors/unsupported-version cases and one unsupported
  ASRC-width case.
- Twelve combinations of ASRC presence, microphone topology and polarity,
  checking commands, duplicate-probe rejection, wrong-owner removal, unmapping
  the old card, rebinding, and old/new file-handle behavior.
- Epoch exhaustion and two codec late-probe return cases.
- Two instruction mutations that bypass readiness or epoch checks; the
  verifier detects unsafe old-handle access in each case.
- Platform-driver callback pointers selecting the repaired probe and removal.

The GPIO-drain model executes the actual board status callback before completing
the modeled drain. Misc registration similarly invokes the compiled open method
before readiness. Kernel allocation, sound-card registration, GPIO services,
clock/register operations and locking remain models; the verifier does not
execute ALSA registration internals or a concurrent scheduler.

On this same kernel, the hardware-identity verifier passes 43 cases, the connected
EEG pipeline 76, bus-frequency checks 617 and DDR preparation 58. With the audio
option disabled, the parameter milestone verifies that the complete board
object is byte-identical to the NXP baseline,
SHA-256 `c77b315cd7089622dbb916d55bf2c5a36f16a42c555a836f3d469423d4596c50`.

The enabled kernel SHA-256 is
`86ca3b72f86a86002bd8f80c54991dd8658f77be0ce080551a48b995532855a1`;
the board object SHA-256 is
`bda592e54d90509f6d517b6da622158c359592bc74b63920e40ca52a540d0d14`.
Private build/verification manifests pin the exact sources, inputs and artifacts.
This build also includes the [connected codec/SAI clock and retry repairs](audio-clocking.md),
the [SAI startup/close repairs](sai-lifetime.md),
[SAI parameter ownership](sai-parameters.md), and
[PCM cyclic-path repairs](pcm-dma.md), [submission/control checks](pcm-trigger.md)
and [DMA retirement](pcm-lifetime.md), with [direct-link trigger rollback](soc-trigger.md),
whose verification is recorded separately.

## Remaining work

Board stream startup now publishes state only after clock acquisition succeeds.
The codec clock and failure-state repairs are covered by the linked clock
verifier; SAI startup/shutdown now has checked rollback and connected tests.
PCM configuration and descriptor preparation now have connected checks.
SAI parameter writes and clock ownership now have separate checks.
[PCM submission/control](pcm-trigger.md) and [DMA retirement](pcm-lifetime.md)
now have connected checks. [Direct-link ASoC rollback](soc-trigger.md) also
has connected DMA checks. [SAI control and IRQ handling](sai-control.md) now
checks errors and failed-stop clock retention. Physical DMA stop timing,
full ALSA linked-stream/DPCM handling, reconfiguration without
an explicit free, actual PCM sample transfer, physical SAI/codec interaction,
power management, independent codec/controller unbind, audible output and
recording fidelity remain to be qualified. The old ASoC core does not make
independent component removal safe merely because this board's own teardown
is repaired. Nothing was installed or flashed; the known headset SSH endpoint
timed out during this work. These offline checks do not establish bootability
or physical timing.
