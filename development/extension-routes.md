# Hardware routes and Bluetooth source matching

The saved firmware uses UART2 for its Bluetooth controller. Its M4 wake/stop
helpers only perform control I/O for hardware version 3. The previously recorded
`v2plus_medical` identity maps to version 2 in the recorder's metadata writer.
These are offline findings, not a current device or wiring check.

Public BlueZ 5.52 source now reproduces the surviving allocated sections of
20 objects in two shipped Bluetooth archives. This supplies editable components
for a Bluetooth sensor integration; no additional sensor has been connected or
qualified alongside the recorder.

## Hardware selection

Evidence uses the exact stock core identified in [source findings](source-findings.md).
`verify_extension_routes.py` executes selected original ARM routines with
synthetic system services. It does not execute shell commands, change GPIOs,
open a device, or start the original application.

| Interface | Original-code evidence | Extension consequence |
| --- | --- | --- |
| UART2, `/dev/ttymxc1` | HCI initializer at `0x86b4c` selects `hciattach`, `bcm43xx`, 3,000,000 baud, hardware flow control for versions 0, 1 and 2 | This is the stock Bluetooth transport, not an available sensor UART |
| `/dev/ttyLP2` | The same initializer selects `rtk_hciattach`/`rtk_h5` for version 3 | This other hardware branch must not be treated as the version-2 wiring |
| `/dev/m4_control` | Wake at `0x1b33c` and stop at `0x1b5b8` return zero without I/O unless the cached version is 3 | Success from these functions does not prove an M4 is present |
| I2C4, `/dev/i2c-3` | Existing accelerometer, optical and touch ownership is documented in [source findings](source-findings.md#existing-sensor-interfaces) | Additional I2C hardware requires routing, electrical and ownership verification |

The HCI routine uses Linux GPIO 38 for the Broadcom branch and 71 for the
version-3 branch. These numbers identify software GPIOs, not connector pins.
An existing process matching `hciattach` causes an early success return without
initialization; this is not proof of an operational Bluetooth link.

The core's hardware accessor at `0x8d4e8` reads the cached word at `0xda2ad0`.
The verifier seeds that word and runs the accessor. It does not read the physical
OTP or establish the currently installed hardware identity. The metadata writer
at `0x28910`, reviewed separately, writes `v2plus_medical` for value 2.

NXP identifies [i.MX6ULL as a single Cortex-A7 processor](https://www.nxp.com/docs/en/data-sheet/IMX6ULLIEC.pdf).
Enabling other SoCs and RPMsg in a shared kernel configuration does not add an
integrated M4 to that chip. The firmware's M4 strings therefore do not establish
an extra programmable core on the photographed i.MX6ULL board. This does not
rule out every possible separately attached controller.

The M4 helpers below are **not internally hardware-gated**. Their presence is
interface evidence, not an instruction to call them on version 2:

| Helper address | Observed request | Meaning in core log |
| --- | ---: | --- |
| `0x1ae2c` | 10 | Enable microphone |
| `0x1af70` | 11 | Disable microphone |
| `0x1b0b4` | 1 | Start acquisition |
| `0x1b1f8` | 5 | Enable square test signal |
| `0x1b494` | 9 | Query M4 state |

They open `/dev/m4_control` with `O_RDWR`, pass a pointer to an initially zero
word, and store a nonnegative **ioctl return value** as the cached state at
`0xda3350`. Open/ioctl failures return 1; success returns 0. The guarded wake
uses request 3. Guarded stop polls state with request 9, then issues request 0.
It still attempts stop after eleven non-ready status responses and ten modeled
one-second waits. These are userspace call semantics, not a recovered controller
protocol or firmware implementation.

The verifier passes **37 cases**: hardware gates, control return/error paths,
stop polling, both Bluetooth initialization branches and the existing-process
short circuit. Fixture/result SHA-256:
`091058ca63565d007389c658db2a064a5fbc337264475200cf6a8225c220f7cb`.

## Public Bluetooth component source

The stock archive contains `usr/bin/bluetoothd` with a `5.52` version string,
`usr/bin/hciattach`, and two archives under `usr/local/bluez5/gatt/`:

| Archive | SHA-256 | Object count |
| --- | --- | ---: |
| `libshared-mainloop.a` | `f6327f4c4116ac40ffedeed299a2363b4fde2382da5aff0c73dca615015294f3` | 26 |
| `libbluetooth-internal.a` | `6cbce33b014774efc386a97e782f11fce5ec550afc100f3536e5c3e2f4524d89` | 4 |

All 30 saved objects lack symbol tables and relocation sections. They preserve
code/data for comparison but do not provide ordinary reusable link libraries.
They contain no recovered original C source.

`match_bluetooth_sources.py` uses the public
[BlueZ 5.52 release archive](https://www.kernel.org/pub/linux/bluetooth/bluez-5.52.tar.xz),
SHA-256 `f7144ce2039202cfac18ccb52426efea11c98e4f6e1bb8041bcb994b8378560a`.
With Bootlin's ARM/uClibc GCC 7.3.0 toolchain, `-Os -marm -mcpu=cortex-a7
-mfpu=neon-vfpv4 -mfloat-abi=hard -fPIC`, and the preprocessor definitions in
the script, it reproduces these objects without editing the public source:

- Shared library: `queue`, `mgmt`, `crypto`, `ringbuf`, `hci`, `hci-crypto`,
  `uhid`, `pcap`, `att`, `gatt-helpers`, `gatt-client`, `gatt-server`, `gap`,
  `log`, `io-mainloop`, `timeout-mainloop`, `mainloop`, `mainloop-notify`.
- Bluetooth library: `hci`, `uuid`.

The comparison checks the complete allocated-section set, types, flags,
alignment, sizes and initialized bytes: **20 objects, 85,053 initialized bytes,
and 532 bytes of zero-initialized storage**. It does not claim the other ten
objects match. The initial broader comparison found differences in `util`,
`ecc`, `hfp`, `btsnoop`, `gatt-db` and `bluetooth`; `tester`, `ad`, `shell` and
`sdp` still needed build-header/dependency work. Differences are not yet
attributed to vendor edits, compiler changes or configuration.

Because the original relocation information is gone, matching sections cannot
prove original external symbol bindings. Matching a release also does not prove
that release was the unique original source. Neither the complete daemon,
`hciattach`, radio firmware nor the proprietary Dreem application is claimed
source-matched by this check.

## Reproduce and continue

Both commands use private inputs and emit JSON evidence; vendor payloads and
decompilation stay outside this repository. The source matcher builds in an
automatically removed private temporary directory and runs no upstream scripts
or generated executables. It requires `pyelftools`, GNU `ar` and the ARM
compiler. The route verifier additionally requires Unicorn.

```sh
/private/venv/bin/python development/verify_extension_routes.py \
  /private/inspection/nano_core > /private/extension-routes.json
/private/venv/bin/python development/match_bluetooth_sources.py \
  /private/firmware_FEMTO_4.7.11_production.tar.bz2 \
  /private/bluez-5.52.tar.xz \
  --compiler /private/toolchain/bin/arm-linux-gcc \
  > /private/bluetooth-source-match.json
```

The [independent GATT capture client](bluetooth-capture.md) now builds its complete
dependency set from public source and passes host/ARM packet-exchange tests.
Device-side checks of controller capabilities, existing connection ownership,
recording fidelity and resource use remain. The existing UART2 Bluetooth link
should remain under its current owner. Wired expansion
still needs a physically verified connector/pad and electrical map; disabled
device-tree controllers alone do not supply that map.
