# SAI parameter setup and clock ownership

The experimental audio profile checks SAI format, clock-selection and frame
register operations before publishing successful configuration. A failed
parameter setup releases its newly acquired master-clock reference. Repeated
free or shutdown releases only references actually owned by that direction.
This is source-built ARM behavior with modeled services, not physical audio proof.

## Configuration behavior

`kernel/sai_parameters.inc` adapts the pinned public NXP SAI driver under
`CONFIG_DREEM_WM8960`. Format parsing supports the original I2S, left-justified,
DSP A/B, clock inversion, bit-order and master/slave combinations. It validates
the format before register changes and publishes `is_slave_mode` and
`is_dsp_mode` only after both directions' writes succeed. Selecting I2S after
DSP now clears the old DSP flag.

Format, clock-source and frame updates use the existing checked forced-write
helper. A bus failure can leave the register cache ahead of hardware; a retry
must transmit the write even if the cache already holds the requested value.
Channel-mask writes are unconditional and their errors are also propagated.
These operations preserve unrelated register bits. An I/O failure can still
leave partially programmed physical registers; the error is returned rather
than claiming electrical rollback.

Non-atomic setup/free callbacks use the existing per-SAI stream mutex, including
calls through separate direct and ASRC links. Parameter setup requires an open
stream, supported sample widths, one or two channels, valid two-slot geometry
and a nonzero rate within the CPU DAI's range. The board/codec's narrower rate
advertisement remains unchanged. Unrequested slave-mode slots retain the original
sample-width behavior; explicitly requested slots retain their selected width.

For CPU-master operation, clock planning keeps the public divider range and
frequency-deviation rule, but holds its selected clock locally until setup
succeeds. Clock-selection register failures and clock preparation/enable errors
propagate. Every successful master stream holds its own reference to the actual
clock pointer, even when both directions use the same source. Free uses that
pointer rather than mutable mode or selector fields. Shutdown also releases
leftover parameter resources before releasing the bus clock and PM reference.

Synchronous streams must agree on rate and word width. A second stream reuses
the shared clock without reprogramming its selector/divider or an already
configured peer's frame registers. Asynchronous directions retain separate
clock selection. Both directions synchronizing to an external SAI while also
requesting CPU-master clock generation is rejected.

Identical repeated parameters acquire no extra resources. Changing an already
configured direction's parameters requires `hw_free` first. Format and slot
changes are rejected while either direction remains configured; an identical
format or explicit slot request is accepted. Output sysclk changes also require
freed parameters. This explicit-free restriction is a research-profile limit:
the generic PCM core permits parameter calls in SETUP/PREPARED states without
necessarily freeing the old configuration first. Trigger-aware reconfiguration
and complete ALSA application compatibility remain unfinished.

## ALSA failure ownership

The pinned `sound/soc/soc-pcm.c` calls board, codec, CPU DAI and platform
`hw_params` in that order. If the CPU callback fails, this inner routine skips
CPU `hw_free`, while unwinding the completed codec and board. The new CPU callback
therefore releases its own new acquisition immediately. The outer
`sound/core/pcm_native.c` parameter-error path subsequently calls the platform's
full `hw_free`; cleanup remains idempotent for that later call. Direct/DPCM
callback paths do not all traverse the same outer routine.

The trigger path is a separate unresolved boundary: the pinned ASoC routine
invokes the DMA platform before the CPU DAI. Propagating a CPU trigger error
alone would not establish that previously started DMA was stopped. The later
[PCM submission/control repair](pcm-trigger.md) checks DMA errors,
and [DMA retirement](pcm-lifetime.md) now orders deferred cleanup.
[Direct-link ASoC rollback](soc-trigger.md) now uses that cleanup, and
[SAI control](sai-control.md) reports register errors and stop timeouts. Full ALSA
linked-stream/DPCM handling and physical DMA stop timing remain unresolved.

## Build and verification

Use the [audio integration build](audio-lifetime.md), then compare with the
previous build from `eece17bb90eefa1f6e2373eac92c099c479a17c6`:

```sh
/private/work/venv/bin/python development/verify_sai_parameters.py \
  /private/work/audio-kernel /private/work/pre-parameters-kernel \
  > /private/work/audio-kernel/sai-parameters-verification.json
```

The verifier executes the linked SAI routines and forced-write helper with
separate register-cache and hardware dictionaries, clock reference counts, and
lock checks. It exercises the supported format combinations, every observed
setup I/O failure with one-shot and persistent errors, subsequent successful
retry, all three synchronization configurations, master-clock failures, duplicate
opens, duplex conflicts and cleanup. Connected checks execute real board/codec
callbacks with modeled ALSA error ordering. Previous-driver controls reproduce
ignored parameter writes, stale DSP mode and mode-dependent clock leakage.

Verified offline on October 3, 2026 (America/New_York): **362 cases** pass,
including three previous-driver controls. Full object comparisons cover all
8,288 emitted SAI function/table/registration bytes and the linked codec. The
37 PCM lifetime, 69 PCM trigger, 58 SAI lifetime, 305 clock, 58 PCM preparation, 50 board lifetime,
43 identity, 76 connected EEG, 617 bus-frequency and 58 DDR preparation cases
also pass on this kernel.
The original codec source matcher retains all seven negative controls, and all
44 ADC imports match the rebuilt kernel exports.

At the parameter milestone, with the research audio option disabled, complete
SAI and board objects were byte-identical to the preceding source controls at
the same path/configuration and matched the recorded NXP baseline hashes. Disposable source was
restored after the comparison. Private manifests bind source inputs, artifacts
and reports; [SAI control verification](sai-control.md) records the
current set. [Board integration](audio-lifetime.md) owns the
kernel/board hashes; [clock verification](audio-clocking.md) owns the
codec/SAI/regmap hashes.

Runtime-PM, clocks, register transactions, locks and ALSA ordering remain models.
These checks do not execute the ALSA core, concurrent scheduling, trigger/IRQ
paths, physical clock generation or DMA transfers. The separate [SAI control
verifier](sai-control.md) covers trigger/IRQ and connected ASoC rollback.
Codec bias/power transitions,
actual playback and recording fidelity still require work. Nothing is flashed.
