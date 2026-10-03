# WM8960 audio source reconstruction

The saved Dreem 4.7.11 kernel's WM8960 codec can be reproduced from public NXP
source with four groups of changes. All **21 emitted functions** and the
driver's constant/mutable tables match after applying real ARM relocations.
This supplies editable codec source; it does not recover the complete audio
board driver or qualify a replacement kernel on the headset.

The earlier [hardware inspection](../docs/dreem-2-recovery-reference.md)
identifies WM8960 and a 19.2 MHz oscillator on the user's Dreem 2. The saved
device tree includes both WM8960 and an older AIC31xx variant. The stock board
drivers select between variants using an OTP hardware-version value. The
WM8960 work therefore targets the documented hardware, without claiming a new
physical inspection or assuming the BLE hardware name equals that OTP value.

## Source and verified changes

The input is [NXP's wm8960.c](https://github.com/nxp-imx/linux-imx/blob/30278abfe0977b1d2f065271ce1ea23c0e2d1b6e/sound/soc/codecs/wm8960.c),
copyright 2007-11 Wolfson Microelectronics, GPL version 2, at NXP commit
`30278abfe0977b1d2f065271ce1ea23c0e2d1b6e`.
`build_wm8960_reference.py` requires that clean checkout and verifies the
original source hash before creating an isolated source snapshot.

| Change | Observed stock behavior |
| --- | --- |
| Direct-clock search | Skip candidates with signed `sysclk > 16000015`; the limit is not rounded to 16 MHz and is absent from the later PLL search |
| PLL startup | Omit the upstream `msleep(250)` between PLL power-on and clock selection |
| Input-volume names | Label the two INBMIX1 controls Left/LINPUT and the two INBMIX2 controls Right/RINPUT; keep their register and bit mappings |
| Input routes | Give the LINPUT1 and RINPUT1 input-mixer routes the explicit `Boost Switch` control instead of NULL |

The delay removal reproduces the vendor binary; it is not a new recommendation
about PLL settling time. Existing arithmetic, error propagation, and other
driver behavior are deliberately preserved in this source-match candidate.

## Verification

Verified offline on October 3, 2026 (America/New_York), using GCC 7.3.0 and the
existing NXP baseline configuration. The reviewed stock raw-kernel SHA-256 is
`e15f659afdffab3fde7c997883475e4c95b5d3cdc1fbcd23c76365ee4cd52dcb`.

| Compared content | Bytes |
| --- | ---: |
| All 21 functions, including built-in init/exit code | 4,924 |
| Read-only tables | 9,640 |
| Initialized writable tables and registration structures | 2,228 |
| Built-in initialization pointer | 4 |
| Total file-backed comparison | 16,796 |

The verifier applies **689 actual relocations**, validates **115 referenced
string locations** by their complete NUL-terminated contents, and checks the
four-byte PLL state object's BSS layout. Section bases must agree across named
symbols. Resolved internal branches, literal pools, callback pointers, table
values, padding and instruction opcodes are compared too; addresses are not
masked out. The same procedure independently matches the original NXP object
against its clean baseline kernel (16,756 bytes).

Seven negative controls reject the unmodified upstream codec and deliberate
changes to the clock limit, bit-clock divisor, diagnostic string, external
call, resolved internal call, and codec registration callback. This comparison
does not include ARM unwind tables, discarded exit-call metadata, or the initial
contents of BSS, which are not stored in the reviewed raw image. It proves the
specified source/object match, not live audio, electrical timing, or the
behavior of external kernel routines.

Reproduction, with all generated material outside the repository:

```sh
python3 development/build_wm8960_reference.py \
  /private/work/linux-imx /private/work/kernel-build-gcc7/.config \
  /private/work/wm8960-reference /private/toolchain/bin/arm-linux-

/private/work/venv/bin/python development/verify_wm8960_sources.py \
  /private/work/inspection/kernel.elf \
  /private/work/wm8960-reference/kernel/sound/soc/codecs/wm8960.o \
  /private/work/kernel-build-gcc7/vmlinux \
  /private/work/kernel-build-gcc7/sound/soc/codecs/wm8960.o \
  > /private/work/wm8960-reference/source-verification.json
```

Use the dependencies in `development/requirements.txt`. The builder records
source/configuration/object hashes in `reference-build.json`; the verifier
records both input objects, the baseline kernel, comparison results, and its
own source dependencies. The matched reference source SHA-256 is
`3092654cfa65c9a6643824d950f139de4e154d8018f54177a580c69ec5c1357e`;
the reference object SHA-256 is
`8124f172a2ac39718699b8223c9e619a91add75615a708fb44f1f1adcb0cd16d`.
Vendor binaries and decompilations remain private.

## Remaining board integration

The separate `sound/soc/fsl/imx-wm8960.c` still needs reconstruction. Static
ARM inspection of the reviewed kernel establishes these differences:

- The board probe excludes OTP hardware version zero and adds a `/dev/jack`
  character device. It accepts nonzero values, including the getter's -1
  error result; a new implementation needs an explicit initialization policy.
- Plain ioctl commands 5 and 6 pass logical values 1 and 0 to a jack-status
  helper. The helper compares that value with the configured active-low flag,
  changes the `Ext Spk` route, and reports headphone state through ALSA. Unknown
  commands return success; a missing sound card returns -1. These are observed
  interfaces, not commands to probe on a recording headset.
- Late probe only sets register 9, mask/value `0x40`. The five additional NXP
  jack-detection register updates are absent.
- The added character-device registration occurs before the rest of the probe;
  later error returns do not consistently unwind it. Reconstructing the board
  driver must address publication, error cleanup, open-file lifetime and removal.

The matched codec is intentionally built as an isolated reference object. It
is not yet integrated into the experimental kernel. Board-driver reconstruction,
hardware-version handling, SAI/clock integration, power management, playback
and recording fidelity, and physical qualification remain. A fresh connection
to the known headset SSH endpoint timed out during this work; nothing was
installed or flashed.
