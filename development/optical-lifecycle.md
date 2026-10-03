# Owned optical sensor lifecycle

`optical_sensor.c/.h` connect reset, configuration, acquisition and shutdown to
the [bounded FIFO reader](optical-fifo.md). They provide editable source for a
process that exclusively owns a compatible red/infrared sensor. This research
component is compiled and tested on host and ARM emulation; it is not installed
or physically qualified on the headset.

## Interface and ownership

Link `optical_sensor.c`, `optical_fifo.c`, `optical_samples.c` and `sensor_i2c.c`.
The public interface is:

```c
int dreem_optical_sensor_init(struct dreem_optical_sensor *sensor,
                               int fd, unsigned address);
int dreem_optical_sensor_start(struct dreem_optical_sensor *sensor,
                                const struct dreem_optical_profile *profile);
int dreem_optical_sensor_read(struct dreem_optical_sensor *sensor,
                               struct dreem_optical_batch *output);
int dreem_optical_sensor_stop(struct dreem_optical_sensor *sensor);
```

The owner supplies an already-open descriptor and explicit address. Init binds
software state only and must not overwrite a running instance. It does not
identify or reset hardware. Ownership, descriptor lifetime, concurrency and
electrical compatibility belong to the integrating process; ordinary I2C address
selection does not establish an exclusive lease. Never call this alongside
the vendor optical manager or reassign its descriptor during an operation.

Start is explicitly destructive: it resets the sensor and discards queued data
and partial-frame state. It checks the reported ID before any writes, clears
old status, requests reset, and polls the reset bit at most twenty times with
one-millisecond sleeps between pending polls. An interrupted sleep aborts.
The bound covers reset polling, not kernel I2C timeout/retry policy, scheduling,
or total wall-clock duration. Cleanup can perform one additional reset-state
read, but does not wait indefinitely for a stuck reset.

After reset, start selects shutdown with mode 3, configures the device, and
checks readback before activating it. The profile takes explicit red and IR
current register codes (0..255), and one of 50, 100, 200 or 400 samples/second.
The source chooses two channels, 18-bit conversion, range code 2, no averaging,
no rollover, and a FIFO threshold code of 6. Those are requested settings, not
physical timing or optical performance measurements. Choosing suitable current
codes requires the actual board/power/optical context; there is no default LED
current and no automatic on-head or clinical interpretation.

Both interrupt-enable registers are cleared. Configuration, current settings
and zeroed pointers are read back while shut down, then power-ready status is
checked. Start enables mode 3, verifies it, and only then arms the FIFO reader.
The verified reset and pointer initialization establish the new byte boundary.
Calling start on a running instance returns `-EBUSY` without I/O. Changing a
profile requires explicit stop/start and therefore creates a new acquisition
segment rather than silently joining incompatible data.

## Failure and shutdown behavior

The state is `UNKNOWN`, `STOPPED` or `RUNNING`. An acquisition call outside
`RUNNING` returns `-EPIPE` without I/O. An equal-pointer `-EAGAIN` remains
retryable; other FIFO faults withhold samples and trigger a bounded shutdown
attempt. The original failure stays in `last_error`, and a separate
`cleanup_error` records failure to confirm shutdown. Zero cleanup error can also
mean no cleanup was attempted because identity was not established; state is
the authority for whether a stop was confirmed.

Shutdown reads mode, rejects an unfinished reset, writes shutdown without
requesting another reset when needed, and verifies the resulting mode. A failed
write/readback leaves `UNKNOWN`, even if the model happens to show the hardware
stopped. No API closes the descriptor, releases ownership or restarts acquisition
automatically. After failed cleanup, the caller must retain ownership and arrange
explicit recovery; closing a descriptor does not prove the sensor stopped.
An explicit stop retry is supported. A subsequent explicit start always resets
and revalidates framing before allowing reads.

