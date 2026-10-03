# EEG DMA reconstruction

Verified offline against the stock 4.7.11 archive on October 2, 2026. Firmware
identity is recorded in [source findings](source-findings.md). Vendor binary,
recovered assembly, and decompiled C remain outside this repository.

## Recovered program and loader

The root filesystem contains `etc/firmware/ads_sdma.bin`: 106 bytes, or 53
little-endian 16-bit SDMA instructions. Its SHA-256 is
`8718f1d8aef068043ddcf82b2237674ccdb769eaec363ae38c8f1305ec5bfc3f`.
`etc/init.d/S99_load_sdma_firmware` loads it through the SDMA `user_script`
sysfs attribute and writes to `trigger` for a selected hardware revision.
That startup script also changes clock/event registers; reading it is not an
instruction to execute it on a running recorder.

The kernel reserves SDMA channel 1 for this path. Relevant function addresses:

| Function | Address | Recovered interface |
| --- | --- | --- |
| `store_user_script` | `0x802e891c` | Loads code after the existing RAM firmware and retains a copy for resume |
| `trigger_user_script` | `0x802e8d14` | On first call allocates a 1,024-byte DMA ring and a separate 32-bit counter, then installs channel context |
| `sdma_int_handler` | `0x802e9b80` | Handles initialization, advances a 64-slot producer index, and signals waiting readers as the counter changes |

The trigger replaces initial registers r0–r2 with ring physical address, ring
size (1,024), and counter physical address. It exposes r0–r7 through sysfs,
but these three initial values are controlled by the loader. Subsequent
triggers enable the existing channel rather than rebuilding its context.

