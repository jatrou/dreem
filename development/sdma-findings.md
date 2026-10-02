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
