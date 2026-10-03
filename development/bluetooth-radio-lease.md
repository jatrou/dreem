# Bluetooth capture and recording power coordination

The private core overlay now has an optional radio-lease integration. A running
[capture client](bluetooth-capture.md) can request that two normal recording
transitions keep an already enabled controller available. The event worker
attempts the deferred power-off when the request expires or disappears. The
companion's button window restores its peripheral settings without cycling an
already-up radio. Recovery and other disable callers revoke the deferral.

This is implemented source and an offline-qualified private candidate. It has
not been installed or started on the headset. It does not enable a controller
that is already off, acknowledge that a running core accepted the lease, or
establish simultaneous physical sensor/companion support. Those startup,
deployment and native recording checks remain necessary for an on-device feature.

## Capture ownership

The optional direct-connection argument is:

```text
--radio-lease /run/dreem-extension-radio
```

The exact peer must already be in the private overlay's immutable sensor table.
The directory argument must match the core's fixed directory above. The option
is rejected with `--att-fd`, whose connection identity is supplied externally.
It does not replace Bluetooth security settings or authorize an unknown peer.
The controller must already be enabled through its original owner before
capture connects; this code does not take over UART attachment or startup.

The capture process opens an owner-only mode-0700 directory and an owner-only
regular mode-0600 `owner.lock`, then takes an exclusive nonblocking `flock`.
Another capture cannot replace or delete that owner's lease. The lock file
remains after normal exit; deleting it while another process might hold it
would create competing lock identities. It is not a secret or a live lease.

Capture publishes `lease` with mode 0600 by writing a new `lease.tmp` and
atomically renaming it. It renews every second with an expiry three monotonic
seconds ahead. Renewal failure stops acquisition. Normal exit, connection
failure, timeout and SIGTERM remove the lease after closing capture resources.
SIGKILL can leave a file, but renewal stops and that request expires. A transient
lease needs no durable `fsync`; a partially written temporary file is never
accepted as the published request.

The 52-byte format is deliberately bounded:

| Bytes | Meaning |
| --- | --- |
| 0–7 | ASCII `DRBTL001` |
| 8–23 | Current Linux boot UUID, parsed into 16 bytes |
| 24–27 | Little-endian uint32 monotonic expiry seconds |
| 28–31 | Little-endian uint32 expiry nanoseconds |
| 32–49 | Exactly 17 address characters followed by NUL |
| 50–51 | Reserved, zero |

The core uses ARM Linux `openat`, `fstat64`, `read`, `close`, `geteuid32` and
`clock_gettime` syscalls, with checked kernel structures from the compiler's
UAPI headers. It opens the directory without following its final symlink, then
opens the file relative to that descriptor with `O_NOFOLLOW` and `O_NONBLOCK`.
It checks type, exact permissions, same effective owner, one link and exact file
length. It reads at most 53 lease bytes and 38 boot-ID bytes. Invalid, missing,
expired, overlong, future-dated beyond three seconds, wrong-boot or unlisted-peer
requests cannot defer power-off. Short/interrupted reads and syscall failures
also decline the request. Descriptors close before a cancellation point.

## Original core integration

The builder still requires the exact core identified in
[peer separation](bluetooth-peer-overlay.md). With `--radio-lease`, it redirects
six checked call instructions: the two peer calls plus these four:

| Call site | Integration |
| --- | --- |
| `0x870e4`, original disable probe | Revoke old deferral for other callers; retain this invocation's probe result |
| `0x87130`, original disable power-off | Defer only a checked normal recording pause with an UP result and valid lease |
| `0x86fd8`, original enable probe | Restore peripheral settings if the controller was held up |
| `0x64d64`, event worker wait | Reconcile a deferred lease while waiting for the next event |

The two permitted pause return addresses are `0x67a30` (disconnect event 13,
state 7 to 6) and `0x676e8` (timer event 14, state 8 to 6). Both call pause, which
returns from disable at `0x36cf8`. The shims read those exact saved return
addresses and require current state 6. They inspect the parent pause frame only
when the immediate caller matches it.

In this pinned disable routine, the local word at `sp+0` is unused and `sp+4`
holds the original cancellation state. The probe shim stores its result at
`sp+0`; the power-off shim passes that value to the guard. This avoids sharing a
probe result between caller threads. A zero/UP probe is required. Failed or
down-controller probes preserve the original disable behavior. The existing
unusual literal-1 skip gate remains, but cannot leave an old deferral attached
to a recovery/stop call.

Discovery-off, advertising-off and connectable-off still execute during the
permitted recording pause. Only its final power-off command can be suppressed.
Recovery event 32 and non-whitelisted disable callers preserve the original
power-off request and clear deferral before the probe decision. The integration
never powers a controller back on after that revocation.

