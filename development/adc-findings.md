# ADC acquisition reconstruction

Verified offline on October 2, 2026 against the stock kernel identified in
[source findings](source-findings.md). This reconstructs the initialization
used by the SDMA acquisition path, its start/stop/release sequences, internal
test-signal setup, and ring-to-record conversion. It is not a complete Linux
acquisition driver.

## Source and transport boundary

`ads129x_init.c` contains independently written C for the power, SPI-command,
identification, register setup, acquisition control, and sample extraction.
It compiles for the host and
Cortex-A7. A callback supplies ordered MMIO, GPIO, and delay operations; the
verification tools supply synthetic operations only. The separate
[Linux research adapter](kernel-integration.md) now supplies a hardware-facing
transport and file operations, but has not been installed or tested on the
headset. The portable checks below continue to use emulation only.

The experimental adapter implements ordered MMIO, resource ownership, and the
character-device interface using the stock SDMA provider. Full reconstruction
still needs SDMA channel/script setup and interrupt handling; lifecycle,
suspend/resume, and hardware testing remain open. It must not run alongside
the existing ADC owner. An upstream kernel with this one component would still
be incomplete.

The reconstruction uses the same register addresses and observed sequence as
six stock functions: `ads1296_sdma_open`, `spi_conf_command`, `spi_flush`,
`sdma_request_disable`, `sdma_send_command`, and `sdma_write_registers`.
The verifier copies only those functions into an emulator and substitutes
synthetic GPIO, mapping, and delay calls. Firmware code is never launched as
a host process and is not bundled with the source.

## Register configuration

Successful initialization writes these register/value pairs in this order:

| Register address | Value | Register name |
| --- | --- | --- |
| `0x01` | `0x06` | CONFIG1 |
| `0x02` | `0xc0` | CONFIG2 |
| `0x03` | `0xec` | CONFIG3 |
| `0x17` | `0x00` | CONFIG4 |
| `0x04` | `0x00` | LOFF |
| `0x0f`, `0x10` | `0x00` each | LOFF_SENSP, LOFF_SENSN |
| `0x05`–`0x08` | `0x10` each | CH1SET–CH4SET |

