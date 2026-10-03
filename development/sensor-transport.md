# Sensor transport and optical sample reconstruction

`sensor_i2c.c` provides checked Linux userspace register reads and writes for
future sensor integrations. `optical_samples.c` reconstructs the existing
recorder's two-channel optical conversion. These are editable source components,
not a replacement for the complete sensor manager or a deployed headset driver.
Both are independently written under Apache-2.0.

## Original I2C failure behavior

The hash-pinned 4.7.11 `nano_core` from [source findings](source-findings.md)
contains three relevant helpers:

| Address | Operation |
| --- | --- |
| `0x1c828` | Select slave address, then write a register/value pair |
| `0x1c878` | Select address, write a register index, then read one byte |
| `0x1c8dc` | Select address, write a register index, then read a requested byte count |

These helpers start their return accumulator with the `I2C_SLAVE` ioctl result
and add one for each incomplete write/read. They continue transferring after
selection fails. A selection failure returns `-1`; exactly one later transfer
failure therefore cancels it to zero. For example:

| Operation | Address selection | Register write | Data read | Original return |
| --- | --- | --- | --- | --- |
| Write register | -1 | 0 of 2 bytes | — | 0, falsely successful |
| Read optical row | -1 | 0 of 1 bytes | 6 of 6 bytes | 0, falsely successful |
| Read optical row | -1 | 1 of 1 bytes | 5 of 6 bytes | 0, falsely successful |

The read helper writes directly into the destination, so a short read can also
leave a mixture of new and old bytes. The return feeds the optical manager's
status byte; a zero result can consequently misrepresent bus failure. These
are reproduced synthetic failure paths, not evidence that they occurred in a
particular saved recording or on the current device.

Executing the original ARM instructions reproduces **48 transport cases**,
including **11 false successes**. The actual optical register initializer also
continues to its successful return in ten modeled cases where selection and
write failures cancel at different steps. The new transport stops at the first
failed operation instead of combining unrelated return codes.

## New transport contract

Include `sensor_i2c.h` and link `sensor_i2c.c` into the process that owns the
sensor. The caller supplies its already-open i2c-dev descriptor, a nonreserved
seven-bit address, a one-byte register index, and 1–256 data bytes:

```c
int dreem_i2c_read_register(int fd, unsigned address, uint8_t reg,
                            void *output, size_t length);
int dreem_i2c_write_register(int fd, unsigned address, uint8_t reg,
                             const void *input, size_t length);
```

