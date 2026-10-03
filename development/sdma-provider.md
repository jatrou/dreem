# Experimental Linux EEG DMA provider

Verified offline on October 2, 2026. The reconstructed context and progress
functions now run inside an extension to the pinned NXP `imx-sdma` driver.
The complete kernel links, and the ADC module builds against its real exports.
This is a source-built acquisition component, **not a complete Dreem board
kernel or a firmware image qualified for installation**.

The [cooperative acquisition program](sdma-program.md) is now assembled into
the driver, with its padded control allocation and consumer ownership API.
Both this managed mode and the historical script ABI can be built and tested.
The current sleep and buffer-retention restrictions still apply.

## Build and interfaces

`build_sdma_kernel.py` requires the clean NXP revision recorded in
[source findings](source-findings.md), a completed baseline build, the reviewed
private stock kernel ELF, and the GCC 7.3 toolchain. It creates a new private
source snapshot and build directories. The original checkout is unchanged.
`apply_sdma_overlay.py` checks the hashes of all three upstream files it edits
and requires every replacement anchor to be unique.

```sh
/private/work/venv/bin/python development/build_sdma_kernel.py \
  /private/work/linux-imx /private/work/kernel-baseline \
  /private/work/inspection/kernel.elf /private/work/sdma-provider \
  /path/to/armv7-eabihf--uclibc--stable-2018.11-1/bin/arm-linux- --jobs 8

/private/work/venv/bin/python development/verify_sdma_provider.py \
  /private/work/sdma-provider/kernel/drivers/dma/imx-sdma.o \
  /private/work/inspection/ads_sdma.bin

/private/work/venv/bin/python development/verify_adc_module.py \
  /private/work/sdma-provider/module/dreem_eeg_research.ko

/private/work/venv/bin/python development/verify_sdma_pipeline.py \
  /private/work/sdma-provider/kernel/drivers/dma/imx-sdma.o \
  /private/work/sdma-provider/module/dreem_eeg_research.ko
```

The generated Kconfig option `DREEM_EEG_SDMA` requires built-in `IMX_SDMA`
and a static device tree. The builder enables it for compilation; runtime
activation through `dreem_eeg_enabled` remains false by default. Activation
also requires the Femto machine compatible and SDMA address `0x020ec000`.
The research overlay suppresses the driver's bind/unbind sysfs attributes.
No build or verifier command activates, installs, or flashes anything.

When enabled, channel 1 is reserved before ordinary DMA channels are
registered. The driver exposes root-only `user_script`, `trigger`,
`reg_r0` through `reg_r7`, and `eeg_status`. Script sizes must be nonzero,
even, at most 1,024 bytes, and fit the available program address range.
Register changes and repeated triggers are refused after startup. As in
stock, the context replaces r0-r2 with the ring address, 1,024, and counter
address; user-supplied r3-r7 remain in the legacy context. Trigger value `1`
starts that legacy ABI. Value `2` assembles no runtime input: it loads the
program built from `sdma_acquire.asm`, allocates 64 control bytes, sets r3 to
ECSPI1, and completes the initial pause before publishing the ring. Repeated
triggers cannot reset an armed channel. Never raw-upload the new program
with the legacy four-byte allocation.

The three shared objects reproduce stock CRCs from the actual provider:

| Export | CRC |
| --- | --- |
| `ads_data_sem` | `de92eac3` |
| `sdma_ads_user_buffer` | `288e1fff` |
| `sdma_queue_head` | `7c303094` |

The additional GPL export `dreem_sdma_status()` lets the ADC distinguish a
ready provider from unavailable data or a latched fault. The second GPL export,
`dreem_sdma_control()`, provides exclusive claim, pause, run, and release.
The integrated ADC has 44 imports, all checked against this kernel's generated `Module.symvers`.
With the option absent, the ADC retains its stock interface: 42 imports still
match the reviewed stock image and its 64 existing emulated cases pass.

## Memory ownership and startup

