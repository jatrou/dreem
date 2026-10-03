#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check independent event framing against isolated stock ARM writer/replay code.

All files, recorder state, and callbacks are synthetic. No vendor process or
hardware runs; only the hash-pinned routines execute in a bounded emulator.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import struct
import subprocess

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE
from unicorn.arm_const import (UC_CPU_ARM_CORTEX_A7, UC_ARM_REG_SP,
                               UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                               UC_ARM_REG_R3, UC_ARM_REG_LR, UC_ARM_REG_PC)

CORE_SHA256 = 'dfc83b247b505aa08ea62060f999192112a47b606754d5f469357b7addf0295a'
WRITER, WRITER_END, READER, READER_END = 0x22bcc, 0x22d54, 0x281b4, 0x28910
RETURN, SCRATCH = 0x10000, 0x10000000
PAYLOAD, ERRNO, FILE, STATE, STACK = SCRATCH, SCRATCH+0x100, SCRATCH+0x200, SCRATCH+0x1000, SCRATCH+0xf000
PULSE_OFFSET, MOTION_OFFSET = 32, 64
SERVICES = {0x16530: 'write', 0x161a0: 'read', 0x16494: 'errno',
            0x16008: 'log', 0x28038: 'event', 0xa3c84: 'stim'}
SIZES = {1: 16, **dict.fromkeys((2, 3, 15, 23, 28, 32, 33), 0),
         **dict.fromkeys((18, 19, 24, 34), 1),
         **dict.fromkeys((13, 14, 16, 17, 20, 21, 22, 25, 26, 30, 31, 35, 36), 4),
         27: 8, 29: 8, 37: 12}
REGS = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)
SOURCE = Path(__file__).resolve().parent


def require(condition, message):
    if not condition:
        raise ValueError(message)


class Stock:
    def __init__(self, path):
        raw = path.read_bytes()
        require(len(raw) == 14_170_096 and hashlib.sha256(raw).hexdigest() == CORE_SHA256,
                'unreviewed firmware executable')
        elf = ELFFile(io.BytesIO(raw))
        self.cpu = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_A7)
        for base, size, perms in ((0x10000, 0x1000, 5), (0x16000, 0x1000, 5),
                                  (0x22000, 0x1000, 5), (0x28000, 0x1000, 5),
                                  (0xa3000, 0x1000, 5), (0xda2000, 0x2000, 3),
                                  (SCRATCH, 0x10000, 3)):
            self.cpu.mem_map(base, size, perms)
        for start, end in ((WRITER, WRITER_END), (READER, READER_END)):
            for segment in elf.iter_segments():
                base = segment['p_vaddr']
                if segment['p_type'] == 'PT_LOAD' and base <= start < end <= base + segment['p_filesz']:
                    self.cpu.mem_write(start, segment.data()[start-base:end-base])
                    break
            else:
                raise ValueError('routine is not file backed')
        self.cpu.hook_add(UC_HOOK_CODE, self.code)
        self.reset()

    def reset(self):
        self.data, self.position, self.writes, self.requests = b'', 0, [], []
        self.callbacks, self.logs, self.short_write = [], 0, None
        self.cpu.mem_write(ERRNO, b'\0'*4)

    def code(self, cpu, address, size, _):
        service = SERVICES.get(address)
        if service is None:
            require(WRITER <= address < WRITER_END or READER <= address < READER_END,
                    'execution left isolated routines')
            return
        a, b, c, d = [cpu.reg_read(r) for r in REGS]
        if service in ('read', 'write'):
            require(b == 1 and d == FILE and c in (0, 1, 4, 8, 12, 16), 'unexpected stream operation')
            if service == 'write':
                payload = bytes(cpu.mem_read(a, c)) if c else b''
                result = max(0, c-1) if self.short_write == len(self.writes) else c
                self.writes.append(payload[:result])
            else:
                payload = self.data[self.position:self.position+c]
                self.position += len(payload)
                result = len(payload)
                self.requests.append(c)
                if payload:
                    require(SCRATCH <= a < SCRATCH+0x10000 or a == 0xda27d0, 'unexpected read destination')
                    cpu.mem_write(a, payload)
        elif service == 'errno':
            result = ERRNO
        elif service == 'log':
            self.logs += 1
            result = 0
        elif service == 'event':
            require(a == STATE and b in (23, 35), 'unexpected replay event callback')
            if b == 35:
                require(d == 4, 'unexpected stimulation payload size')
            self.callbacks.append(('event', b))
            result = 0
        else:
            require((a, b) == (0x12345678, 1), 'unexpected stimulation callback')
            self.callbacks.append(('stim', 1))
            result = 0
        cpu.reg_write(UC_ARM_REG_R0, result)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def call(self, entry, *args):
        self.cpu.reg_write(UC_ARM_REG_SP, STACK)
        for reg, value in zip(REGS, args):
            self.cpu.reg_write(reg, value)
        if len(args) == 5:
            self.cpu.mem_write(STACK, struct.pack('<I', args[4]))
        self.cpu.reg_write(UC_ARM_REG_LR, RETURN)
        self.cpu.emu_start(entry, RETURN, timeout=100000, count=1000)
        require(self.cpu.reg_read(UC_ARM_REG_PC) == RETURN, 'execution exceeded bound')
        return self.cpu.reg_read(UC_ARM_REG_R0)

    def write(self, sample, code, payload, short=None):
        self.reset()
        self.short_write = short
        self.cpu.mem_write(PAYLOAD, payload+b'\xa5'*16)
        status = self.call(WRITER, FILE, sample, code, PAYLOAD, len(payload))
        require(bytes(self.cpu.mem_read(PAYLOAD, len(payload)+16)) == payload+b'\xa5'*16,
                'writer modified input')
        return status, b''.join(self.writes)

    def read(self, code, payload, cut=None):
        self.reset()
        counter = 123456
        self.data = bytes([code])+payload+struct.pack('<I', counter+1)
        if cut is not None:
            self.data = self.data[:cut]
        initial = bytearray(b'\xa5'*0x6000)
        initial[0x547e:0x5482] = struct.pack('<I', counter)
        self.cpu.mem_write(STATE, bytes(initial))
        self.cpu.mem_write(0xda3370, struct.pack('<I', FILE))
        self.cpu.mem_write(0xda27d0, struct.pack('<II', counter, len(self.data)+4))
        status = self.call(READER, 0x12345678, STATE, PULSE_OFFSET, MOTION_OFFSET)
        expected = initial.copy()
        if code in (30, 31) and (cut is None or cut >= 5):
            offset = MOTION_OFFSET+0x388a if code == 30 else PULSE_OFFSET+0x1f6a
            expected[offset] = int(struct.unpack('<I', payload)[0] == 0)
        require(bytes(self.cpu.mem_read(STATE, len(initial))) == bytes(expected), 'unexpected recorder mutation')
        require(self.position == len(self.data), 'reader did not consume supplied fixture')
        return status


