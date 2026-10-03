#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Identify saved-firmware hardware routes through bounded original ARM execution.

All file, GPIO, process, sleep and ioctl services are synthetic. This does not
launch nano_core, run its shell commands, open devices or assert live ownership.
Only the exact reviewed private executable is accepted by the shared loader.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import struct

from elftools.elf.elffile import ELFFile
from unicorn.arm_const import (UC_ARM_REG_C1_C0_2, UC_ARM_REG_FPEXC,
                               UC_ARM_REG_R0, UC_ARM_REG_PC, UC_ARM_REG_LR,
                               UC_ARM_REG_SP)
from verify_optical_transport import Stock, CORE_SHA256, REGISTERS, require

RANGES = ((0x1ad24, 0x1b774), (0x864f4, 0x86b6c), (0x8d4e8, 0x8d500))
SERVICES = {0x15c78: 'strlen', 0x16074: 'sleep', 0x1638c: 'memset',
            0x163ec: 'unlink', 0x16ae8: 'open', 0x16bb4: 'sprintf',
            0x16cd4: 'close', 0x16e78: 'strerror', 0x16f50: 'system',
            0x4ffb4: 'event', 0x8ccf0: 'address', 0x916d4: 'gpio_export',
            0x917a0: 'gpio_unexport', 0x91bb8: 'gpio_direction',
            0x92574: 'gpio_write'}
STATE, VERSION = 0xda3350, 0xda2ad0
SYNTHETIC_ADDRESS = '02:00:00:00:00:01'
COMMANDS = {'mic_enable': (0x1ae2c, 10), 'mic_disable': (0x1af70, 11),
            'acquisition_start': (0x1b0b4, 1), 'test_signal': (0x1b1f8, 5),
            'query_state': (0x1b494, 9)}


class Routes(Stock):
    def __init__(self, path):
        super().__init__(path, additional_ranges=RANGES, additional_services=SERVICES)
        self.cpu.mem_map(0xda3000, 0x1000, 3)
        elf = ELFFile(io.BytesIO(path.read_bytes()))
        for start, end in ((0xe5000, 0xed000), (0x103000, 0x104000)):
            self.cpu.mem_map(start, end-start, 1)
            for segment in elf.iter_segments():
                base = segment['p_vaddr']
                if segment['p_type'] == 'PT_LOAD' and base <= start < end <= base+segment['p_filesz']:
                    self.cpu.mem_write(start, segment.data()[start-base:end-base])
                    break
            else:
                raise ValueError('unbacked constant range')
        self.cpu.reg_write(UC_ARM_REG_C1_C0_2, 0xf00000)
        self.cpu.reg_write(UC_ARM_REG_FPEXC, 1 << 30)

    def get(self, address):
        return struct.unpack('<I', self.cpu.mem_read(address, 4))[0]

    def string(self, address):
        result = bytearray()
        for offset in range(4096):
            byte = self.cpu.mem_read(address+offset, 1)[0]
            if not byte:
                return result.decode('ascii')
            result.append(byte)
        raise ValueError('unterminated modeled string')

    def prepare(self, version, *, opened=42, ioctl=3, already_attached=False):
        self.reset()
        self.version = version
        self.opened, self.ioctl_result = opened, ioctl
        self.already_attached = already_attached
        self.events = []
        self.word(VERSION, version)
        self.word(STATE, 0x77)

    def code(self, cpu, address, size, user):
        name = self.services.get(address)
        a, b, c, d = [cpu.reg_read(r) for r in REGISTERS]
        result = 0
        if name == 'open':
            path = self.string(a)
            require(path in ('/dev/m4_control', '/tmp/bdaddr'), 'unexpected modeled file')
            require(b == (2 if path == '/dev/m4_control' else 0x242), 'unexpected open flags')
            result = self.opened
            self.events.append([name, path, b, result])
        elif name == 'close':
            require(a == 42, 'unexpected modeled descriptor')
            self.events.append([name, a])
        elif name == 'ioctl':
            require(a == 42 and b in (0, 1, 3, 5, 9, 10, 11) and self.get(c) == 0,
                    'unexpected modeled control request')
            result = self.ioctl_result
            self.events.append([name, b, result])
        elif name == 'sleep':
            require(a in (1, 5), 'unexpected modeled delay')
            self.events.append([name, a])
        elif name == 'system':
            command = self.string(a)
            allowed = ('ps aux | grep hciattach | grep -v grep',
                       'killall -9 rtk_hciattach 2>/dev/null >/dev/null',
                       'killall -9 hciattach 2>/dev/null >/dev/null',
                       '/usr/bin/hciconfig hci0',
                       '/usr/bin/rtk_hciattach -n -s 115200 /dev/ttyLP2 rtk_h5 &',
                       '/usr/bin/hciattach /dev/ttymxc1 bcm43xx 3000000 flow -t 10 bdaddr '
                       + SYNTHETIC_ADDRESS)
            require(command in allowed, 'unexpected modeled shell command')
            result = int(not self.already_attached) if command == allowed[0] else 0
            self.events.append([name, command, result])
        elif name == 'sprintf':
            require(self.string(b) == '%s %s %s %d flow -t %d bdaddr %s',
                    'unexpected command format')
            stack = cpu.reg_read(UC_ARM_REG_SP)
            values = (self.string(c), self.string(d), self.string(self.get(stack)),
                      self.get(stack+4), self.get(stack+8), self.string(self.get(stack+12)))
            command = self.string(b) % values
            require(len(command) < 4096, 'modeled command too long')
            cpu.mem_write(a, command.encode()+b'\0')
            result = len(command)
        elif name == 'address':
            cpu.mem_write(a, SYNTHETIC_ADDRESS.encode()+b'\0')
        elif name == 'memset':
            require(c == 4096 and b == 0, 'unexpected modeled clearing')
            cpu.mem_write(a, bytes(c))
            result = a
        elif name == 'strlen':
            result = len(self.string(a))
        elif name == 'write':
            require(a == 42 and c == len(SYNTHETIC_ADDRESS) and
                    bytes(cpu.mem_read(b, c)) == SYNTHETIC_ADDRESS.encode(),
                    'unexpected modeled address write')
            result = c
            self.events.append([name, 'synthetic_address', c])
        elif name == 'unlink':
            require(self.string(a) == '/tmp/bdaddr', 'unexpected modeled unlink')
            self.events.append([name, '/tmp/bdaddr'])
        elif name in ('gpio_export', 'gpio_unexport', 'gpio_direction', 'gpio_write'):
            require(a == (71 if self.version == 3 else 38), 'unexpected Bluetooth GPIO')
            if name == 'gpio_direction':
                require(self.string(b) == 'out', 'unexpected GPIO direction')
            elif name == 'gpio_write':
                require(b in (0, 1), 'unexpected GPIO value')
            self.events.append([name, a, b if name == 'gpio_write' else None])
        elif name == 'event':
            require((a, b) == (1, 0), 'unexpected HCI event')
            self.events.append([name, a, b])
        elif name == 'strerror':
            result = 0
        else:
            return super().code(cpu, address, size, user)
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def result(self, entry):
        result = self.call(entry)
        return {'return': result, 'events': self.events, 'state': self.get(STATE)}


