# Reduce idle work in the existing file streamer

The existing project-written ARM streamer provides a route for adding software
features while the vendor recorder retains its sensors. It tails growing native
files and copies complete rows to a receiver. Its current source also has a busy
polling loop: it requests socket writability even when its send queue is empty,
then scans the source files again every time `poll()` returns.

`repair_streamer_poll.py` makes one checked source replacement: request `POLLOUT`
only while the connection preface or frame queue has bytes pending. The normal
20 ms polling timeout then applies to an idle connected socket. Error/hangup
events remain observable with an empty requested-events mask, as documented by
the [Linux poll interface](https://man7.org/linux/man-pages/man2/poll.2.html).
Existing connection handling, including write-half-closed receivers, is retained.
No new client control operation is introduced.

## Input and preservation

The input is the existing project's `deployment/live-streaming/device/dreem_live_streamer.c`,
not Dreem's proprietary `nano_core`. The reviewed private snapshot is 35,316
bytes, SHA-256
`9c1cb2751d560917a18fa8867cf8dc9be00b93a9f8a8ec353f48bbdadebe46bf`.
It came from a desktop checkout with 204 pre-existing changes. That checkout
was read only; the full source was copied to private review storage rather than
silently imported into this branch.

The repair tool rejects any other source hash, checks its replacement anchor
occurs exactly once, and normalizes line endings in the generated source. The
qualified repaired source SHA-256 is
`af658375798818325beb9b574a79cf9e8283a1d015f0c93e498d0a348ecca9b6`.
Generated input/repaired source, logs and executables must remain in a new
private directory outside this repository. The builder records compiler,
source, tool and binary hashes and checks that ARM output is static ELF32.

```sh
python3 development/repair_streamer_poll.py /private/dreem_live_streamer.c \
  /private/work/streamer-poll-repair
DREEM_STREAMER_SOURCE=/private/dreem_live_streamer.c \
  DREEM_STREAMER_POLL_REPORT=/private/work/streamer-poll-verification.json \
  python3 -m unittest tests.test_streamer_poll -v
```

The comparison requires the private source explicitly. Without it the connected
test class is skipped; that is not qualification. A changed future source needs
review, not replacement of the hash merely to bypass the pin. Host and ARM
compilers plus QEMU were present for the qualified run.

## Observed effect

On October 3, 2026 (America/New_York), a real localhost TCP receiver connected
to the streamer with empty synthetic native files for a two-second run. A small
wrapper counted real `poll()` calls without replacing socket, clock or file
operations. Results from the qualified run:

| Build | Poll calls | Timeout returns | Process CPU time |
| --- | ---: | ---: | ---: |
| Original host | 498,615 | 0 | 1.991046 seconds |
| Repaired host | 100 | 99 | 0.002959 seconds |
| Repaired ARM under QEMU | 100 | 99 | Not a headset measurement |

All three runs delivered four valid metadata/heartbeat frames. Counts and CPU
times are measurements of this short test, including process startup; they are
not a fixed performance guarantee. ARM was executed through QEMU with actual
host TCP sockets, not on the headset. This establishes the idle-loop defect and
its removal in the tested builds, not a target battery or temperature reduction.

The five tests include 13 TCP process runs: one original host baseline and six
cases on each repaired host/ARM build. They verify:

- Idle connection waits instead of repeatedly waking for writability.
- Growing files deliver all 500 EEG, 100 motion and 100 optical synthetic rows
  with unchanged bytes and valid header/payload CRCs; incomplete final rows are
  withheld and the input files are unchanged by the streamer.
- A closed peer can reconnect; write-half-close and unsolicited client bytes
  do not reintroduce idle spinning or stop normal heartbeat delivery.
- A deliberately stalled receiver exercises blocked writes; complete received
  frames remain CRC-valid and match their indexed source rows.
- Unreviewed source is rejected before building.

The backpressure run observed 24 blocked-write polls, 77 complete frames and a
228-byte incomplete final frame on each build. The verifier explicitly reports
and excludes that final fragment. It does not reinterpret it as a complete
frame or claim complete capture. The inherited queue-overflow/disconnect and
shutdown behavior can still end a TCP connection mid-frame; receivers must
retain that loss information. This polling repair does not change that policy.

## Deployment evidence and remaining work

A saved physical-trial report records a useful but limited prior result:
received rows compared bit-for-bit to their corresponding native rows, while
complete sample capture failed. A separate short resource report attributed
about 30% of one CPU core to the streamer and did not pass the continuous-power
gate. Those are historical report findings, not a new physical qualification.
The current busy loop is a reproducible source-level defect; its contribution
to that earlier on-device measurement has not been isolated.

The current receiver service was inspected as disabled/inactive. This work did
not change that service, its networking, the desktop checkout or the headset.
The new binaries have not been installed. Restored headset access is needed to
verify the deployed source/version, stage the repair, check recorder preservation,
repeat native byte comparisons, and measure CPU, timing, power and temperature.
Neither this repair nor the prior received-row match qualifies unattended
streaming or complete overnight capture.
