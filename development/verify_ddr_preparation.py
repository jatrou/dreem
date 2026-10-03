#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Execute DDR preparation, probe failure paths and C wrappers with modeled I/O.

Uses the actual linked ARM routines. Register values, allocation, mapping, clocks,
notifiers and workqueues are synthetic. The copied DDR assembly is inspected but
its physical transition is a stub; no firmware is installed or run on a headset.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import struct

from elftools.elf.elffile import ELFFile
from unicorn import UC_HOOK_MEM_READ
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                               UC_ARM_REG_R3, UC_ARM_REG_LR, UC_ARM_REG_PC,
                               UC_ARM_REG_CPSR)
from verify_adc_module import Module
from verify_busfreq import Image, Machine, FIXTURE, STACK, STOP, RAW_HASH, require

MMDC, IOMUX, IRAM, HEAP = 0x50000000, 0x50002000, 0x50004000, 0x50008000
PDEV, NODE, CACHE = FIXTURE + 0xc000, FIXTURE + 0xd000, FIXTURE + 0xe000
RATE, REQUIRED = 400000000, 1916
DDR_STATE = ('curr_ddr_rate', 'iram_ddr_settings', 'ddr_settings_size',
             'iram_iomux_settings', 'iomux_settings_size', 'normal_mmdc_settings',
             'imx6_busfreq_info', 'mmdc_base', 'imx6_up_change_ddr_freq', 'iomux_base')
SERVICES = ('of_find_compatible_node', 'of_iomap', '__arm_iounmap', 'kmem_cache_alloc',
            'kmem_cache_alloc_trace', '__kmalloc', 'kfree', 'memcpy', '__memzero',
            'v7_coherent_kern_range', 'warn_slowpath_fmt', 'save_ttbr1', 'restore_ttbr1',
            'devm_clk_get', 'of_property_read_u32_array', 'imx_mmdc_get_ddr_type',
            'init_timer_key', 'register_pm_notifier', 'register_reboot_notifier',
            'unregister_pm_notifier', 'unregister_reboot_notifier', 'sysfs_create_file_ns',
            'queue_delayed_work_on', 'cancel_delayed_work_sync')
PROBE_CLOCKS = ('osc', 'pll2_pfd2_396m', 'pll2_198m', 'pll2_bus', 'pll3_usb_otg',
                'periph', 'periph_pre', 'periph_clk2', 'periph_clk2_sel', 'ahb', 'ocram',
                'periph2', 'periph2_pre', 'periph2_clk2', 'periph2_clk2_sel', 'mmdc',
                'arm', 'step', 'pll1', 'pll1_bypass_src', 'pll1_bypass', 'pll1_sys', 'pll1_sw')


def load_image(path, stock=False):
    elf = ELFFile(io.BytesIO(path.read_bytes()))
    symbols = list(elf.get_section_by_name('.symtab').iter_symbols())
    routines = ['init_mmdc_ddr3_settings_imx6_up', 'update_ddr_freq_imx6_up', 'busfreq_probe']
    if any(s.name == 'dreem_ddr_discard_settings' for s in symbols):
        routines.append('dreem_ddr_discard_settings')
    image = Image(path, stock, routines)
    anchor = image.symbols['imx6_busfreq_info']
    for name in DDR_STATE:
        candidates = {s['st_value'] for s in symbols if s.name == name and abs(s['st_value'] - anchor) < 0x100}
        require(len(candidates) == 1, 'DDR global is missing or ambiguous: ' + name)
        image.symbols[name] = candidates.pop()
    image.named_addresses = {}
    for symbol in symbols:
        image.named_addresses.setdefault(symbol.name, set()).add(symbol['st_value'])
    return image


def resolve_probe_globals(image, obj):
    """Resolve duplicated local clock names through the whole object's layout."""
    for section_name in ('.bss', '.data'):
        section = obj.elf.get_section_index(section_name)
        symbols = [s for s in obj.elf.get_section_by_name('.symtab').iter_symbols()
                   if s['st_shndx'] == section and s['st_info']['type'] == 'STT_OBJECT']
        bases = None
        for symbol in symbols:
            require(symbol.name in image.named_addresses, 'missing probe object: ' + symbol.name)
            choices = {a - symbol['st_value'] for a in image.named_addresses[symbol.name]}
            bases = choices if bases is None else bases & choices
        require(bases is not None and len(bases) == 1, 'probe object layout differs from kernel')
        base = bases.pop()
        for symbol in symbols:
            image.symbols[symbol.name] = base + symbol['st_value']


