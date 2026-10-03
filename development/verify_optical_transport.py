#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Verify optical formats and I2C failure behavior using isolated saved ARM code.

The optional original executable is hash pinned and never started as a process.
Only selected conversion, writer, register-setup and transport routines execute
in Unicorn. System calls and hardware responses are synthetic. No device opens.
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
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE
from unicorn.arm_const import (UC_CPU_ARM_CORTEX_A7, UC_ARM_REG_R0, UC_ARM_REG_R1,
                               UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_SP,
                               UC_ARM_REG_LR, UC_ARM_REG_PC)

CORE_SHA256 = 'dfc83b247b505aa08ea62060f999192112a47b606754d5f469357b7addf0295a'
RANGES = ((0x1c828, 0x1c944), (0x27980, 0x27a08),
          (0x902b8, 0x905d0), (0x91578, 0x915b4))
RETURN, DATA, OUTPUT, ERRNO, FILE, STACK = 0x10000, 0x10000000, 0x10000100, 0x10000400, 0x10000300, 0x10003f00
SERVICES = {0x16038: 'ioctl', 0x164d0: 'write', 0x163a4: 'read',
            0x16530: 'fwrite', 0x16494: 'errno', 0x16008: 'syslog'}
REGISTERS = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)
SOURCE = Path(__file__).resolve().parent
HARNESS = '''#include "optical_samples.h"
#include <stdio.h>
int main(void) {
    unsigned char raw[6]; uint32_t output[2]; size_t n;
    while ((n = fread(raw, 1, sizeof raw, stdin)) == sizeof raw) {
        dreem_optical_decode(raw, output);
        if (fwrite(output, 1, sizeof output, stdout) != sizeof output) return 1;
    }
    return n || ferror(stdin) || fflush(stdout) ? 1 : 0;
}
'''


def require(condition, message):
    if not condition:
        raise ValueError(message)


def pattern(length):
    return bytes((i * 37 + 11) & 255 for i in range(length))


