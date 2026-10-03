# SAI stream startup and shutdown

The experimental audio profile now balances SAI runtime-PM references and bus
clocks when stream startup fails. It publishes stream ownership only after the
channel-enable write and rate constraint succeed. This closes a resource leak
in the public NXP driver used as the reconstruction baseline.

## Why startup must clean up

In the pinned NXP `sound/soc/soc-pcm.c`, `soc_pcm_open` calls the CPU DAI first.
If that callback fails, the core skips its shutdown callback. Later platform,
codec or board startup failures do call CPU shutdown. The old SAI driver sets
its open flag before acquiring resources, ignores the runtime-resume result,
and returns directly after bus-clock or rate-constraint failures. Its channel
register update also discards errors.

`kernel/sai_lifetime.inc` repairs those paths under `CONFIG_DREEM_WM8960`:

- Negative `pm_runtime_get_sync` results release the newly acquired reference
  with `pm_runtime_put_noidle`, without attempting another power transition.
  Positive success results are accepted.
- Clock and subsequent setup failures unwind their completed acquisitions and
  preserve the original error, even if channel cleanup or runtime idle fails.
- Failed channel setup and rate constraints attempt to clear that direction's
  channel-enable bit before releasing its clock. Cleanup I/O failures are logged.
- A per-device mutex serializes acquisition and release across direct and ASRC
  links, which can have distinct ASoC runtime mutexes. Existing SAI structure
  members retain their offsets. The mutex is initialized during SAI probe.
- Repeated shutdown of an unopened stream does nothing. Closing one direction
  leaves the other's ownership and clock references intact.
- Shutdown also releases any remaining [parameter-owned MCLK reference](sai-parameters.md)
  before releasing the bus clock and PM reference.

Channel setup and cleanup use the existing internal forced-write regmap helper.
The SAI uses a flat register cache, so an unsuccessful write can update its
cached value before hardware. A retry must send the write even if that cached
value already matches. This helper preserves unrelated bits under the map lock.
Shutdown has no error return in the DAI interface: a failed register cleanup is
reported, but cannot establish that the physical channel was disabled.

The original NXP code remains selected when the research option is disabled.
The original source-match recipes are unchanged.

## Verification

Build the kernel with the [audio integration command](audio-lifetime.md), then:

```sh
/private/work/venv/bin/python development/verify_sai_lifetime.py \
  /private/work/audio-kernel/kernel/vmlinux \
  /private/work/audio-kernel/kernel/sound/soc/fsl/imx-wm8960.o \
  /private/work/audio-kernel/kernel/sound/soc/codecs/wm8960.o \
  /private/work/audio-kernel/kernel/sound/soc/fsl/fsl_sai.o \
  /private/work/audio-kernel/kernel/drivers/base/regmap/regmap.o \
  /private/work/kernel-build-gcc7/vmlinux \
  /private/work/kernel-build-gcc7/sound/soc/fsl/fsl_sai.o \
  > /private/work/audio-kernel/sai-lifetime-verification.json
```

Verified offline on October 3, 2026 (America/New_York): **58 cases** execute
compiled ARM startup/shutdown, a bounded probe prefix, and connected board/codec
callbacks. They cover PM success and failure, pre-existing PM references, clock
failures, one-shot and persistent register errors, cleanup errors, duplicate
opens/closes, duplex isolation, and later platform/codec/board startup failures
followed by retry, parameter setup, free and close. Five controls reproduce the
original NXP driver's ownership leaks and ignored PM/register errors.

The verifier checks all 7,076 emitted SAI function/table/registration bytes
against the linked kernel, validating relocations and strings. Zero-sized BSS
lock-class keys also receive a checked relocation base. It executes the actual
forced-write helper with modeled cache/bus operations and separate lock checks.
The probe check stops after observing mutex initialization, before device-tree
and hardware setup; it does not qualify the full SAI probe.

The 69 PCM trigger, 362 SAI parameter, 58 PCM preparation, 305 clock,
50 board-lifetime, 43 identity,
76 connected EEG, 617 bus-frequency
and 58 DDR preparation cases also pass on the same kernel. All 44 ADC imports
match its real exports. The original SAI milestone's disabled SAI and board
objects are byte-identical to the clean NXP baseline.
[Board integration](audio-lifetime.md) owns the kernel/board
hashes; [clock verification](audio-clocking.md) owns the codec/SAI/regmap hashes.
Private manifests pin build inputs, artifacts and verifier dependencies.

## Remaining boundaries

Runtime-PM services, clocks, mutex behavior, register cache/bus and ALSA callback
ordering are modeled. Actual ALSA core execution, simultaneous scheduling,
physical register writes, recording fidelity and power transitions remain
unqualified. The test tracks the driver's additional PM references separately
from references held by callers; it does not execute runtime-PM internals.

[SAI parameter setup](sai-parameters.md) now propagates register errors and
tracks exact master-clock ownership. [PCM submission/control](pcm-trigger.md)
now propagates DMA errors. SAI trigger/IRQ handling, coordinated DMA termination
and ALSA rollback, reconfiguration without
an explicit free, actual PCM sample transfer and codec bias/power cleanup remain.
[Cyclic DMA preparation](pcm-dma.md) accepts packed 20-bit samples and now has
checked bounds and context-failure cleanup. Fixing
startup does not establish complete playback or capture. No kernel was flashed;
the known headset SSH endpoint timed out during this work.
