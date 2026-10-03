# Audio DMA retirement and PCM lifetime

The research SAI path now requests a channel stop before retiring descriptors,
drains callbacks before freeing them, and synchronizes retirement before PCM
buffer reuse, buffer release and close. This repairs software lifetime ordering.
It uses the upstream stop-settling assumption; physical DMA quiescence has not
been established on the headset.

## Source evidence and timing assumption

The pinned old NXP driver frees descriptors before its stop-register write.
It also leaves `vc.cyclic` pointing at the freed descriptor. Its virtual DMA
tasklet reads that pointer and dereferences the descriptor after dropping the
channel lock, so clearing the pointer alone does not protect a callback that
already captured it.

Current public [Linux SDMA source at revision
d52d42e2e5d9f13166e81ac837ebb023d1306e61](https://github.com/torvalds/linux/blob/d52d42e2e5d9f13166e81ac837ebb023d1306e61/drivers/dma/imx-sdma.c)
requests stop, defers descriptor release to work, waits 1–2 ms, and provides a
synchronization callback. Its comment attributes the 1 ms minimum to NXP R&D.
The fetched source SHA-256 is
`ecc9be87725e20fba600cc01c08509a7b54f054ee1c21ef788b4456d115527c4`.
This is a documented upstream workaround, not a measured bound for a stalled
peripheral or an acknowledgement from the controller.

The i.MX6ULL manual does not justify substituting a simple idle-bit poll:
section 46.8.3 says STOP_STAT reports host-enable bits, and section 46.8.12
says the scheduler's current-channel fields can retain the previous channel
while sleeping. A forced reschedule affects the shared controller. This change
does not introduce a global reset or forced reschedule alongside EEG acquisition.

## Implementation and scope

`apply_pcm_lifetime_overlay.py` runs after the PCM preparation/trigger overlay.
Under `CONFIG_DREEM_WM8960`, SAI termination:

1. Takes the channel lock, marks retirement pending, requests stop and reads
   back the register to flush the posted write.
2. Withdraws the active descriptor and cyclic callback, clears cyclic/resume
   state, invalidates the context, and moves submitted, issued, completed and
   pending descriptors to a retained list.
3. Queues work and returns without freeing DMA storage. Repeated stops while
   the work is pending are idempotent.
4. The worker drains the old virtual DMA tasklet, waits the upstream settling
   interval, releases retired descriptors and only then clears the pending flag.

Configuration and allocation reject a SAI channel during retirement, and
issue-pending cannot enable it. A raw START attempted before synchronization
receives the old PCM preparation failure result, `-ENOMEM`, because that API
returns a null descriptor; it is not evidence of exhausted memory. The normal
PCM prepare path synchronizes first. Direct DMA clients must also synchronize
before reuse; this patch does not support racing submit/start against stop.

The existing NXP `device_wait_tasklet` hook now flushes the retirement worker
and drains callbacks for SAI. Channel release invokes that synchronization
before disabling events or clocks. Other peripheral types retain their old
provider termination behavior, including ASRC; this is not an all-SDMA repair.

The core PCM helper `snd_dmaengine_pcm_sync_stop` terminates, returns a provider
error if present, then invokes the existing synchronization API in process
context. The generic PCM platform uses it before hardware parameters, in prepare,
and before freeing pages. Core close uses it before releasing private runtime
data. Close-and-release closes first, then releases the saved channel pointer.
These core/generic PCM changes apply to all DMAengine PCM users in this research
audio build. They do not change the public DMA-device structure or add a new
DMAengine callback ABI.

## Verification

Use the [audio integration build](audio-lifetime.md) and preserve a build from
commit `1bfb6372b975131a093507cbe7bd893fec128525` for previous-driver controls:

```sh
/private/work/venv/bin/python development/verify_pcm_lifetime.py \
  /private/work/audio-kernel /private/work/pre-lifetime-kernel \
  > /private/work/audio-kernel/pcm-lifetime-verification.json
```

Verified offline on October 3, 2026 (America/New_York): **37 cases** cover:

- Sixteen direction/width/channel combinations: start, queued callback, repeated
  stop, rejection of reuse during retirement, synchronization, restart and
  close/channel release.
- Eight buffer/runtime release sequences with running, queued-callback,
  already-captured-callback and paused streams.
- Two submitted-but-unissued retirements and two actual virtual DMA → SDMA loop
  → PCM period-callback sequences before retirement.
- Two packed-20 parameter calls after stop, four provider-error returns and the
  PCM operation table's prepare/free/close registration.
- Two previous-driver controls reproducing free-before-stop and a queued
  tasklet reading a freed descriptor.

The verifier executes linked ARM PCM/provider/worker/callback instructions.
Workqueue dispatch, tasklet scheduling/drain, DMA API dispatch, allocator, MMIO
and elapsed settling time are modeled. The captured-callback case injects a
callback that would already have escaped the channel lock; it does not execute
a concurrent kernel scheduler. Guards reject reads of released descriptors or
runtime data and buffer/clock release before modeled retirement completes.

Full relocation/string comparisons cover 23,827 SDMA, 1,276 core PCM, 648 i.MX
PCM, 744 virtual DMA and 1,860 generic PCM function/table bytes. The earlier
58-case PCM verifier now obtains old SDMA layout offsets from the old object's
DWARF; it no longer applies the expanded new channel-array layout to an old
kernel. Its four original negative controls remain required.

All eleven previous reports also pass against their stated inputs. The complete
kernel/module build retains all 44 matching ADC imports. With only the research
audio option disabled, core and generic PCM objects are byte-identical to their
same-path previous-source controls. SDMA differs only in debug information;
the complete objects match after stripping debug sections. All disposable source
files were restored after the comparison.

[PCM preparation](pcm-dma.md) owns SDMA/core/i.MX PCM hashes;
[PCM trigger](pcm-trigger.md) owns the virtual DMA hash; and
[board integration](audio-lifetime.md) owns the kernel hash. The generic PCM
object SHA-256 is
`e9be41a45b1a8ae28e8b2f4300892914d17c0200000476023b27a7c6a86639cb`.
Private manifests bind 38 build-source inputs, 13 artifacts and twelve reports.

## Remaining boundaries

The delay does not detect a wedged bus or prove physical transfer completion.
No sample DMA, device boot, full ALSA core, concurrent scheduler or independent
device unbind is qualified here. ASoC trigger rollback, SAI trigger/IRQ errors,
parameter changes without an explicit free, power management, physical clocks
and playback/recording remain unfinished. The SAI trigger can still fail after
platform DMA has started; this retirement mechanism is a prerequisite for that
rollback repair. Nothing was installed or flashed.
