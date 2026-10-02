#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Instruction encodings/operand layouts derived from mx51_sdma_set.pm:
# Copyright (c) Eli Billauer, 2011, https://billauer.co.il
# Decoder and listing implementation: Copyright 2026 Dreem research contributors.
"""Decode little-endian i.MX SDMA instructions without executing them.

Output syntax is accepted by Eli Billauer's GPL assembler and Jonah Petri's
raw-output variant. This tool does not contain or download a vendor program.
"""

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import struct

# Each format identifies the variable operand bits in a 16-bit instruction.
MASKS = {"none": 0, "j": 0x700, "f": 0x300, "r": 0x700,
         "rn": 0x71F, "rs": 0x707, "ri": 0x7FF, "mem": 0x7FF,
         "loop": 0x3FF, "branch": 0xFF, "absolute": 0x3FFF}
SPECS = [
    ("done", 0x0000, "j"), ("notify", 0x0001, "j"),
    ("softbkpt", 0x0005, "none"), ("ret", 0x0006, "none"),
    ("clrf", 0x0007, "f"), ("illegal", 0x0707, "none"),
    ("jmpr", 0x0008, "r"), ("jsrr", 0x0009, "r"),
    ("ldrpc", 0x000A, "r"), ("revb", 0x0010, "r"),
    ("revblo", 0x0011, "r"), ("rorb", 0x0012, "r"),
    ("ror1", 0x0014, "r"), ("lsr1", 0x0015, "r"),
    ("asr1", 0x0016, "r"), ("lsl1", 0x0017, "r"),
    ("bclri", 0x0020, "rn"), ("bseti", 0x0040, "rn"),
    ("btsti", 0x0060, "rn"), ("mov", 0x0088, "rs"),
    ("xor", 0x0090, "rs"), ("add", 0x0098, "rs"),
    ("sub", 0x00A0, "rs"), ("or", 0x00A8, "rs"),
    ("andn", 0x00B0, "rs"), ("and", 0x00B8, "rs"),
    ("tst", 0x00C0, "rs"), ("cmpeq", 0x00C8, "rs"),
    ("cmplt", 0x00D0, "rs"), ("cmphs", 0x00D8, "rs"),
    ("cpshreg", 0x06E2, "none"), ("ldi", 0x0800, "ri"),
    ("xori", 0x1000, "ri"), ("addi", 0x1800, "ri"),
    ("subi", 0x2000, "ri"), ("ori", 0x2800, "ri"),
    ("andni", 0x3000, "ri"), ("andi", 0x3800, "ri"),
    ("tsti", 0x4000, "ri"), ("cmpeqi", 0x4800, "ri"),
    ("ld", 0x5000, "mem"), ("st", 0x5800, "mem"),
    ("ldf", 0x6000, "ri"), ("stf", 0x6800, "ri"),
    ("loop", 0x7800, "loop"), ("bf", 0x7C00, "branch"),
    ("bt", 0x7D00, "branch"), ("bsf", 0x7E00, "branch"),
    ("bdf", 0x7F00, "branch"), ("jmp", 0x8000, "absolute"),
    ("jsr", 0xC000, "absolute"),
]


@dataclass(frozen=True)
class Instruction:
    pc: int
    word: int
    name: str
    operands: str
    relative_target: int | None = None

    def assembly(self):
        return f"{self.name:8} {self.operands}".rstrip()


def decode(word, pc=0):
    if not 0 <= word <= 0xFFFF or pc < 0:
        raise ValueError("invalid word or program offset")
    for name, base, form in SPECS:
        if word & ~MASKS[form] != base:
            continue
        reg, byte = (word >> 8) & 7, word & 255
        target = None
        if form == "none":
            operands = ""
        elif form in ("j", "f"):
            operands = str(reg if form == "j" else reg & 3)
        elif form == "r":
            operands = f"r{reg}"
        elif form == "rn":
            operands = f"r{reg}, {word & 31}"
        elif form == "rs":
            operands = f"r{reg}, r{word & 7}"
        elif form == "ri":
            operands = f"r{reg}, {byte}"
        elif form == "mem":
            operands = f"r{reg}, (r{word & 7}, {(word >> 3) & 31})"
        elif form == "loop":
            if byte == 0:
                raise ValueError(f"empty loop at word offset {pc:#x}")
            operands = f"{byte}, {reg & 3}"
            target = pc + 1 + byte
        elif form == "branch":
            displacement = byte if byte < 128 else byte - 256
            operands = str(displacement)
            target = pc + 1 + displacement
        else:
            operands = str(word & 0x3FFF)
        return Instruction(pc, word, name, operands, target)
    raise ValueError(f"unknown instruction {word:#06x} at word offset {pc:#x}")


def disassemble(data):
    if not data or len(data) % 2 or len(data) > 32768:
        raise ValueError("expected 1 to 16384 complete 16-bit instructions")
    return [decode(word, pc) for pc, (word,) in enumerate(struct.iter_unpack("<H", data))]


def listing(data):
    lines = [f"# Input SHA-256: {hashlib.sha256(data).hexdigest()}",
             "# Offsets and relative targets are in 16-bit instruction words."]
    for instruction in disassemble(data):
        tail = f"# {instruction.pc:04x}: {instruction.word:04x}"
        if instruction.relative_target is not None:
            tail += f"; target word {instruction.relative_target:#x}"
        lines.append(f"{instruction.assembly():28} {tail}")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("program", type=Path)
    parser.add_argument("output", type=Path, help="new private assembly output file")
    args = parser.parse_args()
    try:
        if args.program.stat().st_size > 32768:
            raise ValueError("program exceeds the SDMA instruction address space")
        data = args.program.read_bytes()
        text = listing(data)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Disassembly failed: {exc}\n")
    print(f"Decoded {len(data) // 2} instructions; SHA-256 {hashlib.sha256(data).hexdigest()}")


if __name__ == "__main__":
    main()
