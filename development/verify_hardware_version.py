#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Verify compiled board-identity reads and provider lifetime with modeled I/O.

Only the reviewed shadow word and controller status are readable MMIO. Every
OTP write is rejected. Original provider probe/remove are modeled services;
their internal sysfs/devres behavior and physical fuse contents are not tested.
"""
import argparse
import json
from pathlib import Path
import struct

from unicorn import UC_HOOK_MEM_READ
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_PC, UC_ARM_REG_LR
from verify_adc_module import Module
from verify_busfreq import Image, Machine, FIXTURE, STACK, STOP, RAW_HASH, require
from verify_ddr_preparation import resolve_probe_globals
from verify_wm8960_sources import names_in, sha
from verify_ddr_sources import bytes_at

OTP = 0x50000000
PDEV, OTHER, RESOURCE, TYPE, CLOCK, OUTPUT = (FIXTURE + n for n in
                                          (0xc000, 0xc100, 0xd000, 0xe000, 0xe100, 0xf000))
STATE = ('dreem_otp_state', 'dreem_otp_owner', 'dreem_otp_size', 'otp_base', 'otp_clk',
         'fsl_otp', 'otp_kobj', 'otp_kattr', 'otp_attr_group')
SERVICES = ('mutex_lock', 'mutex_unlock', 'of_machine_is_compatible', 'clk_prepare',
            'clk_enable', 'clk_disable', 'clk_unprepare', 'platform_get_resource',
            'fsl_otp_probe_original', 'fsl_otp_remove_original', 'printk')


def load(path, obj):
    image = Image(path, extra_routines=('dreem_get_hardware_version', 'fsl_otp_probe',
                  'fsl_otp_remove', 'fsl_otp_probe_original', 'fsl_otp_remove_original'))
    image.named_addresses = names_in(image)
    resolve_probe_globals(image, obj)
    return image


class OtpMachine(Machine):
    def __init__(self, image, types, *, legacy=False, value=1, femto=True, failure=None,
                 status=(0, 0), extent=(0, 0x3fff), missing_resource=False):
        super().__init__(image, femto=femto)
        self.legacy, self.types = legacy, types
        self.inject, self.status = failure, status
        self.missing_resource = missing_resource
        self.prepared = self.enabled = 0
        self.reads, self.events = [], []
        self.original_probes = self.original_removals = 0
        self.cpu.mem_map(OTP, 0x4000)
        self.put(OTP + 0x6c0, value)
        self.put(OUTPUT, 0xa55aa55a)
        self.put(TYPE + types.members['fsl_otp_devtype_data']['devtype'], 6)
        for name, v in zip(('start', 'end'), extent):
            self.put(RESOURCE + types.members['resource'][name], v)
        self.stub_addresses = {image.symbols[n]: n for n in SERVICES if n in image.symbols}
        if legacy:
            self.set('otp_base', OTP)
            self.set('value.16256', 0xffffffff)
        else:
            for name in STATE:
                self.set(name, 0)
        self.cpu.hook_add(UC_HOOK_MEM_READ, self.read)

    def put(self, address, value):
        self.cpu.mem_write(address, struct.pack('<I', value & 0xffffffff))

    def write(self, cpu, access, address, size, value, extra):
        allowed = [self.symbols['value.16256']] if self.legacy else [self.symbols[n] for n in STATE] + [OUTPUT]
        require((STACK <= address and address + size <= STOP) or
                (size == 4 and address in allowed), f'unexpected write, including any OTP write: {address:#x}')

    def read(self, cpu, access, address, size, value, extra):
        if not OTP <= address < OTP + 0x4000:
            return
        require(size == 4 and address in (OTP, OTP + 0x6c0), 'read outside status/version shadow')
        require(self.legacy or (self.locked and self.prepared == self.enabled == 1 and
                               self.word(self.symbols['dreem_otp_state']) == 2), 'unprotected OTP read')
        if address == OTP:
            count = self.reads.count(0)
            require(count < 2, 'unexpected repeated status read')
            self.put(OTP, self.status[count])
        self.reads.append(address - OTP)

    def code(self, cpu, address, size, extra):
        name = self.stub_addresses.get(address)
        if name is None:
            require(any(a <= address < b for a, b in self.image.ranges),
                    f'identity execution left allowlist: {address:#x}')
            return
        a, b, c, d = [cpu.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
        result = 0
        if name == 'printk':
            pass
        elif name in ('mutex_lock', 'mutex_unlock'):
            require(a == self.symbols['otp_mutex'] and self.locked == (name == 'mutex_unlock'),
                    'wrong/unbalanced provider lock')
            self.locked = name == 'mutex_lock'
            self.events.append(name)
        elif name == 'of_machine_is_compatible':
            require(self.string(a) == 'fsl,imx6ull-femto', 'wrong board gate')
            result = int(self.femto)
        elif name.startswith('clk_'):
            require(a == CLOCK and self.locked, 'clock operation without provider lock')
            self.events.append(name)
            if name == 'clk_prepare':
                require(not self.prepared and not self.enabled, 'double clock prepare')
                result = -5 if self.inject == name else 0
                self.prepared = int(result == 0)
            elif name == 'clk_enable':
                require(self.prepared == 1 and not self.enabled, 'unprepared clock enable')
                result = -11 if self.inject == name else 0
                self.enabled = int(result == 0)
            elif name == 'clk_disable':
                require(self.prepared == self.enabled == 1, 'unbalanced clock disable')
                self.enabled = 0
            elif name == 'clk_unprepare':
                require(self.prepared == 1 and not self.enabled, 'unbalanced clock unprepare')
                self.prepared = 0
        elif name == 'platform_get_resource':
            require(not self.locked and a in (PDEV, OTHER) and b == 0x200 and c == 0,
                    'unexpected resource lookup')
            result = 0 if self.missing_resource else RESOURCE
        elif name == 'fsl_otp_probe_original':
            require(not self.locked and self.word(self.symbols['dreem_otp_state']) == 1 and
                    self.word(self.symbols['dreem_otp_owner']) == a, 'provider published before original probe')
            self.original_probes += 1
            for name, v in [('otp_base', OTP), ('otp_clk', CLOCK), ('fsl_otp', TYPE),
                            ('otp_kobj', FIXTURE + 0x1000), ('otp_kattr', FIXTURE + 0x2000),
                            ('otp_attr_group', FIXTURE + 0x3000)]:
                self.set(name, v)
            result = self.inject if isinstance(self.inject, int) else 0
        elif name == 'fsl_otp_remove_original':
            require(not self.locked and self.word(self.symbols['dreem_otp_state']) == 3 and
                    self.word(self.symbols['otp_base']) == OTP, 'remove did not withdraw access before draining users')
            self.original_removals += 1
        else:
            raise ValueError('unmodeled identity service: ' + name)
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def probe(self, owner=PDEV):
        require(self.call('fsl_otp_probe', owner) == 0, 'modeled provider probe failed')
        require(self.word(self.symbols['dreem_otp_state']) == 2, 'provider not ready after success')

    def get(self, error=0, expected=1, pointer=OUTPUT):
        before = self.word(OUTPUT)
        self.reads.clear()
        result = self.call('dreem_get_hardware_version', pointer)
        require(result == error & 0xffffffff, f'identity result differs: {result:#x}, wanted {error}')
        require(self.word(OUTPUT) == (expected if error == 0 else before), 'failure changed output or wrong identity')
        require(not self.locked and not self.prepared and not self.enabled, 'read retained lock/clock')


def verify(stock, rebuilt, types):
    cases = []
    for value in (0, 1, 2, 0x7fffffff, 0x80000000, 0xfffffffe):
        old = OtpMachine(stock, types, legacy=True, value=value)
        new = OtpMachine(rebuilt, types, value=value)
        new.probe()
        require(old.call('get_dreem_hardware_version') == value and old.reads == [0x6c0], 'stock shadow decode differs')
        new.get(expected=value)
        require(new.reads == [0, 0x6c0, 0], 'checked read did not bracket the same shadow word')
        cases.append('stock value comparison ' + str(value))
    for state in (0, 1, 3):
        m = OtpMachine(rebuilt, types)
        m.set('dreem_otp_state', state)
        m.get(error=-517)
        require(not m.reads, 'unready provider read OTP')
        cases.append('unready provider state ' + str(state))
    for femto, pointer, error in [(False, OUTPUT, -19), (True, 0, -22)]:
        m = OtpMachine(rebuilt, types, femto=femto)
        m.probe()
        m.get(error=error, pointer=pointer)
        require(not m.reads, 'argument/board rejection read OTP')
        cases.append('board/argument gate ' + repr((femto, pointer)))
    for field, value, error in [('otp_base', 0, -5), ('otp_base', 0xfffffff4, -5),
                               ('otp_clk', 0, -5), ('otp_clk', 0xfffffff4, -5), ('fsl_otp', 0, -19)]:
        m = OtpMachine(rebuilt, types)
        m.probe()
        m.set(field, value)
        m.get(error=error)
        require(not m.reads, 'invalid provider pointer reached OTP')
        cases.append('invalid pointer ' + field + ' ' + str(value))
    for devtype in (0, 5, 7):
        m = OtpMachine(rebuilt, types)
        m.probe()
        m.put(TYPE + types.members['fsl_otp_devtype_data']['devtype'], devtype)
        m.get(error=-19)
        require(not m.reads, 'wrong SoC reached OTP')
        cases.append('unsupported OTP type ' + str(devtype))
    for extent, missing, error in [((0, 0x6c2), False, -34), ((0, 0x6c3), False, 0),
                                    ((1, 0), False, -34), ((0, 0xffffffff), False, -34),
                                    ((0, 0x3fff), True, -34)]:
        m = OtpMachine(rebuilt, types, extent=extent, missing_resource=missing)
        m.probe()
        m.get(error=error)
        require(not error or not m.reads, 'out-of-range shadow read')
        cases.append('resource bounds ' + repr((extent, missing)))
    for failure, error in [('clk_prepare', -5), ('clk_enable', -11)]:
        m = OtpMachine(rebuilt, types, failure=failure)
        m.probe()
        m.get(error=error)
        require(not m.reads, 'failed clock read OTP')
        cases.append('clock failure ' + failure)
    for position in (0, 1):
        for status in (0x100, 0x200, 0x400, 0x700):
            values = [0, 0]
            values[position] = status
            m = OtpMachine(rebuilt, types, status=values)
            m.probe()
            m.get(error=-5 if status & 0x200 else -16)
            require(m.reads == ([0] if position == 0 else [0, 0x6c0, 0]), 'incorrect status read ordering')
            cases.append('controller status ' + repr((position, status)))
    m = OtpMachine(rebuilt, types, status=(8, 8))
    m.probe()
    m.get()
    cases.append('unrelated status bit accepted')
    m = OtpMachine(rebuilt, types, value=0xffffffff)
    m.probe()
    m.get(error=-61)
    cases.append('all-ones identity rejected without output change')
    for error in (-12, -517):
        m = OtpMachine(rebuilt, types, failure=error)
        require(m.call('fsl_otp_probe', PDEV) == error & 0xffffffff and
                all(m.word(m.symbols[n]) == 0 for n in STATE), 'failed probe left stale provider state')
        m.get(error=-517)
        require(not m.reads, 'failed probe permitted MMIO')
        m.inject = None
        m.probe()
        m.get()
        cases.append('probe failure cleared and retried ' + str(error))
    m = OtpMachine(rebuilt, types)
    m.probe()
    state = [m.word(m.symbols[n]) for n in STATE]
    require(m.call('fsl_otp_probe', OTHER) == 0xfffffff0 and m.original_probes == 1 and
            [m.word(m.symbols[n]) for n in STATE] == state, 'duplicate probe altered live provider')
    require(m.call('fsl_otp_remove', OTHER) == 0xffffffed and not m.original_removals,
            'wrong owner removed provider')
    m.get()
    require(m.call('fsl_otp_remove', PDEV) == 0 and m.original_removals == 1 and
            all(m.word(m.symbols[n]) == 0 for n in STATE), 'remove retained provider state')
    m.cpu.mem_unmap(OTP, 0x4000)
    m.get(error=-517)
    require(not m.reads, 'removed provider reached unmapped MMIO')
    require(m.call('fsl_otp_remove', PDEV) == 0xffffffed, 'repeated removal accepted')
    m.cpu.mem_map(OTP, 0x4000)
    m.put(OTP + 0x6c0, 2)
    m.probe(OTHER)
    m.get(expected=2)
    cases.append('duplicate/foreign-owner rejection, withdraw, unmap, rebind')
    old = OtpMachine(stock, types, legacy=True, value=1)
    require(old.call('get_dreem_hardware_version') == 1, 'legacy fixture failed')
    old.cpu.mem_unmap(OTP, 0x4000)
    require(old.call('get_dreem_hardware_version') == 1, 'legacy cached result not reproduced')
    new = OtpMachine(rebuilt, types, value=1)
    new.probe()
    new.get()
    new.put(OTP + 0x6c0, 2)
    new.get(expected=2)
    cases.append('legacy caching contrasted with fresh checked reads')
    driver = rebuilt.symbols['fsl_otp_driver']
    for member in ('probe', 'remove'):
        address = driver + types.members['platform_driver'][member]
        require(new.word(address) == rebuilt.symbols['fsl_otp_' + member], 'platform callback bypasses lifetime wrapper')
    cases.append('platform registration points to lifetime wrappers')
    function = next(s for s in types.elf.get_section_by_name('.symtab').iter_symbols()
                    if s.name == 'dreem_get_hardware_version')
    address = rebuilt.symbols['dreem_get_hardware_version']
    code = bytes_at(rebuilt, address, function['st_size'])
    loads = [(i, struct.unpack_from('<I', code, i)[0]) for i in range(0, len(code), 4)
             if struct.unpack_from('<I', code, i)[0] & 0x0ff00fff == 0x059006c0]
    require(len(loads) == 1, 'negative control requires the single immediate shadow load')
    offset, word = loads[0]
    for instruction, label, expected_error in [
        (word & ~(1 << 20), 'injected OTP store rejected', 'unexpected write'),
        (word | 4, 'injected adjacent OTP read rejected', 'read outside status/version shadow')]:
        m = OtpMachine(rebuilt, types)
        m.probe()
        m.put(address + offset, instruction)
        try:
            m.get()
        except ValueError as error:
            require(expected_error in str(error), 'negative control failed for unexpected reason')
        else:
            raise ValueError('unsafe mutation was accepted: ' + label)
        cases.append(label)
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stock_kernel', type=Path)
    parser.add_argument('rebuilt_kernel', type=Path)
    parser.add_argument('rebuilt_otp_object', type=Path)
    args = parser.parse_args()
    types = Module(args.rebuilt_otp_object)
    stock = Image(args.stock_kernel, True, ('get_dreem_hardware_version',))
    rebuilt = load(args.rebuilt_kernel, types)
    cases = verify(stock, rebuilt, types)
    print(json.dumps({'stock_kernel_raw_sha256': RAW_HASH, 'rebuilt_kernel_sha256': sha(rebuilt.binary),
                      'otp_object_sha256': sha(types.binary), 'passed_cases': len(cases), 'cases': cases,
                      'verifier_sources': {n: sha((Path(__file__).parent / n).read_bytes()) for n in
                          ('verify_hardware_version.py', 'verify_adc_module.py', 'verify_busfreq.py',
                           'verify_ddr_preparation.py', 'verify_wm8960_sources.py', 'verify_ddr_sources.py')},
                      'otp_writes_allowed': False, 'hardware_qualified': False,
                      'limits': 'Synthetic clocks, registers, locking and original provider callbacks; no real concurrency, sysfs/devres lifetime or fuse contents verified.'}, indent=2))


if __name__ == '__main__':
    main()
