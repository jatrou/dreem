#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Verify normal-recording cadence using bounded original ARM blocks.

Runs acquisition reset, the per-sample acquisition/file-write prefix, and the
counter increment block. Algorithm processing between prefix and increment is
not executed: checks assume that intervening processing completed successfully.
Sensor reads/conversions, synchronization, file writes and Nerves callbacks are
modeled. This is not execution of the complete recorder or its hardware.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import struct
import subprocess
import tempfile

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE
from unicorn.arm_const import (UC_CPU_ARM_CORTEX_A7, UC_ARM_REG_C1_C0_2, UC_ARM_REG_FPEXC,
                               UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
                               UC_ARM_REG_R6, UC_ARM_REG_R7, UC_ARM_REG_R10, UC_ARM_REG_R11,
                               UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC)

CORE_SHA256 = 'dfc83b247b505aa08ea62060f999192112a47b606754d5f469357b7addf0295a'
RANGES = ((0x19240, 0x19498), (0x2ea50, 0x2ecb0), (0x2ee68, 0x2ee90),
          (0x2f43c, 0x2f488), (0x2f58c, 0x2f5ac))
RETURN, DATA, STATE, STACK = 0x10000, 0x10000000, 0x10001000, 0x1000b000
REGS = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)
SERVICES = {0x1638c: 'memset', 0x161b8: 'sem_destroy', 0x16068: 'sem_init',
            0x16944: 'clock', 0x8644c: 'wait_eeg', 0x863b8: 'wait_sensor',
            0x91578: 'pulse_convert', 0x2d6f0: 'motion_convert', 0x2d66c: 'eeg_convert',
            0x28038: 'event', 0x27980: 'pulse_write', 0x27a08: 'motion_write', 0x278f8: 'eeg_write',
            **dict.fromkeys((0xa3b04, 0xa3b64, 0xa3b7c, 0xa3af8, 0xa3b58, 0xa3b70), 'nerves')}
SOURCE = Path(__file__).resolve().parent


def require(condition, message):
    if not condition:
        raise ValueError(message)


def event(counter, code, value):
    return struct.pack('<IBI', counter, code, value)


