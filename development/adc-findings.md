# ADC initialization and acquisition control

Verified offline on October 2, 2026 against the stock kernel identified in
[source findings](source-findings.md). This reconstructs the initialization
used by the SDMA acquisition path, plus its start, stop, and release sequences.
It is not a complete acquisition driver.

## Source and transport boundary

`ads129x_init.c` contains independently written C for the power, SPI-command,
identification, register setup, and acquisition-control sequences. It compiles for the host and
Cortex-A7. A callback supplies ordered MMIO, GPIO, and delay operations; the
verification tools supply synthetic operations only. There is no hardware
backend, device-node access, or installation step. The start function can
operate only through a caller-supplied transport; these checks use emulation.

Integration into a replacement kernel still needs Linux MMIO barriers and
resource ownership, the ADC character-device interface, SDMA channel/script
setup, sample delivery, suspend/resume, and hardware testing. It must
not run alongside the existing ADC owner. An upstream kernel with this one
component would still be incomplete.

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
