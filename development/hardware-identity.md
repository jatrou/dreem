# Checked hardware identity access

The research kernel now provides `dreem_get_hardware_version(u32 *version)`
for built-in drivers. It reads the same `0x6c0` OTP shadow word as the saved
Dreem kernel, with provider lifetime, board/type, resource and clock checks.
It is a new implementation, not recovered original source. Audio integration
must use this checked result instead of treating the old getter's -1 error as
a nonzero hardware version.

## Interface and behavior

The header is generated at `include/linux/dreem_hardware.h` from
`development/kernel/dreem_hardware.h`. The function sleeps and requires normal
process context. On success it returns zero and writes the raw 32-bit hardware
value, including zero for the older hardware branch. On failure the output is
unchanged. There is no cached identity or arbitrary register/address argument.

| Condition | Result |
| --- | --- |
| NULL output pointer | `-EINVAL` |
| Root device tree is not `fsl,imx6ull-femto` | `-ENODEV` |
| Provider not successfully bound, probing, or being removed | `-EPROBE_DEFER` |
| OTP driver is not the i.MX6ULL variant | `-ENODEV` |
| Invalid mapped-register or clock pointer | `-EIO` |
| Resource cannot contain bytes `0x6c0` through `0x6c3` | `-ERANGE` |
| Clock preparation/enabling fails | Propagate its error |
| Controller error before or after the shadow read | `-EIO` |
| Controller busy or reloading shadows | `-EBUSY` |
| Identity is all ones, the old getter's sentinel value | `-ENODATA` |

The read takes the existing OTP mutex, enables the controller clock, reads
status at offset zero, reads the single version word, checks status again,
and releases its clock reference before unlocking. It does not program fuses,
change timing, clear status, or request a shadow reload. The older public
sysfs OTP functions remain part of the original NXP driver; this API adds no
new userspace endpoint.

## Provider lifetime

The optional overlay wraps NXP's original probe/remove functions with a
single-owner state: idle, probing, ready, removing. A second probe cannot
overwrite the active provider's global mapping. Readiness is published only
after the original probe succeeds. Failed probe clears the pointers and permits
retry; removal withdraws readiness before draining original sysfs users, then
clears all provider pointers before devres can release the mapping and clock.
Removal by a different platform device is rejected.

These callbacks do not hold the OTP mutex while calling original probe/remove:
sysfs removal may wait for an existing reader that needs that mutex. The
original end-of-probe `mutex_init` is omitted in the enabled configuration;
`DEFINE_MUTEX` already initialized it, and resetting it could race a caller
checking readiness.

`CONFIG_DREEM_HW_VERSION` defaults to disabled and requires built-in `FSL_OTP`
and `SOC_IMX6ULL`. When enabled, the provider wrappers are active. The identity
register is read only when an internal caller invokes the API; compiling it
does not automatically read a headset's identity. The function is not exported
to loadable modules. It is not yet connected to the reconstructed audio driver.

## Build and evidence

`apply_hardware_overlay.py` checks the pinned NXP source hashes before editing
an isolated tree. The original source is NXP `drivers/char/fsl_otp.c`, copyright
2010-2016 Freescale Semiconductor, GPL version 2. The new API and wrappers are
in `development/kernel/dreem_hardware.inc`.

```sh
/private/work/venv/bin/python development/build_sdma_kernel.py \
  /private/work/linux-imx /private/work/kernel-build-gcc7 \
  /private/work/inspection/kernel.elf /private/work/hardware-kernel \
  /private/toolchain/bin/arm-linux- --busfreq-policy --hardware-version

/private/work/venv/bin/python development/verify_hardware_version.py \
  /private/work/inspection/kernel.elf \
  /private/work/hardware-kernel/kernel/vmlinux \
  /private/work/hardware-kernel/kernel/drivers/char/fsl_otp.o \
  > /private/work/hardware-kernel/hardware-verification.json
```

Verified offline on October 3, 2026 (America/New_York). The complete research
kernel and ADC module build; all 44 ADC imports still match their actual kernel
exports. The compiled ARM verifier passes **43 cases**, including:

- Comparisons with the saved getter for zero, ordinary and high-bit values.
- Deferred/unavailable providers, wrong boards/types, invalid pointers,
  short/reversed/overflowing resources, and clock failures.
- Controller status failures on both sides of the read and unchanged outputs.
- Probe failure/retry, duplicate-probe and wrong-owner rejection, removal,
  access after the modeled mapping is unmapped, and rebind to a new owner.
- Platform callback pointers selecting the wrappers.
- Deliberate instruction mutations: an OTP store and an adjacent-word read
  are both rejected by the verifier's memory guards.

With the option disabled, the complete rebuilt `fsl_otp.o` is byte-identical
to the clean NXP baseline object (SHA-256
`b5f84e8754745fc7fa49820d77f351b9682d63761fe8bfe90a3132ef3a5caa8e`).
On the new enabled build, the connected EEG pipeline passes 76 cases,
bus-frequency checks pass 617, and DDR preparation checks pass 58.

The kernel SHA-256 is
`7df8b74963713999cb5178f9cd4734fe3bf1d1c4e284d6074cddf2b55b699ffb`;
the enabled OTP object SHA-256 is
`be6cafeda92e7928d5ff0b321e69226bbec3b7cccdf5f626aba44335f852230b`.
Build and verification manifests pin inputs and outputs outside the repository.

These tests execute compiled getter/wrapper instructions with modeled clocks,
registers, locking, and original provider callbacks. They do not execute the
original sysfs/devres implementations, model real concurrent scheduling, or
verify physical fuse contents. No firmware was installed or flashed. The jack
lifetime repairs, audio integration and device-side qualification remain.
