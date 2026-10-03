# Third-party notices

The root Apache-2.0 license and the CC BY 4.0 photograph license apply only to material identified in [`NOTICE`](NOTICE). The following files retain separate provenance and are not relicensed by this project.

## U-Boot-derived recovery files

The following files were produced from or configured for [U-Boot](https://source.denx.de/u-boot/u-boot), which identifies its primary license as GPL-2.0-or-later:

- `recovery/u-boot/ums-boot-assets/*u-boot-with-spl.imx`
- `recovery/u-boot/build-configs/*/generated_defconfig`

The GPL version 2 text is provided in [`LICENSES/GPL-2.0-or-later.txt`](LICENSES/GPL-2.0-or-later.txt). Anyone redistributing the compiled images must independently satisfy the applicable GPL source and notice requirements. The included configurations are research artifacts and do not replace complete corresponding U-Boot source.

## SDMA instruction decoder

`development/sdma_disassemble.py` and `tests/test_sdma_disassemble.py` are
licensed under GPL-2.0-or-later, separately from the root Apache license.
Instruction encodings and operand layouts derive from Eli Billauer's
`mx51_sdma_set.pm`, copyright Eli Billauer, 2011; the test includes his small
published assembler example. See his [SDMA tutorial and assembler](https://billauer.co.il/blog/2011/10/imx-sdma-howto-assembler-linux/)
and the [GPL text](LICENSES/GPL-2.0-or-later.txt).
The decoder implementation is copyright 2026 Dreem research contributors.

`development/sdma_assemble.py` uses those same encoding definitions and is
also GPL-2.0-or-later. The new acquisition program `development/sdma_acquire.asm`,
its instruction model `development/sdma_program_model.py`, verifier
`development/verify_sdma_program.py`, and `tests/test_sdma_program.py` use
GPL-2.0-only. They are newly written source based on the documented i.MX6ULL
instruction/functional-unit behavior and observed acquisition interface, not
a bundled copy of Dreem's recovered program. Copyright 2026 Dreem research
contributors. The model does not reproduce the complete SDMA hardware.

The optional external assembler is Billauer's GPL-2.0-or-later tool, with
Jonah Petri's [raw binary output variant](https://blog.petri.us/sdma-hacking/part-2.html).
Neither the external assembler nor Dreem's DMA binary/recovered assembly is
bundled here. Their use does not relicense generated vendor code.

## ADC driver reconstruction

`development/ads129x_init.c`, `development/ads129x_init.h`,
`development/verify_adc_init.py`, `development/verify_adc_control.py`,
`development/verify_adc_read.py`, and `development/verify_adc_test_signal.py`
are GPL-2.0-only, separately from the root
Apache license. They are independently written reconstruction and verification
code based on observed Linux firmware behavior and published ADC register
definitions; original vendor source has not been obtained. Copyright 2026
Dreem research contributors. The [GPL version 2 text](LICENSES/GPL-2.0-or-later.txt)
is included in this repository; the SPDX identifiers on these files select
version 2 only. No vendor kernel code or extracted binary is bundled with them.

`development/kernel/`, `development/build_adc_module.py`, and
`development/verify_adc_module.py` use the same GPL-2.0-only terms. Their acquisition
integration and verification code is independently written. Public Linux
headers/tooling and private stock export metadata are external build inputs;
compiled kernels and modules are not bundled here.

`development/sdma_eeg.c`, `development/sdma_eeg.h`, and
`development/verify_sdma_eeg.py` also use GPL-2.0-only. They independently
reconstruct and verify the observed kernel channel-context and interrupt
progress behavior. Original vendor code, recovered assembly, and compiled
firmware are not included in these files.

`development/build_sdma_kernel.py`, `development/verify_sdma_provider.py`,
and `development/verify_sdma_pipeline.py`
use GPL-2.0-only. The overlay script `development/apply_sdma_overlay.py` uses
GPL-2.0-or-later and retains small matching excerpts from NXP's `imx-sdma.c`,
copyright 2010 Sascha Hauer/Pengutronix and 2004-2016 Freescale Semiconductor.
The upstream file retains its notice in the generated private source tree.
The complete upstream source, reference-manual PDF, and vendor firmware are
external inputs, not bundled by these build/verification tools.

## Bus-frequency policy reconstruction

`development/apply_busfreq_overlay.py` uses GPL-2.0-or-later and contains
matching anchors from the public NXP `busfreq-imx.c` and `busfreq_ddr3.c`, copyright 2011-2016
Freescale Semiconductor and 2017 NXP. The generated upstream file retains its
notice. `development/kernel/busfreq_dreem.inc` uses GPL-2.0-only; its clock
operations adapt that public driver's helpers, with independently reconstructed
Femto policy. `development/kernel/ddr_linux.inc` is independently reconstructed
GPL-2.0-only interface code. `development/verify_busfreq.py`,
`development/verify_ddr_control.py`, `development/verify_ddr_sources.py`,
`development/verify_ddr_c_sources.py`, `development/arm_relocations.py`,
`development/verify_ddr_preparation.py`, and `tests/test_arm_relocations.py`
are new GPL-2.0-only verification code, copyright 2026 Dreem research
contributors. Private stock decompilations and vendor binaries are not included.

`development/kernel/ddr_prepare_dreem.inc` and
`development/kernel/busfreq_probe_dreem.inc` use GPL-2.0-only. They adapt public
NXP DDR preparation and probe operations, retaining attribution to Freescale
Semiconductor (2011-2016) and NXP (2017), with new checked layout, resource
handling and publication logic by Dreem research contributors (2026).

## WM8960 codec source matching

`development/build_wm8960_reference.py` uses GPL-2.0-only and contains small
matching anchors from public `sound/soc/codecs/wm8960.c`, copyright 2007-11
Wolfson Microelectronics. The generated private source retains its original
notice. `development/verify_wm8960_sources.py` is new GPL-2.0-only verification
code, copyright 2026 Dreem research contributors. The upstream checkout and
vendor firmware are external inputs; no compiled codec or vendor decompilation
is included. See [audio findings](development/audio-findings.md).

`development/wm8960_board_reference.py` uses GPL-2.0-or-later. Its matching
anchors and jack-routing helper adapt public NXP `imx-wm8960.c`, copyright
2015-2016 Freescale Semiconductor, with new interface/lifecycle reconstruction
by Dreem research contributors (2026). The generated private source retains
the upstream notice. `development/verify_wm8960_board_sources.py` and
`development/verify_wm8960_jack.py` are new GPL-2.0-only verification code,
copyright 2026 Dreem research contributors. The reference preserves observed
defects for analysis; no compiled driver or vendor decompilation is bundled.

`development/apply_wm8960_overlay.py`, `development/kernel/wm8960_jack.inc`,
`development/kernel/wm8960_lifetime.inc` and
`development/kernel/wm8960_streams.inc` use GPL-2.0-or-later. They adapt
that public NXP board setup and the matching reference, with new publication,
resource-ownership and teardown logic by Dreem research contributors (2026).
`development/verify_wm8960_lifetime.py` is new GPL-2.0-only verification code.
See [research audio integration](development/audio-lifetime.md) for its limits.

`development/apply_wm8960_codec_overlay.py` uses GPL-2.0-only and selects the
same Wolfson/NXP codec reconstruction described above. It retains the original
codec notice in generated private source. `development/verify_wm8960_streams.py`
is new GPL-2.0-only verification code, copyright 2026 Dreem research contributors.
See [connected stream verification](development/audio-streams.md) for the
remaining inherited codec defects and physical-testing limits.

## Hardware identity access

`development/apply_hardware_overlay.py` uses GPL-2.0-only and contains small
matching anchors from NXP `drivers/char/fsl_otp.c`, copyright 2010-2016
Freescale Semiconductor. The generated private source retains its notice.
`development/kernel/dreem_hardware.h`, `development/kernel/dreem_hardware.inc`,
and `development/verify_hardware_version.py` are new GPL-2.0-only source and
verification code, copyright 2026 Dreem research contributors. No original
vendor getter source, firmware binary, or physical OTP values are included.

## Upstream source snapshots

The files under `third-party/source-snapshots/` are attributed snapshots of these upstream repositories:

- [jabituyaben/DreemEEG](https://github.com/jabituyaben/DreemEEG), commit `084172e1565e75327eaf80e44b949602da585cf6`
- [Dreem-Organization/dreem-standalone](https://github.com/Dreem-Organization/dreem-standalone), commit `c103b70fe9bff3187e7b3d0f3d0982a21975f6a8`

No license file accompanied either captured snapshot. This project does not grant permission to copy, modify, or redistribute those files; consult the upstream copyright holders.

## Dreem documentation

[`docs/dreem-2-user-manual.pdf`](docs/dreem-2-user-manual.pdf) is Dreem documentation. It is not covered by this project's Apache-2.0 or CC BY 4.0 grants.

## FCC construction-photo exhibit

[`hardware/dreem-2-fcc-internal-construction-photos.pdf`](hardware/dreem-2-fcc-internal-construction-photos.pdf) is a Bureau Veritas construction-photo exhibit obtained through the public FCC filing for FCC ID `2AH2Q-DREEM2`. Public availability is not represented here as a copyright license. It is not covered by this project's Apache-2.0 or CC BY 4.0 grants.
