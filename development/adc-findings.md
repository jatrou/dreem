# ADC initialization reconstruction

Verified offline on October 2, 2026 against the stock kernel identified in
[source findings](source-findings.md). This reconstructs the initialization
used by the SDMA acquisition path. It is not a complete acquisition driver.

## Source and transport boundary

`ads129x_init.c` contains independently written C for the power, SPI-command,
identification, and register-setup sequence. It compiles for the host and
Cortex-A7. A callback supplies ordered MMIO, GPIO, and delay operations; the
verification tool supplies synthetic operations only. There is no hardware
backend, device-node access, installation step, or recording start command.

Integration into a replacement kernel still needs Linux MMIO barriers and
resource ownership, the ADC character-device interface, SDMA buffer/event
setup, acquisition start/stop, suspend/resume, and hardware testing. It must
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