While a deferred stop exists in state 6, the event worker checks its real
semaphore with `sem_trywait` before checking the lease. An already queued event
takes precedence at that check. Idle polls are 250 ms and include
`pthread_testcancel`; no event is invented to perform the deferred stop. Other
states and absence of deferral use the original checked blocking wait.

When the lease is invalid, the worker requests the fixed power-off command.
Successful command completion clears deferral; a failed command or inability to
guard cancellation is logged and retried after a one-second sleep request.
Cancellation is disabled around that command and its state update, then restored.
This is not a hard three-second physical shutdown guarantee: scheduling, queued
work, filesystem operations and the inherited `system()` command can take longer,
and a command can fail. The controller's actual state still needs native proof.

When a held controller is already UP, stock enable would otherwise skip all
configuration. The probe wrapper requests connectable-on, bondable-on,
discovery-on and advertising-on, without power-off/on. Command failures are
logged. It retains the original probe result because returning a manufactured
failure would make the original function power-cycle the sensor connection.
That return value does not prove successful restoration. A genuinely down
controller retains the original full initialization sequence.

The optional profile adds a separate read/write, non-executable segment at
`0x01020000` for one atomic deferral word. Code and constants remain read/execute.
Program headers still satisfy the Linux 4.1 `AT_PHDR` calculation. Original
entry, interpreter, dependencies and segments remain in place, with only the
declared call instructions and ELF header fields changed. The added highest
segment changes initial heap placement; independent loader tests do not prove
every vendor runtime assumption.

## Build and reproduce

Build the capture client normally, then use the optional overlay profile:

```sh
/private/venv/bin/python development/build_bluetooth_peer_overlay.py \
  /private/inspection/nano_core /private/peers.json /private/radio-overlay \
  --compiler /private/toolchain/bin/arm-linux-gcc --radio-lease
```

The patched output remains private, mode 0600 and non-executable. It contains
vendor firmware and is not added to the repository. Build manifests retain
source hashes and commands. No startup file is modified and no deployment
command is supplied by this milestone.

Verification on October 3, 2026 (America/New_York):

- **78 original cases** still match with an empty sensor table.
- **68 original-ARM radio cases** cover the two allowed transitions, expiry,
  renewal, pending-event handling, metadata/syscall failures, recovery, direct
  disable, the original skip gate, unsuccessful UP probes, restoration failures
  and retry after power-off/cancellation-guard failure. Fixture SHA-256:
  `6bc9ad84f179738ae983533b2b353eaf8a5363ac7d3331a3ade9aac8e73ada69`.
- The source-built capture suite passes **17 groups** on host, ARM and host
  ASan/UBSan: **114 synthetic captures**, including 15 with leases. Nine lease
  flows feed actual published records from those capture
  processes into the compiled ARM reader and original event paths. ATT,
  connection syscalls and controller commands remain synthetic.
- Real-file tests cover exclusive publication, atomic replacement, private
  permissions, symlink/FIFO rejection, wrong boot, unknown peer and descriptor
  cleanup. The ARM reader executes its actual kernel syscalls under QEMU.
- Real pthread fixtures check cancellation, lease release and event wakeup on
  host, static ARM/uClibc and dynamically against the firmware's uClibc 1.0.31.
  Separate loader fixtures verify both RX and RW additions, constructors, BSS,
  heap, threads/TLS and entry execution. They contain no vendor program.

```sh
DREEM_OVERLAY_CC=/private/toolchain/bin/arm-linux-gcc \
  DREEM_OVERLAY_SYSROOT=/private/firmware-runtime \
  /private/venv/bin/python -m unittest \
  tests.test_radio_lease tests.test_bluetooth_peer_overlay -v
/private/venv/bin/python development/verify_bluetooth_radio_overlay.py \
  /private/inspection/nano_core /private/empty-radio-overlay /private/sensor-radio-overlay
# Set the three capture binary paths as in bluetooth-capture.md; these two
# additional inputs enable the capture-to-original-ARM lease checks.
DREEM_BT_RADIO_CORE=/private/inspection/nano_core \
  DREEM_BT_RADIO_OVERLAY=/private/sensor-radio-overlay \
  /private/venv/bin/python -m unittest tests.test_bluetooth_capture -v
```

The radio verifier's file metadata, clock, semaphore, controller responses and
nested managers are modeled. It is not a full core scheduler or a physical radio
test. A [read-only startup inventory](startup-inventory.md) now collects bounded
process/descriptor observations for this work. The next steps remain
startup/activation ownership, a native rollback path,
and real recording, sensor, latency and battery qualification. Peer classification
and a valid local lease alone do not complete those checks.
