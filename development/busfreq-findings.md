# Femto bus-frequency reconstruction

The saved 4.7.11 kernel changes NXP's bus-frequency policy and the high-frequency
transition sequence. `kernel/busfreq_dreem.inc` reconstructs those changes in
editable source. A complete kernel and the acquisition module build with it.
This remains an offline research build; nothing has been installed on a headset.

## Recovered behavior

The exact stock kernel identity is recorded in [source findings](source-findings.md).
ARM disassembly, private decompilation, and execution of the selected stock
routines establish these differences from the pinned public NXP baseline:

| Entry | Saved Femto behavior |
| --- | --- |
| `request_bus_freq`, modes 0–3 | Increment the corresponding high/medium/audio/low counter under the mutex; do not request a clock transition |
| `request_bus_freq`, mode 7 | Increment the high counter and call the high-frequency transition |
| `release_bus_freq`, modes 0–3 | Decrement the corresponding counter; a zero count logs an error and dumps the stack; do not lower clocks |
| Request/release, other values | No counter or clock change, including release of mode 7 |
| `set_low_bus_freq` | Return zero without changing state or scheduling work |
| Idle bus-frequency worker | Lock and unlock, with no lowering operation |

The stock DDR character-device ioctl at `0x8041c804` forwards command 7 to
`request_bus_freq(7)` and returns zero. Other commands also return zero but log
an unsupported-command message. The overlay now includes a reconstructed
character-device interface, described below. Mode 7 is not a new balanced reference-count category: repeated
requests keep incrementing the high count, and release of 7 does not undo them.
Its original enum spelling and export CRCs have not been recovered.

The high-frequency entry at `0x8002a038` checks initialization, active policy,
suspend state, and the current high-mode flag. For an i.MX6ULL transition it:

1. Restores the CPU rate when coming from low mode and enables the PLL2 clock.
2. Requests **264 MHz OCRAM**, then **132 MHz AHB** directly. It omits the
   baseline's preliminary 6/12 MHz rates and its changes to `periph_pre`,
   `periph`, and `periph_clk2_sel` parents in this helper.
3. Calls the DDR3 or LPDDR2/LPDDR3 transition helper with the configured normal
   DDR rate, then restores the three `periph2` clock parents.
4. Updates MMDC's reported rate when leaving audio mode, balances the existing
   PLL references, and publishes high mode through the upstream outer routine.

These are observed software requests, not measurements of physical rates.
The overlay preserves the observed behavior when clock operations fail; the
stock path logs some errors and continues. A matched trace is not evidence
that continuing after those errors is electrically safe.

## DDR character-device interface

`kernel/ddr_linux.inc` creates the original `/dev/dreem_ddr` interface during
late initialization when the experimental Femto policy is enabled. The saved
`nano_core` wrapper at `0x0008e0a0` opens that path with `O_RDWR`, supplies the
plain command number to `ioctl`, and closes it. It does not pass a data buffer.
The stock file-operations table at `0x8065e5a4` contains only the ioctl callback.
The reconstruction retains that interface and never dereferences the third
ioctl argument. Unknown commands return zero without a hardware operation.

For command 7, the reconstructed entry takes the bus-frequency mutex, increments
the high counter, and calls the matched high-frequency path. Repeated successful
calls retain the stock counter behavior. It deliberately adds these checks:

| Condition | Result before any counter or clock change |
| --- | --- |
| Runtime policy disabled or wrong board | `ENODEV` |
| Bus-frequency driver uninitialized or scaling disabled | `EAGAIN` |
| Driver suspended | `EBUSY` |
| Corrupt negative high counter | `EIO` |
| High counter at `INT_MAX` | `EOVERFLOW` |

These guarded cases are intentional differences from stock's unconditional
success; the internal request/release API still has the behavior in the table
above. Successful ioctl return does not establish that every underlying clock
operation succeeded: the inherited clock path can log an error and continue.

Registration allocates a device number, initializes/adds the character device,
creates its class, and publishes its node last. Every failure unwinds completed
steps. Failed `cdev_add` also drops the reference created by `cdev_init`, without
unmapping an unregistered device. Class/device error pointers are handled with
`IS_ERR`, unlike the stock initializer's null checks. The device uses devtmpfs's
root-owned, mode-0600 creation default. It is built-in and remains registered
for the system lifetime; no hot-unload path is provided.

`verify_ddr_control.py` passes **47 compiled ARM cases**: 24 ready-state
stock/reconstruction comparisons with repeated requests, nine unknown commands,
seven guarded-error states, five registration/failure sequences, and two disabled
or wrong-board registration checks. It checks the actual file-operations table,
late-init wiring, registration order, errno propagation, and modeled ownership of
the device number, cdev reference/mapping, class, and node. An earlier version
missing the failed-add reference release is rejected by this verifier.

## Matched DDR3 transition source