The FIFO batch keeps its diagnostics and continuity-unknown flag. Successful
transfer and mode readback do not prove absence of a transient reset, hidden
overflow, adapter retries, optical corruption or real sample loss. This component
adds lifecycle checks, not a complete physical recording-fidelity guarantee.

## Original cleanup comparison

The saved `nano_core` startup at `0x90758` checks part ID, calls the previously
reconstructed register initializer and activates mode 3. Static inspection of
that path finds no explicit reset polling or configuration readback.

`verify_optical_lifecycle.py` additionally executes original cleanup at
`0x905d0` through its original register helpers. In **145 modeled cases**, it
reads mode once, writes the prior value ORed with `0xc0` when that read succeeds,
and closes/discards the descriptor. There is no mode read after the command.
The return is unchanged by a failed modeled close; successful bus cleanup also
returns success when mutex destruction fails. The new library retains the
caller-owned descriptor and distinguishes confirmed shutdown from uncertainty.

The verifier adds 392 original instruction/literal bytes to the existing
transport emulator and models all I/O. It does not run the manager's full thread
cancellation/join sequence or establish actual reset timing. Fixture-result
SHA-256: `53c1a0e5dc42237e5e33704deb292c0291bd2ff5304adab4c74e3d11e077e616`.
The original transport/decoder verifier still produces its previous fixture
hash after the emulator was made extensible.

## Component documentation and identity limits

The [current MAX30101 datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/MAX30101.pdf)
defines self-clearing reset, register retention in shutdown, the mode bits and
the supported conversion settings. Software reset does not itself generate a
power-ready event. Its revision history records removal of the proximity
function from the documentation in June 2018.

The [original Maxim datasheet, revision 0, March 2016](https://www.farnell.com/datasheets/2064242.pdf),
preserved by Farnell, identifies register `0x30` as the proximity threshold.
This explains the legacy initializer's `0x30 = 0x21` write and its log label.
[Analog Devices' proximity FAQ](https://ez.analog.com/optical_sensing/a/documents/do19448/can-the-max30101-max30102-detect-when-the-medical-device-is-no-longer-attached)
also describes the feature. The new polling lifecycle disables interrupts and
does not rely on that legacy register or feature.

The ID check is a rejection gate, not a unique chip identification: MAX30101,
MAX30102 and [MAX30105](https://www.analog.com/media/en/technical-documentation/data-sheets/MAX30105.pdf)
all document part ID `0x15`. The revision byte is retained without assigning it
an undocumented meaning. This library requires a physically established
compatible sensor and does not claim qualification of all parts sharing that ID.

## Verification and deployment boundary

```sh
python3 -m unittest tests.test_optical_sensor tests.test_optical_fifo \
  tests.test_sensor_i2c -v
/private/work/venv/bin/python development/verify_optical_lifecycle.py \
  /private/work/inspection/nano_core
/private/work/venv/bin/python development/verify_optical_transport.py \
  /private/work/inspection/nano_core
```

On October 3, 2026 (America/New_York), all **25 tests** passed on host and ARM:
12 lifecycle, eight FIFO and five transport tests. The connected model executes
the actual C lifecycle, FIFO decoder and Linux transport with synthetic ioctl
and sleep services. It checks 36 rate/current profiles; faults at every one of
the 19 ordinary startup transfers (171 combinations); partial-frame recovery;
dual primary/cleanup failures; stuck and interrupted reset; configuration
readback; power loss; overflow; shutdown retry; and sustained batch polling.
Host and ARM results agree. Unit tests can run host-only if ARM tools are absent;
both builds were present for this qualification.

The twenty connected lifecycle/FIFO tests also passed under host AddressSanitizer
and UndefinedBehaviorSanitizer, producing 2,085 synthetic result rows. Original
firmware instructions, generated binaries, register captures and decompilations
remain outside the repository. No adapter or physical sensor is opened by these
tests. The current headset recovery ports were unreachable, and the desktop
reported no attached ADB device. Owner integration, board identity, electrical
validation, real reset/stop behavior, sustained bus timing and recording fidelity
remain required before deployment.
