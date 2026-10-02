# Dreem feature development

This directory provides an independent feature example and reproducible tools
for matching the saved firmware to public sources. It does not contain Dreem's
original application source or a complete replacement firmware.

## What is available

The stock 4.7.11 archive yielded the complete Linux configuration, device tree,
and an analyzable kernel with **61,634 recovered symbols**. Public NXP release
`rel_imx_4.1.15_2.1.0_ga` is a concrete source baseline. Only three enabled config
symbols are absent from that tree, but standard drivers also contain Dreem
changes; this is not a claim that three files complete the reconstruction.

The saved core from the locally modified 4.7.18 installation is byte-identical
to stock 4.7.11 `nano_core`. Its original source is still missing. Root access
and our independent tools provide a way to add programs without replacing it.
See [source and hardware findings](source-findings.md) for the evidence and
remaining gaps.

As of October 2, 2026, these results are verified offline. The headset was not
reachable for a new runtime test, and neither checked workstation had the
recovery phone connected. Existing operational records describe working root
access and a previously tested ARM streamer; this change does not repeat that
on-device proof.

## Build an independent feature

`eeg_quality.c` reads native four-channel EEG files and emits one JSON record
per 250-row window: mean, RMS, AC RMS, peak-to-peak range, and finite/invalid
sample counts. This is a small example of a software feature that needs no
vendor application source. Units remain the input units, and the output is not
a sleep-stage or clinical interpretation.

On Linux with `cc`, `arm-linux-gnueabihf-gcc`, and optionally `qemu-arm`:

```sh
sh development/build.sh
python3 -m unittest tests.test_eeg_quality -v
development/build/eeg_quality.host /private/path/eeg.data
qemu-arm -cpu cortex-a7 development/build/eeg_quality.arm /private/path/eeg.data
```

For an already growing file, pass `--follow-seconds 60`. The reader starts at
row zero, waits for complete rows, uses bounded memory, and reports unfinished
windows and unread bytes. It detects removal, inode replacement, and observed
truncation. It refuses symlinks, FIFOs, and device nodes, opens the input read
only, and never starts or stops recording. It does not discover sessions or
install itself at boot. Use a new invocation for each recording file.

The ARM executable is statically linked for Cortex-A7 hard-float, avoiding the
headset's uClibc-versus-host-glibc mismatch. Host and ARM-emulated results agree
for known sine waves, constants, invalid floats, growing files, and partial
rows; the source file's hash is unchanged. This proves the program's arithmetic
and ARM execution, not its power draw or interference with a real recording.
An on-device trial still needs recording-fidelity and resource measurements.

## Reproduce the firmware analysis

Use an already acquired stock archive. None is downloaded or distributed by
these tools. All generated firmware, symbolized images, and decompilations
belong in a private directory outside the repository.

```sh
python3 -m venv /private/work/venv
/private/work/venv/bin/pip install -r development/requirements.txt
git clone --depth 1 --branch rel_imx_4.1.15_2.1.0_ga \
  https://github.com/nxp-imx/linux-imx.git /private/work/linux-imx
python3 development/inspect_firmware.py /private/firmware_FEMTO_4.7.11_production.tar.bz2 \
  /private/work/inspection --kernel-source /private/work/linux-imx
/private/work/venv/bin/vmlinux-to-elf \
  /private/work/inspection/kernel.raw /private/work/inspection/kernel.elf
```

The inspector checks the expected archive hash before reading it, uses exact
member allowlists without following archive links, and creates a new private
output directory. Its manifest contains component hashes, enabled buses,
device-tree pin groups, and config symbols absent from the public tree. It
does not extract credentials or the full root filesystem. Extracted vendor
artifacts remain private; generating them does not grant redistribution rights.

With the outer archive's `rootfs.tar.gz` retained privately, recover export
checksums and check the shipped modules:

```sh
/private/work/venv/bin/python development/recover_exports.py \
  /private/work/inspection/kernel.elf /private/work/exports \
  --rootfs /private/work/rootfs.tar.gz
```