The NXP-authored [i.MX 6ULL reference manual, revision 1](https://foofoodamon.github.io/references/i.MX%206ULL%20Applications%20Processor%20Reference%20Manual.pdf)
establishes the relevant constraints:

- Table 46-12 gives instruction RAM as `0x1000..0x1fff` in 16-bit words.
  With 32 contexts of 32 32-bit words, program storage begins at `0x1800`.
  Context layout is described in section 46.7.1.4.
- Section 46.5.2.21 specifies that `done 4` clears the channel's pending
  event. It does **not** raise the ARM interrupt that `done 3` raises.
- Section 46.8.3 says STOP_STAT reads the channel enable bits. Clearing an
  enable bit is not a documented completion barrier for an in-flight DMA.

The reviewed update archive contains `ads_sdma.bin` and its startup script,
but no standard `imx/sdma/sdma-imx6q.bin`. Its kernel has an empty
`CONFIG_EXTRA_FIRMWARE`. This does not establish the contents of a live
headset's other partitions or previously installed files.

The enabled research provider uses synchronous direct firmware lookup to
avoid a late asynchronous upload overwriting an EEG script. If the firmware
is absent, it reserves program storage from `0x1800`. Other lookup errors
remain errors. External firmware must pass address-table, version, alignment,
size, and RAM-boundary checks before upload. Its address table becomes usable
only after successful upload. EEG placement after it additionally requires
`dreem_ram_tail_confirmed`: an image's length alone cannot prove that its
scripts leave the remaining RAM unused as working storage. That confirmation
is not established by this build. Direct lookup during early probe can also
precede root-filesystem availability; any device trial must establish the
actual firmware lookup and RAM ownership first.

Legacy startup checks both coherent allocations and loads the recovered 128-byte
channel context. It sets the initialization event, starts channel 1, and
waits for that event to clear, with at most 1,000 sleeps requesting 100-200 us.
This is an attempt bound, not a hard scheduling deadline. The private script's
arithmetic prefix was checked through its first scheduling instruction:
it prepares registers and executes `done 4`. Therefore startup must not wait
for the first sample interrupt. Only successful initialization publishes the
ring pointer. Publication and IRQ faults share a spinlock so publication
cannot erase a concurrent fault.

The first later legacy EEG interrupt follows the stock suppression behavior;
subsequent interrupts take one counter snapshot and notify at most 64 times.
An excessive jump latches `EOVERFLOW`, disables EEG requests and channel
priority, and wakes the reader. The managed ADC checks status before and
after waiting and before copying a frame and propagates the error. It only
issues ADC shutdown commands after an acknowledged DMA pause. This does not detect every accumulated backlog
or eliminate concurrent ring overwrite during a sample copy.

Allocation and clock failures before command submission unwind and permit
retry. After submission, possibly referenced DMA storage stays allocated.
A command timeout clears channel enable/priority, latches failure, prevents
further commands, and retains the upload buffer and clocks. It never treats
a stale completion bit as completion of the new command. Successfully
published ring/counter allocations are also retained until reboot; they are
not owned by an ADC file descriptor and cannot be freed when one closes.

## Managed acquisition handoff

The managed loader checks alignment and overlap for the entire padded control
allocation. Initialization uses fresh request `2` and requires both its exact
acknowledgement and EP clear. It does not require an ADC sample. Managed
progress starts initialized, so the first real sample is delivered rather than
suppressed. The consumer cannot claim a legacy-script instance.

Claim/pause/run/release calls are serialized by a mutex. State transitions and
IRQ accounting share a spinlock. Pause masks the event, reads that register
back, publishes a fresh even request with a DMA barrier, and explicitly wakes
the script. At most 1,000 100–200 us sleep attempts wait for matching ACK and
EP clear; a stale ACK or just one of those conditions cannot establish a stop.
After that proof the driver synchronizes IRQ handling, accounts the final
producer count, clears a pending notification, and leaves channel priority zero.
Resume preserves the counter/next ring slot, uses a fresh odd request, and
enables acquisition only after CPU configuration is finished. Generation
exhaustion returns an error rather than reusing an old acknowledgement.

The ADC claims the paused provider before initialization. Every SPI control
operation and ring reset requires a confirmed pause; event writes in its
portable transport are replaced by provider-owned event updates. Both ordinary
NXP event updates and EEG updates use one register lock and preserve other
channel bits. Test-signal setup stays paused until an explicit start.

Normal close releases the claim and controller PM reference. If pause fails,
the ADC marks the instance unusable and pins its GPIO, mappings, device, and
controller PM reference until reboot. Repeated close/remove paths retain one
extra instance reference, not an ever-growing leak. They issue no further SPI
or power-off commands. System sleep is refused for an open or poisoned ADC.
The provider retains its own coherent buffers and SDMA clocks in either mode.
An unexpected EP clear during running is a fault even if the DMA error report
could not be written. These mechanisms still require hardware qualification.

## Verification and remaining work

The actual compiled ARM provider object passes **89 synthetic cases**:
script boundaries and rejected states; allocation/clock rollback; channel-0
timeouts with stale completion; bounded initialization failure; context and
descriptor contents; startup with no interrupt; mixed IRQ dispatch preserving
channel 0; counter/index wrap; fault latching and notification; malformed
external firmware; firmware upload failures; and the external-RAM ownership
gate. The 25 managed-mode cases additionally execute the source-built SDMA
program against modeled coherent buffers: initial pause, consumer exclusivity,
first-frame delivery, stop/resume, undispatched IRQ accounting, error/timeout
retention, rejected stale/partial acknowledgements, and generation exhaustion.
For the private legacy program only its initial arithmetic is evaluated; its
transfer loop is not emulated.

The integrated ADC passes **78 compiled ARM cases**, including seven provider
fault paths: read, pending-copy retry, blocking/nonblocking wakeup, start,
test-signal setup, and open, plus seven managed ownership/failure sequences.
The stock-interface build independently passes
its 64 cases. These models check resource accounting and ordered observations;
they do not establish real concurrent scheduling or physical DMA behavior.
Generic DMA dispatch is checked only with an ordinary channel having no active
descriptor; unrelated NXP DMA workloads are not qualified by this test.

`verify_sdma_pipeline.py` connects the actual compiled ADC and provider entry
points in two ARM emulator instances, with the uploaded program running in the
instruction model. Its **76 cases** include 70 delivered synthetic frames across
a ring wrap, stop/test/restart, close/reopen, an undispatched IRQ at stop, normal
resource release, a real modeled SDMA error reaching the reader, and timeout
retention. It checks delivered bytes, ordering, queue depth, and initialized
padding. Peripheral wire timing, concurrent kernel scheduling, and physical
recording quality are not established by this connected test.

The ARM loaders share `arm_relocations.py` for MOVW/MOVT address relocations.
Both use a signed 16-bit addend before selecting the appropriate address half,
matching the pinned NXP kernel's ARM module loader. This corrects the earlier
shift-before-add handling of MOVT. Current provider/ADC artifacts have zero MOVT
addends, so their existing results are unchanged. Fifteen executed instruction
pairs cover nonzero signed addends, carry/borrow, wraparound, and three target
registers; run `python -m unittest tests.test_arm_relocations -v` in the analysis
environment. Provider, ADC, and connected-pipeline checks are rerun after the fix.

Current artifact identifiers, which include build-path/metadata effects:

| Artifact | SHA-256 |
| --- | --- |
| Integrated `vmlinux` | `8ffa24deb34e53f1e12e5334e92884a0aa739309372b2665442e80938c6483cb` |
| Integrated `imx-sdma.o` | `9270a3bc12426869dcc77b386c958ff467a0e777e8a3a0f4c42933de08be252b` |
| Integrated ADC module | `e991a25e2ae8507595ab17ea410d52857c9be096a0dea72750071b4c24a76cda` |

The manual input's SHA-256 is
`7bf1aaaa2b108e6dc9b1e6f73deefd7b955090564bf39c4a7e57a5fcc4e861fa`.
The PDF, firmware, generated source tree, kernel, and modules remain private.

The enabled research provider currently refuses system sleep. Cooperative
pause/resume is implemented and modeled; coordinated context reset after a
fault, suspend/resume, and normal memory reclamation are unfinished. A latched
DMA fault requires reboot, not repeated trigger writes.
No hot-unbind/unload lifecycle is supported. These limitations prevent using
this build as an everyday headset kernel. An optional
[bus-frequency overlay](busfreq-findings.md) reconstructs the Femto clock policy
and its high-rate sequence, including the recorder's DDR control interface.
The DDR3 transition assembly, C preparation routines, and static settings match
public NXP source. DDR initialization hazards and hardware behavior, audio, and
other board modifications still need repair, reconstruction, or qualification.
Device-side qualification still needs verified recovery, hardware identity,
recording fidelity, latency, power measurements, and restoration proof.