def verify(path):
    stock = Routes(path)
    gated, controls, bluetooth = [], [], []
    for version in (0, 1, 2, 3, 4):
        for operation, entry, requests in (('wake', 0x1b33c, [3]),
                                           ('stop', 0x1b5b8, [9, 0])):
            stock.prepare(version)
            row = stock.result(entry)
            require(row['return'] == 0, 'gated operation return changed')
            if version == 3:
                require([e[1] for e in row['events'] if e[0] == 'ioctl'] == requests and
                        row['state'] == 3, 'version 3 control path differs')
            else:
                require(row['events'] == [] and row['state'] == 0x77,
                        'non-version-3 path performed control I/O')
            gated.append({'version': version, 'operation': operation, **row})
    # These helpers have no internal hardware gate. They must not be used as
    # probes simply because wake/stop report success on another hardware version.
    for name, (entry, command) in COMMANDS.items():
        for opened, result in ((42, 0), (42, 7), (42, -1), (-1, 7)):
            stock.prepare(2, opened=opened, ioctl=result)
            row = stock.result(entry)
            require(row['return'] == int(opened < 0 or result < 0), 'control return differs')
            require(row['state'] == (result if opened >= 0 and result >= 0 else 0x77),
                    'control state is not ioctl return value')
            require([e[1] for e in row['events'] if e[0] == 'ioctl'] ==
                    ([command] if opened >= 0 else []), 'control command differs')
            require(len([e for e in row['events'] if e[0] == 'close']) == int(opened >= 0),
                    'control descriptor cleanup differs')
            controls.append({'operation': name, 'version': 2, **row})
    stock.prepare(3, ioctl=2)
    slow_stop = stock.result(0x1b5b8)
    require(slow_stop['return'] == 0 and
            [e[1] for e in slow_stop['events'] if e[0] == 'ioctl'] == [9]*11+[0] and
            [e[1] for e in slow_stop['events'] if e[0] == 'sleep'] == [1]*10,
            'stop polling no longer falls through after ten modeled seconds')
    for version in (0, 1, 2, 3):
        stock.prepare(version)
        row = stock.result(0x86b4c)
        commands = [e[1] for e in row['events'] if e[0] == 'system']
        expected = '/dev/ttyLP2 rtk_h5' if version == 3 else '/dev/ttymxc1 bcm43xx 3000000'
        require(row['return'] == 0 and len(commands) == 5 and
                expected in commands[3], 'Bluetooth transport selection differs')
        bluetooth.append({'version': version, **row})
    for version in (2, 3):
        stock.prepare(version, already_attached=True)
        row = stock.result(0x86b4c)
        require(row['return'] == 0 and row['events'] ==
                [['system', 'ps aux | grep hciattach | grep -v grep', 0]],
                'existing attach process no longer skips hardware initialization')
        bluetooth.append({'version': version, 'already_attached': True, **row})
    payload = {'gated': gated, 'controls': controls, 'slow_stop': slow_stop,
               'bluetooth': bluetooth}
    return {'core_sha256': CORE_SHA256, 'cases': len(gated)+len(controls)+1+len(bluetooth),
            'fixture_sha256': hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
            'results': payload, 'hardware_services': 'synthetic',
            'shell_commands_executed': False, 'physical_qualification': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('core', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.core), indent=2))


if __name__ == '__main__':
    main()
