# Keep partially sent frames during queue pressure

The existing streamer's queue can fill while a TCP receiver is stalled. Its
original eviction removes the head even when some of that frame has already
been sent, closing the connection to avoid appending a new header into its
unfinished payload. This produces a truncated connection and forces recovery
through reconnect even though unsent frames could instead have been discarded.

The optional queue repair preserves every frame with a nonzero sent offset and
evicts the oldest wholly unsent frame. It keeps the configured limit on queued
wire bytes and the existing loss-reporting protocol. It does not promise lossless capture:
unsent samples can still be dropped under sustained receiver congestion.

## Build the combined candidate

The input is the same private project-written source reviewed for the
[idle polling repair](streamer-polling.md), with SHA-256
`9c1cb2751d560917a18fa8867cf8dc9be00b93a9f8a8ec353f48bbdadebe46bf`.
Both repairs are applied for this candidate:

```sh
python3 development/repair_streamer_poll.py /private/dreem_live_streamer.c \
  /private/work/streamer-queue-repair --preserve-partial-frame
DREEM_STREAMER_SOURCE=/private/dreem_live_streamer.c \
  DREEM_STREAMER_POLL_REPORT=/private/work/streamer-queue-verification.json \
  python3 -m unittest tests.test_streamer_queue -v
```

The resulting source hash is
`cd44c1c1a5c1540e95e2e30b9a290371f48d1621673cc0572bf77d0d5f81f46e`.
The build manifest records `preserve_partial_frame: true`, both compiler
versions and binary hashes. Complete source, binaries and logs remain in a
new private directory outside the repository. The original desktop checkout
is not edited and no device deployment is performed by the tool.

Without the flag, the earlier polling-only transformation remains byte-for-byte
unchanged for comparison and reproduction of its recorded qualification.

## Queue behavior

When a new frame would exceed the ring limit, the queue searches from its head
for the oldest frame whose sent offset is zero. Removing that frame updates
the links, tail, total queued wire bytes and cumulative dropped-frame counter.
An already partially sent frame retains its complete wire bytes, sent offset
and place in the queue. Normal nonblocking sending can then finish it on the
same connection before sending a later frame.

The limit includes each retained frame's full wire buffer, including its
already-sent bytes. Queue metadata, the connection preface and temporary payload
buffers are additional allocations; the ring limit is not a total process-RAM cap.

If no wholly unsent frame can be evicted, the incoming frame is rejected and
counted. This prevents both an endless eviction loop and forced corruption of
the current frame. The native reader retains its file offset and sample index
when queue insertion fails, so a later attempt can read those source rows again.
This fallback matters for future larger batches; current configured frame sizes
are much smaller than the minimum ring size.

The queue remains lossy. Sequence and sample-index gaps expose omitted frames
and rows. The cumulative drop count in an already queued frame can predate a
later eviction; updated counts appear in subsequent constructed/queued frames.
A dropped-frame counter is not an exact lost-sample count: streams have different
row counts, and a rejected insertion can later be retried at the same source
offset. No header of a partially transmitted frame is rewritten to update its
drop count or CRC.

## Verification

On October 3, 2026 (America/New_York), all six tests passed. The connected checks
ran 14 real localhost TCP processes: original and polling-only controls plus
the combined host/ARM candidates. Only the polling counter is instrumented in
these process tests; TCP sockets, clock and file operations are real. ARM runs
use QEMU and do not measure the physical headset.

The stalled-receiver case keeps a small receive buffer for 1.3 seconds, then
increases it and drains the connection. A 64 KiB streamer queue and synthetic
native files force queue pressure. In the qualified three-second runs:

| Build | Congestion-forced disconnects | Complete CRC-valid frames | Incomplete final bytes |
| --- | ---: | ---: | ---: |
| Polling-only host control | 1 | 77 | 228 |
| Combined host repair | 0 | 196 | 0 |
| Combined ARM repair under QEMU | 0 | 196 | 0 |

Both combined runs retained explicit sample-index gaps and nonzero dropped-frame
counts. Every complete data payload matched its indexed bytes in the source
files, and those files were unchanged by the streamer. These counts describe
this test, not guaranteed throughput or proof that every source row arrived.

The connected suite also covers growing files with partial trailing rows,
closed-peer reconnection, write-half-closed receivers, unsolicited client bytes,
and the idle polling improvement. The combined builds still made 100 polling
calls in the measured two-second idle case, versus 498,794 for the original
host source.

Separate compiled tests execute the actual modified queue and reader functions
on host and ARM. Five cases check empty eviction, ordinary head eviction,
immutable partial-head bytes across 196 evictions, bounded rejection when only
a partial frame remains, and preservation of the source offset after rejection.
The host queue checks also pass AddressSanitizer and UndefinedBehaviorSanitizer.
An explicit hash check proves that the default polling-only output is unchanged.

## Remaining deployment boundary

This removes one congestion-induced reason for closing a connection. Process
termination, network loss and other socket errors can still leave a partial
final frame; receivers must continue recording that incomplete tail and the
associated data gaps. Ring overflow still drops unsent data, and restart/late
attachment behavior remains unchanged.

The candidate has not been installed on the headset. Native version/source
verification, recorder-preservation tests, byte comparisons, timing, CPU and
power measurements remain open. The receiver was rechecked as disabled/inactive;
no service, network or headset configuration was changed in this work.
