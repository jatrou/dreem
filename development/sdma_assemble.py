#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright 2026 Dreem research contributors.
"""Assemble the deliberately small, relocatable SDMA acquisition language.

Uses the attributed instruction definitions in sdma_disassemble. No eval,
macros, absolute jumps, implicit padding, or device access. Output is created
exclusively; an existing file is never overwritten.
"""

import argparse
import hashlib
import os
from pathlib import Path
import re
import struct

try:
    from .sdma_disassemble import SPECS, decode
except ImportError:
    from sdma_disassemble import SPECS, decode

FORMS = {name: (base, form) for name, base, form in SPECS}
SUPPORTED = {"ldi", "mov", "add", "addi", "cmpeq", "btsti", "clrf",
             "stf", "ldf", "bdf", "bsf", "bf", "bt", "notify", "done"}


def assemble(source):
    """Return raw little-endian bytes and word-address labels (max 512 words)."""
    labels, rows = {}, []
    for line_no, line in enumerate(source.splitlines(), 1):
        line = re.split(r"[#;]", line, maxsplit=1)[0].strip().lower()
        if ":" in line:
            label, line = line.split(":", 1)
            if not re.fullmatch(r"[a-z_][a-z_0-9]*", label) or label in labels:
                raise ValueError(f"line {line_no}: invalid or duplicate label")
            labels[label] = len(rows)
            line = line.strip()
        if line:
            parts = line.split(maxsplit=1)
            name = parts[0]
            args = [x.strip() for x in parts[1].split(",")] if len(parts) == 2 else []
            rows.append((line_no, name, args))
    if not 1 <= len(rows) <= 512:
        raise ValueError("program must contain 1..512 instructions")

    def number(value, low, high):
        if not re.fullmatch(r"-?(?:0x[0-9a-f]+|[0-9]+)", value):
            raise ValueError("expected integer literal")
        result = int(value, 16 if "0x" in value else 10)
        if not low <= result <= high:
            raise ValueError("operand out of range")
        return result

    def register(value):
        if not re.fullmatch(r"r[0-7]", value):
            raise ValueError("expected r0..r7")
        return int(value[1])

    words = []
    for pc, (line_no, name, args) in enumerate(rows):
        try:
            if name not in SUPPORTED:
                raise ValueError(f"unsupported opcode {name}")
            base, form = FORMS[name]
            expected = 1 if form in ("j", "f", "branch") else 2
            if len(args) != expected:
                raise ValueError(f"expected {expected} operands")
            if form in ("j", "f"):
                value = number(args[0], 0, 4 if form == "j" else 3)
                if name == "notify" and value == 0:
                    raise ValueError("notify 0 is unused")
                word = base | value << 8
            elif form == "branch":
                displacement = (labels[args[0]] - pc - 1 if args[0] in labels
                                else number(args[0], -128, 127))
                if not -128 <= displacement <= 127 or not 0 <= pc + 1 + displacement < len(rows):
                    raise ValueError("branch target outside program or relative range")
                word = base | (displacement & 255)
            else:
                right = (register(args[1]) if form == "rs" else
                         number(args[1], 0, 31 if form == "rn" else 255))
                word = base | register(args[0]) << 8 | right
            if decode(word, pc).name != name:
                raise ValueError("encoding mismatch")
            words.append(word)
        except ValueError as exc:
            raise ValueError(f"line {line_no}: {exc}") from exc
    return struct.pack(f"<{len(words)}H", *words), labels


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        data, _ = assemble(args.source.read_text())
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Assembly failed: {exc}\n")
    print(f"Assembled {len(data) // 2} words; SHA-256 {hashlib.sha256(data).hexdigest()}")


if __name__ == "__main__":
    main()
