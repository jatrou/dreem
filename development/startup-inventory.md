# Recorder startup and process ownership inventory

`startup_inventory.c` adds a source-built, read-only Linux process probe for
planning a recorder handoff. It identifies recorder/watchdog candidates and
observes existing device descriptors. It does not stop or launch a recorder,
change startup files, open sensor or watchdog devices, or establish exclusive
ownership. The private Bluetooth overlay remains uninstalled.

## Why activation needs a separate owner

The stock [control-script review](sensor-ownership.md#archived-startup-and-watchdog-behavior)
already identifies a restart race: core stop sends `TERM` before writing the
shell supervisor's hold value, and does not wait for exit. Rechecking the exact
stock archive also establishes:

- BusyBox `inittab` remounts `/` read-only before running `/etc/init.d/rcS`.
  It contains no direct recorder respawn entry. Startup through other scripts
  remains relevant.
- `fstab` mounts `/data` and `/audio` separately. A writable data partition does
  not by itself provide a boot-time activation or rollback mechanism.
- The shell supervisor searches process-list text for recorder names. A wrapper
  command line can create a false match, and multiple matches break its
  assumption of one PID. It may remove the recorder lock and start another core
  when it believes the old one is absent.
- A separate hardware watchdog remains active. The shell supervisor's zombie
  branch can kill its feeder; disabling just one supervisor is insufficient
  reasoning about the other's behavior.

These are static findings from archive SHA-256
`6e6356b51cf197a63fc73acfd7580e5f009c97569d6e716d82767ef13c15f17d`.
Additional inspected members are `/etc/inittab`, SHA-256
`2e390e6e7a9ad3aaad07840398d3f7fefbdbaecd95635e6963ff8a8fc4b8b888`,
and `/etc/fstab`, SHA-256
`ff63d2bd25b43f7c5598d58a401079d56428c4c1ef5d7de6636d1ec12545e3d8`.
They were read without executing them. The modified headset's currently
installed init scripts have not been rechecked.
The firmware inspector now includes these two files in its exact allowlist,
preserving their hashes in the private manifest:

```sh
python3 development/inspect_firmware.py /private/firmware_FEMTO_4.7.11_production.tar.bz2 \
  /private/inspection-with-init
```

## What the probe records

The production executable accepts no arguments and requires `/proc` to have
Linux procfs's filesystem type. It opens process directories relative to that
root, rejects final symlinks for those directories and textual entries, and
limits each textual read to fewer than 4,096 bytes. It scans at most 4,096
processes and 4,096 descriptor entries per process.

For recorder/watchdog candidates and processes with observed device descriptors,
the JSON contains:

- PID, parent PID, start ticks, process state, and proc-directory UID;
- separate executable-basename and mutable process-name hints;
- executable device/inode/size and a deleted-executable indicator;
- counts for EEG, DDR, I2C, SPI, UART, audio, watchdog, and other device descriptors;
- inspection errors and whether start identity, executable metadata and command
  bytes agreed at the two checks.

Raw arguments, executable paths, descriptor targets, environment variables,
recording names, device serials and network credentials are not emitted.
Command bytes are used only for identifying the shell/BusyBox watchdog forms
and detecting an observed change. Linux's kernel-thread flag allows genuinely
empty-command kernel threads without an executable link; a missing userspace
executable is reported as incomplete inspection.

Descriptor inspection uses `readlinkat` and metadata-only `fstatat`. It never
opens the link target. Hardware-looking names whose target cannot be verified
are counted as observations with an error, not verified device identities.
Unknown character/block devices appear in the `other_device` count, which can
also include benign descriptors such as `/dev/null`.

The global PID set is enumerated again after the per-process checks. Any
detected churn, permission/read error, malformed input, missing userspace
executable or limit exhaustion gives exit status **1** and
`inspection_complete:false`. Exit **0** means the prescribed inspection
completed; it is not approval to activate an overlay. Invalid invocation,
unavailable procfs or output failure returns **2**.

Both `exclusive_access_established` and `activation_ready` are always **false**.
`identity_unchanged_at_checks` compares two observations within this run; it is
not an atomic process handle, a promise of continued identity, or an image hash.
PID/start pairs also require same-boot context for comparison across reports.

## Build and run

```sh
python3 development/build_startup_inventory.py /private/startup-probe-arm \
  --compiler /private/toolchain/bin/arm-linux-gcc --target arm
```

The builder requires a new output directory, creates it with mode `0700`, and
records the source hash, executable hash and compiler command. The executable
is mode `0700`; the manifest is mode `0600`. No vendor object is linked.
The ARM build is static for Cortex-A7. Toolchain runtime licenses still apply
to generated executables.

When an independently verified root session on the headset is available, copy
only this independently built executable and capture its report in a new private
directory. For example, once it is staged at `/data/dreem-startup-inventory`:

```sh
umask 077
survey=$(mktemp -d /data/dreem-startup-survey.XXXXXX)
/data/dreem-startup-inventory > "$survey/processes.json"
status=$?
printf '%s\n' "$status" > "$survey/exit-status"
```

This example assumes a shell without `set -e`; status 1 retains a useful but
incomplete report. The command is a diagnostic, not a service-stop procedure.
Do not treat the staging path or example as evidence that deployment occurred.

## Verification and remaining work

On October 3, 2026 (America/New_York), ten test groups passed. Fixture cases run
on the host, under ASan/UBSan, and as static ARM/uClibc under QEMU. They cover
role distinctions, process names containing spaces/parentheses, deleted paths,
64-bit start ticks, kernel-thread versus missing-userspace executables,
descriptor classes, malformed/oversized input, FIFO/symlink rejection, and
process/descriptor caps. Deterministic mutations exercise changed start ticks,
command bytes, executable identity, process disappearance and a new PID.

A separate host test observes a real independently built child through live
procfs, checks its executable inode, keeps it running, and verifies that its
private argument is absent from output. A syscall trace confirms that this
observer run opens no device paths, makes no writable opens, sends no signals,
and executes no additional program. The native build rejects fixture arguments;
fixture support and mutation hooks are confined to test builds.

```sh
DREEM_OVERLAY_CC=/private/toolchain/bin/arm-linux-gcc \
  python3 -m unittest tests.test_startup_inventory -v
```

This is an inventory of visible process leaders, not a kernel ownership barrier.
Other PID namespaces, hidden processes, threads with separately unshared file
tables, short-lived activity between observations, device aliases and accesses
without retained descriptors can escape this view. Even an error-free report
cannot prove that sensors are reset or that a supervisor will not restart.
The Linux [`stat`](https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html),
[`exe`](https://man7.org/linux/man-pages/man5/proc_pid_exe.5.html) and
[`fd`](https://man7.org/linux/man-pages/man5/proc_pid_fd.5.html) interfaces describe
the underlying fields and access restrictions.

Activation still requires fresh installed-script and executable hashes, verified
supervision, explicit retirement of existing consumers, a controlled startup
owner and a tested restoration path. Native recorder, sensor and battery tests
remain unavailable while the headset cannot be reached. No change in this
milestone installs an overlay, edits init, remounts a filesystem or changes a
watchdog.

## Retained recovery installation and native access

A further inspection on October 3, 2026 establishes an existing path for
independent programs. The saved modified recovery archive, SHA-256
`5c475a980ec2e17f1220dcab281501015873db49e574ca90bc9cf1a7ffcaaaa2`,
includes `/etc/init.d/S99zz-recovery-toolkit`. That hook detaches its startup
work and invokes `/data/recovery/current/bin/start` when the toolkit is enabled
and its version-link checks pass. Three recorded launcher failures disable
subsequent automatic starts. A successful launcher exit resets the failure
counter; this is not a continuing health check of its children.

The hook's SHA-256 is
`2f8674f2060c88ffc12f212b1948a516dae43e088de60e2870606b940919e661`.
It exactly matches the retained project-authored source. Read-only inspection
of a fresh copy of the retained data-partition image also found
`current -> versions/1.1.0` and `always-associated` Wi-Fi mode. Its `toolkitctl`
and version-1.1.0 `start`, `stop`, `wifi-supervisor` and `ssh-supervisor` files
all match the retained source: **six source matches including the boot hook**.
The archive's recorder-start and shell-watchdog scripts remain byte-identical
to the stock scripts reviewed above.

These snapshots support a writable-storage deployment route without requiring
another root-filesystem rebuild for an independent utility. They do not prove
the headset's current toolkit version or qualify replacement of the vendor
recorder. The existing launcher/stop scripts still use process-name/PID checks;
they are not a verified ownership barrier for sensor or core replacement.

The same private image supplied the saved recovery Wi-Fi profile and the
headset's fuse-derived Wi-Fi address. Both were corroborated against historical
controller records without publishing their values. A temporary recovery trial
restored that profile with a one-device MAC allowlist and an isolated firewall
zone. Six allow policies covered analysis-host recovery sessions and gateway
DHCP/DNS; thirteen block policies covered the remaining zone directions. The
SSID was observed up on two access points, including the previously used AP.

Between **4:35 p.m. and 4:39 p.m. EDT**, the four-minute trial observed no
successful headset association or recovery SSH banner. The saved client record's
last-seen value did not advance. MAC-filter rejection counters did increase,
but those counters did not identify the rejected devices. The exact headset
address was also absent from a fresh passive Bluetooth scan, and a targeted
Bluetooth connection returned `BleakDeviceNotFoundError`. Neither checked
primary recovery machine had a connected recovery phone or matching recovery
USB device. USB inventories on the other two known LAN workstations likewise
found no matching recovery/phone vendor; the attempted ADB commands were not
available there.

The temporary SSID, network, zone and associated policies were removed. The
eight original networks, two WLANs, six zones and 113 policy objects were
verified against the pre-trial snapshot, including their configuration fields
apart from traffic counters/timestamps. The fallback cleanup timer was stopped.
No headset configuration or recording was changed. Private credentials,
device identifiers, partition images and vendor scripts remain outside Git.

This is bounded negative connectivity evidence, not proof of power state,
location, current radio configuration or a defective headset. Native deployment
and qualification remain blocked on a reachable device; another offline test
cannot supply that evidence.
