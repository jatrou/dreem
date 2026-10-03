# Source-built EEG DMA program

Verified offline on October 2, 2026. `sdma_acquire.asm` is a new, editable
acquisition program using the observed four-word ECSPI interface and NXP's
documented SDMA instructions. Its host control protocol is new and is now
implemented by the [Linux provider](sdma-provider.md). It has not been loaded
on the headset.

**Do not raw-upload this program using the legacy trigger value `1`.** That
path allocates a four-byte counter. The reconstructed provider's trigger value
`2` instead loads its built-in assembled copy and uses the new 64-byte control
allocation. Using the old allocation would allow DMA outside allocated memory.

## Why a different program is needed

The stock program has no cooperative stop acknowledgement. Clearing host
enable bits or changing priority does not prove that its current instruction
or pending bus transfers have finished. This prevents a sound ownership handoff
before the CPU changes SPI configuration or resets recording buffers.

The new program checks a host request before each frame and while waiting for
receive readiness. It drains memory and peripheral writes before publishing a
pause acknowledgement, then clears its event-pending bit with `done 4`.
It explicitly checks immediate and delayed bus errors and parks on a fault.
A bus that never completes can still stall an instruction; host-side timeouts
must retain DMA storage and clocks instead of assuming the stop succeeded.

The primary specification is NXP's *i.MX 6ULL Applications Processor Reference
Manual*, Rev. 1, November 2017, chapter 46
([NXP URL](https://www.nxp.com/docs/en/reference-manual/IMX6ULLRM.pdf),
[NXP-authored copy consulted](https://foofoodamon.github.io/references/i.MX%206ULL%20Applications%20Processor%20Reference%20Manual.pdf)).
The private PDF SHA-256 is
`7bf1aaaa2b108e6dc9b1e6f73deefd7b955090564bf39c4a7e57a5fcc4e861fa`.
Relevant provisions:

- Sections 46.4.12.1.5–6: a nonzero-size MD flush can acknowledge before its
  store completes; reading MS waits for completion and exposes errors.
- Sections 46.4.12.2.3 and 46.4.12.2.8.6–9: peripheral writes can acknowledge
  before completion; reading PS establishes the final transfer status.
- Sections 46.5.2.21 and 46.5.2.35: `done 4` clears EP and reschedules;
  `notify 1` raises HI without itself stopping the channel.
- Sections 46.4.2.4.2 and 46.8.3: priority/host-enable changes alone are
  insufficient evidence that the currently executing channel has stopped.

## Control ABI, version 1

All addresses are 32-bit DMA bus addresses supplied by a future host adapter.
The adapter must validate allocation ownership, placement, alignment, bounds,
and non-overlap before submitting the context.

| Initial register | Required value |
| --- | --- |
| r0 | Word-aligned coherent 1,024-byte sample ring |
| r1 | Exactly 1,024 |
| r2 | 64-byte-aligned coherent 64-byte control allocation |
| r3 | ECSPI1 base, `0x02008000` |
| r4–r7 | Scratch, initialized or overwritten by the program |

A single word load via the Burst DMA unit may read a larger memory burst.
The control allocation therefore includes padding; a four-byte request or
counter allocation is insufficient. Control fields are little-endian u32:

| Byte offset | Writer | Meaning |
| --- | --- | --- |
| 0 | SDMA | Completed-frame counter, modulo 2^32 |
| 4 | Host | Bit 0: run=1, pause=0; remaining bits: request generation |
| 8 | SDMA | Exact even request acknowledged after draining transfers |
| 12 | SDMA | Zero healthy; one indicates a DMA fault |
| 16–63 | Neither | Reserved, zero-initialized padding |

Start with produced/ack/fault zero and request `2`. The initial context
acknowledges this pause without touching ECSPI. Unlike the stock program, the
first frame goes into slot zero and the first completed frame is counted and
notified. Each frame has four raw 32-bit RX words; no first-frame discard,
sample conversion, or channel reordering occurs in this program. Ring offset
advances by 16 bytes with wrap at 1,024. The existing byte decoder remains a
separate component.

The host sequence implemented by the reconstructed provider is:

1. Reserve the channel and allocations, mask its hardware event, install the
   new context, and use event ownership. Explicitly set EP to run initialization.
2. Wait for both the exact initial ACK and EP clear, checking the fault field.
   Disable channel priority and synchronize its IRQ before CPU setup. Do not
   use the stock first-interrupt suppression for this protocol.
3. Initialize the ADC/SPI while paused. Publish a fresh odd request with a DMA
   write barrier, then enable the channel/event and explicitly wake it. A
   resumed context preserves the producer counter and next ring position.
4. To pause, mask the hardware event and read the register back before
   publishing a fresh even request. Serialize requests; never reuse the
   currently acknowledged value or let generations wrap silently. Publish
   with a DMA write barrier, then explicitly wake the channel even if it is idle.
5. Check fault, exact ACK, and EP clear with appropriate DMA read ordering.
   Only this combination under the masked-event condition establishes the
   protocol's successful handoff. Disable priority and synchronize IRQ before
   modifying shared state. A notification by itself is insufficient.
6. Treat a fault, unexpected EP clear while running, or timeout as failure.
   A fault-report write can itself fail. Disable further requests and retain
   allocations/clocks where completion remains unproved. Merely waking a
   faulted context never resumes acquisition.

An acknowledgement establishes completion of this program's DMA accesses.
It does **not** establish that ECSPI has finished shifting bytes on the wire,
that the ADC has stopped converting, or that other DMA channels are idle.
The CPU must additionally follow the SPI/ADC shutdown sequence. Resetting the
counter without rebuilding the SDMA context would desynchronize r5; ordinary
pause/resume must leave the producer counter intact. The provider implements
consumer ownership and pause/resume. Normal allocation reclamation, coordinated
context reset after faults, and suspend/resume still need implementation.

## Build and verification

The included assembler accepts only the instructions needed here, checks
operand/branch bounds, and uses relative branches exclusively. Its attributed
encoding table comes from the existing decoder. No vendor binary, decompilation,
network connection, or hardware access is needed:

```sh
python3 development/sdma_assemble.py development/sdma_acquire.asm /private/work/acquire.bin
python3 development/verify_sdma_program.py
python3 -m unittest tests.test_sdma_program tests.test_sdma_disassemble -v
```

The assembled program has 106 instructions, 212 bytes, and SHA-256
`3bc23298ae1c1f21618c99ad5343e2211a1e42bd92133a784b1f35bb2a776ca1`.
Billauer's independently implemented assembler with Petri's raw-output change
produced identical bytes. See [assembler provenance](sdma-findings.md#reassembly-evidence)
for the external downloads. Optional reproduction verifies both input hashes:

```sh
python3 development/verify_sdma_program.py \
  --external-assembler /private/work/sdma_asm.pl \
  --external-module /private/work/sdma_asm/mx51_sdma_set.pm
```

The module hash is
`f4eeee73b889cf32926d35489efd1cd248b5f63f84f807d1491008b26f2a98b2`.
Neither external tool is bundled. Generated binaries and full verification
reports belong outside the repository.

The assembled bytes pass **933 instruction-model cases**. These include pause
at every instruction boundary through a frame, followed by resume; four bus
delays and three program locations; permanently absent receive readiness;
130 frames crossing two ring wraps; producer-counter wrap; immediate errors at
every normal-frame functional access; delayed errors and stalls on the modeled
transfer operations; initialization faults; and faulted-context wake attempts.
Three negative controls remove the counter, ACK, or peripheral-completion read;
all are rejected for publishing before pending DMA completes. A fourth removes
the partial-frame flush from the fault handler; the model rejects changing
memory addresses with unflushed data instead of inventing an implicit flush.
Ten unit tests
also cover assembler boundaries, private/exclusive output, decoder regression,
and rejection of verification under Python optimization.

The model executes the assembled instructions and delays store completion.
It is not a silicon emulator: it does not establish real bus timing, scheduler
context save/restore, interrupt races, ECSPI request timing, ADC fidelity,
power consumption, or on-device safety. It also does not detect all ring
overruns or protect a CPU reader against concurrent sample overwrite. Those
limits remain separate from the tested cooperative-pause protocol.