class Stock:
    def __init__(self, path):
        raw = path.read_bytes()
        require(len(raw) == 14_170_096 and hashlib.sha256(raw).hexdigest() == CORE_SHA256,
                'unreviewed executable')
        elf = ELFFile(io.BytesIO(raw))
        self.cpu = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_A7)
        for page in (0x10000, 0x16000, 0x19000, 0x27000, 0x28000, 0x2d000,
                     0x2e000, 0x2f000, 0x86000, 0x91000, 0xa3000):
            self.cpu.mem_map(page, 0x1000, 5)
        self.cpu.mem_map(DATA, 0x10000, 3)
        for start, end in RANGES:
            for segment in elf.iter_segments():
                base = segment['p_vaddr']
                if segment['p_type'] == 'PT_LOAD' and base <= start < end <= base+segment['p_filesz']:
                    self.cpu.mem_write(start, segment.data()[start-base:end-base])
                    break
            else:
                raise ValueError('unbacked code range')
        self.cpu.reg_write(UC_ARM_REG_C1_C0_2, 0xf00000)
        self.cpu.reg_write(UC_ARM_REG_FPEXC, 1 << 30)
        self.cpu.hook_add(UC_HOOK_CODE, self.code)

    def word(self, address, value=None):
        if value is not None:
            self.cpu.mem_write(address, struct.pack('<I', value & 0xffffffff))
        return struct.unpack('<I', self.cpu.mem_read(address, 4))[0]

    def execute(self, start, end):
        self.cpu.emu_start(start, end, timeout=100000, count=1000)
        require(self.cpu.reg_read(UC_ARM_REG_PC) == end, 'execution exceeded bound')

    def code(self, cpu, address, size, _):
        name = SERVICES.get(address)
        if name is None:
            require(any(start <= address < end for start, end in RANGES), 'execution left reviewed blocks')
            return
        a,b,c,d = [cpu.reg_read(r) for r in REGS]
        result = 0
        counter = self.word(STATE+0x547e)
        if name == 'memset':
            require(STATE <= a <= a+c <= STATE+0x6000 and b == 0, 'unexpected reset range')
            cpu.mem_write(a, b'\0'*c)
            result = a
        elif name in ('sem_destroy', 'sem_init'):
            require(STATE <= a < STATE+0x6000, 'unexpected semaphore')
        elif name == 'clock':
            require(a == 0 and b == STACK+0xb0, 'unexpected clock call')
            cpu.mem_write(b, struct.pack('<II', 1000, 0))
        elif name == 'wait_eeg':
            require(a == STATE+0x1f44, 'unexpected EEG semaphore')
        elif name == 'wait_sensor':
            require(a in (STATE+0x3864, STATE+0x5184), 'unexpected sensor semaphore')
            require(self.iteration % 5 == 0, 'sensor sampled off cadence')
        elif name in ('motion_convert', 'pulse_convert'):
            row = self.iteration//5
            require(self.iteration % 5 == 0, 'conversion off cadence')
            ring = STATE + (0x3884 if name == 'motion_convert' else 0x1f64) + (row % 400)*16
            require(a == ring, 'unexpected sensor ring index')
            cpu.mem_write(a+6, bytes([int(row % 6 in (2, 3))]))
            values = struct.pack('<3f', row % 3-1, 0, 1) if name == 'motion_convert' else b'\x01'*8
            cpu.mem_write(b, values)
        elif name == 'eeg_convert':
            require(a == STATE+4, 'unexpected modeled EEG ring origin')
            cpu.mem_write(b, struct.pack('<4f', 1, 2, 3, 4))
        elif name == 'event':
            require(a == STATE and b in (30, 31) and d == 4, 'unexpected event arguments')
            value = self.word(c)
            self.events.append(event(counter, b, value))
            self.trace.append(('health', counter, b, value))
        elif name.endswith('_write'):
            kind = name.removesuffix('_write')
            expected = {'eeg': (0x4001, 16), 'pulse': (0x4002, 8), 'motion': (0x4003, 12)}[kind]
            require(b == expected[0], 'unexpected output file handle')
            data = bytes(cpu.mem_read(a, expected[1]))
            self.files[kind].append(data)
            self.trace.append((kind, counter))
        elif name == 'nerves':
            require(a == STACK+0x358, 'unexpected Nerves context')
        cpu.reg_write(UC_ARM_REG_R0, result)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def run(self, rows, hardware, initial_counter=0):
        self.events, self.trace = [], []
        self.files = {'eeg': [], 'pulse': [], 'motion': []}
        self.cpu.mem_write(DATA, b'\0'*0x10000)
        self.cpu.mem_write(STATE, b'\xa5'*0x6000)
        self.cpu.reg_write(UC_ARM_REG_SP, STACK)
        self.cpu.reg_write(UC_ARM_REG_R0, STATE)
        self.cpu.reg_write(UC_ARM_REG_LR, RETURN)
        self.execute(0x19240, RETURN)
        require(self.cpu.reg_read(UC_ARM_REG_R0) == STATE and self.word(STATE+0x547e) == 0,
                'reset did not establish zero counter')
        self.word(STATE+0x547e, initial_counter)
        for offset,value in ((0x52d0, 0x4001), (0x52d4, 0x4002), (0x52d8, 0x4003)):
            self.word(STATE+offset, value)
        self.cpu.reg_write(UC_ARM_REG_SP, STACK)
        for reg,value in ((UC_ARM_REG_R6, STATE), (UC_ARM_REG_R7, 0),
                          (UC_ARM_REG_R10, 0), (UC_ARM_REG_R11, 0)):
            self.cpu.reg_write(reg, value)
        for offset,value in ((0x14, hardware), (0x18, STATE), (0x20, 0), (0x34, 0),
                              (0x38, 0xffffffff), (0x3c, 0x51eb851f),
                              (0x40, 0xffffffff), (0x44, STATE+0x1f44)):
            self.word(STACK+offset, value)
        for self.iteration in range(rows):
            self.execute(0x2ea60, 0x2ecb0)
            # The excluded algorithm section can terminate; this continuation
            # explicitly models only its successful completion.
            self.execute(0x2ea50, 0x2ea60)
        require(self.word(STATE+0x547e) == (initial_counter+rows) & 0xffffffff, 'counter step differs')
        expected = []
        previous_motion = previous_pulse = None
        expected_motion = []
        for i in range(rows):
            counter = (initial_counter+i) & 0xffffffff
            if i % 5 == 0:
                row = i//5
                good = int(row % 6 not in (2, 3))
                if hardware != 3:
                    if good != previous_pulse: expected.append(('health', counter, 31, good))
                    expected.append(('pulse', counter))
                    previous_pulse = good
                motion_good = 1 if hardware == 3 else good
                if motion_good != previous_motion: expected.append(('health', counter, 30, motion_good))
                expected.append(('motion', counter))
                previous_motion = motion_good
                expected_motion.append(struct.pack('<3f', row % 3-1, 0, 1) if motion_good else b'\0'*12)
            expected.append(('eeg', counter))
        require(self.trace == expected, 'health/event/sample ordering differs')
        require(self.files['motion'] == expected_motion, 'failed motion reads were not replaced with zeros')
        return self.files, self.events