class Stock:
    def __init__(self, path):
        raw = path.read_bytes()
        require(len(raw) == 14_170_096 and hashlib.sha256(raw).hexdigest() == CORE_SHA256,
                'unreviewed executable')
        elf = ELFFile(io.BytesIO(raw))
        self.cpu = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_A7)
        for page in (RETURN, 0x16000, 0x1c000, 0x27000, 0x90000, 0x91000):
            self.cpu.mem_map(page, 0x1000, 5)
        for base, length in ((DATA, 0x4000), (0xda2000, 0x1000), (0xeca000, 0x1000)):
            self.cpu.mem_map(base, length, 3)
        for start, end in RANGES:
            for segment in elf.iter_segments():
                base = segment['p_vaddr']
                if segment['p_type'] == 'PT_LOAD' and base <= start < end <= base + segment['p_filesz']:
                    self.cpu.mem_write(start, segment.data()[start-base:end-base])
                    break
            else:
                raise ValueError('unbacked code range')
        self.cpu.hook_add(UC_HOOK_CODE, self.code)
        self.reset()

    def word(self, address, value):
        self.cpu.mem_write(address, struct.pack('<I', value & 0xffffffff))

    def reset(self, selection=0, write=None, read=None, fail_at=None, cancel_at=None):
        self.selection, self.write_result, self.read_result = selection, write, read
        self.fail_at, self.cancel_at = fail_at, cancel_at
        self.trace, self.file_writes, self.logs, self.selects, self.writes = [], [], 0, 0, 0
        self.file_result = 8
        self.word(ERRNO, 0)
        self.word(0xda2e08, 42)
        self.cpu.mem_write(OUTPUT - 4, b'\xa5' * 264)

    def code(self, cpu, address, size, _):
        name = SERVICES.get(address)
        if name is None:
            require(any(start <= address < end for start, end in RANGES),
                    f'execution left selected code at {address:#x}')
            return
        a, b, c, d = [cpu.reg_read(r) for r in REGISTERS]
        if name == 'ioctl':
            require((a, b, c) == (42, 0x703, 0x57), 'unexpected address selection')
            self.selects += 1
            result = -1 if self.selects == self.cancel_at else self.selection
            self.trace.append(['select', c, result])
        elif name == 'write':
            require(a == 42 and c in (1, 2), 'unexpected original write')
            self.writes += 1
            result = c if self.write_result is None else self.write_result
            if self.writes in (self.fail_at, self.cancel_at):
                result = 0
            self.trace.append(['write', bytes(cpu.mem_read(b, c)).hex(), result])
        elif name == 'read':
            require(a == 42 and b == OUTPUT and c in (1, 6), 'unexpected original read')
            result = c if self.read_result is None else self.read_result
            if result > 0:
                cpu.mem_write(b, pattern(min(result, c)))
            self.trace.append(['read', c, result])
        elif name == 'fwrite':
            require((a, b, c, d) == (OUTPUT, 1, 8, FILE), 'unexpected pulse writer')
            self.file_writes.append(bytes(cpu.mem_read(a, c)))
            result = self.file_result
        elif name == 'errno':
            result = ERRNO
        else:
            self.logs += 1
            result = 0
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def call(self, entry, *arguments):
        self.cpu.reg_write(UC_ARM_REG_SP, STACK)
        for register, value in zip(REGISTERS, arguments):
            self.cpu.reg_write(register, value)
        if len(arguments) > 4:
            self.word(STACK, arguments[4])
        self.cpu.reg_write(UC_ARM_REG_LR, RETURN)
        self.cpu.emu_start(entry, RETURN, timeout=100000, count=3000)
        require(self.cpu.reg_read(UC_ARM_REG_PC) == RETURN, 'execution exceeded bound')
        value = self.cpu.reg_read(UC_ARM_REG_R0)
        return value if value < 0x80000000 else value - 0x100000000

    def convert(self, row):
        self.cpu.mem_write(DATA, row + b'\xa5' * 10)
        self.cpu.mem_write(OUTPUT - 4, b'\xa5' * 16)
        self.call(0x91578, DATA, OUTPUT)
        require(bytes(self.cpu.mem_read(DATA, 16)) == row + b'\xa5' * 10, 'modified optical input')
        require(bytes(self.cpu.mem_read(OUTPUT - 4, 4)) == b'\xa5' * 4 and
                bytes(self.cpu.mem_read(OUTPUT + 8, 4)) == b'\xa5' * 4, 'optical buffer overrun')
        return bytes(self.cpu.mem_read(OUTPUT, 8))


