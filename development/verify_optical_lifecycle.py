#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Verify original optical cleanup, including its absent hardware readback.

Only selected instructions from the pinned private executable are emulated.
All bus operations, close and mutex destruction are synthetic.
"""
import argparse
import hashlib
import itertools
import json
from pathlib import Path

from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_PC, UC_ARM_REG_LR
from verify_optical_transport import Stock, REGISTERS, STACK, require, CORE_SHA256


class Cleanup(Stock):
    def __init__(self, path):
        super().__init__(path, additional_ranges=((0x905d0, 0x90758),),
                         additional_services={0x16cd4:'close', 0x8616c:'destroy'})

    def code(self, cpu, address, size, user):
        name = self.services.get(address)
        a, b, c, d = [cpu.reg_read(reg) for reg in REGISTERS]
        if name == 'read':
            require(a == 42 and c == 1 and b == STACK - 17, 'unexpected cleanup mode read')
            result = self.received
            if result == 1:
                cpu.mem_write(b, bytes([self.mode]))
            self.trace.append(['read_mode', self.mode, result])
        elif name == 'write':
            require(a == 42 and c in (1, 2), 'unexpected cleanup bus write')
            data = bytes(cpu.mem_read(b, c))
            require(data[0] == 9, 'cleanup accessed a different register')
            result = 1 if c == 1 else self.written
            self.trace.append(['write', data.hex(), result])
        elif name == 'close':
            require(a == 42, 'cleanup closed another descriptor')
            self.closes += 1
            result = self.closed
        elif name == 'destroy':
            require(a == 0xecae74, 'cleanup destroyed another mutex')
            self.destroys += 1
            result = self.destroyed
        else:
            return super().code(cpu, address, size, user)
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def run(self, mode, received, written, closed, destroyed):
        self.reset()
        self.mode, self.received, self.written = mode, received, written
        self.closed, self.destroyed = closed, destroyed
        self.closes = self.destroys = 0
        result = self.call(0x905d0)
        expected = 0 if received == 1 and written == 2 else 1
        require(result == expected, 'unexpected cleanup return')
        require(self.closes == 1, 'cleanup failed to attempt descriptor close')
        require(self.destroys == int(expected == 0), 'unexpected mutex cleanup')
        require(bytes(self.cpu.mem_read(0xda2e08, 4)) == b'\xff'*4,
                'cleanup did not discard descriptor')
        reads = [t for t in self.trace if t[0] == 'read_mode']
        require(len(reads) == 1, 'cleanup now checks mode after command')
        writes = [bytes.fromhex(t[1]) for t in self.trace if t[0] == 'write']
        require(writes == [b'\x09'] + ([bytes([9, mode | 0xc0])] if received == 1 else []),
                'cleanup command differs')
        return {'mode':mode, 'received':received, 'written':written,
                'close_result':closed, 'destroy_result':destroyed,
                'return':result, 'mode_reads':len(reads), 'closes':self.closes,
                'destroys':self.destroys, 'trace':self.trace}


def verify(path):
    original = Cleanup(path)
    cases = [original.run(*args) for args in itertools.product(
        (0, 3, 0x83, 0xff), (-1, 0, 1), (-1, 0, 2), (0, -1), (0, -1))]
    original.reset()
    original.word(0xda2e08, -1)
    original.closes = original.destroys = 0
    require(original.call(0x905d0) == 0 and not original.trace and
            original.closes == original.destroys == 0, 'already-closed cleanup differs')
    return {'core_sha256':CORE_SHA256, 'cleanup_cases':len(cases)+1,
            'additional_code_and_literal_bytes':0x90758-0x905d0,
            'fixture_sha256':hashlib.sha256(json.dumps(cases,sort_keys=True).encode()).hexdigest(),
            'mode_reads_after_command':0,
            'close_failure_does_not_change_return':True,
            'boundaries':['isolated chip cleanup only', 'synthetic bus and close results',
                          'no full manager cancellation/join or physical reset timing']}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('nano_core',type=Path)
    args=parser.parse_args()
    print(json.dumps(verify(args.nano_core),indent=2))