def verify(path):
    stock = Stock(path)
    cases = frames = 0
    combined = hashlib.sha256()
    artifacts = {}
    with tempfile.TemporaryDirectory(prefix='dreem-cadence-') as tmp:
        root = Path(tmp)
        for hardware in (0, 1, 3):
            for rows in (0, 1, 5, 6, 251, 2001):
                files, events = stock.run(rows, hardware)
                cases += 1; frames += rows
                header = bytearray(142)
                struct.pack_into('<II', header, 110, 1000, 1001)
                struct.pack_into('<I', header, 134, rows)
                fixtures = {'meta.data': bytes(header)+b'\0'*253,
                            'algo.data': event(0, 16, 1000)+b''.join(events)+event(rows, 17, 1001),
                            'eeg.data': b''.join(files['eeg']),
                            'accelerometer.data': b''.join(files['motion'])}
                for name, data in sorted(fixtures.items()):
                    (root/name).write_bytes(data); combined.update(data)
                expected_good = sum(hardware == 3 or i % 6 not in (2, 3) for i in range((rows+4)//5))
                output = None
                for label, runner in (('host', []), ('arm', ['qemu-arm', '-cpu', 'cortex-a7'])):
                    exe = SOURCE/'build'/('session_motion.'+label)
                    p = subprocess.run(runner+[str(exe), str(root)], capture_output=True, text=True,
                                       check=True, timeout=10)
                    require(output is None or p.stdout == output, 'host/ARM summaries differ')
                    output = p.stdout
                    end = json.loads(p.stdout.splitlines()[-1])
                    require(end['eeg_rows'] == rows and end['motion_rows'] == (rows+4)//5 and
                            end['reported_good'] == expected_good and end['included_rows'] == expected_good,
                            'session report differs from original cadence/health state')
                    artifacts[label] = hashlib.sha256(exe.read_bytes()).hexdigest()
        # A restarted decimator follows its starting counter, not counter % 5.
        for counter in (27, 0xfffffff9):
            stock.run(16, 1, counter)
    return {'core_sha256': CORE_SHA256, 'normal_recording_cases_per_build': cases,
            'original_sample_prefix_iterations': frames+32, 'nonzero_counter_cases': 2,
            'mismatches': 0, 'fixture_sha256': combined.hexdigest(),
            'isolated_code_and_literal_bytes': sum(end-start for start,end in RANGES),
            'artifacts': artifacts, 'source_sha256': {name:hashlib.sha256((SOURCE/name).read_bytes()).hexdigest()
                for name in ('session_motion.c', 'algo_events.c', 'algo_events.h', 'verify_recording_cadence.py')},
            'limits': ['algorithm processing between acquisition prefix and counter increment is modeled successful',
                       'sensor reads, conversions, synchronization, Nerves callbacks and writes are modeled',
                       'no complete recorder, storage durability or physical qualification'],
            'hardware_qualified': False, 'installed': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('nano_core', type=Path)
    print(json.dumps(verify(parser.parse_args().nano_core), indent=2))
