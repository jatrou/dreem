# Bluetooth ownership and recording policy

The saved core assumes one Bluetooth peer and deliberately powers the controller
off during several recording-related transitions. The independent
[GATT capture client](bluetooth-capture.md) therefore needs a cooperative owner
integration before it can support a continuous wireless sensor alongside the
stock recorder. Using BlueZ D-Bus alone does not resolve these restrictions.

These findings were verified from the exact stock core on October 3, 2026
(America/New_York), using isolated original ARM execution. They are not a live
controller test. No headset program, pairing record or radio setting was changed.

## The single peer slot

The proxy-added callback at `0x38538` handles an already-connected
`org.bluez.Device1`. The property callback at `0x38828` handles `Connected`
changes. Both route a connection to `0x38318`, without a companion-versus-sensor
classification in those paths.

The connection helper stores one address at Bluetooth context offset `0x18` and
uses the seconds field of a monotonic timestamp at `0x105c` as its occupied flag.
When empty, it saves the peer, sets the core's connection flag and queues event
12. This event comes from a connection callback; it does not by itself prove
authenticated pairing or bonding.

When that timestamp is nonzero, a new connection instead issues
`bluetoothctl remove` for the incoming address. Replay confirms this for both a
different synthetic peer and a repeated callback for the same peer. The original
address remains tracked and no second connection event is queued. If removal
fails, the old slot still remains; there is no verified rollback of the second
physical connection. This is a software ownership restriction, not evidence
that the radio hardware can support only one link.

In the pinned public BlueZ 5.52 source, `client/main.c` implements that command
through the adapter's `RemoveDevice` method. `doc/adapter-api.txt` states that
the method also removes pairing information. Those files are in the
[public release archive](https://www.kernel.org/pub/linux/bluetooth/bluez-5.52.tar.xz).
The verifier records the attempted command and never executes it; successful
device removal is not established by this analysis.

A disconnect for another address leaves the tracked peer unchanged. A disconnect
for the tracked address clears its address, timestamp and connection flag, then
queues event 13. The replay uses disabled notification flags; the additional
active-notification shutdown branches are outside its tested scope.

The timestamp is also an imperfect occupancy test: a failed `clock_gettime`, or
a successful reading in monotonic second zero, leaves it zero. Both tested
conditions allow a later peer to overwrite the slot. A replacement should use
explicit ownership state and checked clock results.

## Recording and radio transitions

The event worker at `0x64c8c` dispatches event IDs 1–48 through a table at
`0x64dc4`. Its 48 entries have 45 distinct destinations. The relevant replayed
transitions below use the original numeric state values; they are not recovered
vendor enum declarations.

| Input and starting state | Result | Bluetooth consequence |
| --- | --- | --- |
| Record-started event 31 | State 7 | This branch itself does not change radio power |
| Tracked peer disconnect, event 13 in state 7 | State 6 | Stops Bluetooth audio helpers and attempts controller power-off |
| Short button, event 18 in state 6 | State 8 | Attempts controller enable and requests a 60-second event timer |
| Short button, event 18 in state 8 | State 8 | Refreshes that timer without another enable sequence |
| Connection event 12 in state 8 | State 7 | Stops the event timer |
| Timer event 14 in state 8 | State 6 | Attempts controller power-off |
| Recovery-record event 32 in states 6 or 7 | State 6 | Attempts power-off before the nested recovery recording-start request |

A connected replay follows the tracked disconnect into the dispatcher, then
the button and timer events, and verifies the `7 -> 6 -> 8 -> 6` sequence and
ordered off/on/off commands. Timer requests and controller responses are
synthetic; no timer actually elapses and no radio changes state.

Pause at `0x36cb8` calls the background/exercise stop helpers and the controller
disable routine. Resume at `0x36d70` calls enable. Both return success even after
the tested nested failures. Enable at `0x86f18` can retry controller-existence
checks ten times with one-second sleep requests. Its configuration sequence
requests power-off, LE/BR-EDR support, connectability, bonding, discovery, name,
advertising and power-on. Each configuration command's result is ignored.

Disable at `0x870cc` requests discovery-off, advertising-off, connectability-off
and power-off. Its initial shell-result gate compares against literal 1, while
[`system()` returns an encoded wait status](https://man7.org/linux/man-pages/man3/system.3.html).
For example, an ordinary shell exit code 1 is status 256. Replay confirms that
256 still enters the disable sequence, and command failures still return success.
Neither helper's return value establishes actual controller state.

## Recovering usable analysis

The earlier Ghidra analysis missed the event switch and emitted a short function
with an unresolved indirect call. `ghidra/ExportBluetoothPolicy.java` checks the
imported core's recorded hash and the table bytes, applies a temporary switch
override and checks that the decompiler recovers the complete set of 45 destinations. Its private output
includes a mapping from event IDs to the address-based case labels.

The script also repairs the imported prototype of
[`dbus_message_iter_get_basic`](https://dbus.freedesktop.org/doc/api/html/group__DBusMessage.html):
it has both an iterator and an output-pointer parameter. The previous one-argument
prototype caused the decompiler to treat a stack boolean as unchanged and hide
the `Connected=true` branch. Correcting the prototype restores that branch;
the ARM replay independently verifies both true and false cases.

The export contains the event worker and seven Bluetooth helpers. It remains
decompiled analysis, with inferred types, boundary calls and address-based switch
labels, rather than original or directly rebuildable source. It is kept outside
this repository. The tool creates a new mode-0600 file, refuses existing paths,
and rolls back its analysis overrides. A fresh unmodified export still exhibits
the earlier switch/prototype limitations, confirming they were not persisted.

With an already analyzed private `dreem-core` Ghidra project:

```sh
/private/ghidra/support/analyzeHeadless /private/ghidra-projects dreem-core \
  -process nano_core -readOnly -noanalysis \
  -scriptPath development/ghidra \
  -postScript ExportBluetoothPolicy.java /private/new-bluetooth-policy.c
/private/venv/bin/python development/verify_bluetooth_policy.py \
  /private/inspection/nano_core > /private/bluetooth-policy.json
```

The verifier requires `pyelftools` and Unicorn. It passes 78 cases covering
controller gates, per-command failures, existence retries, ignored pause/resume
errors, 16 selected state transitions, both peer callbacks, removal failures,
unrelated/tracked disconnections, zero timestamps and the connected event
sequence. Result fixture SHA-256:
`1efce308bac418d4f16ac602f4d6843fce93096c5731a30e3e9679de73cac031`.

All system/D-Bus/thread/clock services are modeled. Nested audio, recording,
security, gesture, LED and timer managers are explicit stub boundaries; their
side effects and scheduler interactions are not reproduced. Recovering the
48-entry dispatch table does not mean all event/state combinations have been
verified. The separate peer and controller checks operate only on synthetic
addresses and supplied responses.

## Integration consequence

An additional BLE sensor needs its own peer classification and connection
lifetime, without using the companion's single slot or triggering its removal
path. The controller policy also needs to account for an active sensor when
handling recording, button and timeout events, while preserving deliberate
shutdown and recovery behavior. These requirements apply whether the sensor
uses a raw ATT socket or BlueZ D-Bus.

The [peer-separation overlay](bluetooth-peer-overlay.md) now implements the
classification at the two original connection call sites, with original-ARM
callback checks and separate executable-loader fixtures. It creates a private
candidate without installing or starting it. Controller power coordination
remains, followed by qualification of real controller capabilities, recording
fidelity and power use. An external radio or wired
sensor is another hardware route, but would require physical interface and power
verification. No vendor executable is bundled in the repository, and no on-device
handoff has been qualified.
