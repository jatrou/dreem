# Source-built Bluetooth sensor capture

`bluetooth_capture.c` is an independent Linux BLE GATT client. It discovers one
requested service/characteristic pair, reads a complete value or subscribes to
updates, and writes private JSONL output. The static ARM/uClibc build uses public
BlueZ 5.52 source plus the small checked patch in this directory. It links no
extracted vendor objects and does not require Dreem's original application source.

On October 3, 2026 (America/New_York), the host, ARM-emulated and host sanitizer
builds passed 13 test groups. All sensor traffic in those tests was synthetic.
This client has not been deployed or physically qualified on the headset.

## Build from public source

Inputs are the pinned
[BlueZ 5.52 archive](https://www.kernel.org/pub/linux/bluetooth/bluez-5.52.tar.xz)
and a compiler. The archive SHA-256 is
`f7144ce2039202cfac18ccb52426efea11c98e4f6e1bb8041bcb994b8378560a`.
The builder requires Python with `pyelftools`, GNU `patch`, and a Linux compiler.
The verified ARM toolchain is Bootlin ARMv7 EABI hard-float/uClibc stable
2018.11-1, GCC 7.3.0, as used by the [source matcher](extension-routes.md).

Each output directory must be new. It is created with mode 0700 and retains
selected upstream sources and headers, license texts, the patch, the application,
all object files, the executable and a `build.json` containing commands and
hashes. No upstream configure scripts or firmware binaries are executed.

```sh
/private/venv/bin/python development/build_bluetooth_capture.py \
  /private/bluez-5.52.tar.xz /private/capture-host \
  --compiler cc --target host
/private/venv/bin/python development/build_bluetooth_capture.py \
  /private/bluez-5.52.tar.xz /private/capture-arm \
  --compiler /private/toolchain/bin/arm-linux-gcc --target arm
/private/venv/bin/python development/build_bluetooth_capture.py \
  /private/bluez-5.52.tar.xz /private/capture-asan \
  --compiler cc --target host --sanitize
```

The dependency set contains 14 public C files: the ATT/GATT client, database and
helpers; queue, crypto, ECC and utility code; mainloop I/O, timeout, mainloop and
notification support; and the Bluetooth/UUID utilities. Some dependencies were
outside the 20 matching objects in the earlier comparison. Building them here
does not establish that every dependency matches the vendor build. The separate
source matcher still uses unmodified upstream source as its reference.

`bluez-capture.patch` changes the public GATT client in three places:

- Failed subscription registration releases its list reference and decrements
  its count; an invalid zero ATT error becomes an explicit failure. The tested
  single-subscription failure leaked 64 bytes with unmodified BlueZ.
- A long read exceeding the 512-byte attribute limit fails, instead of silently
  clipping the returned value to 512 bytes.
- A notification/indication shorter than its handle is rejected before decoding.

The application also observes Service Changed updates and stops before accepting
later values from a potentially stale characteristic. Database removal remains
a fallback. It does not attempt automatic rediscovery or reconnect.

## Connection and ownership

Every invocation requires `--service`, `--characteristic`, `--mode`,
`--duration-ms`, `--max-values`, and `--output`. UUIDs may be 16-, 32- or 128-bit.
Duration is 1–3,600,000 ms and the value limit is 1–1,000,000. Read mode requires
`--max-values 1`. Missing, duplicate and incompatible options are rejected.

Choose one connection route:

- `--att-fd N`: an exclusively owned, already connected LE ATT `SOCK_SEQPACKET`
  socket on CID 4. The parent establishes ownership and security, passes the
  descriptor and closes its own copy. This process takes responsibility for its
  copy. An AF_UNIX packet socket is accepted for tests and labeled `simulation`.
- `--local ADDRESS --peer ADDRESS --peer-type public|random --security
  low|medium|high`: one explicitly selected local public-address controller and
  peer. The program binds a nonblocking LE L2CAP socket and requests the selected
  Bluetooth security level. There is no discovery scan, security fallback,
  pairing agent, controller power-up, UART attachment or daemon management.

The old BlueZ event loop requires its watched descriptors to be below 128;
launch with only the necessary inherited descriptors. The connection deadline
also covers discovery and capture. It is an event-loop limit, not a hard upper
bound on blocking filesystem writes, scheduling or final `fsync`.

Example for an already qualified connection, using placeholder addresses and
an Environmental Sensing temperature characteristic:

```sh
/private/capture-arm/dreem-bluetooth-capture \
  --local LOCAL_CONTROLLER_ADDRESS --peer SENSOR_ADDRESS --peer-type public \
  --security medium --service 181a --characteristic 2a6e \
  --mode notify --duration-ms 30000 --max-values 100 \
  --output /private/new-sensor-values.jsonl
```

This is an interface example, not evidence that a particular headset/controller
supports concurrent sensor capture. UART2 remains owned by the existing
Bluetooth transport. A native integration must first establish controller
capabilities, connection ownership and coexistence with the recorder.
The [original core policy](bluetooth-policy.md) now shows specific obstacles:
its D-Bus callbacks track one peer and attempt removal of another, while several
recording-related events power the controller off. The capture client does not
change those policies and is not yet a cooperative on-device integration.
The separate [private peer overlay](bluetooth-peer-overlay.md) now implements
sensor-address separation in the original callbacks. Recording-related radio
power changes still require coordination before a continuous sensor session.

Discovery requires exactly one matching characteristic across matching services.
Reads require the read property. Subscription accepts notification or indication
properties and writes the characteristic configuration descriptor (CCCD). BlueZ
can also enable the standard Service Changed indication during discovery, even
in read mode. The command therefore does not promise a completely read-only
interaction with a peer's configuration.

On exit the client releases its socket; it does not guarantee the final
indication acknowledgement or CCCD-disable write is transmitted before closing.
Only nonterminal indication acknowledgements are verified. A parent retaining
another socket reference can prevent disconnection. Peer subscription state,
especially on bonded connections, needs native qualification; closing is not a
claim that all remote state was reset.

## Output and failures

The output must be a new path. Existing files and final-component symlinks are
refused; creation uses mode 0600 and `O_EXCL`. Use a private parent directory.
The program writes no captured values to stdout and omits device addresses from
the output. Raw sensor payloads may themselves be sensitive; private permissions
are not an automatic clearance to publish captured values.

JSONL contains a request, the selected handle/properties, a subscription record
when applicable, zero or more raw hexadecimal values, and an end record.
Each value has a sequence number and `CLOCK_MONOTONIC` receive timestamp in
nanoseconds. These are local receipt times, not sensor acquisition times or wall
clock. Multi-packet long reads do not guarantee one atomic sensor snapshot.
Calibration, units and unobserved sample loss remain unknown; a sequence number
counts delivered values and does not establish sensor continuity.

Exit 0 means a completed read, a reached value limit, or elapsed duration with
at least one value. Timeout with no values, connection/discovery/read/subscription
errors, ambiguous selection, service changes and output failures return 1.
Invalid options or inherited sockets return 2. SIGINT/SIGTERM return 128 plus
the signal number. The final process exit status is authoritative: failure of
the final flush, `fsync` or close can leave an earlier end record with status 0.

## Verification and remaining work

```sh
DREEM_BT_CAPTURE_HOST=/private/capture-host/dreem-bluetooth-capture \
DREEM_BT_CAPTURE_ARM=/private/capture-arm/dreem-bluetooth-capture \
DREEM_BT_CAPTURE_ASAN=/private/capture-asan/dreem-bluetooth-capture \
/private/venv/bin/python -m unittest discover \
  -s tests -p test_bluetooth_capture.py -v
```

The ARM runner uses `qemu-arm -cpu cortex-a7`. Each executable needs its sibling
`build.json`, source and object files because the tests also link a separate
connection harness. With no executable configured, the suite explicitly skips;
that is not a passing qualification.

The independent Python peer exchanges real ATT packets with the source-built
client over a UNIX packet socket. The 13 groups cover exact 0/2/44/512-byte reads,
notifications and indications, absent/ambiguous characteristics, properties,
authentication and subscription errors, oversized reads, Service Changed,
short notifications, deadlines, interruption, disconnect/malformed discovery,
existing-output preservation and invalid arguments.

The connection group links a separate test-only wrapper around socket calls. It
checks local/peer addresses, address type, CID and requested security; immediate
and pending connection; setup/completion failures; stalled-connect timeout and
interruption. This executes the application's connection logic on host and ARM
but does not exercise Linux Bluetooth or a physical radio. Synthetic addresses
are used throughout. The normal capture binaries contain no wrapper.

Physical ARM execution, pairing/security enforcement, controller limits,
coexistence, radio loss, battery/CPU/storage load and unchanged EEG recording
fidelity remain unverified. The old BlueZ dependency has not undergone a general
security audit; these repairs cover the exercised capture paths. No bus scan,
device-node read, radio access or vendor-manager replacement is part of this
qualification.

## Source licensing

The independent C application and connection harness use GPL-2.0-or-later;
the Python builder and peer tests use Apache-2.0. The patch retains
LGPL-2.1-or-later. Public BlueZ files retain their own notices: most shared code
uses LGPL-2.1-or-later, `lib/bluetooth.c` and `lib/uuid.c` use GPL-2.0-or-later,
and `ecc.c` uses its BSD notice. The complete linked executable cannot be
described as Apache-only or LGPL-only.

This repository publishes source, the patch and build instructions, not a
compiled executable or Dreem firmware. The private build retains inputs and
objects for inspection/relinking. Distribution of a compiled build would also
need its applicable corresponding source and notices, including linked
toolchain runtimes; `build.json` alone is not that source package. See
[third-party notices](../THIRD_PARTY_NOTICES.md),
[GPL text](../LICENSES/GPL-2.0-or-later.txt) and
[LGPL text](../LICENSES/LGPL-2.1-or-later.txt).
