# Linux ADC adapter

Verified offline on October 2, 2026. `kernel/adc_linux.c` connects the
[reconstructed ADC component](adc-findings.md) to Linux file operations,
ordered MMIO, GPIO, and the **existing stock SDMA provider**. It is an
experimental module, not a complete replacement kernel or a qualified driver.
No module has been installed, loaded, or bound on the headset.

## Target and dependencies

The target is the reviewed stock Linux 4.1.15 image with the nonzero hardware
revision's SDMA path. The adapter intentionally depends on these stock exports:

| Declaration | Independently reproduced export CRC |
| --- | --- |
| `struct semaphore ads_data_sem` | `0xde92eac3` |
| `u8 *sdma_ads_user_buffer` | `0x288e1fff` |
| `int sdma_queue_head` | `0x7c303094` |

The build computes these CRCs from the actual public kernel headers and those
declarations before using the recovered export metadata. A plain `char *`
declaration did not match the buffer export; `u8 *` does. CRC agreement is
evidence about the encoded declarations, not recovery of the original source
or proof of every structure layout and runtime assumption.

The NXP baseline alone does not export these objects and cannot run this
adapter without a real provider. Its matched headers and public export table
are used to build the module; the three additional entries are recovered from
the exact stock kernel. No substitute provider, modified CRC, forced-load
option, or replacement SDMA implementation is included.

Binding is disabled unless `sdma_hardware_confirmed` is explicitly enabled.
The probe additionally restricts the target to the archived Femto machine
compatible, SPI controller address `0x02008000`, its sole chip select zero, and an
already allocated SDMA ring. This flag does not itself verify the hardware
revision; a future device-side preflight must do that. The original `eeg` SPI
device must be quiescent and unbound before another driver can bind it.

## Implemented Linux behavior

- The `eeg_cdev` misc device is mode 0600 and permits one open descriptor.
  Probe requires a one-chip-select controller, so the bound ADC is its sole
  SPI device. The controller runtime-PM reference spans the open session;
  SPI bus locks cover individual control operations and are released in the
  task that acquired them. GPIO claims precede device registration.
- Initialization, start, stop, release, and frame extraction use the verified
  portable component. MMIO uses `readl`/`writel`; a DMA write barrier precedes
  request enable, and a DMA read barrier follows notification acquisition.
- Recorder ioctls `0`, `1`, and `4` implement stop, start, and error-count copy.
  Unknown commands return `-ENOTTY`. The archived test-signal ioctl `5` is
  **not implemented in this adapter yet**.
- Reads require at least 16 bytes and return exactly one record. A failed or
  partial `copy_to_user` returns `-EFAULT` while retaining the frame for retry.
  Padding is initialized by the reconstructed reader.
- Nonblocking reads return `-EAGAIN` when the queue is empty. Blocking waits
  use at most ten 100-ms semaphore waits; no data returns `-ETIMEDOUT`.
  Signals and cancellation are checked between waits. This bounds driver wait
  attempts; it is not a hard real-time scheduling guarantee.
- Stop cancels a waiting reader before acquiring the operation mutex. Driver
  removal marks the instance detached, deregisters the device, shuts down
  acquisition, and retains instance/resources until open references close.
- Suspend is refused while a descriptor remains open. Idle suspend relies on
  the stock provider's resume behavior. This conservative policy still needs
  physical power-management testing and may affect application sleep behavior.

An ADC control timeout invalidates initialization; close/reopen is required.
The interface changes above deliberately do not preserve several unsafe or
ambiguous stock behaviors. The native recorder still needs a compatibility
trial, including its response to bounded read timeouts and failed copies.

## Reproduce the build and interface checks

Use the pinned clean NXP tree, a completed baseline build, the private stock
kernel ELF, and the Bootlin GCC 7.3 toolchain documented in
[development instructions](README.md). All generated outputs stay in a new
private directory outside Git:

```sh
/private/work/venv/bin/python development/build_adc_module.py \
  /private/work/linux-imx /private/work/kernel-baseline \
  /private/work/inspection/kernel.elf /private/work/adc-module \
  /path/to/armv7-eabihf--uclibc--stable-2018.11-1/bin/arm-linux-

/private/work/venv/bin/python development/verify_adc_module.py \
  /private/work/adc-module/module/dreem_eeg_research.ko
```

The builder pins NXP commit `30278abfe0977b1d2f065271ce1ea23c0e2d1b6e`
and the reviewed raw kernel SHA-256. It uses a separate kernel output directory,
disables automatic local-version suffixes, prepares headers, computes the three
shared declaration CRCs, builds the module, and checks **all 42 imports** against
stock. The declaration-check module is removed and is never a functional
provider. The original source checkout and completed baseline stay unchanged.

The module uses the reviewed release string:
`4.1.15 preempt mod_unload modversions ARMv7 p2v8 `.
It builds with warnings treated as errors, soft-float, and vectorization
disabled. The older GCC 7.3 does not support the newer host verifier's
`-mgeneral-regs-only` option. Debug information is retained for the emulator's
structure offsets; generated modules may contain local build paths.

The recorded build's module SHA-256 is
`55081903e4be87716248988d4199ff7c64766a919dcd25f5975373b51c824bff`.
This is an evidence identifier, not a reproducible-build claim: path and build
metadata can change it. Each run writes `module-report.json` with source,
configuration, artifact hashes, checked declarations, and import results.

## Compiled-module verification and remaining gates

The emulator loads only the generated ARM ELF into synthetic memory, applies
its relocations, reads member offsets from DWARF, and stubs selected Linux
calls. The module is not inserted into the host kernel. Twenty cases pass:
short outputs, payload/padding, a partial-copy retry, empty nonblocking and
blocking queues, detached/cancelled reads, cancellation or a signal during a
wait, interrupted mutex acquisition, counter copies and copy failure, unknown
ioctl, duplicate start, stop/restart, and stalled-stop cleanup.

This exercises the actual compiled read/ioctl code and its ADC callbacks.
It does **not** execute probe/open/removal or PM, model real scheduling and DMA
concurrency, or prove device safety. Required remaining work includes:

1. Validate lifecycle and cleanup paths, especially failed initialization,
   unbind with an open descriptor, and controller runtime-PM interactions.
2. Re-establish verified headset access and recovery before any driver swap;
   confirm the actual kernel, hardware revision, active provider, and owners.
3. Compare recording bytes, dropped frames, latency, CPU, and memory during a
   reversible on-device trial; prove restoration of the original driver.
4. Validate suspend/resume and power behavior, and implement/test any further
   required ioctl behavior.
5. Reconstruct the provider's channel/script loading and interrupt handling
   for a fully source-built acquisition stack. The public NXP kernel remains
   incomplete for the board even after this ADC component.
