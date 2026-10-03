#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Verify the compiled DDR character-device interface and its clock-policy calls.

No real device registration, MMIO, syscalls, or firmware installation. Stock
ioctl comparisons and rebuilt failure paths execute ARM instructions in Unicorn.
"""
import argparse
import hashlib
import itertools
import json
from pathlib import Path

from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC
from verify_busfreq import Image, Machine, FIXTURE, RAW_HASH, require

SERVICES = ('alloc_chrdev_region', 'unregister_chrdev_region', 'cdev_init', 'cdev_add',
            'cdev_del', 'kobject_put', '__class_create', 'class_destroy', 'device_create')
NUMBER, CLASS, DEVICE = 0x12300000, FIXTURE + 0xb000, FIXTURE + 0xc000


class DDRMachine(Machine):
    def __init__(self, image, state=None, *, registration_failure=None, **options):
        super().__init__(image, state, **options)
        self.registration_failure = registration_failure
        self.allocated = self.registered = self.class_created = self.node_created = False
        self.cdev_refs = 0
        self.registration_trace = []
        self.stub_addresses.update({self.symbols[name]: name for name in SERVICES})
        self.allowed_writes.update(self.symbols[name] for name in ('dreem_ddr_number', 'dreem_ddr_class'))

    def resources(self):
        return self.allocated, self.cdev_refs, self.registered, self.class_created, self.node_created

    def code(self, cpu, address, size, extra):
        name = self.stub_addresses.get(address)
        if name not in SERVICES:
            return super().code(cpu, address, size, extra)
        a, b, c, d = [cpu.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
        self.registration_trace.append(name)
        failed = name == self.registration_failure
        result = -12 if failed else 0
        if name == 'alloc_chrdev_region':
            require(a == self.symbols['dreem_ddr_number'] and b == 0 and c == 1 and
                    self.string(d) == 'dreem_ddr' and not self.allocated, 'wrong device allocation')
            if not failed:
                self.set('dreem_ddr_number', NUMBER)
                self.allocated = True
        elif name == 'unregister_chrdev_region':
            require(a == NUMBER and b == 1 and self.allocated and not self.registered, 'invalid number release')
            self.allocated = False
        elif name == 'cdev_init':
            require(a == self.symbols['dreem_ddr_cdev'] and b == self.symbols['dreem_ddr_fops'] and
                    self.cdev_refs == 0, 'wrong cdev initialization')
            fields = [self.word(b + i * 4) for i in range(28)]
            require(fields[8] == self.symbols['dreem_ddr_ioctl'] and
                    all(value == 0 for i, value in enumerate(fields) if i != 8), 'unexpected file operations')
            self.cdev_refs = 1
        elif name == 'cdev_add':
            require(a == self.symbols['dreem_ddr_cdev'] and b == NUMBER and c == 1 and
                    self.allocated and self.cdev_refs == 1, 'invalid cdev addition')
            if not failed:
                self.registered = True
        elif name == 'kobject_put':
            require(a == self.symbols['dreem_ddr_cdev'] and self.cdev_refs == 1 and
                    not self.registered, 'invalid failed-add kobject release')
            self.cdev_refs = 0
        elif name == 'cdev_del':
            require(a == self.symbols['dreem_ddr_cdev'] and self.registered and self.cdev_refs == 1,
                    'invalid cdev removal')
            self.registered = False
            self.cdev_refs = 0
        elif name == '__class_create':
            require(a == 0 and self.string(b) == 'dreem_ddr' and self.registered, 'invalid class creation')
            if not failed:
                result = CLASS
                self.class_created = True
        elif name == 'class_destroy':
            require(a == CLASS and self.class_created and not self.node_created, 'invalid class release')
            self.class_created = False
        elif name == 'device_create':
            require(a == CLASS and b == 0 and c == NUMBER and d == 0 and self.class_created and
                    self.registered and self.string(self.word(cpu.reg_read(UC_ARM_REG_SP))) == 'dreem_ddr',
                    'node published before file operations/class or with wrong identity')
            if not failed:
                result = DEVICE
                self.node_created = True
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))


def verify(stock_path, rebuilt_path):
    stock = Image(stock_path, True, ('dreem_ddr_ioctl',))
    rebuilt = Image(rebuilt_path, extra_routines=('dreem_ddr_ioctl', 'dreem_ddr_init'))
    cases = []
    for high, low, audio, ddr in itertools.product((0, 1), (0, 1), (0, 1), (0, 1, 3)):
        state = dict(high_bus_freq_mode=high, low_bus_freq_mode=low, audio_bus_freq_mode=audio, ddr_type=ddr)
        a, b = Machine(stock, state), DDRMachine(rebuilt, state)
        for argument in (0, 0xdeadbeef):
            require(a.call('dreem_ddr_ioctl', 0, 7, argument) == b.call('dreem_ddr_ioctl', 0, 7, argument) == 0,
                    'ready ioctl failed')
            require(a.trace == b.trace and a.snapshot() == b.snapshot(), 'ioctl clock/state mismatch')
        cases.append('stock ready ioctl and repeated request ' + repr((high, low, audio, ddr)))
    for command in (0, 1, 2, 3, 4, 5, 6, 8, 0xffffffff):
        a, b = Machine(stock), DDRMachine(rebuilt)
        before = b.snapshot()
        require(a.call('dreem_ddr_ioctl', 0, command, 0xdeadbeef) ==
                b.call('dreem_ddr_ioctl', 0, command, 0xdeadbeef) == 0, 'unknown ioctl failed')
        require(a.trace == b.trace == [] and a.snapshot() == b.snapshot() == before, 'unknown ioctl changed state')
        cases.append('unknown command accepted without side effects ' + str(command))
    for state, options, error in [
        ({}, dict(enabled=False), 19), ({}, dict(femto=False), 19),
        (dict(bus_freq_scaling_initialized=0), {}, 11), (dict(bus_freq_scaling_is_active=0), {}, 11),
        (dict(busfreq_suspended=1), {}, 16), (dict(high_bus_count=0x7fffffff), {}, 75),
        (dict(high_bus_count=0xffffffff), {}, 5)]:
        machine = DDRMachine(rebuilt, state, **options)
        before = machine.snapshot()
        require(machine.call('dreem_ddr_ioctl', 0, 7, 0) == (-error & 0xffffffff), 'incorrect guarded error')
        require(machine.snapshot() == before and all(x[0].startswith('mutex_') for x in machine.trace),
                'guarded failure touched clock/counter state')
        cases.append('guarded error ' + repr((state, options, error)))
    for failure in (None, 'alloc_chrdev_region', 'cdev_add', '__class_create', 'device_create'):
        machine = DDRMachine(rebuilt, registration_failure=failure)
        result = machine.call('dreem_ddr_init')
        require(result == (0 if failure is None else (-12 & 0xffffffff)), 'registration returned wrong errno')
        require(machine.resources() == ((True, 1, True, True, True) if failure is None else
                                       (False, 0, False, False, False)), 'registration leaked resources')
        require(not machine.trace, 'registration changed frequency state')
        if failure is None:
            require(machine.word(machine.symbols['__initcall_dreem_ddr_init7']) == machine.symbols['dreem_ddr_init'],
                    'interface is not wired to late initialization')
            require(machine.call('dreem_ddr_ioctl', 0, 7, 0) == 0, 'registered interface cannot request high mode')
        cases.append('registration and cleanup ' + str(failure))
    for options in (dict(enabled=False), dict(femto=False)):
        machine = DDRMachine(rebuilt, **options)
        require(machine.call('dreem_ddr_init') == 0 and not machine.registration_trace and
                machine.resources() == (False, 0, False, False, False), 'inactive interface registered')
        cases.append('registration gated ' + repr(options))
    return {'stock_raw_sha256': RAW_HASH,
            'rebuilt_kernel_sha256': hashlib.sha256(rebuilt.binary).hexdigest(), 'passed_cases': len(cases),
            'cases': cases, 'runtime_qualified': False,
            'limits': 'Synthetic registration and clock services; real devtmpfs, userspace syscalls, concurrency and hardware timing unverified'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stock_kernel', type=Path)
    parser.add_argument('rebuilt_kernel', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.stock_kernel, args.rebuilt_kernel), indent=2))


if __name__ == '__main__':
    main()