def verify(path):
    stock = Stock(path)
    frames, expected = [], []
    reader_cases = short_read_cases = 0
    for code, size in sorted(SIZES.items()):
        payload = bytes((code+i*17) % 256 for i in range(size))
        require(stock.read(code, payload) == 0, f'replay failed for code {code}')
        require(stock.requests == ([1, size, 4] if size else [1, 4]), 'replay payload size differs')
        require(len(stock.callbacks) == (2 if code in (23, 35) else 0), 'replay callbacks differ')
        reader_cases += 1
        for cut in range(1+size+4):
            require(stock.read(code, payload, cut) == 1, f'short replay read accepted for {code}/{cut}')
            short_read_cases += 1
        for counter in (0, 0x7fffffff, 0x80000000, 0xffffffff):
            status, frame = stock.write(counter, code, payload)
            require(status == 0 and frame == struct.pack('<IB', counter, code)+payload, 'writer framing differs')
            frames.append(frame)
            expected.append((counter, code, payload.hex()))
    for code in (30, 31):
        for value in (0, 1, 2, 0xffffffff):
            require(stock.read(code, struct.pack('<I', value)) == 0, 'health replay differs')
            reader_cases += 1
    for short in range(3):
        status, frame = stock.write(17, 30, struct.pack('<I', 1), short)
        require(status == 1 and len(stock.writes) == short+1 and stock.logs == 1, 'short write accepted')
    # Exercise the real shipped parser/CLI against bytes emitted by stock ARM.
    import tempfile
    artifacts = {}
    with tempfile.TemporaryDirectory(prefix='dreem-algo-') as tmp:
        file = Path(tmp)/'algo.data'
        file.write_bytes(b''.join(frames))
        for label, runner in (('host', []), ('arm', ['qemu-arm', '-cpu', 'cortex-a7'])):
            exe = SOURCE/'build'/('algo_health.'+label)
            result = subprocess.run(runner+[str(exe), str(file)], capture_output=True, check=True,
                                    text=True, timeout=15)
            rows = [json.loads(line) for line in result.stdout.splitlines()]
            require([(e['sample_counter'], e['code'], e['payload_hex']) for e in rows[:-1]] == expected,
                    label+' parser differs from original writer/replay framing')
            require(rows[-1]['bytes_remaining'] == 0 and rows[-1]['events_consumed'] == len(frames),
                    'unexpected remaining input')
            artifacts[label] = hashlib.sha256(exe.read_bytes()).hexdigest()
    return {'core_sha256': CORE_SHA256, 'known_codes': len(SIZES), 'stock_reader_cases': reader_cases,
            'stock_short_read_cases': short_read_cases, 'stock_writer_frames_per_build': len(frames),
            'stock_short_write_cases': 3, 'mismatches': 0,
            'isolated_code_and_literal_bytes': WRITER_END-WRITER+READER_END-READER,
            'fixture_sha256': hashlib.sha256(b''.join(frames)).hexdigest(), 'artifacts': artifacts,
            'source_sha256': {n: hashlib.sha256((SOURCE/n).read_bytes()).hexdigest()
                              for n in ('algo_events.h', 'algo_events.c', 'algo_health.c', 'verify_algo_events.py')},
            'hardware_qualified': False, 'installed': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('nano_core', type=Path)
    print(json.dumps(verify(parser.parse_args().nano_core), indent=2))
