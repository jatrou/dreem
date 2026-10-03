#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compare independent host/ARM motion conversion with isolated stock code.

Only the reviewed conversion and row-writer routines are emulated. All inputs
are synthetic. No vendor process, device, network, or personal recording runs.
"""
import argparse
import hashlib
import io
import itertools
import json
from pathlib import Path
import random
import struct
import subprocess
import tempfile

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE, UC_PROT_READ, UC_PROT_WRITE, UC_PROT_EXEC
from unicorn.arm_const import (UC_CPU_ARM_CORTEX_A7, UC_ARM_REG_C1_C0_2,
                               UC_ARM_REG_FPEXC, UC_ARM_REG_FPSCR, UC_ARM_REG_SP,
                               UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                               UC_ARM_REG_R3, UC_ARM_REG_LR, UC_ARM_REG_PC)

CORE_SHA256 = 'dfc83b247b505aa08ea62060f999192112a47b606754d5f469357b7addf0295a'
ENTRY, END, WRITER, WRITER_END = 0x2d6f0, 0x2d7e0, 0x27a08, 0x27a90
RETURN, INPUT, OUTPUT, STACK = 0x10000, 0x10000000, 0x10000100, 0x10003000
INITIAL, COSINE, SINE = 0xda27d8, 0xda3398, 0xda33a0
ERRNO, FILE = INPUT + 0x200, INPUT + 0x300
SERVICES = {0x16530: 'fwrite', 0x16494: 'errno', 0x16008: 'syslog'}
SOURCE = Path(__file__).resolve().parent
HARNESS = '''#include "motion_samples.h"
#include <stdio.h>
int main(void) {
    unsigned char row[6]; float output[3]; size_t n;
    while ((n = fread(row, 1, sizeof row, stdin)) == sizeof row) {
        dreem_motion_decode(row, output);
        if (fwrite(output, 1, sizeof output, stdout) != sizeof output) return 1;
    }
    return n || ferror(stdin) || fflush(stdout) ? 1 : 0;
}
'''


def require(test, message):
    if not test:
        raise ValueError(message)


class Stock:
    def __init__(self, path):
        require(path.stat().st_size == 14_170_096, 'unexpected core size')
        raw = path.read_bytes()
        require(hashlib.sha256(raw).hexdigest() == CORE_SHA256, 'unreviewed core hash')
        elf = ELFFile(io.BytesIO(raw))
        self.cpu = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_A7)
        for base, size, flags in ((0x10000, 0x1000, 5), (0x16000, 0x1000, 5),
                                  (0x27000, 0x1000, 5), (0x2d000, 0x1000, 5),
                                  (0xda2000, 0x2000, 3), (INPUT, 0x4000, 3)):
            self.cpu.mem_map(base, size, flags)
        for start, end in ((ENTRY, END), (WRITER, WRITER_END)):
            for segment in elf.iter_segments():
                base = segment['p_vaddr']
                if segment['p_type'] == 'PT_LOAD' and base <= start < end <= base + segment['p_filesz']:
                    self.cpu.mem_write(start, segment.data()[start-base:end-base])
                    break
            else:
                raise ValueError('routine is not file backed')
        self.cpu.reg_write(UC_ARM_REG_C1_C0_2, 0xf00000)
        self.cpu.reg_write(UC_ARM_REG_FPEXC, 1 << 30)
        self.cpu.hook_add(UC_HOOK_CODE, self.code)
        self.write_result, self.writes, self.logs = 12, [], 0
        self.cpu.mem_write(INITIAL, struct.pack('<I', 1))

    def code(self, cpu, address, size, _):
        name = SERVICES.get(address)
        if name is None:
            require(ENTRY <= address < 0x2d7c4 or WRITER <= address < WRITER_END,
                    'execution left isolated routines')
            return
        a, b, c, d = [cpu.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
        if name == 'fwrite':
            require((a, b, c, d) == (OUTPUT, 1, 12, FILE), 'unexpected motion writer arguments')
            self.writes.append(bytes(cpu.mem_read(a, 12)))
            result = self.write_result
        elif name == 'errno':
            result = ERRNO
        else:
            self.logs += 1
            result = 0
        cpu.reg_write(UC_ARM_REG_R0, result)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def call(self, entry, a, b):
        self.cpu.reg_write(UC_ARM_REG_SP, STACK)
        self.cpu.reg_write(UC_ARM_REG_R0, a)
        self.cpu.reg_write(UC_ARM_REG_R1, b)
        self.cpu.reg_write(UC_ARM_REG_LR, RETURN)
        self.cpu.reg_write(UC_ARM_REG_FPSCR, 0)
        self.cpu.emu_start(entry, RETURN, timeout=100000, count=250)
        require(self.cpu.reg_read(UC_ARM_REG_PC) == RETURN, 'routine exceeded execution bound')
        return self.cpu.reg_read(UC_ARM_REG_R0)

    def convert(self, row):
        self.cpu.mem_write(INPUT, row + b'\xa5' * 10)
        self.cpu.mem_write(OUTPUT - 4, b'\xa5' * 20)
        self.call(ENTRY, INPUT, OUTPUT)
        require(bytes(self.cpu.mem_read(INPUT, 16)) == row + b'\xa5' * 10, 'conversion modified input')
        require(bytes(self.cpu.mem_read(OUTPUT-4, 4)) == b'\xa5' * 4 and
                bytes(self.cpu.mem_read(OUTPUT+12, 4)) == b'\xa5' * 4, 'conversion wrote outside output')
        return bytes(self.cpu.mem_read(OUTPUT, 12))


def verify(path):
    stock = Stock(path)
    values = (-32768, -16384, -1, 0, 1, 16384, 32767)
    rows = [struct.pack('<3h', *v) for v in itertools.product(values, repeat=3)]
    rng = random.Random(20261003)
    rows += [struct.pack('<3h', *(rng.randrange(-32768, 32768) for _ in range(3))) for _ in range(4096)]
    expected = b''.join(stock.convert(row) for row in rows)
    # Prove both lazy initialization and the already-initialized branch.
    require(stock.cpu.mem_read(INITIAL, 4) == b'\0' * 4, 'lazy initialization not completed')
    require(bytes(stock.cpu.mem_read(COSINE, 16)) == struct.pack('<2d',
            float.fromhex('0x1.e11f642522d1cp-1'), float.fromhex('0x1.5e3a8748a0bf5p-2')),
            'rotation constants differ')
    for i, row in enumerate(rows[:343]):
        require(stock.convert(row) == expected[12*i:12*i+12], 'warm conversion differs')
    artifact_hashes = {}
    with tempfile.TemporaryDirectory(prefix='dreem-motion-') as tmp:
        root = Path(tmp)
        (root/'harness.c').write_text(HARNESS)
        for label, compiler, flags, runner in (
            ('host', 'cc', [], []),
            ('arm', 'arm-linux-gnueabihf-gcc', ['-marm', '-mcpu=cortex-a7', '-mfpu=neon-vfpv4', '-mfloat-abi=hard', '-static'],
             ['qemu-arm', '-cpu', 'cortex-a7'])):
            exe = root/label
            subprocess.run([compiler, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-ffp-contract=off',
                            *flags, '-I', str(SOURCE), str(root/'harness.c'), str(SOURCE/'motion_samples.c'),
                            '-lm', '-o', str(exe)], check=True, capture_output=True)
            actual = subprocess.run(runner+[str(exe)], input=b''.join(rows), capture_output=True, check=True, timeout=30).stdout
            require(actual == expected, label+' conversion differs from stock output')
            artifact_hashes[label] = hashlib.sha256(exe.read_bytes()).hexdigest()
    for result in (12, 11, 1, 0):
        stock.write_result, stock.writes, stock.logs = result, [], 0
        stock.cpu.mem_write(OUTPUT, expected[:12])
        stock.cpu.mem_write(ERRNO, b'\0'*4)
        ret = stock.call(WRITER, OUTPUT, FILE)
        require(ret == (0 if result == 12 else 0xffffffff) and stock.writes == [expected[:12]] and
                stock.logs == int(result != 12), 'row writer or short-write handling differs')
    return {'core_sha256': CORE_SHA256, 'compared_records_per_build': len(rows),
            'host_and_arm_mismatches': 0, 'warm_repeat_cases': 343, 'row_writer_cases': 4,
            'isolated_code_and_literal_bytes': END-ENTRY+WRITER_END-WRITER,
            'fixture_result_sha256': hashlib.sha256(b''.join(rows)+expected).hexdigest(),
            'temporary_harness_builds': artifact_hashes,
            'source_sha256': {n: hashlib.sha256((SOURCE/n).read_bytes()).hexdigest()
                              for n in ('motion_samples.c', 'motion_samples.h', 'verify_motion_decoder.py')},
            'hardware_qualified': False, 'installed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('nano_core', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.nano_core), indent=2))


if __name__ == '__main__':
    main()