The complete **1,764-byte** `imx6_up_ddr3_freq_change` body, including its literal
pool, matches the public NXP
[`arch/arm/mach-imx/ddr3_freq_imx6sx.S`](https://github.com/nxp-imx/linux-imx/blob/30278abfe0977b1d2f065271ce1ea23c0e2d1b6e/arch/arm/mach-imx/ddr3_freq_imx6sx.S)
build after applying its sole declared relocation. In the saved kernel the span
is `0x8002d7e0` through `0x8002dec4` exclusive. The relocatable assembler object
specifies `R_ARM_ABS32` at byte offset `0x680`, targeting `iram_tlb_phys_addr`
with addend zero. The verifier substitutes each kernel's address for that same
symbol and then requires an exact comparison of every byte. It does not mask
addresses or skip instruction/data bytes.

The saved routine's SHA-256 is
`4b973552634c1f150de954e89b91b7de3d14c3c28e4bfbb80bd5e635416945f1`.
The public build's `save_ttbr1` and `restore_ttbr1` helpers also match the saved
kernel exactly, for eight and sixteen bytes respectively. This establishes a
concrete public source match for those routines. It does not establish the
correctness of the surrounding initialization, DDR settings, call-site state,
memory hardware, or LPDDR2 paths, and none of these routines was run on hardware.

## Build and activation boundaries

`apply_busfreq_overlay.py` accepts only the SHA-pinned NXP files in a disposable
non-Git source tree. It rejects modified inputs and repeated application.
The baseline checkout is never modified. The existing research builder accepts
the optional `--busfreq-policy` flag:

```sh
/private/work/venv/bin/python development/build_sdma_kernel.py \
  /private/work/linux-imx /private/work/kernel-build \
  /private/work/inspection/kernel.elf /private/work/femto-clock-build \
  /private/work/armv7-eabihf--uclibc--stable-2018.11-1/bin/arm-linux- \
  --jobs 16 --busfreq-policy

/private/work/venv/bin/python development/verify_busfreq.py \
  /private/work/inspection/kernel.elf \
  /private/work/femto-clock-build/kernel/vmlinux \
  /private/work/kernel-build/vmlinux

/private/work/venv/bin/python development/verify_ddr_control.py \
  /private/work/inspection/kernel.elf \
  /private/work/femto-clock-build/kernel/vmlinux

/private/work/venv/bin/python development/verify_ddr_sources.py \
  /private/work/inspection/kernel.elf \
  /private/work/femto-clock-build/kernel/vmlinux \
  /private/work/femto-clock-build/kernel/arch/arm/mach-imx/ddr3_freq_imx6sx.o
```

The option adds `CONFIG_DREEM_BUSFREQ=y`, but **runtime activation is still off
by default**. The built-in parameter is `busfreq_imx.dreem_busfreq=1`; the
policy also requires the root device-tree compatible `fsl,imx6ull-femto`.
The parameter is read-only after boot. This describes the implementation, not
an instruction to boot this unqualified kernel. Without activation or on a
different board, the original NXP behavior remains selected.

The public `enum bus_freq_mode` and function declarations remain unchanged.
The source build therefore retains NXP's bus-frequency export CRCs, which
still differ from the saved Dreem exports. No stock CRC is substituted to force
module compatibility. Other compiled acquisition imports are checked against
the actual generated kernel exports as before.
The builder captures research-input hashes before building and rejects a run
if those inputs have changed by the time the build finishes.

## Verification and remaining work

`verify_busfreq.py` executes the allowlisted ARM routines from both the saved
kernel and the rebuilt kernel. It compares counter/mode state and ordered
clock, notification, work, and mutex calls after each operation. Execution is
bounded and unexpected function calls or state writes fail the comparison.
The reviewed stock raw-kernel hash is required. Test inputs are synthetic.

The **617 cases** comprise 611 stock/reconstruction comparisons, four NXP
fallback checks, and two negative controls. They cover all combinations of
initialization, enablement, and suspend flags; ordinary and invalid modes;
counter underflow and 32-bit wrapping; explicit high transitions from multiple
states and DDR types; ten clock-error locations; PM notifications; sysfs
enable/disable; repeated explicit requests; and idle lowering suppression.
An explicit expected call sequence checks the DDR3 high transition. The
unmodified NXP kernel must differ on both ordinary-request policy and high
transition ordering, so a no-op overlay cannot pass those negative controls.

The option-disabled object also compiles. The integrated acquisition pipeline
continues to pass its 76 cases with this kernel build. `provider-build.json`
records source/configuration and kernel, bus-frequency object, provider, and
ADC hashes; the verification reports identify their actual binary inputs.

The clock/DDR service bodies and CPU-rate restoration are modeled boundaries
in the execution verifiers; the separate DDR3 source comparison proves binary
identity after relocation. Physical clock timing, DDR settings and wrapper
behavior, voltage changes, real scheduling, and suspend/resume remain unverified.
Actual devtmpfs/syscall behavior, other board changes, and headset recovery/runtime
proof also remain unfinished. This does not complete the board kernel.
