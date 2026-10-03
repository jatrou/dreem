# PCM DMA submission and trigger results

The research audio build now checks a DMA submission cookie before publishing
it and returns DMA pause, resume and termination errors from the PCM trigger
callback. Compiled checks execute the connected PCM submission, virtual DMA
queue and SDMA channel-start instructions. This is a prerequisite for reliable
audio error handling, not a completed SAI trigger or DMA shutdown repair.

## Implementation and scope

`apply_pcm_audio_overlay.py` modifies the public NXP
[`sound/core/pcm_dmaengine.c`](https://github.com/nxp-imx/linux-imx/blob/30278abfe0977b1d2f065271ce1ea23c0e2d1b6e/sound/core/pcm_dmaengine.c)
under `CONFIG_DREEM_WM8960`:

- START prepares and submits a descriptor, checks `dma_submit_error`, then
  publishes the successful cookie, resets the position and calls issue-pending.
  Failed preparation or submission preserves the previous cookie and position
  and does not call issue-pending.
- PAUSE and pause-capable SUSPEND return the DMA pause result. RESUME and
  PAUSE_RELEASE return the resume result. STOP and other SUSPEND operations
  return the termination result. Missing callbacks retain the DMA API's
  `-ENOSYS`; invalid commands return `-EINVAL`.

These core PCM changes apply to every DMAengine PCM user in a kernel with the
research audio option enabled. They are not restricted to a single SAI instance.
With that option disabled, the original implementation remains selected.

The caller does not attempt to free a descriptor after submitting it. Submission
hands descriptor management to the provider; this old API supplies no generic
client-side release operation for that failure. The injected submission-error
cases establish caller behavior, not provider cleanup. The actual NXP virtual
DMA submit implementation assigns a cookie and succeeds; its negative-cookie
path is modeled as an API robustness check, not an observed SDMA failure.

## Verification

Build with the [audio integration command](audio-lifetime.md), retaining a build
from commit `9fefed40b67be8a268a2460de1c9882d4b5b6d3e` for negative controls:

```sh
/private/work/venv/bin/python development/verify_pcm_trigger.py \
  /private/work/audio-kernel /private/work/pre-trigger-kernel \
  > /private/work/audio-kernel/pcm-trigger-verification.json
```

Verified offline on October 3, 2026 (America/New_York): **69 cases** pass:

- Sixteen playback/capture, sample-width and channel-count combinations execute
  actual PCM configuration, cyclic descriptor preparation, virtual DMA cookie
  assignment/list insertion and SDMA issue-pending. They check callback pointers,
  byte counts, active descriptor ownership, control-block pointers and the final
  modeled channel-enable write.
- Six submission failures and two descriptor-allocation failures preserve the
  previous runtime values without issuing the channel.
- Thirty-six cases cover control dispatch, success, errors and missing callbacks;
  three more cover invalid commands.
- Two sequences execute actual SDMA pause/resume and failed context restoration.
  The existing SDMA resume routine converts a context-load failure to `-EINVAL`;
  PCM now returns it without enabling the channel or changing its paused status.
- Four previous-build controls reproduce the old publication of a failed cookie,
  the subsequent call to issue-pending, and ignored stop/pause/resume errors.
  The failed-submit fixture does not enqueue a descriptor, so it does not claim
  that this old issue-pending call starts a physical transfer.

Whole-object relocation/string comparisons cover 23,827 SDMA, 1,276 core PCM,
648 i.MX PCM and 744 virtual DMA function/table bytes in the linked kernel.
Allocation, MMIO, channel-zero transactions and injected control errors remain
models. Successful submission uses the actual virtual DMA and SDMA instructions;
real pause/resume checks cover driver state and writes, not hardware quiescence.
No ROM transfer script, sample payload, completion IRQ or scheduler is executed.

The separate [PCM lifetime verification](pcm-lifetime.md) now passes 37 cases.
All ten earlier verification reports also pass against their stated inputs;
the original codec reference remains a separate comparison against stock.
The same-path disabled comparison produces byte-identical core and i.MX PCM
objects with and without this submission/control overlay, retaining the EEG
option. The disposable source was restored after that comparison.

[PCM preparation](pcm-dma.md) owns the SDMA/core/i.MX PCM hashes and
[board integration](audio-lifetime.md) owns the kernel hash. The virtual DMA
object SHA-256 is
`e381fc7a08535e95bec68aada9a63b9859e8e99a1d6d7b9a6d96d0b680cd4b71`.
Private manifests bind build-source inputs, artifacts and reports;
[direct-link trigger verification](soc-trigger.md) records the current set.

## Unresolved trigger and termination behavior

The pinned [`soc_pcm_trigger`](https://github.com/nxp-imx/linux-imx/blob/30278abfe0977b1d2f065271ce1ea23c0e2d1b6e/sound/soc/soc-pcm.c)
calls codec, platform DMA, CPU SAI and board callbacks in that order, returning
on the first error. DMA can therefore be running when SAI start fails. A DMA
stop error can also prevent the later SAI stop callback. Returning errors from
PCM alone does not make those multi-component operations transactional.
The [direct-link ASoC repair](soc-trigger.md) now adds rollback for the opted-in
research link and continues stop cleanup after a component error.

The outer [`pcm_native.c`](https://github.com/nxp-imx/linux-imx/blob/30278abfe0977b1d2f065271ce1ea23c0e2d1b6e/sound/core/pcm_native.c)
has different rollback paths: a single-stream action invokes its undo callback
after failure, while linked-stream rollback undoes earlier streams and skips
the stream whose action failed. Its stop action also discards the trigger return.
Consequently, this change does not guarantee that every failure reaches userspace
or that all partially started components are stopped. Those source paths were
inspected, not executed by this verifier.

The old SDMA termination implementation freed descriptor storage before its
stop request. The later [DMA retirement repair](pcm-lifetime.md) now withdraws
callbacks, drains tasklets and defers release through the upstream settling
interval, with process-context synchronization before PCM buffer reuse/free.
These trigger tests still model termination; the lifetime verifier executes it
separately. Physical DMA completion and recovery from a wedged peripheral remain
unqualified. STOP_STAT host-enable bits alone do not prove transfer completion.

SAI trigger/IRQ handling, full ALSA linked-stream/DPCM handling, reconfiguration
without an explicit free, power management, physical clocks, playback and recording remain
unfinished. Nothing was installed or flashed.
