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

The [WM8960 audio codec](audio-findings.md) now has a source-match recipe:
four groups of changes to public NXP source reproduce all 21 emitted functions,
control/routing tables, and registration data in the saved kernel. The comparison
covers 16,796 bytes and validates 115 referenced strings. The separate board
driver now also matches all 17 functions and tables (5,644 bytes). Its jack
verifier reproduces 79 cases, including startup/removal defects that a deployable
replacement must fix. Those matching references remain isolated. A separate
[research board implementation](audio-lifetime.md) now integrates repaired
startup, jack publication, stale-handle rejection and cleanup into the kernel,
with 50 compiled ARM scenarios. The [codec/SAI clock and retry repairs](audio-clocking.md)
now configure all nine advertised rates and four widths in connected callback
tests, with 305 scenarios covering clock arithmetic, setup failures, duplex
isolation and cached-register retries. The [SAI startup repair](sai-lifetime.md)
adds 58 compiled checks for resource rollback, retry, duplex isolation and
connected startup/close. [PCM cyclic preparation](pcm-dma.md) adds 58 checks
for sample widths, descriptor bounds, context-failure cleanup and previous-driver
controls. [SAI parameter setup](sai-parameters.md) now checks register errors
and exact master-clock ownership, with 362 compiled cases.
[PCM submission/control](pcm-trigger.md) adds 69 checks for actual virtual DMA
submission, SDMA issue and error returns. [DMA retirement](pcm-lifetime.md) adds
37 checks for callback drain, deferred release and PCM buffer/runtime lifetime.
[Direct-link ASoC rollback](soc-trigger.md) now unwinds failed triggers through
DMA retirement and continues stop cleanup after component errors.
Actual DMA sample transfer, physical stop timing, full ALSA linked-stream/DPCM handling, SAI trigger/IRQ
handling, reconfiguration without an
explicit free, power management and physical audio tests remain. The [earlier stream reference](audio-streams.md)
preserves the original clock-policy limits for comparison.

The research kernel also builds a [checked hardware identity API](hardware-identity.md)
for the optional audio board driver. Its 43 compiled ARM checks cover provider
lifetime, bounded shadow reads, clock/status errors and unchanged outputs on
failure. With the option off, the OTP driver object exactly matches the NXP
baseline. The audio option uses it before claiming resources.

An optional [Femto bus-frequency policy](busfreq-findings.md) now reconstructs
the saved counter rules, disabled automatic lowering, and modified high-rate
clock sequence. It builds into the kernel and passes 617 modeled checks,
including comparisons with the saved ARM routines and negative controls against
the unmodified NXP kernel. Runtime activation remains off by default.
It now includes the recorder's `/dev/dreem_ddr` control interface, with 47
compiled checks for command behavior and registration cleanup. The 1,764-byte
DDR3 transition routine matches public NXP assembly exactly after applying its
one declared relocation. The two surrounding C routines and nine static settings
tables also match. The active research path now repairs the initializer's
capacity/pointer arithmetic and allocation/mapping failures, and defers probe
readiness until preparation succeeds. Its 58 compiled ARM checks cover rollback,
retry and stock C-wrapper comparisons. Physical qualification remains unfinished.

The EEG DMA program can now be recovered into assembly that reassembles
exactly, and an independent raw-sample decoder matches the recorder's ARM
conversion routine byte-for-byte on 8,198 synthetic records. These are useful
reconstructed components, not the complete original source.

Independent SDMA primitives construct the channel context and handle producer
progress; their native and ARM builds match 399 stock cases. They are now
integrated into an [experimental Linux provider](sdma-provider.md), which links
into a complete NXP kernel build and passes 89 compiled ARM cases. The provider
implements checked allocation, bounded loading, initialization, and IRQ wiring.
Runtime activation is disabled by default; power management and physical
qualification remain unfinished. See [DMA findings](sdma-findings.md).

A [new source-built acquisition program](sdma-program.md) now implements a
cooperative pause protocol with explicit DMA completion checks. Its 106 SDMA
instructions assemble identically with two assemblers and pass 933 modeled
execution cases. The Linux provider now loads it with a padded control allocation
and coordinates exclusive ownership and pause/resume with the ADC. A connected
test of the compiled ADC/provider and assembled program passes 76 cases,
including delivered frames across a ring wrap. Device qualification remains.

