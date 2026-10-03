# SAI trigger, interrupt and failed-stop ownership

The research SAI path now checks register operations, keeps trigger and interrupt
updates under one atomic lock, and reports a stop timeout instead of resetting
a direction that has not stopped. A real SAI error now reaches the
[direct-link ASoC rollback](soc-trigger.md) and its DMA retirement path.
This is compiled-code and register-model evidence; physical audio remains
unqualified.

## Register and clock evidence

The i.MX6ULL reference manual, revision 1, chapter 45, identifies three relevant
behaviors. TCSR/RCSR status bits WSF, SEF and FEF are cleared by writing one;
the FIFO request/warning flags are read-only. TE/RE and BCE can remain set
until the current frame ends after software clears them. FIFO reset is allowed
with the direction disabled or its FIFO error flag set. Software reset resets
internal logic and status, and remains asserted until software clears it.

Section 45.3.3 recommends enabling the synchronous clock provider last and
disabling it first. A direction can therefore remain enabled after its own
stream stops if the peer still needs its clocks. The public
[Linux v6.6 SAI source](https://github.com/torvalds/linux/blob/v6.6/sound/soc/fsl/fsl_sai.c)
also distinguishes the active stream from its synchronous clock provider and
clears BCE when disabling this hardware. Its fetched SHA-256 is
`94be2544e08669211826df0b661f6584cbf20b13e6020e3424a8c282d9217509`.
The research implementation checks operations that remain unchecked in that
reference; it is not a complete modern-driver backport.

The pinned NXP MMIO regmap sets `fast_io = true` and uses atomic clock
enable/disable calls for bus access. Its clock-enable errors can propagate as
register-read/write errors. The control lock does not contain a mutex, a DMA
worker flush, or a sleeping clock-prepare operation.

## Control and failure behavior

`kernel/sai_control.inc` is selected only by `CONFIG_DREEM_WM8960`. Probe
initializes its spinlock before requesting the IRQ. Setup/close take the stream
mutex before that control lock; trigger/IRQ take only the control lock.

Start validates open/configured state and channel count before register access.
It configures synchronous mode when no peer is running, primes transmit data
before enabling DMA requests, enables only the required directions, and
publishes running state after the final request/interrupt-enable write succeeds.
Repeated starts do not prime again. Control read/modify/write operations strip
status and reset bits, so changing enable bits does not acknowledge interrupts.

On failure, start preserves its original error and attempts to stop its partial
state while preserving an active peer. Stop clears the stream's requests and
interrupts, disables unneeded directions in clock-source-first order, then
checks both TE/RE and BCE. It polls at most 100 times with a 10 microsecond
delay per iteration. Directions confirmed stopped receive software reset;
the driver attempts to clear reset even if asserting it reports an error.
A failed read or timeout prevents reset of that direction. Other cleanup
steps continue so one error does not suppress all cleanup.

Failed cleanup and IRQ I/O failures latch a controller error. New starts return
that error until a successful stop has quiesced both streams. This deliberately
does not claim recovery of one stream while the shared controller remains
uncertain. The IRQ handler uses each CSR's actual interrupt enables, preserves
unenabled status flags, and resets a FIFO only for its observed FIFO error.
Unexpected read-only FIFO-level interrupts are masked because this DMA driver
does not service FIFO data in the ISR. Failed reads never supply a write value.

Hardware free preserves its owned master clock if stop fails. The void shutdown
callback also retains bus/runtime-PM ownership and marks the stream orphaned.
A later open retries that stop, releases the retained references on success,
and then acquires a fresh stream. Persistent faults continue to reject reopening.
This prevents losing the references needed for recovery; it does not qualify
platform removal or force a hardware reset after a missing frame clock.

## Verification

Use the [research audio build](audio-lifetime.md) and retain a previous build
from commit `b2c3b94c1abc073fffd71dfed8ed00c201ab7a32`:

```sh
/private/work/venv/bin/python development/verify_sai_control.py \
  /private/work/audio-kernel /private/work/pre-sai-control-kernel \
  > /private/work/audio-kernel/sai-control-verification.json
```

The verifier compares the complete linked SAI object and executes its ARM
trigger, ISR, free, shutdown and reopening routines. Connected cases execute
ASoC dispatch, PCM submission, virtual DMA, SDMA controls and deferred retirement.
The CSR model implements W1C/read-only flags, reset strobes, automatic BCE
enable and delayed enable clearing. Register access must occur with interrupts
and preemption excluded; callbacks must restore the entry state. Actual sample
shifting, real elapsed frame time and concurrent interrupt scheduling are not
modeled.

Cases cover both directions, all three valid synchronization modes, active-peer
isolation, each start/stop register failure, ambiguous writes that may have
reached hardware, persistent faults, stop-poll boundaries, actual IRQ enables,
IRQ read/write errors, cleanup/retry and retained clocks after failed close.
Connected rollback covers four sample widths, mono/stereo and both directions.
Previous-driver controls reproduce ignored register errors, accidental W1C
acknowledgement, reset after a stop timeout, and claiming a disabled interrupt.

Verified offline on October 3, 2026 (America/New_York): **430 cases** passed,
including the four previous-driver controls. The complete SAI function/table
comparison covers **8,288 bytes**; the forced-write helper adds 140 executed
bytes. All thirteen earlier reports also passed against the same build or their
explicit reference inputs, including seven codec-comparison negative controls.
The kernel/module build retains all 44 matching ADC imports.

The private qualification manifest binds **41 build-source inputs, 14 artifacts,
14 reports** and their local verifier dependency hashes. The new verifier's full
local import closure has 21 files. [Board integration](audio-lifetime.md) owns
the current kernel/board hashes; [clocking](audio-clocking.md) owns the SAI,
codec and regmap hashes. Existing PCM records own their respective object hashes.

With only `CONFIG_DREEM_WM8960` disabled, complete SAI and board objects match
the previous-source build at the same source path. Their SHA-256 values are
`32f47cb862c0205d44a9bc354ab2bf421533aa529190051fc789e1496cd57188`
and `c77b315cd7089622dbb916d55bf2c5a36f16a42c555a836f3d469423d4596c50`.
All four temporarily substituted source files were restored and hash-checked.

## Remaining boundaries

The modeled 1 ms polling window is bounded software behavior, not a measured
hardware stop guarantee. Missing frame clocks can prevent normal stopping;
the driver retains resources and reports the fault instead of claiming recovery.
IRQ I/O faults latch an error but do not automatically issue an ALSA XRUN or
stop an already-running peer. SAI probe and system-suspend/resume register
handling, full ALSA linked-stream/DPCM/ASRC behavior, independent controller or
codec unbind, physical DMA completion and audio fidelity remain unfinished.
Nothing was installed or flashed.