Each call checks adapter support for full I2C and uses normal `I2C_SLAVE`
selection before transferring. No forced-address ioctl is used. Reads send the
register index and receive the requested bytes in one `I2C_RDWR` request with
two messages; writes use one message containing the index and payload. Message
counts must match exactly. The [Linux userspace I2C interface](https://www.kernel.org/doc/html/latest/i2c/dev-interface.html)
defines the combined-transaction semantics and explicit message addresses.

The result is zero only after a complete transfer; failures return a negative
errno value. Missing capability returns `-EOPNOTSUPP`, invalid arguments
return `-EINVAL` or `-EBADF`, and an incomplete message count returns
`-EREMOTEIO`. Unexpected successful-but-nonzero setup returns are rejected.
Reads use a private receive buffer and publish it only after complete success.
The caller's output stays unchanged after a failed or partial transfer.

There are no userspace retries, because repeating a FIFO read or state-changing
write may duplicate or consume data. The kernel adapter can have its own retry
and timeout policy, which this library does not change. A failed transaction
can still have affected the sensor even though the output was withheld.
Sensor-specific recovery, register ranges and FIFO framing belong to the caller.

The matched NXP `drivers/i2c/i2c-dev.c` checks bound kernel clients during normal
address selection; `I2C_RDWR` alone does not make that check. Selection remains
a point-in-time check, not a reservation. It does not exclude another userspace
process, prevent later driver binding, or coordinate with `nano_core`'s mutex.
The caller must own the descriptor, prevent its concurrent reuse/close, and
establish exclusive sensor access. In particular, these helpers must not be
used to consume the existing optical FIFO while the vendor manager owns it.

This component opens no device, scans no addresses and selects no bus by
default. It does not set adapter-wide retry/timeouts, impose an execution
deadline, or establish electrical compatibility for an added sensor.

## Optical data and register evidence

`0x91578` converts six FIFO bytes to two unsigned 32-bit values. Each input
triplet is big-endian; the saved `pulse.data` writer at `0x27980` emits their
eight little-endian bytes. They are integer ADC counts, not floats or pulse-rate
estimates. The converter preserves all 24 bits of each triplet. The independent
`dreem_optical_decode` does the same, allowing unexpected upper bits to remain
visible rather than masking them away. It does not validate bus status.

The processor checks a separate status byte and can store two zero values for
failed acquisitions. A zero row by itself does not establish sensor failure.
The recorded-health event is code 31; see [recording events](algo-findings.md).
Hardware version 3 skips the optical file path in the inspected code.

The original initializer at `0x902b8` writes, in order:

| Register | Value |
| --- | --- |
| `0x04`, `0x05`, `0x06` | Zero to each FIFO pointer/overflow register |
| `0x08` | `0x06` |
| `0x0a` | `0x47` |
| `0x0c`, `0x0d` | Low bytes of the current red/infrared settings |
| `0x30` | `0x21` |
| `0x02`, `0x03` | `0x80`, `0x00` |

Its caller subsequently selects mode 3. Under the
[MAX30101 register definitions](https://www.analog.com/media/en/technical-documentation/data-sheets/MAX30101.pdf),
that mode produces red then infrared triplets. The configuration requests
18-bit conversion, 100 samples/second, an 8192 nA ADC range, no sample averaging
and no FIFO rollover. This interprets the programmed values; it does not measure
effective sample timing. The current datasheet does not list register `0x30`,
so its observed legacy write is recorded as evidence, not prescribed as a new
driver configuration.

The firmware names the device MAX30101, but its identity check alone is not
unique: the [MAX30102 datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/max30102.pdf)
also specifies part ID `0x15`. That check cannot prove a green LED is present.
Physical identity must be established before planning a third optical channel.

The recording processor consumes an optical row on the same every-fifth-EEG
iteration as motion. The [optical cadence and FIFO reconstruction](optical-fifo.md)
now executes the original wake/producer blocks: the EEG reader posts an optical
wake every five iterations, and the producer reads one six-byte row per wake.
A bounded source reader drains the visible FIFO batch and quarantines uncertain
framing after faults. A model demonstrates backlog under the original single-row
policy; physical timing and actual recording loss remain unmeasured. Do not
equate a file-row index with an exact physical sample timestamp or infer a
clinical measurement from these counts.

## Reproduce verification

```sh
python3 -m unittest tests.test_sensor_i2c -v
/private/work/venv/bin/python development/verify_optical_transport.py \
  /private/work/inspection/nano_core > /private/work/optical-verification.json
```

The unit suite uses a link-time ioctl replacement and opens no sensor. On
October 3, 2026 (America/New_York), all five tests passed: **137 scenarios each
on host and ARM**, with identical results. They exercise full transfers, UAPI
message layouts, boundary sizes, invalid arguments, missing adapter capability,
busy/address errors, partial messages, interrupted operations, untouched failed
read outputs and input/canary preservation. The test wrapper handles both
`ioctl` and the current ARM glibc `__ioctl_time64` symbol. Tests use the host
compiler and, when available, the ARM cross-compiler plus QEMU; the differential
verifier requires both compilers and QEMU.

The differential verifier requires the private executable and the pinned
analysis dependencies. It compares **4,193 optical records per host/ARM build**
with original instructions, including ordinary boundaries, all individual bit
positions, unexpected upper bits and deterministic random inputs. Four writer
cases cover complete and short writes. **132 register-setup cases** cover
12 LED-setting combinations and failure at each write. Ten additional cases
demonstrate error cancellation through the actual initializer. The selected
original code totals 1,272 bytes; all system calls and hardware responses are
modeled. Fixture-result SHA-256:
`1c013f3f3dd34500bde6029ed6107ea3d420dc040e0ebc4eda139eab65ab2f7d`.

No full vendor process, live adapter, physical sensor or complete recording
pipeline is executed by these checks. Sensor lifecycle, physical FIFO recovery,
measured producer timing, hardware identity, power/pad routing and device
qualification remain open. The independent transport does not patch the
installed `nano_core`, and the source has not been deployed. Firmware/decompiler
output remains private.