Names, channel reservation, placement after RAM firmware, register attributes,
and loader structure closely resemble
[Jonah Petri's published Linux 4.1.15 SDMA examples](https://blog.petri.us/sdma-hacking/part-2.html).
This is evidence for a useful source starting point, not proof of an exact
source match or complete code provenance. Dreem adds acquisition-specific
buffers, event ownership, interrupts, and resume behavior.

## What the instructions establish

The recovered program constructs the ECSPI1 base address `0x02008000`, checks
status bit 4 at offset `0x18`, transfers four 32-bit words per ring slot,
advances the offset in steps of 16 with wrap at r1, and updates the separate
counter. The initial ring offset is r1 minus 16. It also writes four zero words
through the SPI transmit path and uses `done 3` to notify the ARM core.

These are static instruction interpretations, corroborated by the kernel
buffer layout and [published SDMA functional-unit examples](https://billauer.se/blog/2011/11/imx-sdma-assembler-example/).
They do not prove peripheral timing, event sequencing, first-sample handling,
or physical recording fidelity. A software model must not silently replace
those hardware checks.

## Reassembly evidence

`sdma_disassemble.py` derives its instruction encodings from Eli Billauer's
GPL assembler. Its output reassembles with Jonah Petri's raw-output variant.
Independent checks performed on a little-endian Linux host:

- All 53 recovered instructions reassembled to the exact original 106 bytes
  and the SHA-256 above.
- All 63,012 16-bit encodings accepted by the decoder were reassembled by the
  external Perl assembler, matching their original words. This checks encoding
  and operand representation, not execution semantics. Reserved instructions
  and the assembler's unsupported zero-length loop remain rejected.
- Unit tests cover the published loop example, signed branch boundaries,
  register/immediate limits, malformed input, and word-address annotations.

External inputs used, downloaded from their authors:

| Input | SHA-256 |
| --- | --- |
| [Billauer assembler archive](https://billauer.se/download/sdma_asm.tar.gz) | `721748ebf1198ac8d9916d38abe9a92aa7bae3db9eab5ccad7e8fdcfd24dae0a` |
| [Petri raw-output assembler](https://blog.petri.us/sdma-hacking/sdma_asm.pl) | `d1772f6a3a982c2105edafbab76972dfde9ddcd2bb965d3e9e52dc34716cebc3` |

The raw-output variant uses Perl's native-endian packing. The reproduction
command requires a little-endian host. Keep generated vendor assembly and
binary private; the tool's GPL license does not relicense its input or output.

Editable, exactly reassemblable code is now available for this small component.
The full acquisition driver and vendor application are still not rebuilt.

## Reconstructed provider primitives

`sdma_eeg.c` and `sdma_eeg.h` now implement channel-context construction and
producer-counter progress in independent C. **These are not yet a Linux SDMA
provider.** They do not allocate DMA memory, load firmware, bind a device,
install an interrupt handler, publish kernel exports, or replace the installed
driver. They are intended for the pending provider integration.

The recovered context is 32 words (128 bytes). Word 0 holds the 14-bit program
counter; words 2–4 hold ring physical address, 1,024, and counter physical
address. Words 5–9 retain supplied r3–r7; all remaining words are zero. The
stock channel-0 descriptor uses command/status/count word `0x018b0020`, the
context DMA address, and SDMA data-memory address `0x820` for channel 1.
The reconstructed builder validates output length, PC encoding, nonzero aligned
DMA addresses, ring-address wrap, and ring/counter overlap. Those checks do not
establish that the addresses belong to allocated DMA memory or valid program RAM.

The progress function preserves these stock behaviors:

- The first interrupt only marks initialization; it publishes no frames and
  leaves the software counter and producer index unchanged.
- With a stable DMA counter, each elapsed count advances the producer index
  modulo 64 and publishes it before notifying the reader. The software counter
  increments after the notification, including unsigned 32-bit wrap.
- A duplicate counter produces no new notifications.

The stock interrupt routine repeatedly rereads the DMA counter inside its
catch-up loop. In emulation, a counter that advances with each read prevents
that loop from finishing within the instruction bound. A reset from 10 to 0
also exceeds the bound; it is treated as nearly a full 32-bit wrap. Even a
65-frame jump publishes 65 notifications although the ring holds only 64 frames.

The reconstruction instead accepts one coherent counter snapshot per interrupt
and processes at most 64 notifications. Larger jumps latch `-75` (`EOVERFLOW`)
without publishing new frames. The fault persists until a coordinated reset.
The future Linux provider must disable DMA, coordinate with the reader, and
implement the notification callback with the required DMA/publication barriers.
The primitive itself cannot perform those operations. This per-interrupt bound
also does not detect every accumulated overrun across interrupts or prevent
concurrent DMA writes from overwriting a frame during a read.

### Differential verification

```sh
/private/work/venv/bin/python development/verify_sdma_eeg.py \
  /private/work/inspection/kernel.elf \
  --arm-compiler /path/to/armv7-eabihf--uclibc--stable-2018.11-1/bin/arm-linux-gcc
```

The verifier executes only the original `sdma_int_handler` and
`trigger_user_script` instructions in synthetic memory, stubbing kernel
services. The interrupt comparison supplies only channel-1 interrupts with no
ordinary DMA descriptors; it does not verify general DMA dispatch. Context
comparisons use successful synthetic allocations and context loads; they do
not validate the stock loader's error paths.

Native C and Cortex-A7 C match **387 progress cases** and **12 context cases**.
Progress covers all 64 producer positions, deltas 0/1/2/63/64, counter wrap at
every position, and initialization with zero/nonzero counters. Comparisons
include notification order and the software counter visible at each wakeup.
Context comparisons cover different PC encodings, DMA addresses, and seeded
register values, including unchanged guard bytes around the output.

Ten invalid-context cases and four invalid-progress cases are rejected without
changing output/state. Three excessive-jump cases latch faults on both targets;
the moving-counter case demonstrates the deliberate snapshot behavior. Runs
with both GCC 7.3 and GCC 13 produce the same combined comparison SHA-256:
`d76e3efca4be49489e2ae5bbb2a5c26839177026eeb0efd28c5f5c08588cdb5f`.
The ARM builds use soft-float and disable vectorization. The C component also
compiles as an object through the pinned Linux 4.1.15 Kbuild with GCC 7.3 and
warnings treated as errors. This is compile compatibility, not a linked or
qualified replacement provider.

### Remaining loader work

The original trigger marks its one-time initialization flag before allocating
the ring and counter; the inspected path does not check the allocation results.
The script-store routine copies the caller's byte count into its saved-script
region without an observed length check. The next recovered symbol is 1,024
bytes after that region's start; the recovered symbol table does not preserve
the original array declaration. It also saves the script even when the upload
returns an error. The archived 106-byte program fits that region, but these
observations are reasons to add validated sizes, allocation rollback, and
success-only publication in the replacement loader.

Provider integration still requires real DMA allocation/lifetime ownership,
bounded script placement, channel reservation and context loading, ordered
interrupt dispatch, reader coordination on faults, and suspend/resume handling.
The complete board kernel additionally needs the other Dreem-specific drivers
and board behavior identified in [source findings](source-findings.md).
