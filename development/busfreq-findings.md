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
an unsupported-command message. The character device itself is not yet rebuilt
by this overlay. Mode 7 is not a new balanced reference-count category: repeated
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
in this verifier. Physical clock timing, DDR self-refresh/transition internals,
voltage changes, real scheduling, and suspend/resume remain unverified. The
original DDR character-device interface, other board changes, and headset
recovery/runtime proof also remain unfinished. This closes a source gap in
the frequency policy; it does not complete the board kernel.