class PreparationMachine(Machine):
    def __init__(self, image, types, *, capacity=4096, base=IRAM, fail=None, enabled=True,
                 femto=True, pending_pm_work=False):
        super().__init__(image, dict(bus_freq_scaling_initialized=0, bus_freq_scaling_is_active=0),
                         enabled=enabled, femto=femto)
        self.types, self.fail, self.capacity, self.base = types, fail, capacity, base
        self.pending_pm_work = pending_pm_work
        self.maps, self.allocated = set(), False
        self.pm = self.reboot = self.sysfs = self.copied = False
        self.work, self.events, self.reads, self.transitions = set(), [], [], []
        self.iram_writes = 0
        for address, size in ((MMDC, 4096), (IOMUX, 4096), (IRAM, 8192), (HEAP, 4096)):
            self.cpu.mem_map(address, size)
        self.cpu.mem_write(IRAM, bytes([0xa5]) * 8192)
        for offset in range(0, 4096, 4):
            self.cpu.mem_write(MMDC + offset, struct.pack('<I', 0xabc00000 + offset))
            self.cpu.mem_write(IOMUX + offset, struct.pack('<I', 0xdef00000 + offset))
        self.cpu.mem_write(MMDC + 0x8b8, struct.pack('<I', 0x01550000))
        for name, value in [('ddr_freq_change_iram_base', base), ('ddr_freq_change_total_size', capacity),
                            ('ddr_freq_change_iram_phys', 0x00904000), ('iram_tlb_phys_addr', 0x00900000)]:
            self.set(name, value)
        for i in range(16):
            self.cpu.mem_write(self.symbols['kmalloc_caches'] + 4 * i, struct.pack('<I', CACHE + 16 * i))
        device = PDEV + types.members['platform_device']['dev']
        self.device = device
        self.cpu.mem_write(device + types.members['device']['of_node'], struct.pack('<I', NODE + 0x200))
        self.set('busfreq_dev', device)
        self.stub_addresses.pop(self.symbols['update_ddr_freq_imx6_up'])
        self.stub_addresses.update({self.symbols[n]: n for n in SERVICES if n in self.symbols})
        self.allowed_writes.update(self.symbols[n] for n in DDR_STATE)
        self.allowed_writes.update(self.symbols[n] for n in ('ddr_low_rate', 'ddr_normal_rate', 'busfreq_dev', 'ddr_type')
                                   if n in self.symbols)
        self.allowed_writes.update(address for name, address in self.symbols.items() if name.endswith('_clk'))
        self.extra_writes = [(self.symbols['iomux_offsets_mx6ul'], 16),
                             *[(self.symbols[n], types.sizes['delayed_work'])
                               for n in ('low_bus_freq_handler', 'bus_freq_daemon')]]
        self.cpu.hook_add(UC_HOOK_MEM_READ, self.read_mmio, begin=MMDC, end=IOMUX + 4095)

    def resources(self):
        return sorted(self.maps), self.allocated, self.pm, self.reboot, self.sysfs, bool(self.work)

    def write(self, cpu, access, address, size, value, extra):
        if hasattr(self, 'extra_writes'):
            if IRAM <= address < IRAM + 8192:
                require(self.base <= address and address + size <= self.base + self.capacity,
                        'write exceeds declared IRAM reservation')
                self.iram_writes += size
                return
            if HEAP <= address < HEAP + 4096:
                require(self.allocated and address + size <= HEAP + 96, 'write outside allocated settings')
                return
            if address == self.symbols.get('dreem_ddr_prepared') and size == 1:
                return
            if any(start <= address and address + size <= start + length for start, length in self.extra_writes):
                return
            if address == self.symbols['bus_freq_scaling_initialized'] and value:
                require(self.allocated and self.maps == {MMDC, IOMUX} and self.copied,
                        'driver ready before DDR preparation')
        return super().write(cpu, access, address, size, value, extra)

    def write_bytes(self, address, data):
        for offset in range(0, len(data), 4):
            part = data[offset:offset + 4]
            self.write(self.cpu, None, address + offset, len(part), int.from_bytes(part, 'little'), None)
        self.cpu.mem_write(address, data)

    def read_mmio(self, cpu, access, address, size, value, extra):
        region = MMDC if address < MMDC + 4096 else IOMUX
        require(region in self.maps and size == 4 and address % 4 == 0, 'read of unmapped MMIO')
        self.reads.append(('mmdc' if region == MMDC else 'iomux', address - region))

    def code(self, cpu, address, size, extra):
        a, b, c, d = [cpu.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
        name = self.stub_addresses.get(address)
        if address == self.base + 24:
            require(a == self.base and cpu.reg_read(UC_ARM_REG_CPSR) & 0x80, 'DDR transition lacked IRQ exclusion')
            self.transitions.append(self.contents())
            result = 0
        elif name not in SERVICES:
            return super().code(cpu, address, size, extra)
        else:
            result = 0
            self.events.append(name)
            if name == 'of_find_compatible_node':
                compatible = self.string(c)
                require(a == b == 0, 'unexpected node search')
                which = {'fsl,imx6q-mmdc': 'mmdc', 'fsl,imx6ul-iomuxc': 'iomux'}[compatible]
                result = 0 if self.fail == 'node:' + which else NODE + (0x100 if which == 'iomux' else 0)
            elif name == 'of_iomap':
                require(a in (NODE, NODE + 0x100) and b == 0, 'unexpected mapping request')
                region = MMDC if a == NODE else IOMUX
                which = 'mmdc' if region == MMDC else 'iomux'
                if self.fail != 'map:' + which:
                    self.maps.add(region)
                    result = region
            elif name == '__arm_iounmap':
                require(a in self.maps and not self.word(self.symbols['bus_freq_scaling_initialized']),
                        'unmapping absent or live DDR registers')
                self.maps.remove(a)
            elif name in ('kmem_cache_alloc', 'kmem_cache_alloc_trace', '__kmalloc'):
                require(not self.allocated, 'duplicate settings allocation')
                # The pinned ARM configuration has 64-byte DMA alignment:
                # its 96-byte request uses the 128-byte cache, index 7.
                # Keep writes bounded to the requested 96-byte payload.
                require(a == (96 if name == '__kmalloc' else CACHE + 7 * 16), 'unexpected allocation')
                if name == 'kmem_cache_alloc_trace':
                    require(c == 96, 'unexpected allocation size')
                if self.fail != 'allocation':
                    self.allocated = True
                    result = HEAP
            elif name == 'kfree':
                require(a == HEAP and self.allocated and
                        not self.word(self.symbols['bus_freq_scaling_initialized']), 'invalid settings release')
                self.allocated = False
            elif name in ('memcpy', '__memzero'):
                length = c if name == 'memcpy' else b
                require(length <= 4096, 'unexpected copy size')
                self.write_bytes(a, bytes(cpu.mem_read(b, length)) if name == 'memcpy' else bytes(length))
                result = a
            elif name == 'v7_coherent_kern_range':
                require(a == self.base + 24 and b == a + 1764, 'wrong executable copy range')
                require(bytes(cpu.mem_read(a, 1764)) == bytes(cpu.mem_read(self.symbols['imx6_up_ddr3_freq_change'], 1764)),
                        'copied assembly differs')
                self.copied = True
            elif name == 'warn_slowpath_fmt':
                raise ValueError('initializer continued through a warning')
            elif name == 'save_ttbr1':
                require(cpu.reg_read(UC_ARM_REG_CPSR) & 0x80, 'TTBR save without IRQ exclusion')
                result = 0xa5a50000
            elif name == 'restore_ttbr1':
                require(a == 0xa5a50000 and cpu.reg_read(UC_ARM_REG_CPSR) & 0x80, 'TTBR restore differs')
            elif name == 'devm_clk_get':
                clock = self.string(b)
                require(a == self.device and clock in PROBE_CLOCKS, 'unexpected probe clock')
                result = -517 if self.fail == 'clock:' + clock else FIXTURE + PROBE_CLOCKS.index(clock) * 0x100
            elif name == 'of_property_read_u32_array':
                require(a == NODE + 0x200 and self.string(b) == 'fsl,max_ddr_freq' and d == 1 and
                        c == self.symbols['ddr_normal_rate'], 'unexpected DDR property read')
                result = -22 if self.fail == 'property' else 0
                if not result:
                    self.write_bytes(c, struct.pack('<I', RATE))
            elif name == 'imx_mmdc_get_ddr_type':
                result = 0
            elif name == 'init_timer_key':
                require(any(a == self.symbols[n] + self.types.members['delayed_work']['timer']
                            for n in ('low_bus_freq_handler', 'bus_freq_daemon')), 'wrong delayed-work timer')
            elif name in ('register_pm_notifier', 'register_reboot_notifier'):
                which = 'pm' if name == 'register_pm_notifier' else 'reboot'
                target = 'imx_bus_freq_pm_notifier' if which == 'pm' else 'imx_busfreq_reboot_notifier'
                require(a == self.symbols[target] and not getattr(self, which), 'wrong notifier registration')
                result = -12 if self.fail == which else 0
                if not result:
                    setattr(self, which, True)
                    if which == 'pm' and self.pending_pm_work:
                        self.work.add(self.symbols['bus_freq_daemon'])
            elif name in ('unregister_pm_notifier', 'unregister_reboot_notifier'):
                which = 'pm' if name == 'unregister_pm_notifier' else 'reboot'
                require(getattr(self, which), 'unregistering absent notifier')
                setattr(self, which, False)
            elif name == 'sysfs_create_file_ns':
                require(a == self.device + self.types.members['device']['kobj'] and
                        b == self.symbols['dev_attr_enable'] and c == 0 and self.pm and self.reboot and
                        self.word(self.symbols['bus_freq_scaling_initialized']) == 1,
                        'sysfs published before complete initialization')
                result = -12 if self.fail == 'sysfs' else 0
                self.sysfs = not result
            elif name == 'queue_delayed_work_on':
                require(c == self.symbols['bus_freq_daemon'] and self.sysfs and
                        self.word(self.symbols['bus_freq_scaling_initialized']) == 1,
                        'work queued before successful publication')
                self.work.add(c)
                result = 1
            elif name == 'cancel_delayed_work_sync':
                require(a in (self.symbols['bus_freq_daemon'], self.symbols['low_bus_freq_handler']), 'unknown work cancelled')
                self.work.discard(a)
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def contents(self):
        info = self.word(self.symbols['imx6_busfreq_info'])
        ddr = self.word(self.symbols['iram_ddr_settings'])
        iomux = self.word(self.symbols['iram_iomux_settings'])
        require(info and ddr and iomux, 'DDR contents unavailable')
        require(self.word(ddr) == 12 and self.word(iomux) == 2, 'wrong table counts')
        return {'frequency': self.word(info), 'dll_off': self.word(info + 8),
                'delay': self.word(info + 16),
                'ddr': [self.word(ddr + 8 + 4 * i) for i in range(24)],
                'iomux': [self.word(iomux + 8 + 4 * i) for i in range(4)]}

    def empty(self):
        require(self.resources() == ([], False, False, False, False, False), 'resources leaked')
        require(all(self.word(self.symbols[n]) == 0 for n in DDR_STATE), 'partial DDR state published')
        require(not self.word(self.symbols['bus_freq_scaling_initialized']) and
                not self.word(self.symbols['bus_freq_scaling_is_active']), 'failed probe left readiness enabled')


def verify(stock_path, rebuilt_path, probe_object):
    stock, rebuilt = load_image(stock_path, True), load_image(rebuilt_path)
    require('dreem_ddr_discard_settings' in rebuilt.symbols, 'checked preparation is absent')
    types = Module(probe_object)
    resolve_probe_globals(rebuilt, types)
    cases = []
    for mode in range(5):
        a, b = PreparationMachine(stock, types), PreparationMachine(rebuilt, types, capacity=REQUIRED)
        for machine in (a, b):
            require(machine.call('init_mmdc_ddr3_settings_imx6_up', PDEV) == 0, 'valid preparation failed')
            machine.set('cur_bus_freq_mode', mode)
            require(machine.call('update_ddr_freq_imx6_up', 300000000) == 0, 'wrapper failed')
            require(machine.word(machine.symbols['curr_ddr_rate']) == 300000000, 'current rate not updated')
            require(not machine.cpu.reg_read(UC_ARM_REG_CPSR) & 0x80, 'wrapper left IRQs disabled')
            before = len(machine.transitions)
            machine.call('update_ddr_freq_imx6_up', 300000000)
            require(len(machine.transitions) == before == 1, 'same-rate request repeated a transition')
        require(a.contents() == b.contents() and a.reads == b.reads and a.transitions == b.transitions,
                'prepared settings or wrapper behavior differs from stock')
        require(b.contents()['dll_off'] == int(mode in (2, 3)) and b.contents()['delay'] == 0x155,
                'incorrect mode or delay passed to assembly')
        require(b.word(b.symbols['iram_ddr_settings']) == IRAM + 1812 and
                bytes(b.cpu.mem_read(IRAM + REQUIRED, 16)) == bytes([0xa5]) * 16,
                'compact settings layout or reservation guard differs')
        cases.append('stock settings and C wrapper mode ' + str(mode))
    for options in (dict(enabled=False), dict(femto=False)):
        machine = PreparationMachine(rebuilt, types, **options)
        require(machine.call('init_mmdc_ddr3_settings_imx6_up', PDEV) == 0, 'fallback preparation failed')
        require(machine.word(machine.symbols['iram_ddr_settings']) == IRAM + 1980 and
                not machine.cpu.mem_read(machine.symbols['dreem_ddr_prepared'], 1)[0], 'NXP fallback changed')
        cases.append('unmodified NXP preparation fallback ' + repr(options))
    for capacity in (0, 16, 1788, REQUIRED - 1):
        machine = PreparationMachine(rebuilt, types, capacity=capacity)
        require(machine.call('init_mmdc_ddr3_settings_imx6_up', PDEV) == (-28 & 0xffffffff), 'wrong short-reservation error')
        require(not machine.events and not machine.iram_writes, 'short reservation used before validation')
        machine.empty()
        cases.append('reservation rejected before any side effect ' + str(capacity))
    for base in (0, IRAM + 4, 0xfffffff8):
        machine = PreparationMachine(rebuilt, types, base=base)
        require(machine.call('init_mmdc_ddr3_settings_imx6_up', PDEV) == (-22 & 0xffffffff), 'invalid base accepted')
        require(not machine.events and not machine.iram_writes, 'invalid base had side effects')
        machine.empty()
        cases.append('invalid reservation base ' + hex(base))
    for name, value, error in [('__mxc_cpu_type', 0x64, 19), ('ddr_normal_rate', 0, 22)]:
        machine = PreparationMachine(rebuilt, types)
        machine.set(name, value)
        require(machine.call('init_mmdc_ddr3_settings_imx6_up', PDEV) == (-error & 0xffffffff), 'invalid setup accepted')
        machine.empty()
        cases.append('invalid setup ' + name)
    failures = [('node:mmdc',19), ('map:mmdc',12), ('node:iomux',19), ('map:iomux',12), ('allocation',12)]
    for fail, error in failures:
        machine = PreparationMachine(rebuilt, types, fail=fail)
        require(machine.call('init_mmdc_ddr3_settings_imx6_up', PDEV) == (-error & 0xffffffff), 'wrong preparation error')
        machine.empty()
        require(not machine.iram_writes, 'failed preparation wrote executable RAM')
        machine.fail = None
        require(machine.call('init_mmdc_ddr3_settings_imx6_up', PDEV) == 0, 'retry after preparation failure failed')
        before = machine.resources(), list(machine.events)
        require(machine.call('init_mmdc_ddr3_settings_imx6_up', PDEV) == (-16 & 0xffffffff) and
                (machine.resources(), machine.events) == before, 'duplicate preparation changed ownership')
        machine.call('dreem_ddr_discard_settings')
        machine.empty()
        machine.call('dreem_ddr_discard_settings')
        machine.empty()
        cases.append('preparation rollback, retry, duplicate and discard ' + fail)
    for fail, error in [('clock:' + n,22) for n in PROBE_CLOCKS] + [('property',22), *failures,
                                                                      ('pm',12), ('reboot',12), ('sysfs',12)]:
        machine = PreparationMachine(rebuilt, types, fail=fail)
        require(machine.call('busfreq_probe', PDEV) == (-error & 0xffffffff), 'wrong probe error: ' + fail)
        machine.empty()
        machine.fail = None
        require(machine.call('busfreq_probe', PDEV) == 0, 'probe retry failed: ' + fail)
        require(machine.resources() == ([MMDC, IOMUX], True, True, True, True, True), 'probe ownership differs')
        require(machine.word(machine.symbols['bus_freq_scaling_initialized']) == 1 and
                machine.word(machine.symbols['bus_freq_scaling_is_active']) == 1, 'successful probe state differs')
        before = machine.resources(), list(machine.events)
        require(machine.call('busfreq_probe', PDEV) == (-16 & 0xffffffff) and
                (machine.resources(), machine.events) == before, 'duplicate probe changed ownership')
        cases.append('full probe rollback and successful retry ' + fail)
    for fail in ('reboot', 'sysfs'):
        machine = PreparationMachine(rebuilt, types, fail=fail, pending_pm_work=True)
        require(machine.call('busfreq_probe', PDEV) == (-12 & 0xffffffff), 'wrong notifier-work failure')
        machine.empty()
        require(machine.events.index('unregister_pm_notifier') < machine.events.index('cancel_delayed_work_sync') <
                machine.events.index('kfree'), 'work not retired before freeing prepared settings')
        cases.append('failed probe drains modeled PM notification work ' + fail)
    # The saved initializer accepts its nominal 1916-byte capacity, but its
    # typed-pointer arithmetic places the DDR table beyond that reservation.
    machine = PreparationMachine(stock, types, capacity=REQUIRED)
    require(machine.call('init_mmdc_ddr3_settings_imx6_up', PDEV) == 0, 'stock boundary control did not prepare')
    try:
        machine.call('update_ddr_freq_imx6_up', 300000000)
    except ValueError as error:
        require('exceeds declared IRAM reservation' in str(error), 'unexpected stock rejection')
    else:
        raise ValueError('stock negative control failed to detect out-of-reservation table write')
    cases.append('stock negative control detects undersized accepted reservation')
    machine = PreparationMachine(stock, types, capacity=16)
    try:
        machine.call('init_mmdc_ddr3_settings_imx6_up', PDEV)
    except ValueError as error:
        require('exceeds declared IRAM reservation' in str(error), 'unexpected stock copy rejection')
    else:
        raise ValueError('stock negative control failed to detect copy before capacity check')
    cases.append('stock negative control detects code copy before capacity check')
    machine = PreparationMachine(stock, types, fail='node:iomux')
    require(machine.call('init_mmdc_ddr3_settings_imx6_up', PDEV) == (-22 & 0xffffffff) and
            machine.maps == {MMDC} and machine.word(machine.symbols['mmdc_base']) == MMDC,
            'stock negative control did not reproduce retained mapping')
    cases.append('stock negative control detects mapping retained on failure')
    return {'stock_raw_sha256': RAW_HASH, 'rebuilt_kernel_sha256': hashlib.sha256(rebuilt.binary).hexdigest(),
            'probe_object_sha256': hashlib.sha256(types.binary).hexdigest(), 'passed_cases': len(cases),
            'cases': cases, 'runtime_qualified': False,
            'limits': 'Synthetic MMIO values and kernel services; DDR assembly execution, real scheduling, early SRAM mapping and hardware timing unverified'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stock_kernel', type=Path)
    parser.add_argument('rebuilt_kernel', type=Path)
    parser.add_argument('probe_object', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.stock_kernel, args.rebuilt_kernel, args.probe_object), indent=2))


if __name__ == '__main__':
    main()