[TI's ADS129x datasheet](https://www.ti.com/lit/ds/symlink/ads1296.pdf),
sections 9.6.1.2, 9.6.1.4, and 9.6.1.6, identifies these settings as low-power
250 samples/second at the nominal clock, internal 4 V reference, and channel
gain 1 with normal electrode input. Therefore the recorder's factor
`4,000,000 / 8,388,607` is consistent with nominal microvolts at the ADC input.
This does not measure reference accuracy, analog supply, external front-end
gain, electrode contact, or the installed unit's current registers.

The stock ID check accepts low five bits `0x10` or `0x11`, corresponding to
the four- and six-channel IDs, and ignores the upper family bits. Only four
channels are configured here. The observed CONFIG2 write includes reserved
bits that the current datasheet does not recommend; the reconstruction
preserves the observed value for comparison rather than silently changing it.

## Differential verification

Run with the analysis dependencies, host `cc`, and ARM cross-compiler:

```sh
/private/work/venv/bin/python development/verify_adc_init.py \
  /private/work/inspection/kernel.elf
```

The tool checks the raw kernel SHA-256, builds temporary host and ARM copies
of the reconstruction, and compares their complete modeled I/O traces and
return values to the original ARM kernel routines. Generated binaries are
temporary. Eight scenarios match on both reconstruction targets:

| Scenario | I/O events | Return |
| --- | ---: | ---: |
| Four-channel ID | 252 | 0 |
| Six-channel ID | 252 | 0 |
| Invalid ID followed by valid ID | 270 | 0 |
| Three rejected ID reads | 99 | -16 |
| Power GPIO setup failure | 20 | -16 |
| Chip-select GPIO setup failure | 21 | -16 |
| SPI controller already enabled | 252 | 0 |
| Three stale receive words | 258 | 0 |

Combined trace SHA-256:
`8ecc3c59df6f7788287878921c74553be3df0ffd4f5117e61f8a1c8ea1926f33`.

A ninth scenario models a stalled peripheral. The original exceeds the
emulator instruction limit in its polling loop. The reconstruction deliberately
bounds polling, returns `-110`, and finishes its cleanup with ADC power off
and chip select high. Host and ARM cleanup traces match. The polling limit is
an operation count, not a hardware-qualified wall-clock timeout.

This verifies register operations and control flow under the modeled responses.
It does not verify physical timing, bus ordering, power behavior, recording
fidelity, or every possible peripheral/error response.

## Start, stop, and release

The same C component now exposes `ads129x_sdma_start`, `ads129x_sdma_stop`,
and `ads129x_sdma_release`. Start needs initialized hardware, a configured
SDMA channel, an exclusively owned 1,024-byte ring, and queue callbacks. The
callbacks read the current producer slot and consume pending notifications.
They do not configure the SDMA channel or install its instruction program.

Start clears the error counter, issues ADC commands `0x08` and `0x10`, fills
the ring with `0x42`, places the reader one slot behind the current producer,
and drains pending notifications. It then configures ECSPI control
`0x077170f9`, configuration `0x0f`, and DMA control `0x00830000`, writes four
zero transmit words, and finally enables the SDMA request with value `2` at
`0x020ec20c`. Stop restores command mode and sends `0x11`, then `0x0a`, with
the observed chip-select changes and delays. Release also powers off the ADC.

Reproduce the separate control comparison:

```sh
/private/work/venv/bin/python development/verify_adc_control.py \
  /private/work/inspection/kernel.elf
```

This maps only the selected original functions, substitutes bounded synthetic
kernel helpers, and supplies synthetic global/ring memory. It compares return
values, ordered MMIO/GPIO/delay/queue operations, and final ring/state bytes
against host and Cortex-A7 builds of the reconstruction. **72 cases match**:

- All 64 producer slots, with varying pending-notification counts.
- Start with 63 queued notifications and an already enabled SPI controller.
- Start with stale receive data.
- Stop and release with disabled/enabled controllers and stale receive data.

Combined control trace SHA-256:
`aa493a2047e76fe60873ec3e034e0f3bec15abf30ea3c8e56b1172e9ffc3ec62`.

Ten additional cases cover absent transmit-ready status, a controller that
never finishes, an undrainable receive FIFO, and a notification queue that
never empties. The original routines exhaust the emulator instruction limit;
both reconstructed builds return `-110`, disable requests, turn ADC power off,
and deselect it. An out-of-range producer slot is rejected with `-22` and the
same shutdown. These are intentional improvements to the original behavior.
After release or an error, callers must initialize again before acquisition.

This comparison does not model concurrent interrupts, DMA writes during ring
reset, cache coherency, actual semaphore scheduling, or physical timing. The
callback transport must provide Linux ordering and exclusive resource ownership
when a real driver is implemented. No reconstructed driver is installed.

## Ring-to-record extraction

`ads129x_sdma_read_frame` implements the sample extraction behind the SDMA
character-device read. It consumes a queue notification, validates the frame
status, reorders 12 sample bytes, computes the queue-depth byte, and advances
the reader. The 16-byte output can then use the independently verified sample
decoder. The transport supplies `ADS_QUEUE_WAIT`; its blocking/timeout policy
must be implemented by the eventual Linux adapter. Five interrupted waits
return the original status `-3`.

The ring uses `0x42` placeholders after reset. The reader skips these with
the observed 66-advance bound, returning `-1` and counting an error if none
is replaced. Invalid frame status returns `-2` and counts an error. These are
the observed driver return values, not newly assigned Linux errno meanings.

Reproduce the read comparison:

```sh
/private/work/venv/bin/python development/verify_adc_read.py \
  /private/work/inspection/kernel.elf
```

Host and Cortex-A7 builds match the original sample payload, queue metadata,
return values, queue operations, and final ring/reader state across **270
synthetic cases**. They cover every reader slot with depths 0, 1, 31, and 63;
placeholder skips across wrap; invalid status; an all-placeholder ring; and
interrupted waits. The tested byte permutation is
`7,6,5,4,11,10,9,8,15,14,13,12`.

Combined sample-read trace SHA-256:
`8d8b51ffacbc3ef95b2ad75be180a4bac33c04104c81af7bdc3b037b94455c74`.

The reconstruction deliberately differs at these boundaries:

- **Trailing bytes:** the original copies 16 stack bytes after initializing
  only the first 13. Changing three synthetic prior-stack bytes changes the
  returned padding while the payload remains identical. The reconstruction
  always zeros output bytes 13–15. This reproduces a disclosure mechanism in
  the archived routine; it does not establish what any live recording exposed.
- **Short output:** the original copies 16 bytes for requested lengths 0, 1,
  12, and 15. The reconstruction rejects these before consuming a notification
  or changing output. Guard bytes remain intact on both reconstruction targets.
- **Status after placeholders:** the original accepts the first non-placeholder
  without rechecking its status. The reconstruction validates it and rejects
  malformed input with `-2`.
- **Invalid state:** misaligned/out-of-range reader offsets and out-of-range
  producer slots are rejected. No output is returned from invalid state.

ARM builds use `-mgeneral-regs-only`: the default Cortex-A7 optimization used
NEON for a buffer copy, which is unsuitable for this kernel-oriented component
without special floating-point context handling. The verified code uses general
registers and runs with emulated floating-point access disabled.

No actual userspace pointer is copied by this portable component. The separate
Linux adapter implements caller-length checks, copy retries, exclusive access,
DMA ordering, and bounded waits. Its verification scope and remaining gates
are recorded in the integration document. Concurrent DMA writes and physical
acquisition fidelity remain unverified.

## Internal test waveform

The SDMA ioctl at `0x8041daa0`, command `5`, writes `0x15` to registers
`0x02`, `0x05`, `0x06`, `0x07`, and `0x08`, in that order. Each write is preceded
by SDATAC (`0x11`). The ioctl ignores its argument. It does not reset the ring
or start acquisition, and the original function has no guard against being
called while acquisition is active.

`ads129x_sdma_test_signal` reconstructs this register sequence. The Linux
adapter requires initialized, stopped acquisition and refuses an active stream
with `-EBUSY` before touching hardware. It retains the stock command number
and ignored argument. Starting afterward uses the normal acquisition path.
Close/reopen performs the initialization reset and restores `CONFIG2=0xc0` and
`CH1SET`–`CH4SET=0x10`; no separate disable command is invented.

[TI's ADS129x datasheet](https://www.ti.com/lit/gpn/ADS1296), sections 9.3.1.3.2,
9.6.1.3, and 9.6.1.6, identifies the selected input as the internal test signal
with gain 1, the larger test amplitude, and frequency `fCLK / 2^20`. At the
nominal 2.048-MHz clock that is 1.953125 Hz. Actual clock, waveform, amplitude,
and calibration on the headset remain unmeasured. The test source bypasses the
electrode inputs, so it cannot prove electrode contact or the entire external
analog path. Stock normal-mode `CONFIG2=0xc0` also sets bits the datasheet marks
reserved/write-zero; it is preserved as observed, pending device identification
and register readback rather than silently corrected from a family datasheet.

Run the independent comparison without opening a device:

```sh
/private/work/venv/bin/python development/verify_adc_test_signal.py \
  /private/work/inspection/kernel.elf
```

Native C and Cortex-A7 C match all ordered MMIO/GPIO/delay events in three
stock-ARM comparisons, with zero, one, and three delayed status reads per
transaction. The traces contain 90, 100, and 120 events. Combined trace SHA-256:
`95629106dfd3b8580f64dc5c11ea4307e3b3f1e311f21be7f30541428c8f2b79`.
All ten command/register transactions are separately made unresponsive. The
stock routine exceeds the emulator instruction bound; both reconstruction
targets return `-110`, disable DMA requests, power off, and deselect. Null and
empty transports are also rejected. These checks do not model ADC analog
behavior or establish a physical test waveform.
