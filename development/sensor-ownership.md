# Sensor ownership and recorder shutdown

Stopping a recording does not establish exclusive access to its sensors. The
saved core attempts to restart background optical acquisition after a normal
record stop. Several error paths also discard thread bookkeeping before thread
termination has been confirmed. A replacement must establish that the old
consumer has exited and cannot restart before claiming the same sensor.

These findings were verified offline on October 3, 2026 (America/New_York).
They apply to the stock core hash in [source findings](source-findings.md).
The existing recorder, services and headset have not been modified.

## Original ARM behavior

`verify_sensor_ownership.py` executes selected original ARM routines with
synthetic thread, clock, descriptor and I2C services:

| Routine or path | Observed behavior | Integration implication |
| --- | --- | --- |
| Record stop, `0x2c524`, inactive record flag | Returns zero without stopping an existing optical owner | Recording idle is not sensor idle |
| Record stop, successful retirement | Calls optical stop, then attempts optical restart; a failed restart result is ignored | Successful record stop is not an ownership handoff |
| Optical manager stop, `0x90bf4` | Requests cancellation when marked running, then uses a realtime deadline five seconds ahead to join when marked joinable | A cancel request alone is not proof of termination |
| Optical cancel, clock or join failure | Attempts descriptor close, saves `-1`, clears running/joinable flags and returns one | Cleared flags do not prove retirement or successful close |
| Optical stop retry after failed join | Returns zero without joining or performing bus operations | A later successful stop cannot repair the missing retirement proof |
| Record stop after failed motion join | Clears motion flags and returns one; retry returns zero without joining motion or optical threads | Retry success cannot establish ownership |
| Record stop after failed microphone join | Warns, continues and can return zero while attempting optical restart | Overall stop success does not establish that every worker exited |
| Motion cleanup, `0x901d0` | Writes `CTRL5=0x40` to request software reset, then attempts close without reset polling or readback; close error does not change its result | Successful cleanup does not confirm physical reset completion |

The optical chip cleanup routine at `0x905d0` is included in this execution.
Its register behavior and limitations are documented in
[optical lifecycle](optical-lifecycle.md). On a successful optical join, chip
cleanup errors still lead to a descriptor-close attempt and discarded saved
descriptor. That does not establish chip shutdown or close success.

Error bookkeeping is not uniform: storage cancellation failure retains its
running flag but clears joinable; microphone cancellation failure clears running
but retains joinable. The tested record/EEG/motion cancellation and join errors
clear both flags. These distinctions are checked explicitly, not generalized
from the optical manager.

The thread model treats cancellation as a request and a successful join as
retirement, consistent with the Linux man-pages for
[`pthread_cancel`](https://man7.org/linux/man-pages/man3/pthread_cancel.3.html)
and [`pthread_join`](https://man7.org/linux/man-pages/man3/pthread_join.3.html).
An `unjoined_threads` result means retirement remains unproven in the model;
it does not prove that a real worker continues executing. Original thread
bodies, cancellation cleanup handlers, scheduler behavior and mutex correctness
are outside this replay.

The nested optical restart is a stub boundary: the verifier observes the
attempt and tests success/failure returns, without claiming that a new thread
or physical sensor starts. The actual startup also has a hardware-version-three
skip path. Unrelated managers are stubbed; this is not a full recorder simulation.

## Archived startup and watchdog behavior

The firmware inspector now extracts four additional exact allowlisted files
into its private output directory. Their hashes are pinned by the verifier.
The following is a manual shell control-flow review; the scripts are never run
by the inspection or verification tools and are not distributed here.

| Archived file | Relevant behavior |
| --- | --- |
| `/etc/init.d/S99_dreem` | Start removes the shutdown marker, backgrounds `nano_core` and enables bus frequency. Stop disables bus frequency, sends `TERM`, then writes the intentional-stop value `42` to `/tmp/watchdog.info`; it does not wait for process exit |
| `/usr/bin/mpu_watchdog.sh` | Resets the counter to zero on startup; loops with a five-second sleep and restarts an absent core unless the counter is `42`; also handles shutdown/zombie conditions |
| `/etc/init.d/S99_watchdog` | Starts the shell supervisor in the background; stop signals matching supervisor processes without establishing that they and any children have exited |
| `/etc/init.d/S15watchdog` | Starts a separate hardware watchdog daemon with requested feed/timeout arguments `-t 5 -T 40`; its stop branch is empty |

The stock core-stop script sends its signal before publishing the supervisor
hold. That ordering leaves a possible restart window. Its final successful
echo can also hide a preceding signal or write failure in the script's exit
status. Neither shell exit status nor a single process-list snapshot is a
sufficient ownership barrier. The hardware watchdog is distinct from the shell
restart supervisor; stopping the latter does not qualify the former's behavior.
Requested watchdog timing is not a physical timing measurement.

These are archived stock files, not a fresh inspection of the modified headset's
live supervision. No service-stop procedure is qualified by this review.
The new [startup inventory](startup-inventory.md) records process identity and
existing descriptor observations without opening devices or signaling their
owners. It also documents the reviewed stock init/mount behavior; a successful
inventory still does not establish an ownership barrier.

## Consequences for feature integration

For replacement acquisition on the same sensor, the integrating owner must:

1. Establish retirement of every previous consumer and prevent concurrent or
   supervised restart. Recording status, old flags and a new advisory lock alone
   do not establish this.
2. Keep worker context and descriptor lifetime valid until worker exit is known.
   Failed cancellation/join must preserve uncertainty and recovery state.
3. Separately confirm sensor shutdown/reset and establish the new sample boundary
   through the checked [motion](motion-lifecycle.md) or
   [optical](optical-lifecycle.md) lifecycle. Process exit alone does not reset
   hardware or FIFO framing.
4. Restore the vendor owner only after the replacement's workers and device
   operations have finished, with verified supervision and watchdog behavior.

These are integration requirements, not an implemented handoff. A new sensor at
a different address may coexist with existing bus users; it still needs board,
electrical, address and bus-load checks. The finding does not require stopping
all users of the adapter. Existing file-analysis features avoid direct sensor
ownership and remain a separate route for adding software features.

## Reproduce and interpret the checks

With the private archive and the dependencies in `development/requirements.txt`:

```sh
python3 development/inspect_firmware.py /private/firmware_FEMTO_4.7.11_production.tar.bz2 \
  /private/work/inspection-with-controls
/private/work/venv/bin/python development/verify_sensor_ownership.py \
  /private/work/inspection-with-controls/nano_core \
  --control-dir /private/work/inspection-with-controls
python3 -m unittest tests.test_firmware_development -v
```

The replay passes 49 cases: 26 optical-manager cases, eight motion-cleanup cases,
and 15 record-stop cases. It maps 3,748 additional original instruction/literal
bytes beyond the transport verifier's base ranges. Its result fixture SHA-256 is
`64b9b58a0043f54dd6ccae70d428dd9cb95b1c84750fe15d9ccb3b11cfa985a9`.
The optional control-directory check verifies four script hashes and reports
the manual review separately from ARM execution.

All six existing firmware-inspection tests pass. A fresh stock-archive inspection
also verified directory mode `0700` and file modes `0600`. Original instructions,
scripts and decompiler output remain private inputs. These checks do not prove
physical shutdown, a working on-device handoff or live supervisor configuration.
