# WM8960 audio source reconstruction

The saved Dreem 4.7.11 kernel's WM8960 codec can be reproduced from public NXP
source with four groups of changes. All **21 emitted functions** and the
driver's constant/mutable tables match after applying real ARM relocations.
The separate board driver is now also reconstructed: all **17 emitted functions**
and its tables match. These are editable source references, including observed
vendor defects, not a qualified replacement kernel.

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

## Board-driver source match

`wm8960_board_reference.py` reconstructs the saved board driver from
[NXP imx-wm8960.c](https://github.com/nxp-imx/linux-imx/blob/30278abfe0977b1d2f065271ce1ea23c0e2d1b6e/sound/soc/fsl/imx-wm8960.c),
copyright 2015-2016 Freescale Semiconductor, GPL version 2 or later. The source
match establishes these changes:

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
- Both codec clock calls in `imx_hifi_hw_params` use `WM8960_SYSCLK_PLL`
  (numeric value 1), replacing the upstream `WM8960_SYSCLK_AUTO` (2). Matching
  function sizes did not reveal this difference; the instruction comparison did.
- The added character-device registration occurs before the rest of the probe;
  later error returns do not consistently unwind it. The reference deliberately
  preserves this behavior so it can be compared with the saved binary.

The complete comparison covers **5,644 file-backed bytes**: 3,844 bytes of
function code, 1,096 bytes of read-only tables, 700 bytes of initialized data,
and the four-byte initialization pointer. It applies **308 relocations**,
validates **58 string locations**, and verifies the **208-byte BSS layout**.
Intersecting complete section layouts resolves duplicate names such as
`imx_hifi_hw_params`, `card_priv`, `fops`, and `jack_ioctl` without selecting
the unrelated AIC31xx copies. The generic relocation comparison is shared with
the codec verifier; the original codec comparison still passes unchanged.

The original NXP board object independently matches its clean baseline kernel
(15 functions, 5,104 bytes). Five negative controls reject the unmodified board
driver and altered command, register-mask, ioctl-callback, and hardware-version
call behavior. As with the codec, unwind metadata and initial BSS contents are
outside the byte comparison. `get_dreem_hardware_version` is an external call
target in this object, not recovered source supplied by this recipe.

```sh
python3 development/build_wm8960_reference.py \
  /private/work/linux-imx /private/work/kernel-build-gcc7/.config \
  /private/work/wm8960-board-reference /private/toolchain/bin/arm-linux- --board

/private/work/venv/bin/python development/verify_wm8960_board_sources.py \
  /private/work/inspection/kernel.elf \
  /private/work/wm8960-board-reference/kernel/sound/soc/fsl/imx-wm8960.o \
  /private/work/kernel-build-gcc7/vmlinux \
  /private/work/kernel-build-gcc7/sound/soc/fsl/imx-wm8960.o \
  > /private/work/wm8960-board-reference/board-source-verification.json

/private/work/venv/bin/python development/verify_wm8960_jack.py \
  /private/work/inspection/kernel.elf \
  /private/work/wm8960-board-reference/kernel/sound/soc/fsl/imx-wm8960.o \
  > /private/work/wm8960-board-reference/jack-verification.json
```

The matched board source SHA-256 is
`aec9b1cd05abcd665ab4891112fede85db4e9e55ea7f1dc342a5b281abd567f0`;
the board object SHA-256 is
`c739c7c8ecf55aeda1042ba34090216290744ca0245c6e5ca7c558a9ac18add6`.
The `--board` build also rebuilds the codec and records both inputs/outputs.

## Reproduced jack behavior and defects

The bounded ARM verifier first requires the complete board source match, then
executes the saved matching routines with synthetic kernel services. Its **79
checks** cover active-low routing, headphone versus headset report masks,
known/unknown commands, ignored ioctl arguments, absent-card behavior, late
probe, early registration failures and removal. These are behavior checks,
including tests that reproduce defects; they are not a safety qualification.

- `device_create` is called before `cdev_init`/`cdev_add`.
- A missing `cpu-dai` phandle leaves the device number, cdev reference, class,
  node and cdev registration allocated, even though probe returns `-EINVAL`.
- Class/device creation error pointers are not recognized by the board driver.
  The modeled public `device_create_groups_vargs` rejects an error-pointer
  class, but the caller ignores that returned error pointer too and continues.
- A failed `cdev_add` removes the node/class/number but retains the initial
  cdev reference. It does not call `kobject_put` on this failure path.
- The hardware getter's -1 result proceeds to character-device registration.
- Late probe returns success even if its register update fails.
- Removal deletes the character-device resources but leaves the card pointers
  usable by the ioctl routine. A retained file operation can still request
  routing after removal; real devm teardown and concurrent VFS access are not
  emulated by this test.

Probe execution is intentionally bounded at the first missing audio phandle.
The complete probe's instructions are source-matched, but successful sound-card
initialization, later failure paths, GPIO interrupts/work and actual ALSA
routing effects are outside that source-reference verifier. The separate
[research board verifier](audio-lifetime.md) now exercises full board probe and
cleanup with modeled services and pending GPIO callbacks; actual ALSA internals
and physical routing remain unverified.

## Remaining integration

The matching reference objects stay isolated for comparison. A separate
[research board implementation](audio-lifetime.md) now integrates publication,
error cleanup, open-file lifetime and removal repairs into the experimental
kernel, connected to the [checked hardware-version API](hardware-identity.md).
The matching codec was integrated at the [stream-reference milestone](audio-streams.md).
The active [research clock implementation](audio-clocking.md) now repairs codec
clock selection, stream failure state, cached-write retries and explicit SAI
slot widths, with connected ARM execution checks. The source-match recipe above
remains unchanged. [SAI startup/close](sai-lifetime.md) now repairs resource
rollback. [PCM cyclic preparation](pcm-dma.md) now checks descriptor bounds,
sample widths and context-failure cleanup. [SAI parameter setup](sai-parameters.md)
now checks register errors and clock ownership. [PCM submission/control](pcm-trigger.md)
now checks DMA results. [DMA retirement](pcm-lifetime.md) now checks callback and
storage lifetime. SAI trigger/IRQ error handling, physical DMA stop timing and
ALSA rollback,
reconfiguration without an explicit free, actual DMA sample transfer, power management, physical playback and recording
fidelity remain to be qualified. Nothing was installed or flashed.
