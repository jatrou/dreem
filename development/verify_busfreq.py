#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Compare stock and reconstructed ARM bus-frequency policy with synthetic clocks.

Executes bounded, allowlisted kernel routines in Unicorn. Clock, regulator,
DDR-transition, scheduling and mutex services are models, never real hardware.
The comparison includes ordered calls and state changes, not printk wording.
"""
import argparse
import hashlib
import io
import itertools
import json
from pathlib import Path
import struct

from elftools.elf.elffile import ELFFile
from unicorn import (Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE,
                     UC_HOOK_MEM_WRITE, UC_PROT_ALL)
from unicorn.arm_const import (UC_CPU_ARM_CORTEX_A7, UC_ARM_REG_R0, UC_ARM_REG_R1,
                               UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_SP,
                               UC_ARM_REG_LR, UC_ARM_REG_PC)

RAW_HASH = "e15f659afdffab3fde7c997883475e4c95b5d3cdc1fbcd23c76365ee4cd52dcb"
STACK, STOP, FIXTURE = 0x10000000, 0x10010000, 0x40000000
STATES = ("high_bus_count", "med_bus_count", "audio_bus_count", "low_bus_count",
          "high_bus_freq_mode", "med_bus_freq_mode", "audio_bus_freq_mode",
          "low_bus_freq_mode", "ultra_low_bus_freq_mode", "cur_bus_freq_mode",
          "busfreq_suspended", "bus_freq_scaling_initialized", "bus_freq_scaling_is_active")
CLOCKS = ("pll3_clk", "pll2_400_clk", "ocram_clk", "ahb_clk", "mmdc_clk",
          "periph2_pre_clk", "periph2_clk", "periph2_clk2_sel_clk", "periph_pre_clk",
          "periph_clk", "periph_clk2_sel_clk", "pll2_bus_clk")
ENTRIES = ("request_bus_freq", "release_bus_freq", "set_low_bus_freq", "get_bus_freq_mode",
           "set_high_bus_freq", "bus_freq_daemon_handler", "bus_freq_pm_notify",
           "bus_freq_scaling_enable_store", "busfreq_reboot_notifier_event")
INTERNAL = ("exit_lpm_imx6_up", "busfreq_notify", "dreem_busfreq_active",
            "dreem_busfreq_request", "dreem_busfreq_release", "dreem_exit_lpm_imx6_up")
STUBS = ("mutex_lock", "mutex_unlock", "printk", "dev_err", "dump_stack",
         "of_machine_is_compatible", "clk_prepare", "clk_enable", "clk_disable",
         "clk_unprepare", "clk_set_rate", "clk_set_parent", "__clk_get_name",
         "cancel_delayed_work_sync", "raw_notifier_call_chain", "update_ddr_freq_imx6_up",
         "update_lpddr2_freq", "imx6ull_lower_cpu_rate", "queue_delayed_work_on",
         "usecs_to_jiffies", "msecs_to_jiffies")


def require(condition, message):
    if not condition:
        raise ValueError(message)


class Image:
    def __init__(self, path, stock=False):
        self.binary = path.read_bytes()
        elf = ELFFile(io.BytesIO(self.binary))
        require(elf.elfclass == 32 and elf.little_endian and elf['e_machine'] == 'EM_ARM',
                "expected ARM32 little-endian kernel")
        if stock:
            section = elf.get_section_by_name('.kernel')
            require(section is not None and hashlib.sha256(section.data()).hexdigest() == RAW_HASH,
                    "stock kernel differs from reviewed firmware")
        self.sections = [(s['sh_addr'], s['sh_size'],
                          b'' if s['sh_type'] == 'SHT_NOBITS' else s.data())
                         for s in elf.iter_sections() if s['sh_flags'] & 2 and s['sh_size']]
        symbols = list(elf.get_section_by_name('.symtab').iter_symbols())
        self.symbols = {s.name: s['st_value'] for s in symbols if s.name}
        # Both images contain several local ddr_type symbols. Select the one
        # adjacent to the bus-frequency state, not another driver's variable.
        self.symbols['ddr_type'] = min(
            (s['st_value'] for s in symbols if s.name == 'ddr_type'),
            key=lambda a: abs(a - self.symbols['high_bus_count']))
        addresses = sorted(set(s['st_value'] for s in symbols))
        self.ranges = []
        for name in ENTRIES + INTERNAL:
            found = [s for s in symbols if s.name == name or s.name.startswith(name + '.')]
            if not found:
                require(name in INTERNAL, "missing routine: " + name)
                continue
            require(len(found) == 1, "ambiguous routine: " + name)
            symbol = found[0]
            address = symbol['st_value']
            end = address + symbol['st_size'] if symbol['st_size'] else next(a for a in addresses if a > address)
            self.symbols[name] = address
            self.ranges.append((address, end))


class Machine:
    def __init__(self, image, state=None, *, enabled=True, femto=True, failure=None):
        self.image, self.symbols = image, image.symbols
        self.cpu = cpu = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_A7)
        pages = set()
        for address, size, data in image.sections:
            pages.update(range(address & ~4095, (address + size + 4095) & ~4095, 4096))
        # Merge adjacent pages to keep initialization bounded and inexpensive.
        ordered = sorted(pages)
        start = last = ordered[0]
        for page in ordered[1:] + [None]:
            if page is not None and page == last + 4096:
                last = page
                continue
            cpu.mem_map(start, last - start + 4096, UC_PROT_ALL)
            start = last = page
        for address, size, data in image.sections:
            if data:
                cpu.mem_write(address, data)
        cpu.mem_map(STACK, 0x11000, UC_PROT_ALL)
        cpu.mem_map(FIXTURE, 0x10000, UC_PROT_ALL)
        self.trace, self.locked = [], False
        self.failure, self.failure_used, self.femto = failure, False, femto
        self.clocks = {FIXTURE + i * 0x100: name for i, name in enumerate(CLOCKS)}
        for address, name in self.clocks.items():
            self.set(name, address)
            cpu.mem_write(address, name.encode() + b'\0')
        values = {name: 0 for name in STATES}
        values.update(bus_freq_scaling_initialized=1, bus_freq_scaling_is_active=1,
                      high_bus_freq_mode=1)
        values.update(state or {})
        for name, value in values.items():
            self.set(name, value)
        for name, value in [('__mxc_cpu_type', 0x65), ('ddr_normal_rate', 400000000),
                            ('busfreq_dev', FIXTURE + 0x8000), ('system_wq', FIXTURE + 0x9000)]:
            self.set(name, value)
        if 'ddr_type' not in values:
            self.set('ddr_type', 0)
        if 'dreem_busfreq_enabled' in self.symbols:
            cpu.mem_write(self.symbols['dreem_busfreq_enabled'], bytes([enabled]))
        self.stub_addresses = {self.symbols[n]: n for n in STUBS if n in self.symbols}
        self.allowed_writes = {self.symbols[n] for n in STATES}
        cpu.hook_add(UC_HOOK_CODE, self.code)
        cpu.hook_add(UC_HOOK_MEM_WRITE, self.write)

    def set(self, name, value):
        self.cpu.mem_write(self.symbols[name], struct.pack('<I', value & 0xffffffff))

    def word(self, address):
        return struct.unpack('<I', self.cpu.mem_read(address, 4))[0]

    def string(self, address):
        data = bytearray()
        for i in range(256):
            byte = bytes(self.cpu.mem_read(address + i, 1))
            if byte == b'\0':
                return data.decode('ascii')
            data += byte
        raise ValueError('unterminated modeled string')

    def write(self, cpu, access, address, size, value, _):
        require(STACK <= address < STOP or (address in self.allowed_writes and size == 4),
                f'unexpected kernel write at {address:#x}')

    def code(self, cpu, address, size, _):
        name = self.stub_addresses.get(address)
        if name is None:
            require(any(a <= address < b for a, b in self.image.ranges),
                    f'execution left allowlisted routines at {address:#x}')
            return
        a, b, c, d = [cpu.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
        result = 0
        if name.startswith('mutex_'):
            require(a == self.symbols['bus_freq_mutex'], 'wrong mutex')
            require(self.locked == (name == 'mutex_unlock'), 'unbalanced mutex')
            self.locked = name == 'mutex_lock'
            self.trace.append([name])
        elif name == 'of_machine_is_compatible':
            require(self.string(a) == 'fsl,imx6ull-femto', 'wrong board gate')
            result = int(self.femto)
        elif name == 'printk':
            pass
        elif name == 'dev_err':
            message = self.string(b)
            if '%s' in message:
                message = message.replace('%s', self.string(c), 1)
            self.trace.append([name, message])
        elif name == 'dump_stack':
            self.trace.append([name])
        elif name == '__clk_get_name':
            require(a in self.clocks, 'unknown clock name')
            result = a
        elif name.startswith('clk_'):
            require(a in self.clocks, 'unknown clock')
            record = [name, self.clocks[a]]
            if name == 'clk_set_parent':
                require(b in self.clocks, 'unknown parent clock')
                record.append(self.clocks[b])
            elif name == 'clk_set_rate':
                record.append(b)
            self.trace.append(record)
            if self.failure == (name, self.clocks[a]) and not self.failure_used:
                self.failure_used = True
                result = -5
        elif name == 'cancel_delayed_work_sync':
            require(a == self.symbols['low_bus_freq_handler'], 'wrong work cancelled')
            self.trace.append([name])
        elif name == 'raw_notifier_call_chain':
            require(b == 1 and c == 0, 'unexpected notifier arguments')
            self.trace.append([name, b])
        elif name in ('update_ddr_freq_imx6_up', 'update_lpddr2_freq', 'imx6ull_lower_cpu_rate'):
            self.trace.append([name, a])
        elif name in ('usecs_to_jiffies', 'msecs_to_jiffies'):
            # Reviewed build CONFIG_HZ=100; round up as the kernel helpers do.
            divisor = 10000 if name == 'usecs_to_jiffies' else 10
            result = (a + divisor - 1) // divisor
        elif name == 'queue_delayed_work_on':
            require(c == self.symbols['bus_freq_daemon'], 'unexpected scheduled work')
            self.trace.append([name, d])
            result = 1
        else:
            raise ValueError('unmodeled service: ' + name)
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def call(self, name, *arguments):
        converted = []
        for index, argument in enumerate(arguments):
            if isinstance(argument, bytes):
                require(len(argument) < 256, 'oversized fixture string')
                address = FIXTURE + 0xf000 + 256 * index
                self.cpu.mem_write(address, argument + b'\0')
                argument = address
            converted.append(argument)
        for reg, value in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3),
                              tuple(converted) + (0,) * (4 - len(converted))):
            self.cpu.reg_write(reg, value & 0xffffffff)
        self.cpu.reg_write(UC_ARM_REG_SP, STOP - 16)
        self.cpu.reg_write(UC_ARM_REG_LR, STOP)
        self.cpu.emu_start(self.symbols[name], STOP, count=10000, timeout=200000)
        require(self.cpu.reg_read(UC_ARM_REG_PC) == STOP and not self.locked, 'call did not finish')
        return self.cpu.reg_read(UC_ARM_REG_R0)

    def snapshot(self):
        return {name: self.word(self.symbols[name]) for name in STATES}


def verify(stock_path, rebuilt_path, baseline_path):
    stock, rebuilt, baseline = Image(stock_path, True), Image(rebuilt_path), Image(baseline_path)
    require('dreem_busfreq_enabled' in rebuilt.symbols, 'policy missing from rebuilt kernel')
    results = []

    def compare(label, operations, state=None, **options):
        a, b = Machine(stock, state, **options), Machine(rebuilt, state, **options)
        for entry, arguments in operations:
            ar, br = a.call(entry, *arguments), b.call(entry, *arguments)
            if entry not in ('request_bus_freq', 'release_bus_freq', 'bus_freq_daemon_handler'):
                require(ar == br, label + ': return differs')
            require(a.snapshot() == b.snapshot(), label + ': state differs')
            require(a.trace == b.trace, label + ': ordered calls differ\n' + repr(a.trace) + '\n' + repr(b.trace))
        results.append(label)
        return a, b

    for flags in itertools.product((0, 1), repeat=3):
        state = dict(zip(('bus_freq_scaling_initialized', 'bus_freq_scaling_is_active', 'busfreq_suspended'), flags))
        for count in (0, 1, 17, 0xffffffff):
            state.update({n: count for n in STATES[:4]})
            for mode in (0, 1, 2, 3, 4, 5, 6, 7, 8, 0xffffffff):
                compare(f'counts flags={flags} initial={count} mode={mode}',
                        [('request_bus_freq', (mode,)), ('release_bus_freq', (mode,))], state)
        compare(f'underflow flags={flags}', [('release_bus_freq', (m,)) for m in range(9)],
                dict(zip(('bus_freq_scaling_initialized', 'bus_freq_scaling_is_active', 'busfreq_suspended'), flags)))
        compare(f'low-mode suppression flags={flags}', [('set_low_bus_freq', ()),
                ('bus_freq_daemon_handler', ()), ('get_bus_freq_mode', ())], state)

    for low, audio, high, suspended, initialized, active in itertools.product((0, 1), repeat=6):
        for ddr in (0, 1, 2, 3):
            state = dict(low_bus_freq_mode=low, audio_bus_freq_mode=audio, high_bus_freq_mode=high,
                         busfreq_suspended=suspended, bus_freq_scaling_initialized=initialized,
                         bus_freq_scaling_is_active=active, ddr_type=ddr)
            compare(f'high-transition {low,audio,high,suspended,initialized,active,ddr}',
                    [('request_bus_freq', (7,))], state)
    failures = [('clk_prepare', 'pll3_clk'), ('clk_enable', 'pll3_clk'),
                ('clk_prepare', 'pll2_400_clk'), ('clk_enable', 'pll2_400_clk'),
                ('clk_set_rate', 'ocram_clk'), ('clk_set_rate', 'ahb_clk'),
                ('clk_set_rate', 'mmdc_clk'), ('clk_set_parent', 'periph2_pre_clk'),
                ('clk_set_parent', 'periph2_clk'), ('clk_set_parent', 'periph2_clk2_sel_clk')]
    for failure in failures:
        a, b = compare('clock error ' + repr(failure), [('request_bus_freq', (7,))],
                       dict(high_bus_freq_mode=0, audio_bus_freq_mode=1), failure=failure)
        require(a.failure_used and b.failure_used, 'clock error not reached')
    compare('PM prepare/post and explicit request', [('bus_freq_pm_notify', (0, 3)),
            ('request_bus_freq', (7,)), ('bus_freq_pm_notify', (0, 4)),
            ('busfreq_reboot_notifier_event', (0, 0, 0))], dict(high_bus_freq_mode=0))
    compare('repeated explicit requests retain stock high-count semantics',
            [('request_bus_freq', (7,)), ('request_bus_freq', (7,)),
             ('release_bus_freq', (7,)), ('release_bus_freq', (0,))])
    for state in (dict(high_bus_freq_mode=0, med_bus_freq_mode=1),
                  dict(high_bus_freq_mode=0, ultra_low_bus_freq_mode=1)):
        compare('other prior mode ' + repr(state), [('request_bus_freq', (7,))], state)
    for active in (0, 1):
        compare('sysfs enable/disable active=' + str(active),
                [('bus_freq_scaling_enable_store', (0, 0, text, len(text)))
                 for text in (b'0\n', b'1\n', b'1anything', b'x', b'', b'0')],
                dict(high_bus_freq_mode=0, bus_freq_scaling_is_active=active))
        compare('idle daemon schedules no lowering active=' + str(active),
                [('bus_freq_daemon_handler', ())],
                dict(high_bus_freq_mode=0, bus_freq_scaling_is_active=active))
    a, _ = compare('explicit high DDR3 ordered clock operations',
                   [('request_bus_freq', (7,))], dict(high_bus_freq_mode=0))
    expected = [
        ['mutex_lock'], ['cancel_delayed_work_sync'],
        ['clk_prepare', 'pll3_clk'], ['clk_enable', 'pll3_clk'],
        ['clk_prepare', 'pll2_400_clk'], ['clk_enable', 'pll2_400_clk'],
        ['clk_set_rate', 'ocram_clk', 264000000], ['clk_set_rate', 'ahb_clk', 132000000],
        ['update_ddr_freq_imx6_up', 400000000],
        ['clk_set_parent', 'periph2_pre_clk', 'pll2_400_clk'],
        ['clk_set_parent', 'periph2_clk', 'periph2_pre_clk'],
        ['clk_set_parent', 'periph2_clk2_sel_clk', 'pll3_clk'],
        ['clk_disable', 'pll2_400_clk'], ['clk_unprepare', 'pll2_400_clk'],
        ['clk_disable', 'pll3_clk'], ['clk_unprepare', 'pll3_clk'], ['mutex_unlock']]
    require(a.trace == expected, 'explicit high transition does not match recovered call sequence')
    # Inactive or wrong-board policy must retain NXP behavior, and the unmodified
    # baseline must fail the stock comparison for the meaningful changed paths.
    for options in (dict(enabled=False), dict(femto=False)):
        for entry, args in [('request_bus_freq', (0,)), ('set_high_bus_freq', (1,))]:
            a, b = Machine(baseline, dict(high_bus_freq_mode=0)), Machine(rebuilt, dict(high_bus_freq_mode=0), **options)
            a.call(entry, *args); b.call(entry, *args)
            require(a.trace == b.trace and a.snapshot() == b.snapshot(), 'NXP fallback changed')
            results.append('NXP fallback ' + repr(options) + ' ' + entry)
    for entry, args in [('request_bus_freq', (0,)), ('set_high_bus_freq', (1,))]:
        a, b = Machine(stock, dict(high_bus_freq_mode=0)), Machine(baseline, dict(high_bus_freq_mode=0))
        a.call(entry, *args); b.call(entry, *args)
        require(a.trace != b.trace, 'negative control failed to detect NXP difference')
        results.append('negative control detects unmodified ' + entry)
    return {'stock_raw_sha256': RAW_HASH, 'rebuilt_kernel_sha256': hashlib.sha256(rebuilt.binary).hexdigest(),
            'baseline_kernel_sha256': hashlib.sha256(baseline.binary).hexdigest(),
            'passed_cases': len(results), 'cases': results, 'runtime_qualified': False,
            'limits': 'Synthetic service calls; DDR transition internals, CPU restoration, scheduler races, physical rates and timing unverified'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stock_kernel', type=Path)
    parser.add_argument('rebuilt_kernel', type=Path)
    parser.add_argument('baseline_kernel', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.stock_kernel, args.rebuilt_kernel, args.baseline_kernel), indent=2))


if __name__ == '__main__':
    main()
