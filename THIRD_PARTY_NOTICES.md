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
`development/verify_adc_module.py` use the same GPL-2.0-only terms. Their Linux
integration and verification code is independently written. Public Linux
headers/tooling and private stock export metadata are external build inputs;
compiled kernels and modules are not bundled here.

`development/sdma_eeg.c`, `development/sdma_eeg.h`, and
`development/verify_sdma_eeg.py` also use GPL-2.0-only. They independently
reconstruct and verify the observed kernel channel-context and interrupt
progress behavior. Original vendor code, recovered assembly, and compiled
firmware are not included in these files.

`development/build_sdma_kernel.py` and `development/verify_sdma_provider.py`
use GPL-2.0-only. The overlay script `development/apply_sdma_overlay.py` uses
GPL-2.0-or-later and retains small matching excerpts from NXP's `imx-sdma.c`,
copyright 2010 Sascha Hauer/Pengutronix and 2004-2016 Freescale Semiconductor.
The upstream file retains its notice in the generated private source tree.
The complete upstream source, reference-manual PDF, and vendor firmware are
external inputs, not bundled by these build/verification tools.

## Upstream source snapshots

The files under `third-party/source-snapshots/` are attributed snapshots of these upstream repositories:

- [jabituyaben/DreemEEG](https://github.com/jabituyaben/DreemEEG), commit `084172e1565e75327eaf80e44b949602da585cf6`
- [Dreem-Organization/dreem-standalone](https://github.com/Dreem-Organization/dreem-standalone), commit `c103b70fe9bff3187e7b3d0f3d0982a21975f6a8`

No license file accompanied either captured snapshot. This project does not grant permission to copy, modify, or redistribute those files; consult the upstream copyright holders.

## Dreem documentation

[`docs/dreem-2-user-manual.pdf`](docs/dreem-2-user-manual.pdf) is Dreem documentation. It is not covered by this project's Apache-2.0 or CC BY 4.0 grants.

## FCC construction-photo exhibit

[`hardware/dreem-2-fcc-internal-construction-photos.pdf`](hardware/dreem-2-fcc-internal-construction-photos.pdf) is a Bureau Veritas construction-photo exhibit obtained through the public FCC filing for FCC ID `2AH2Q-DREEM2`. Public availability is not represented here as a copyright license. It is not covered by this project's Apache-2.0 or CC BY 4.0 grants.