def verify(path):
    stock = Stock(path)
    values = (0, 1, 0xffff, 0x10000, 0x3ffff, 0x40000, 0xffffff)
    pairs = list(itertools.product(values, repeat=2))
    pairs += [(1 << bit, 0) for bit in range(24)] + [(0, 1 << bit) for bit in range(24)]
    rng = random.Random(20261003)
    pairs += [(rng.randrange(1 << 24), rng.randrange(1 << 24)) for _ in range(4096)]
    rows = [a.to_bytes(3, 'big') + b.to_bytes(3, 'big') for a, b in pairs]
    expected = b''.join(stock.convert(row) for row in rows)
    require(expected == b''.join(struct.pack('<II', *pair) for pair in pairs), 'unexpected optical format')
    builds = {}
    with tempfile.TemporaryDirectory(prefix='dreem-optical-') as tmp:
        root = Path(tmp)
        (root/'harness.c').write_text(HARNESS)
        for label, compiler, flags, runner in (
            ('host', 'cc', [], []),
            ('arm', 'arm-linux-gnueabihf-gcc', ['-marm', '-mcpu=cortex-a7', '-static'],
             ['qemu-arm', '-cpu', 'cortex-a7'])):
            exe = root/label
            subprocess.run([compiler, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', *flags,
                            '-I', str(SOURCE), str(root/'harness.c'), str(SOURCE/'optical_samples.c'),
                            '-o', str(exe)], capture_output=True, check=True)
            p = subprocess.run(runner+[str(exe)], input=b''.join(rows), capture_output=True, check=True, timeout=20)
            require(p.stdout == expected, label + ' optical decoder differs')
            builds[label] = hashlib.sha256(exe.read_bytes()).hexdigest()
    for count in (8, 7, 1, 0):
        stock.reset()
        stock.file_result = count
        stock.cpu.mem_write(OUTPUT, expected[:8])
        result = stock.call(0x27980, OUTPUT, FILE)
        require(result == (0 if count == 8 else -1) and stock.file_writes == [expected[:8]] and
                stock.logs == int(count != 8), 'unexpected pulse writer handling')
    transport_cases, false_successes = [], []
    for name, entry, length in (('write', 0x1c828, 2), ('read_byte', 0x1c878, 1), ('read_six', 0x1c8dc, 6)):
        write_size = 2 if name == 'write' else 1
        reads = (None,) if name == 'write' else tuple(sorted({-1, 0, length - 1, length}))
        for selection, written, received in itertools.product((-1, 0), (-1, 0, write_size), reads):
            stock.reset(selection=selection, write=written, read=received)
            arguments = (42, 0x57, 7, 0x5a) if name == 'write' else (42, 0x57, 7, OUTPUT, length)
            result = stock.call(entry, *arguments)
            predicted = selection + int(written != write_size)
            if name != 'write': predicted += int(received != length)
            require(result == predicted, 'original error arithmetic changed')
            require(len(stock.trace) == (2 if name == 'write' else 3), 'original unexpectedly stopped early')
            output = bytes(stock.cpu.mem_read(OUTPUT, length))
            n = min(max(received or 0, 0), length) if name != 'write' else 0
            require(output == pattern(n) + b'\xa5' * (length - n), 'unexpected original partial output')
            case = {'helper': name, 'selection': selection, 'write': written,
                    'read': received, 'return': result, 'output': output.hex()}
            transport_cases.append(case)
            if result == 0 and (selection != 0 or written != write_size or (name != 'write' and received != length)):
                false_successes.append(case)
    require(false_successes, 'expected original cancellation defect was not reproduced')
    setup_cases = 0
    for red, infrared in itertools.product((0, 10, 255), (1, 60, 160, 255)):
        expected_writes = [(4, 0), (5, 0), (6, 0), (8, 6), (10, 0x47),
                           (12, red), (13, infrared), (0x30, 0x21), (2, 0x80), (3, 0)]
        for failure in (None, *range(1, 11)):
            stock.reset(fail_at=failure)
            stock.word(0xecae70, red)
            stock.word(0xda2e0c, infrared)
            result = stock.call(0x902b8)
            writes = [bytes.fromhex(item[1]) for item in stock.trace if item[0] == 'write']
            wanted = expected_writes if failure is None else expected_writes[:failure]
            require(writes == [bytes(pair) for pair in wanted], 'register sequence differs')
            require(result == int(failure is not None) and stock.logs == int(failure is not None), 'setup error result differs')
            setup_cases += 1
    for cancellation in range(1, 11):
        stock.reset(cancel_at=cancellation)
        stock.word(0xecae70, 10)
        stock.word(0xda2e0c, 60)
        require(stock.call(0x902b8) == 0 and stock.writes == 10,
                'setup no longer propagates original false success')
    return {'core_sha256': CORE_SHA256, 'decoded_records_per_build': len(rows),
            'decoder_mismatches': 0, 'pulse_writer_cases': 4,
            'original_transport_cases': len(transport_cases),
            'original_false_successes': false_successes,
            'register_setup_cases': setup_cases, 'setup_false_success_cases': 10,
            'isolated_code_bytes': sum(end-start for start, end in RANGES),
            'fixture_result_sha256': hashlib.sha256(b''.join(rows)+expected).hexdigest(),
            'transport_result_sha256': hashlib.sha256(json.dumps(transport_cases, sort_keys=True).encode()).hexdigest(),
            'temporary_decoder_builds': builds,
            'source_sha256': {name: hashlib.sha256((SOURCE/name).read_bytes()).hexdigest()
                              for name in ('optical_samples.c', 'optical_samples.h', 'verify_optical_transport.py')},
            'hardware_qualified': False, 'installed': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('nano_core', type=Path)
    print(json.dumps(verify(parser.parse_args().nano_core), indent=2))