For this exact archive, the full module consistency check **exits 1**: there
are 63 module-to-module CRC mismatches and five unresolved references involving
`mac80211.ko`. The recovered kernel table itself agrees with all 1,971 checked
kernel references. See the output `exports-report.json` for individual results.
Never use a recovered `Module.symvers` to force a mismatched module to load;
matching CRC labels alone cannot prove structure layout or behavior.

For decompilation, Ghidra 12.1.4 and Java 21 were used. Its official release ZIP
was verified against the SHA-256 published in the release metadata:
`ddac49f903da9d5bac833e5cc79395098b9c33cfd3279be5f31bd00387d2d4db`.

```sh
/path/to/ghidra/support/analyzeHeadless /private/work/projects kernel \
  -import /private/work/inspection/kernel.elf -max-cpu 4 \
  -analysisTimeoutPerFile 600 -scriptPath development/ghidra \
  -postScript ExportSelected.java /private/work/drivers.c '.*(ads1296|dreem_ddr).*'
```

Recovered symbols help identify functions, but decompiler types and boundaries
are inferred. Check important operations against ARM disassembly. In particular,
Ghidra omitted an `ioctl` pointer argument in one display even though the ARM
instructions pass it correctly.

## Next verification gates

1. Reconstruct the modified EEG/SDMA and board drivers against the public NXP
   baseline; compare config, exported type CRCs, register setup, and behavior.
2. Map the board connector and sensor power domains to physical pads. Device-tree
   pad names identify SoC functions, not a verified soldering pinout.
3. Test new userspace programs with known fixtures on the headset, then compare
   native recording bytes, timing, CPU, and memory while they run.
4. Add an external sensor through an identified free interface, with a separate
   acquisition process and timestamps. Prove electrical compatibility and bus
   ownership before enabling it.

The existing kernel exposes I2C userspace access. Many sensor additions can use
that interface without a replacement kernel. ADC acquisition, existing sensor
FIFOs, clock control, and the recording process remain shared resources.

## Rebuild and compare the NXP baseline

A full baseline `vmlinux` build succeeded using Bootlin's ARMv7 hard-float
uClibc stable 2018.11 toolchain (GCC 7.3.0). This is close to the stock GCC 7.4.0,
not the original compiler. The downloaded archive and checksum are published
by [Bootlin](https://toolchains.bootlin.com/downloads/releases/toolchains/armv7-eabihf/tarballs/):
`armv7-eabihf--uclibc--stable-2018.11-1.tar.bz2`, SHA-256
`a0300cf5765436607e50d010abbe88a71b2447c40cd9ccd1a733a6e43608f081`.

After extracting the toolchain into a private work directory:

```sh
sh development/build_kernel_baseline.sh /private/work/linux-imx \
  /private/work/kernel-build /private/work/inspection/kernel.config \
  /private/work/armv7-eabihf--uclibc--stable-2018.11-1/bin/arm-linux-
/private/work/venv/bin/python development/recover_exports.py \
  /private/work/kernel-build/vmlinux /private/work/baseline-exports
python3 development/compare_exports.py /private/work/exports/Module.symvers \
  /private/work/baseline-exports/Module.symvers
```

Of 7,041 stock exports, 7,036 match both CRC and export class. Two shared
symbols differ and three are stock-only; details are in the source findings.
This is strong interface evidence, not proof of identical implementations.
The baseline drops Dreem-only config options and has no Femto board DTS. It
must not replace the installed kernel. No baseline image was flashed.

## Tests

```sh
sh development/build.sh
/private/work/venv/bin/python -m unittest \
  tests.test_firmware_development tests.test_kernel_exports \
  tests.test_compare_exports tests.test_eeg_quality -v
```

The suite covers malformed/truncated input, archive link and duplicate rejection,
device-tree bounds, module-version records, and host/ARM feature behavior. The
ARM comparison requires `qemu-arm`; without it only the host example is tested.
