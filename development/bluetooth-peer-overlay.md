# Bluetooth sensor peer separation

The independently authored `bluetooth_peer_filter.c` can now run between the
original core's two connection callbacks and its single-peer connection helper.
An explicitly designated sensor no longer claims the companion slot or triggers
the helper's `bluetoothctl remove` command. Other addresses reach the unchanged
original helper. This is the first implemented part of the
[Bluetooth ownership integration](bluetooth-policy.md).

**Recording still powers the controller off.** This overlay does not yet make
the [capture client](bluetooth-capture.md) usable continuously alongside the
physical recorder. It has not been installed, and no patched vendor process has
been launched. Verification on October 3, 2026 (America/New_York) used isolated
ARM replay and independently compiled loader fixtures.

## Exact scope

The builder accepts only the 14,170,096-byte stock `nano_core` with SHA-256
`dfc83b247b505aa08ea62060f999192112a47b606754d5f469357b7addf0295a`.
It checks and redirects the ARM calls at `0x38670` and `0x38b4c`, inside the
proxy-added and Connected-property callbacks. Both already pass the peer address
to `0x38318`; both ignore its return value. That original helper remains intact.
There is no relocated vendor instruction, copied prologue, D-Bus iterator ABI
replacement, injected shared-library dependency or writable overlay state.

The immutable table contains zero to eight explicit addresses. Address comparison
accepts uppercase or lowercase hexadecimal, requires exactly 17 characters and
a terminating NUL, and performs no allocation or I/O. An empty table provides
an original-behavior control. Changing the list requires a new build and a new
process; this is not a live registration mechanism. A sensor must be designated
before that process receives connection callbacks.

This table is an ownership classification, **not authentication**. The same
address cannot simultaneously represent the companion and an extension. Rotating
addresses, BlueZ identity resolution and actual concurrent radio capabilities
remain unqualified. The filter does not pair, connect, authorize, encrypt, scan,
remove a pairing record or open an ATT socket. The existing capture client's
connection/security requirements still apply.

The original disconnected-peer comparison already ignores an address different
from the tracked companion. Replay checks that sensor disconnects leave that
slot alone and that a companion disconnect still clears it and queues event 13.
Unrelated original defects remain, including removal after a repeated companion
connection callback and zero-timestamp slot handling. This is not a complete
replacement for Dreem's connection manager.

## Executable layout

The builder adds a read/execute segment at `0x01000000`; its program-header table
comes first and the source-built filter starts at `0x01001000`. The new segment
has no write permission. Original writable segments, BSS, entry point, interpreter,
dynamic dependencies, relocation tables, section table and original function
addresses remain in place. Within the original file extent, only the two call
instructions and the ELF program-header offset/count fields change. Padding,
the new program-header table and the compiled filter are appended.

The file offset is chosen so that first-load bias plus `e_phoff` equals the new
table's mapped address. This matters because Linux 4.1 computes `AT_PHDR` that
way; changing `PT_PHDR` alone is insufficient. All original load/BSS extents must
end before the added segment, and ARM branch range/alignment is checked.

The new highest load segment also moves the initial program break. That is a
real layout change, despite the small filter. Independent loader fixtures check
dynamic linking, constructors, zeroed BSS, heap allocation, threads/TLS, the
auxiliary program-header pointer and execution from the added segment. They do
not establish that all vendor startup assumptions, watchdog behavior, runtime
integrity checks or cancellation/unwinding paths tolerate this layout. The
overlay has no unwind table and calls no cancellation point on its sensor path;
unmatched peers tail-call the original helper in the verified build.

## Private build

The builder requires Python with `pyelftools` and the public Bootlin ARMv7
hard-float/uClibc GCC 7.3.0 toolchain described in
[source matching](extension-routes.md). Create an owner-only mode-0600 JSON file
with this schema; the address below is solely a synthetic test fixture:

```json
{"version": 1, "peers": ["02:00:00:00:00:02"]}
```

Then build into a new private directory:

```sh
/private/venv/bin/python development/build_bluetooth_peer_overlay.py \
  /private/inspection/nano_core /private/peers.json /private/peer-overlay \
  --compiler /private/toolchain/bin/arm-linux-gcc
```

The directory has mode 0700. Outputs include the source snapshot, generated
peer table, linker script, compiled component, `nano_core.peer-overlay` and a
manifest with build commands and hashes. Files use mode 0600, including the
non-executable patched core. Existing output directories/files are refused. The
input core is never modified. Nothing is copied to the headset or selected for
startup. Keeping the original executable provides a rollback input, but there
is no deployment/activation procedure in this milestone.

**The generated core still contains vendor firmware.** The project license for
the new filter and builder does not grant rights to distribute that output.
The generated table also contains operator-specific addresses. Keep both outside
the public repository; only the independent source, builder and tests are added.

## Verification

Six unit/integration groups pass with host/glibc, Bootlin uClibc 1.0.30 and the
saved firmware's uClibc 1.0.31 runtime selected for dynamic ARM fixtures. The
original and altered loader fixtures both run under `qemu-arm`. Static fixtures
use their compiler's runtime; selecting the firmware sysroot does not relink
them against firmware libraries. No complete vendor application runs.

The filter harness passes **166 cases per build** on host, host ASan/UBSan and
ARM, covering all eight table positions, case normalization, exact-length
matching, pointer-preserving delegation and every truncated string ending at
a protected-page boundary. Builder checks cover malformed addresses, duplicates,
reserved addresses, private-file permissions, symlink/FIFO rejection, exclusive
creation, wrong firmware, invalid ELF layouts and branch limits.

The original-code verifier reproduces all **78 stock cases** with an empty
table, with the unchanged fixture hash
`1efce308bac418d4f16ac602f4d6843fce93096c5731a30e3e9679de73cac031`.
A table containing the synthetic sensor passes **86 cases**, including both
callback orders, repeated sensor callbacks, sensor/companion disconnects,
companion reconnects, missing properties and the still-active recording power
policy. The unmodified negative controls reproduce the original peer conflict.
Selected fixture SHA-256:
`16cf191ff21ce02ee02002fc623c597cc22736105234decdf9a196bb8121a490`.

```sh
/private/venv/bin/python -m unittest tests.test_bluetooth_peer_overlay -v
DREEM_OVERLAY_CC=/private/toolchain/bin/arm-linux-gcc \
  DREEM_OVERLAY_SYSROOT=/private/toolchain/sysroot \
  /private/venv/bin/python -m unittest tests.test_bluetooth_peer_overlay -v
# Build one empty-list control and the single synthetic-sensor candidate above.
/private/venv/bin/python development/verify_bluetooth_peer_overlay.py \
  /private/inspection/nano_core /private/empty-overlay /private/sensor-overlay
```

The replay requires Unicorn and verifies reconstructed output bytes before
mapping the component and redirecting the actual original call sites. Ordinary
callback/filter returns preserve stack balance and ARM callee-saved registers.
D-Bus, system commands, clocks and nested managers are synthetic, with the same
boundaries as the [stock policy verifier](bluetooth-policy.md).

Next is bounded sensor/controller power coordination, including restoration of
companion advertising and release of any deferred power-off after sensor exit.
Recovery and deliberate shutdown must retain their original precedence. Native
startup, recording fidelity, timing, battery cost and a real sensor session still
need qualification before this becomes an on-device feature.