The SDMA-path ADC initializer is also reconstructed in C. Its host and ARM
builds match the original kernel's modeled I/O traces in eight scenarios,
with bounded polling added for a stalled peripheral. See
[ADC reconstruction](adc-findings.md) for the register settings, comparison
command, and remaining driver-integration work.

Start, stop, and release are reconstructed in the same component. Host and
ARM builds match 72 modeled stock-kernel cases, with bounded shutdown in ten
additional stalled-peripheral/queue cases. These checks cover the portable
component; experimental Linux integration is described below.

The ring reader is also reconstructed: 270 synthetic cases match the stock
sample payload, metadata, and state changes. It initializes the full output,
rejects short buffers, and validates status after skipped placeholders; the
archived reader does not. See the same ADC document for reproduction and limits.

An experimental [Linux ADC module](kernel-integration.md) now connects those
functions to the stock kernel's real SDMA exports. It builds against the matched
NXP headers, reproduces the three shared-object CRCs, and matches all 42 stock
imports. Its compiled interfaces and lifecycle paths pass 64 emulated cases,
including resource cleanup and modeled PM reference accounting. It is not loaded
or qualified on the headset. A second build against the reconstructed provider
matches all 44 imports and passes 78 cases, including resource retention when
a DMA stop cannot be confirmed. That configuration requires the managed script.

The adapter also reconstructs the stock internal test-signal command. Three
ordered-I/O comparisons match the stock ARM routine, and faults at all ten SPI
transactions return bounded errors with power-off cleanup. It requires stopped
acquisition; close/reopen restores normal inputs. See [ADC findings](adc-findings.md)
for verification and the limits of using a test waveform.

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

The allowlist also includes the separate EEG DMA program and its startup
loader, the old acquisition utility, and two additional board DTBs. The old
utility requests a different record size from the shipped driver; do not run
it as a current acquisition example.

### Recover editable EEG DMA assembly

```sh
python3 development/sdma_disassemble.py /private/work/inspection/ads_sdma.bin \
  /private/work/ads_sdma.asm
```

The decoder creates a new mode-600 output file without executing the program.
The original 53 instructions have been decoded and independently reassembled
byte-for-byte. For independent reproduction, obtain the two external assembler
inputs and verify the hashes listed in [DMA findings](sdma-findings.md). Unpack
Billauer's archive into `/private/work/sdma_asm/` and save Petri's variant as
`/private/work/sdma_asm.pl`. On a little-endian host:

```sh
umask 077
PERL5LIB=/private/work/sdma_asm perl /private/work/sdma_asm.pl \
  /private/work/ads_sdma.asm > /private/work/ads_sdma.rebuilt.bin \
  2> /private/work/sdma-assembler.log
cmp /private/work/inspection/ads_sdma.bin /private/work/ads_sdma.rebuilt.bin
```

This reproduces one component, not a replacement firmware. No sysfs write,
device access, or firmware installation is part of this workflow.

### Verify the independent sample decoder

`eeg_samples.decode_driver_record` converts a saved 16-byte driver record
using an explicitly supplied hardware version. It is not a reader for native
`eeg.data` files, which already hold converted float samples. Channel mapping,
polarity, and validation evidence are in [source findings](source-findings.md).

The optional comparison uses the reviewed `nano_core` only as a private source
of one small ARM routine; it does not start the application:

```sh
/private/work/venv/bin/python development/verify_eeg_decoder.py \
  /private/work/inspection/nano_core
```

This requires the pinned analysis dependencies below, including Unicorn.
The verifier generates all test data internally and prints only aggregate
comparison counts and hashes. A match proves tested arithmetic and formatting,
not live recording fidelity or electrical behavior.

### Recover kernel interfaces

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
  tests.test_compare_exports tests.test_sdma_disassemble \
  tests.test_eeg_samples tests.test_eeg_quality -v
```

The suite covers malformed/truncated input, archive link and duplicate rejection,
device-tree bounds, module-version records, SDMA and sample decoding, and
host/ARM feature behavior. The ARM comparison requires `qemu-arm`; without it
only the host example is tested.

With the private symbolized kernel and `nano_core`, the two differential
verifiers provide additional coverage against original ARM instructions:

```sh
/private/work/venv/bin/python development/verify_eeg_decoder.py \
  /private/work/inspection/nano_core
/private/work/venv/bin/python development/verify_adc_init.py \
  /private/work/inspection/kernel.elf
```

The ADC verifier requires both `cc` and `arm-linux-gnueabihf-gcc`. Its hardware
responses are modeled, so passing it does not qualify the source for flashing.
