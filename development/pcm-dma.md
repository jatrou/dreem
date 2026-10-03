# PCM configuration and cyclic DMA preparation

The research audio profile now bounds cyclic SAI requests before allocation,
matches ALSA's period limit to the SDMA descriptor limit, and releases descriptor
storage when loading a channel context fails. Packed 20-bit samples remain
advertised. These changes establish configuration and descriptor behavior in
compiled ARM code; they do not establish physical sample transfer.

## Corrected source interpretation

The earlier clocking record incorrectly attributed the scatter/gather path's
three-byte-width rejection to PCM. The pinned NXP PCM path is:

1. `imx_pcm_dma_prepare_slave_config` calls the core PCM configuration helpers.
2. Physical sample storage is two bytes for S16_LE, three for S20_3LE, and four
   for S24_LE/S32_LE. SAI supplies FIFO addresses and a six-word burst count,
   without overriding that width.
3. `sdma_config` records the width and converts the burst count to a byte
   watermark. PCM submission uses `sdma_prep_dma_cyclic`, which accepts width
   three and writes descriptor command 3. Four-byte transfers use command 0.

`check_bd_buswidth` belongs to the scatter/gather path, not this cyclic path.
The generic DMA-width format filter is also bypassed when the i.MX driver
provides its own `pcm_hardware` table. Neither mechanism justifies removing
S20_3LE or adding software conversion here. Three-byte samples do not imply
that the absolute buffer address must be divisible by three.

The source baseline is [NXP linux-imx, revision
30278abfe0977b1d2f065271ce1ea23c0e2d1b6e](https://github.com/nxp-imx/linux-imx/tree/30278abfe0977b1d2f065271ce1ea23c0e2d1b6e).
The relevant files are `sound/core/pcm_dmaengine.c`,
`sound/soc/fsl/imx-pcm-dma.c`, `sound/soc/soc-generic-dmaengine-pcm.c` and
`drivers/dma/imx-sdma.c`. Descriptor command acceptance alone does not prove
the exact headset ROM script's packing or physical FIFO behavior.

## Repairs

`apply_pcm_audio_overlay.py` applies after the EEG SDMA overlay to a disposable
source copy. Under `CONFIG_DREEM_WM8960`, SAI cyclic preparation rejects invalid
directions, zero sizes, periods above 65,532 bytes, nonintegral period counts,
signed loop-counter overflow, descriptor allocation-size overflow, a wrapping
32-bit DMA address range, unsupported widths, and partial samples. These checks
run before division, allocation, or channel changes.

The old loop allocates `floor(buffer / period)` descriptors but can write
`ceil(buffer / period)` entries. The verifier reproduces that overrun with a
malformed request. Normal PCM open already installs an integer-period constraint,
so this is API hardening, not evidence that valid ALSA negotiation reaches the
overrun.

The shared i.MX PCM table's maximum period changes from 65,535 to 65,532 bytes
when the research audio option is enabled. Previously, a mono packed-20 period
could satisfy the advertised size limit but fail SDMA preparation. This table
change applies to the selected i.MX PCM profile, not only one SAI instance.

For SAI channels, a failed context transaction leaves `context_loaded` false.
If it fails after allocating a descriptor buffer, that buffer is released before
the descriptor, including its allocation accounting. Other peripheral types
retain their existing behavior. This does not repair controller timeout recovery:
the experimental EEG provider can latch a channel-zero failure until controller
recovery, which remains unqualified.

## Verification

Build with the [audio integration command](audio-lifetime.md). Preserve a build
from commit `b8095b57d8111211b7f04a002d098d9d46ca01c4` for negative controls:

```sh
/private/work/venv/bin/python development/verify_pcm_dma.py \
  /private/work/audio-kernel /private/work/pre-pcm-kernel \
  > /private/work/audio-kernel/pcm-verification.json
```

Verified offline on October 3, 2026 (America/New_York): **58 cases** cover
32 direction/width/channel/period combinations, four maximum-period cases,
two context-failure sequences, fourteen malformed requests, descriptor
allocation failure, the advertised period limit, and four old-driver controls.
Those controls reproduce the descriptor overrun, advertised-but-rejected period,
failed-context flag and descriptor-buffer leak.

The context sequences include persistent failure, released allocations, and a
subsequent modeled successful channel-zero transaction. They do not simulate a
real controller recovering from a latched timeout. Actual linked instructions
execute PCM configuration, SDMA channel/context preparation, cyclic descriptor
construction and release. The verifier compares all 22,975 SDMA, 1,240 core PCM
and 648 i.MX PCM function/table bytes with the linked kernel, including checked
relocations and strings. ROM entry addresses come from the public i.MX6UL table;
the headset's active ROM/firmware contents are not verified by this fixture.

The same kernel passes the 58 SAI lifetime, 305 clock, 50 board lifetime,
43 identity, 76 connected EEG, 617 bus-frequency and 58 DDR preparation cases.
The original codec source matcher also passes its seven negative controls after
the shared object matcher was extended to accept objects without init/exit text.
All 44 ADC module imports match the rebuilt kernel exports.

With only the research audio option disabled, both PCM objects are byte-identical
to pre-overlay controls compiled at the same source path and configuration.
The SDMA object differs only in debug information and line records; after removing
debug sections, the entire objects are byte-identical. The EEG option remains
enabled in this comparison. Disposable source files were restored afterward.

The enabled object SHA-256 values are:

| Object | SHA-256 |
| --- | --- |
| SDMA | `399d275f6196f8fe4408230d8e2ad9415d695ec09fbe7ebabf92c67bf1f2bb82` |
| Core PCM | `2f984b2f8575a3986fcacc41be6830c6eb160fb76d6c7b46375f9c26d7ef0536` |
| i.MX PCM | `2e3da9b4ffd70d4fd0f4d7d515d12b904ddbf81069bbaa5189ecc6a6f432967c` |

[Board integration](audio-lifetime.md) owns the kernel hash. Private manifests
pin all 35 build-source inputs, 11 artifacts, verifier sources and reports.

## Remaining boundaries

Allocation services, register access and channel-zero hardware transactions are
modeled. Buffer allocation covers the successful IRAM path, not DMA-allocation
fallback. The checks do not execute the ROM transfer script, submit DMA, deliver
sample payloads, schedule completion IRQs, or qualify residue/pause behavior.
SAI parameter/trigger errors, codec bias/power transitions, physical clocks,
analogue output and recording fidelity remain unfinished. Nothing was flashed.
