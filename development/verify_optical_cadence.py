#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Execute bounded original EEG/optical loop blocks with synthetic services.

No full process, threads, clock delays, device access or recordings are used.
Only the reviewed private executable supplies original instructions.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import struct

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE
from unicorn.arm_const import (UC_CPU_ARM_CORTEX_A7, UC_ARM_REG_R0, UC_ARM_REG_R1,
                               UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R6,
                               UC_ARM_REG_R7, UC_ARM_REG_SP, UC_ARM_REG_LR,
                               UC_ARM_REG_PC)
from verify_optical_transport import CORE_SHA256, require

RANGES = ((0x19e18, 0x19f94), (0x1cd18, 0x1cdec),
          (0x1c8dc, 0x1c944), (0x915b4, 0x916d4))
SERVICES = {0x163a4: 'read', 0x16038: 'ioctl', 0x164d0: 'write',
            0x165b4: 'queue', 0x16788: 'post', 0x16944: 'clock',
            0x16494: 'errno', 0x16008: 'log', 0x85e10: 'lock',
            0x86028: 'unlock', 0x8644c: 'wait'}
STATE, STACK, ERRNO = 0x10000000, 0x10013f00, 0x10010100
REGS = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)


class Loop:
    def __init__(self, raw, mode, count, read_result):
        self.mode, self.limit, self.read_result = mode, count, read_result
        self.reads, self.posts, self.waits, self.logs = 0, 0, 0, 0
        self.trace = []
        self.stopped = False
        self.cpu = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_A7)
        pages = {address & ~4095 for address in SERVICES}
        for start, end in RANGES:
            pages.update(range(start & ~4095, (end + 4095) & ~4095, 4096))
        for page in sorted(pages):
            self.cpu.mem_map(page, 4096, 5)
        for start, size in ((STATE, 0x6000), (0x10010000, 0x4000), (0xda2000, 4096)):
            self.cpu.mem_map(start, size, 3)
        elf = ELFFile(io.BytesIO(raw))
        for start, end in RANGES:
            for segment in elf.iter_segments():
                base = segment['p_vaddr']
                if segment['p_type'] == 'PT_LOAD' and base <= start < end <= base + segment['p_filesz']:
                    self.cpu.mem_write(start, segment.data()[start-base:end-base])
                    break
            else:
                raise ValueError('unbacked selected code')
        self.cpu.mem_write(STATE, b'\xa5' * 0x6000)
        self.word(STACK + 0x0c, STATE)
        for offset in (0x10, 0x14, 0x18, 0x28):
            self.word(STACK + offset, 0)
        self.word(STACK + 0x20, 42)
        self.word(STATE + 0x52cc, 0)
        self.word(0xda2e08, 43)
        self.word(ERRNO, 5)
        self.cpu.reg_write(UC_ARM_REG_SP, STACK)
        self.cpu.reg_write(UC_ARM_REG_R6, 0xe5d48)
        self.cpu.reg_write(UC_ARM_REG_R7, STATE + 0x3874)
        self.cpu.hook_add(UC_HOOK_CODE, self.hook)

    def word(self, address, value):
        self.cpu.mem_write(address, struct.pack('<I', value & 0xffffffff))

    def u32(self, address):
        return struct.unpack('<I', self.cpu.mem_read(address, 4))[0]

    def hook(self, cpu, address, size, _):
        loop = 0x19e18 if self.mode == 'eeg' else 0x1cdc0
        if address == loop and self.reads == self.limit:
            self.stopped = True
            cpu.emu_stop()
            return
        name = SERVICES.get(address)
        if name is None:
            require(any(start <= address < end for start, end in RANGES),
                    f'execution left selected blocks at {address:#x}')
            return
        a, b, c, d = [cpu.reg_read(reg) for reg in REGS]
        result = 0
        if name == 'read':
            row_size = 16 if self.mode == 'eeg' else 6
            row_base = STATE + (4 if self.mode == 'eeg' else 0x1f64)
            require(a == (42 if self.mode == 'eeg' else 43) and c == row_size and
                    b == row_base + 16 * (self.reads % 400), 'unexpected read target')
            n = max(0, min(row_size, self.read_result))
            cpu.mem_write(b, bytes((self.reads * 7 + j * 11) & 255 for j in range(n)))
            self.trace.append(['read', self.reads, row_size, self.read_result])
            self.reads += 1
            result = self.read_result
        elif name == 'post':
            if a == STATE + 0x3874:
                self.trace.append(['motion_wake', self.reads])
            elif a == STATE + 0x1f54:
                self.trace.append(['optical_wake', self.reads])
            else:
                require(a == STATE + (0x1f44 if self.mode == 'eeg' else 0x3864),
                        'unexpected queue publication')
                self.posts += 1
                self.trace.append(['publish', self.reads])
        elif name == 'queue':
            require(a == STATE + (0x1f44 if self.mode == 'eeg' else 0x3864), 'unexpected queue')
            self.word(b, 0)  # Model an available, nonbacklogged software queue.
        elif name in ('lock', 'unlock'):
            require(a == (STATE + 0x5310 if self.mode == 'eeg' else 0xecae74), 'unexpected lock')
        elif name == 'clock':
            require(a == 0 and b == STACK + 0x34, 'unexpected deadline clock')
            self.word(b, 100000)
            self.word(b + 4, 0)
        elif name == 'wait':
            require(a == STATE + 0x1f54 and b == STACK + 0x34 and self.u32(b) == 100020,
                    'unexpected wake or timeout')
            self.waits += 1
            self.trace.append(['wait', self.reads])
        elif name == 'ioctl':
            require((a, b, c) == (43, 0x703, 0x57), 'unexpected optical selection')
            self.trace.append(['select', self.reads])
        elif name == 'write':
            require(a == 43 and c == 1 and bytes(cpu.mem_read(b, 1)) == b'\x07',
                    'optical producer requested a different register')
            self.trace.append(['register', 7])
            result = 1
        elif name == 'errno':
            result = ERRNO
        elif name == 'log':
            self.logs += 1
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def run(self):
        self.cpu.emu_start(0x19e18 if self.mode == 'eeg' else 0x1cd18, 0,
                           timeout=3_000_000, count=(self.limit + 1) * 180)
        require(self.stopped and self.posts == self.limit, 'loop did not complete within bound')
        offset = self.u32(STACK + 0x14 if self.mode == 'eeg' else STATE + 0x52cc)
        require(offset == (self.limit % 400) * 16, 'ring offset differs')
        if self.mode == 'eeg':
            expected = []
            for i in range(self.limit):
                if i % 5 == 0:
                    expected.extend([['motion_wake', i], ['optical_wake', i]])
                expected.extend([['read', i, 16, self.read_result], ['publish', i + 1]])
            require(self.trace == expected, 'EEG wake cadence or publication differs')
            require(self.u32(STACK + 0x18) == self.limit % 400, 'flag ring offset differs')
            require(self.logs == self.limit * int(self.read_result != 16), 'short-read reporting differs')
        else:
            expected = []
            for i in range(self.limit):
                expected.extend([['wait', i], ['select', i], ['register', 7],
                                 ['read', i, 6, self.read_result], ['publish', i + 1]])
            require(self.trace == expected and self.waits == self.limit, 'optical cadence differs')
            require(self.u32(STACK + 0x28) == self.limit * int(self.read_result != 6),
                    'optical error accumulation differs')
            for i in range(min(self.limit, 400)):
                require(self.cpu.mem_read(STATE + 0x1f64 + 16*i + 6, 1)[0] ==
                        int(self.read_result != 6), 'optical status byte differs')
        return {'mode': self.mode, 'iterations': self.limit, 'read_result': self.read_result,
                'published': self.posts, 'ring_offset': offset,
                'trace_sha256': hashlib.sha256(json.dumps(self.trace).encode()).hexdigest()}


def verify(path):
    raw = path.read_bytes()
    require(len(raw) == 14_170_096 and hashlib.sha256(raw).hexdigest() == CORE_SHA256,
            'unreviewed executable')
    counts = (0, 1, 4, 5, 6, 31, 32, 399, 400, 401, 801)
    cases = [Loop(raw, mode, n, result).run()
             for mode, size in (('eeg', 16), ('optical', 6))
             for n in counts for result in (-1, 0, size - 1, size)]
    return {'core_sha256': CORE_SHA256, 'cases': len(cases),
            'selected_code_and_literal_bytes': sum(end-start for start, end in RANGES),
            'fixture_sha256': hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
            'eeg_wake_interval_iterations': 5, 'optical_read_bytes_per_wake': 6,
            'short_reads_still_publish': True,
            'boundaries': ['selected loop blocks only', 'successful semaphore/mutex services',
                           'no queue backlog', 'synthetic I/O and time',
                           'no physical sample rate or scheduler timing measurement']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('nano_core', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.nano_core), indent=2))
