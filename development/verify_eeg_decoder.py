#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compare the independent decoder to an isolated stock ARM function.

Requires an already acquired exact nano_core, pyelftools, and Unicorn 2.1.4.
Maps only the 132-byte conversion routine and literal into a bounded emulator.
No vendor executable is launched as a process, and no device or network exists
in the emulation. This verifies numerical conversion, not acquisition timing.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path
import random
import struct

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_PROT_READ, UC_PROT_WRITE, UC_PROT_EXEC
from unicorn.arm_const import (UC_CPU_ARM_CORTEX_A7, UC_ARM_REG_C1_C0_2,
                               UC_ARM_REG_FPEXC, UC_ARM_REG_FPSCR, UC_ARM_REG_SP,
                               UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_LR, UC_ARM_REG_PC)

try:
    from .eeg_samples import decode_driver_record
except ImportError:
    from eeg_samples import decode_driver_record

CORE_SHA256 = "dfc83b247b505aa08ea62060f999192112a47b606754d5f469357b7addf0295a"
ENTRY, END, RETURN = 0x2D66C, 0x2D6F0, 0x2D000
SETTINGS, INPUT, OUTPUT, STACK = 0xDA3378, 0x10000000, 0x10000100, 0x10003000


def conversion_code(path):
    if path.stat().st_size != 14_170_096:
        raise ValueError("unexpected nano_core size")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != CORE_SHA256:
        raise ValueError("nano_core SHA-256 does not match the reviewed input")
    elf = ELFFile(io.BytesIO(raw))
    for segment in elf.iter_segments():
        start = segment["p_vaddr"]
        if segment["p_type"] == "PT_LOAD" and start <= ENTRY < END <= start + segment["p_filesz"]:
            return segment.data()[ENTRY - start:END - start]
    raise ValueError("conversion routine not in a file-backed load segment")


def verify(path, random_records=4096):
    uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
    uc.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_A7)
    uc.mem_map(0x2D000, 0x1000, UC_PROT_READ | UC_PROT_EXEC)
    uc.mem_write(ENTRY, conversion_code(path))
    uc.mem_map(0xDA3000, 0x1000, UC_PROT_READ | UC_PROT_WRITE)
    uc.mem_map(INPUT, 0x4000, UC_PROT_READ | UC_PROT_WRITE)
    uc.reg_write(UC_ARM_REG_C1_C0_2, 0xF00000)
    uc.reg_write(UC_ARM_REG_FPEXC, 1 << 30)
    rng = random.Random(20261002)
    cases = [b"\0" * 16, b"\xff" * 16,
             b"\x80\x00\x00\x7f\xff\xff\x00\x00\x01\xff\xff\xff" + b"\0" * 4]
    cases.extend(bytes(rng.randrange(256) for _ in range(16)) for _ in range(random_records))
    evidence = hashlib.sha256()
    for revision in (0, 1):
        # Independently recorded initialization data from stock FUN_0002d000.
        settings = (0, 1, 2, 3, 1, 1, 1, 0) if revision == 0 else (2, 1, 3, 0, 0, 1, 1, 1)
        uc.mem_write(SETTINGS, struct.pack("<8I", *settings))
        for case in cases:
            uc.mem_write(INPUT, case)
            uc.mem_write(OUTPUT, b"\xa5" * 16)
            uc.reg_write(UC_ARM_REG_SP, STACK)
            uc.reg_write(UC_ARM_REG_R0, INPUT)
            uc.reg_write(UC_ARM_REG_R1, OUTPUT)
            uc.reg_write(UC_ARM_REG_LR, RETURN)
            uc.reg_write(UC_ARM_REG_FPSCR, 0)
            uc.emu_start(ENTRY, RETURN, timeout=100000, count=200)
            if uc.reg_read(UC_ARM_REG_PC) != RETURN or uc.reg_read(UC_ARM_REG_R0) != 0:
                raise ValueError("stock conversion did not return within the instruction/time limit")
            actual = bytes(uc.mem_read(OUTPUT, 16))
            expected = struct.pack("<4f", *decode_driver_record(case, hardware_version=revision))
            if actual != expected:
                raise ValueError(f"conversion mismatch for hardware branch {revision}")
            evidence.update(bytes([revision]) + case + actual)
    return {"core_sha256": CORE_SHA256, "mapped_code_bytes": END - ENTRY,
            "compared_records": 2 * len(cases), "mismatches": 0,
            "fixture_result_sha256": evidence.hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("nano_core", type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.nano_core)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Verification failed: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
