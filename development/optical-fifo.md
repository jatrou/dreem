# Optical acquisition cadence and bounded FIFO reader

`optical_fifo.c/.h` provide independent, editable source for draining the optical
FIFO through the [checked sensor transport](sensor-transport.md). This is a
research component for a process that exclusively owns the sensor. It is not
installed on the headset and does not replace the current recorder.

## Original acquisition behavior

The hash-pinned 4.7.11 `nano_core` identifies two relevant loops:

| Original block | Observed behavior |
| --- | --- |
| `0x19e18..0x19f90` | Before every fifth EEG read, post the motion and optical wake semaphores |
| `0x1cd18..0x1cde8` | Wait for optical wake, call the optical reader once, then publish one software-queue entry |
| `0x915b4..0x916cc` | With the sensor mutex held, request six bytes from register 7 and store the transfer status |
| `0x1c8dc..0x1c940` | Select the address, write the register index, and perform the requested read |

Both software rings advance by 16 bytes and wrap after 400 entries. The EEG
loop posts wakes before its corresponding device read, starting on iteration
zero. An unsuccessful or short EEG read is logged but still advances the
publication/cadence sequence. The optical producer also publishes an entry
after a short read, with its separate failure status. These code paths perform
no optical occupancy or overflow checks.

`verify_optical_cadence.py` executes the original selected ARM blocks with
modeled semaphore, mutex, clock and I/O services. Its **88 cases** cover zero,
one, fifth-iteration boundaries, both sides of ring wrap, repeated wraps, and
negative/zero/short/complete reads. It includes the actual optical read wrapper
and register helper; the six-byte request is not supplied by a replacement
reader stub. The selected instructions and one literal total **984 bytes**.
Fixture-result SHA-256:
`6287ab943a7c7eb08b53779cc1c1b35bd9308536c12951df6f31ec5ec54c8547`.

This proves iteration relationships under the modeled successful synchronization
services. It does not execute startup, error branches for failed synchronization,
queue-backpressure handling, real scheduling, or the full recorder. Conditional
on 250 successful EEG acquisitions per second, this path requests 50 optical
rows per second. That conditional rate is not a measured device clock.

The original register configuration, independently replayed in the transport
verification, requests 100 optical samples per second with no averaging. In a
deterministic model starting empty, producing two samples before each 20 ms
poll and consuming one causes the first lost sample at poll 32 (640 ms).
After 100 polls, 69 of 200 produced samples have been lost and 31 remain queued.
The new batch reader consumes all 200 without loss under the same schedule.
These are model results, not evidence that a particular recording lost samples.

## Component contract

Initialize `struct dreem_optical_fifo` with `dreem_optical_fifo_init`, then call
`dreem_optical_fifo_drain` with a separate `struct dreem_optical_batch` output.
Link `optical_fifo.c`, `optical_samples.c` and `sensor_i2c.c` into the owning
process. The component opens no device, writes no sensor configuration, scans
no addresses and performs no automatic recovery.

The caller must have an open descriptor, exclusive sensor access, stable
configuration and a verified FIFO byte boundary before initialization. The
initializer only sets software state; it does not establish those conditions.
The caller must prevent concurrent use, descriptor reuse/close, other consumers
and configuration changes. Normal I2C address selection is not an ownership
lock. Do not run this reader alongside `nano_core`'s optical manager.

Each drain reads configuration, interrupt status and FIFO pointers. It accepts
active red/infrared mode 3 with rollover disabled, retains averaging/rate/width
configuration as raw diagnostics, and reads at most 31 rows (186 bytes) in one
FIFO transaction. It checks the resulting read pointer before publishing any
decoded samples. Arrivals after the first pointer snapshot stay for a later
call. There is no loop trying to catch an unbounded producer.

| Result | Meaning and caller action |
| --- | --- |
| `0` | A bounded batch was transferred and its read-pointer advance checked; inspect flags/status before interpreting it |
| `-EAGAIN` | Equal pointers without captured fullness/loss evidence; no sample consumed, and another poll is allowed |
| `-EOVERFLOW` | Observed overflow, or equal pointers with captured almost-full status; resynchronization required, with no exact lost-count claim |
| `-EOPNOTSUPP` | Incompatible mode/configuration; resynchronization required |
| `-EPROTO` | Invalid pointer bits or unexpected pointer advance; resynchronization required |
| `-ESTALE` | A power/reset event was observed, or the reader is already quarantined |
| Other negative errno | Transport failure; resynchronization required even if the bus may have consumed only part of a frame |

Output is cleared on entry; errors publish no samples. Validity flags identify
which diagnostic registers were successfully read, independently of whether
their values passed validation. Raw ambient-light status and unexpected upper
sample bits are preserved. Counts are not pulse rate, oxygen saturation, or
evidence of healthy contact. No physical timestamps are synthesized.

Every fault except the explicitly retryable equal-pointer case quarantines the
reader. Later drain calls return `-ESTALE` without I/O. Recovery belongs to the
owning sensor lifecycle: it must verify hardware resynchronization before
calling init again. Merely clearing software state is not recovery. This
component intentionally supplies no reset sequence or automatic retry. The
separate [owned sensor lifecycle](optical-lifecycle.md) now supplies explicit
reset/start and checked shutdown, with connected host/ARM model tests. Hardware
ownership and physical qualification still belong to the integration.

## Limits of overflow evidence

The [MAX30101 datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/MAX30101.pdf)
describes a 32-sample FIFO, five-bit pointers and a saturating overflow counter.
Popping data clears that counter, and the read pointer advances after the first
byte of a sample. Reading interrupt status acknowledges its flags. Thus a
partial transfer can change framing, equal pointers are ambiguous, and loss
between snapshots can disappear from the next counter read.

The reader always marks `DREEM_OPTICAL_CONTINUITY_UNKNOWN`, including on success.
The test model explicitly demonstrates one lost sample with zero overflow in
both captured snapshots. The pointer check does not prove absence of adapter
retries, spontaneous reset or all forms of corruption. It checks a useful
invariant without presenting it as complete acquisition proof.

## Verification and remaining work

```sh
python3 -m unittest tests.test_optical_fifo tests.test_sensor_i2c -v
/private/work/venv/bin/python development/verify_optical_cadence.py \
  /private/work/inspection/nano_core
```

On October 3, 2026 (America/New_York), all **eight FIFO tests** and **five
transport tests** passed on host and ARM emulation. The FIFO suite links the
actual reader, decoder and transport against a synthetic ioctl-backed chip.
It checks all **992 nonempty pointer-position/batch-size combinations**, 100
transfer-fault combinations, empty/full ambiguity, saturated loss counts,
late arrivals, hidden overflow, bad metadata, power-ready status, unexpected
sample bits, output canaries and quarantine/reinitialization behavior. Host
and ARM results must agree. Missing ARM tools cause host-only unit coverage;
both builds were available for this qualification.

The same eight FIFO tests also passed with host AddressSanitizer and
UndefinedBehaviorSanitizer enabled, covering 1,429 synthetic result rows.

Original instructions and decompiler output remain private. The public verifier
requires the already acquired, hash-pinned executable. The tests open no sensor.
Physical identity, electrical behavior, sustained bus timing, power management,
exclusive-owner integration and physical reset/recovery remain unfinished.
These checks do not qualify deployment or establish recording fidelity.
