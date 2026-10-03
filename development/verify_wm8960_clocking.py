#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Execute repaired board/codec/SAI ARM setup with modeled register and clock I/O.

Independent rational arithmetic checks PLL output, sample rate, bit clock and
class-D clock. This does not execute ALSA core, DMA or physical audio.
"""
import argparse
import io
import itertools
import json
import copy
import struct
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

from elftools.elf.elffile import ELFFile
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_PC, UC_ARM_REG_LR
from verify_adc_module import Module
from verify_busfreq import Image, STOP, require
from verify_wm8960_lifetime import load, DAI, CODEC, DATA
from verify_wm8960_streams import StreamMachine, ARENA, SUB, CAPTURE, PCM, PARAMS, SAI, CPU_DAI, PRIV
from verify_wm8960_sources import Object, compare, section_bases, names_in, sha
from verify_ddr_sources import bytes_at

REGMAP = ARENA + 0x7000
CODEC_MAP, FORCE_LOCK, FORCE_UNLOCK = ARENA + 0x7200, ARENA + 0x7f00, ARENA + 0x7f04
RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)
DIVIDERS = (Fraction(1), Fraction(3, 2), Fraction(2), Fraction(3), Fraction(4),
            Fraction(11, 2), Fraction(6), Fraction(8), Fraction(11), Fraction(12),
            Fraction(16), Fraction(22), Fraction(24), Fraction(32), Fraction(32), Fraction(32))


def linked_object(module):
    table = list(module.elf.get_section_by_name('.symtab').iter_symbols())
    functions = {s.name for s in table if s['st_info']['type'] == 'STT_FUNC'}
    anchors = {s.name for s in table if s['st_info']['type'] in ('STT_FUNC', 'STT_OBJECT')}
    # With lockdep disabled, mutex_init still takes the address of an empty
    # lock-class key in zero-sized BSS. Its relocation needs a checked base.
    sections = {n: None for n in ('.text', '.init.text', '.exit.text', '.rodata', '.data', '.bss', '.initcall6.init')
                if module.elf.get_section_by_name(n) is not None and
                (module.elf.get_section_by_name(n)['sh_size'] or
                any(s['st_shndx'] == module.elf.get_section_index(n) and
                    s['st_info']['type'] == 'STT_OBJECT' for s in table))}
    return Object(module.binary, functions=functions, sections=sections, anchors=anchors)


def attach(image, module):
    obj = linked_object(module)
    result = compare(obj, image)
    bases = section_bases(obj, names_in(image))
    for s in obj.symbols:
        if s.name and s['st_shndx'] in bases:
            image.symbols[s.name] = bases[s['st_shndx']] + s['st_value']
            if s['st_info']['type'] == 'STT_FUNC':
                image.ranges.append((image.symbols[s.name], image.symbols[s.name] + s['st_size']))
    for name in ('.data', '.bss'):
        index = obj.elf.get_section_index(name)
        if index in bases:
            image.board_writes.append((bases[index], bases[index] + obj.elf.get_section(index)['sh_size']))
    image.services.update({image.symbols[s.name]: s.name for s in obj.symbols
                           if s['st_shndx'] == 'SHN_UNDEF' and s.name in image.symbols})
    return result


def arithmetic(image):
    table = list(ELFFile(io.BytesIO(image.binary)).get_section_by_name('.symtab').iter_symbols())
    addresses = sorted({s['st_value'] for s in table})
    for name in ('__aeabi_idiv', '__aeabi_uidiv', '__aeabi_uidivmod', '__do_div64'):
        symbol = next(s for s in table if s.name == name)
        start = symbol['st_value']
        end = start + symbol['st_size'] if symbol['st_size'] else next(a for a in addresses if a > start)
        image.ranges.append((start, end))
        image.services.pop(start, None)


def attach_force_helper(image, module):
    table = list(module.elf.get_section_by_name('.symtab').iter_symbols())
    names = names_in(image)
    symbol = next(s for s in table if s.name == 'dreem_regmap_write_bits')
    require(len(names[symbol.name]) == 1, 'ambiguous forced-update helper')
    address = next(iter(names[symbol.name]))
    base = address - symbol['st_value']
    section = module.elf.get_section(symbol['st_shndx'])
    start, end = symbol['st_value'], symbol['st_value'] + symbol['st_size']
    expected = bytearray(section.data()[start:end])
    relocations = 0
    for reloc_section in module.elf.iter_sections():
        if reloc_section['sh_type'] != 'SHT_REL' or reloc_section['sh_info'] != symbol['st_shndx']:
            continue
        for relocation in reloc_section.iter_relocations():
            offset = relocation['r_offset']
            if not start <= offset < end:
                continue
            require(relocation['r_info_type'] in (28, 29), 'unexpected force-helper relocation')
            target = table[relocation['r_info_sym']]
            require(target['st_shndx'] == symbol['st_shndx'], 'force helper has unexpected external call')
            word = struct.unpack_from('<I', expected, offset - start)[0]
            addend = (((word & 0xffffff) ^ 0x800000) - 0x800000) << 2
            distance = base + target['st_value'] + addend - (base + offset)
            require(distance % 4 == 0 and -(1 << 25) <= distance < (1 << 25), 'force-helper branch overflow')
            struct.pack_into('<I', expected, offset - start,
                             (word & 0xff000000) | ((distance >> 2) & 0xffffff))
            relocations += 1
    require(bytes(expected) == bytes_at(image, address, len(expected)), 'force-helper linked bytes differ')
    image.ranges.append((address, address + symbol['st_size']))
    for name in ('_regmap_read', '_regmap_write'):
        target = next(s for s in table if s.name == name)
        require(base + target['st_value'] in names[name], 'regmap helper section differs')
        image.services[base + target['st_value']] = name
    image.services.pop(address, None)
    image.services[FORCE_LOCK], image.services[FORCE_UNLOCK] = 'force_lock', 'force_unlock'
    return {'function': symbol.name, 'executed_bytes': symbol['st_size'],
            'validated_relocations': relocations, 'linked_sha256': sha(bytes(expected))}


class ClockMachine(StreamMachine):
    def __init__(self, image, types, *, fail_at=None, persistent=False, **kwargs):
        super().__init__(image, types, **kwargs)
        self.sai_registers, self.sai_trace = {}, []
        self.hardware_registers = {}
        self.force_locked = False
        self.io_count, self.fail_at, self.persistent = 0, fail_at, persistent
        self.field('fsl_sai', SAI, 'regmap', REGMAP)
        self.field('fsl_sai', SAI, 'slots', 2)
        self.field('fsl_sai', SAI, 'slot_width', 32)
        self.field('fsl_sai', SAI, 'is_slave_mode', 1, size=1)
        self.field('wm8960_priv', PRIV, 'regmap', CODEC_MAP)
        self.field('regmap', CODEC_MAP, 'lock', FORCE_LOCK)
        self.field('regmap', CODEC_MAP, 'unlock', FORCE_UNLOCK)
        self.field('regmap', CODEC_MAP, 'lock_arg', CODEC_MAP)

    def call(self, name, *arguments):
        require(len(arguments) <= 8, 'too many ARM arguments')
        for i, value in enumerate(arguments[4:]):
            self.put(STOP - 16 + 4 * i, value)
        return super().call(name, *arguments[:4])

    def code(self, cpu, address, size, extra):
        name = self.stub_addresses.get(address)
        a, b, c, d = [cpu.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
        if name in ('force_lock', 'force_unlock'):
            require(a == CODEC_MAP and self.force_locked == (name == 'force_unlock'), 'regmap lock order violated')
            self.force_locked = name == 'force_lock'
            result = 0
        elif name == 'snd_pcm_hw_constraint_list' and d == self.symbols.get('dreem_wm8960_rate_constraints'):
            require(a == PCM and b == 0 and c == 11, 'wrong codec rate-constraint call')
            require(self.field('snd_pcm_hw_constraint_list', d, 'count') == len(RATES), 'wrong codec rate count')
            rates = self.field('snd_pcm_hw_constraint_list', d, 'list')
            require(struct.unpack('<9I', bytes(cpu.mem_read(rates, 36))) == RATES, 'wrong codec rate list')
            result = -22 if self.hit('codec_constraint') else 0
        elif name in ('snd_soc_dai_set_fmt', 'snd_soc_dai_set_pll', 'snd_soc_dai_set_sysclk',
                    'snd_soc_dai_set_tdm_slot', 'snd_soc_dai_set_bclk_ratio'):
            label = name + (':codec' if a == DAI else ':cpu')
            self.calls.append(label)
            if self.hit(label):
                result = -5
            else:
                require(a in (DAI, CPU_DAI), 'unknown DAI')
                if a == DAI:
                    target = {'snd_soc_dai_set_fmt': 'wm8960_set_dai_fmt',
                              'snd_soc_dai_set_pll': 'wm8960_set_dai_pll',
                              'snd_soc_dai_set_sysclk': 'wm8960_set_dai_sysclk',
                              'snd_soc_dai_set_bclk_ratio': 'wm8960_set_bclk_ratio'}[name]
                else:
                    target = {'snd_soc_dai_set_fmt': 'fsl_sai_set_dai_fmt',
                              'snd_soc_dai_set_sysclk': 'fsl_sai_set_dai_sysclk',
                              'snd_soc_dai_set_tdm_slot': 'fsl_sai_set_dai_tdm_slot'}[name]
                cpu.reg_write(UC_ARM_REG_PC, self.symbols[target])
                return
        elif name in ('regmap_update_bits', 'regmap_write', 'regmap_read'):
            require(a == REGMAP and b < 0xe4, 'unexpected SAI register')
            self.sai_trace.append((name, b, c, d))
            if name == 'regmap_update_bits':
                self.sai_registers[b] = (self.sai_registers.get(b, 0) & ~c) | (d & c)
            elif name == 'regmap_write':
                self.sai_registers[b] = c
            else:
                self.put(c, self.sai_registers.get(b, 0))
            result = 0
        elif name in ('snd_soc_read', 'snd_soc_write', 'snd_soc_update_bits', '_regmap_read', '_regmap_write'):
            self.io_count += 1
            raw = name.startswith('_regmap')
            require(a == (CODEC_MAP if raw else CODEC) and b <= 0x37, 'wrong codec register')
            if raw:
                require(self.force_locked, 'forced RMW without map lock')
            old = self.registers.get(b, 0)
            if name in ('snd_soc_read', '_regmap_read'):
                self.regtrace.append(['read', b, old])
                if raw:
                    self.put(c, old)
                result, changed = (0 if raw else old), False
            elif name in ('snd_soc_write', '_regmap_write'):
                self.regtrace.append(['write', b, c])
                self.registers[b] = c
                result, changed = 0, True
            else:
                self.regtrace.append(['update', b, c, d])
                self.registers[b] = (old & ~c) | (d & c)
                changed = old != self.registers[b]
                result = int(changed)
            # The reviewed regmap writes its cache before sending to the bus.
            # update_bits can then skip an identical value during a retry.
            if self.fail_at is not None and self.io_count >= self.fail_at and (not self.failed or self.persistent):
                self.failed = True
                self.regtrace.append(['injected I/O failure', name, b])
                result = -5
            elif changed:
                self.hardware_registers[b] = self.registers[b]
        else:
            return super().code(cpu, address, size, extra)
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def configure(self, direction=0):
        sub = SUB if direction == 0 else CAPTURE
        board = self.call('imx_hifi_hw_params', sub, PARAMS)
        codec = self.call('wm8960_hw_params', sub, PARAMS, DAI) if board == 0 else None
        sai = self.call('fsl_sai_hw_params', sub, PARAMS, CPU_DAI) if codec == 0 else None
        # Model the reviewed public soc_pcm_hw_params error-unwind order.
        if codec not in (0, None) or sai not in (0, None):
            if sai not in (0, None):
                self.call('wm8960_hw_free', sub, DAI)
            self.call('imx_hifi_hw_free', sub)
        require(not self.force_locked, 'codec setup retained regmap lock')
        return board, codec, sai


def clocks(m, direction):
    regs = m.registers
    require(all(m.hardware_registers.get(r, 0) == regs.get(r, 0)
                for r in (4, 5, 7, 8, 0x1a, 0x1b, 0x34, 0x35, 0x36, 0x37)),
            'codec cache disagrees with modeled hardware after retry')
    factor = Fraction((regs[0x34] & 15) * (1 << 24) +
                      (regs[0x35] << 16) + (regs[0x36] << 8) + regs[0x37], 1 << 24)
    vco = Fraction(m.mclk, 2 if regs[0x34] & 16 else 1) * factor
    sysclk = vco / 4 / (2 if regs[4] & 6 == 4 else 1)
    sample = sysclk / (256 * (1, Fraction(3, 2), 2, 3, 4, Fraction(11, 2), 6)[(regs[4] >> 3) & 7])
    bitclock = sysclk / DIVIDERS[regs[8] & 15]
    dclk = sysclk / (Fraction(3, 2), 2, 3, 4, 6, 8, 12, 16)[(regs[8] >> 6) & 7]
    require(abs(sample - m.rate) / m.rate < Fraction(1, 1000000), 'PLL rate error exceeds 1 ppm')
    require(bitclock / sample == 64 and 90000000 <= vco <= 100000000, 'wrong frame clock or PLL VCO')
    require(700000 <= dclk <= 800000 and regs[4] & 1 and regs[0x1a] & 1, 'wrong class-D or PLL state')
    require((regs[4] >> 3) & 7 == (regs[4] >> 6) & 7, 'ADC and DAC dividers differ')
    require(regs[7] & 12 == {16: 0, 20: 4, 24: 8, 32: 12}[m.width], 'codec sample width differs')
    require(regs[0x1b] & 7 == {8000: 5, 11025: 4, 12000: 4, 16000: 3, 22050: 2,
                             24000: 2, 32000: 1, 44100: 0, 48000: 0}[m.rate], 'ALC rate differs')
    base = 0 if direction == 0 else 0x80
    cr4, cr5 = m.sai_registers[base + 0x10], m.sai_registers[base + 0x14]
    require((cr4 >> 16) & 31 == 1 and (cr4 >> 8) & 31 == 31 and
            (cr5 >> 24) & 31 == 31 and (cr5 >> 16) & 31 == 31 and
            (cr5 >> 8) & 31 == m.width - 1, 'SAI word/frame width mismatch')
    require(['sleep', 250] in m.regtrace, 'PLL selected without upstream settling delay')
    return {'rate': m.rate, 'sysclk': float(sysclk), 'bclk': float(bitclock), 'class_d': float(dclk)}


def verify(image, types):
    cases, matrix = [], []
    for direction, failure in itertools.product((0, 1), (None, 'codec_constraint')):
        m = ClockMachine(image, types, fail=failure)
        require(m.call('wm8960_startup', SUB if direction == 0 else CAPTURE, DAI) ==
                ((-22 & 0xffffffff) if failure else 0) and not m.regtrace,
                'codec rate constraint failed or touched registers')
        cases.append('codec startup rate constraint ' + str((direction, failure)))
    m = ClockMachine(image, types)
    dai = image.symbols['wm8960_dai']
    ops = m.field('snd_soc_dai_driver', dai, 'ops')
    require(m.field('snd_soc_dai_ops', ops, 'startup') == image.symbols['wm8960_startup'], 'codec startup callback not registered')
    for name in ('playback', 'capture'):
        stream = dai + types.members['snd_soc_dai_driver'][name]
        require(m.field('snd_soc_pcm_stream', stream, 'rates') == 0x800000fe and
                m.field('snd_soc_pcm_stream', stream, 'rate_min') == 8000 and
                m.field('snd_soc_pcm_stream', stream, 'rate_max') == 48000, 'codec rate advertisement excludes planned rates')
    cases.append('codec rate mask, bounds and startup callback registration')
    for direction, master in itertools.product((0, 1), (False, True)):
        for failure in (('constraint',) if not master else ()) + ('clk_prepare', 'clk_enable'):
            m = ClockMachine(image, types, master=master, fail=failure)
            require(m.startup(direction) & 0x80000000 and m.failed and
                    not m.board_flag('is_stream_opened', direction) and m.enabled == m.prepared == 0,
                    'failed startup retained MCLK or stream ownership')
            m.fail = None
            require(m.startup(direction) == 0 and m.enabled == m.prepared == 1, 'startup retry failed')
            require(m.startup(direction) == (-16 & 0xffffffff) and m.enabled == 1, 'duplicate start owns extra MCLK')
            for _ in range(2):
                m.call('imx_hifi_shutdown', SUB if direction == 0 else CAPTURE)
            require(m.enabled == m.prepared == 0, 'shutdown did not balance MCLK')
            cases.append('board startup failure/retry and idempotent close ' + str((direction, master, failure)))
    failures = ('snd_soc_dai_set_fmt:cpu', 'snd_soc_dai_set_fmt:codec', 'snd_soc_dai_set_bclk_ratio:codec',
                'snd_soc_dai_set_tdm_slot:cpu', 'snd_soc_dai_set_sysclk:cpu',
                'snd_soc_dai_set_pll:codec', 'snd_soc_dai_set_sysclk:codec')
    for direction, failure in itertools.product((0, 1), failures):
        m = ClockMachine(image, types, fail=failure)
        require(m.startup(direction) == 0 and m.configure(direction) == (-5 & 0xffffffff, None, None) and
                m.failed and not m.board_flag('is_stream_in_use', direction), 'failed board setup published active state')
        m.fail = None
        require(m.configure(direction) == (0, 0, 0), 'board setup failure poisoned retry')
        clocks(m, direction)
        cases.append('board DAI error and retry ' + str((direction, failure)))
    m = ClockMachine(image, types, mclk=0)
    require(m.startup() == 0 and m.configure() == (-22 & 0xffffffff, None, None) and
            not m.board_flag('is_stream_in_use', 0) and 0x34 not in m.registers, 'zero MCLK reached PLL programming')
    cases.append('zero MCLK rejected')
    for rate, width, channels, direction in itertools.product(RATES, (16, 20, 24, 32), (1, 2), (0, 1)):
        m = ClockMachine(image, types, rate=rate, width=width, channels=channels)
        require(m.startup(direction) == 0 and m.configure(direction) == (0, 0, 0),
                'clock setup failed ' + str((rate, width, channels, direction)))
        result = clocks(m, direction)
        if width == 16 and channels == 2 and direction == 0:
            matrix.append(result)
        cases.append('connected board/codec/SAI ' + str((rate, width, channels, direction)))
    m = ClockMachine(image, types)
    require(m.startup() == 0 and m.configure() == (0, 0, 0), 'I/O-count fixture failed')
    count = m.io_count
    for direction, failed_io, persistent in itertools.product((0, 1), range(1, count + 1), (False, True)):
        m = ClockMachine(image, types, fail_at=failed_io, persistent=persistent)
        require(m.startup(direction) == 0, 'I/O fixture start failed')
        result = m.configure(direction)
        require(m.failed and any(x == (-5 & 0xffffffff) for x in result), 'I/O error was discarded')
        require(not m.board_flag('is_stream_in_use', direction) and
                not m.flag('wm8960_priv', PRIV, 'is_stream_in_use', direction), 'failed setup retained active flag')
        m.fail_at = None
        require(m.configure(direction) == (0, 0, 0), 'I/O failure poisoned retry')
        clocks(m, direction)
        cases.append('codec I/O error, unwind and retry ' + str((direction, failed_io, persistent)))
    for direction in (0, 1):
        m = ClockMachine(image, types)
        require(m.startup(direction) == 0 and m.configure(direction) == (0, 0, 0), 'duplex fixture failed')
        require(m.startup(1 - direction) == 0, 'duplex start failed')
        before = list(m.regtrace)
        for rate, width in ((44100, 16), (48000, 24)):
            m.params(rate, width)
            sub = SUB if direction else CAPTURE
            require(m.call('wm8960_hw_params', sub, PARAMS, DAI) == (-16 & 0xffffffff), 'codec accepted conflicting duplex')
            require(m.regtrace == before, 'codec conflict changed registers')
        m.params(48000, 16)
        require(m.configure(1 - direction) == (0, 0, 0) and m.regtrace == before,
                'matching duplex changed shared codec clocks')
        for d in (direction, 1 - direction):
            sub = SUB if d == 0 else CAPTURE
            m.call('wm8960_hw_free', sub, DAI)
            m.call('imx_hifi_hw_free', sub)
            m.call('imx_hifi_shutdown', sub)
        require(m.enabled == m.prepared == 0, 'duplex close leaked MCLK')
        cases.append('duplex isolation and balanced close ' + str(direction))
    for source, target in itertools.product((12000000, 13000000, 19200000, 19200001, 24000000, 26000000, 26000001, 27000000),
                                             (22579200, 24576000)):
        m = ClockMachine(image, types)
        require(m.call('wm8960_set_pll', CODEC, source, target) == 0, 'valid PLL pair rejected')
        regs = m.registers
        factor = Fraction((regs[0x34] & 15) * (1 << 24) + (regs[0x35] << 16) +
                          (regs[0x36] << 8) + regs[0x37], 1 << 24)
        actual = Fraction(source, 2 if regs[0x34] & 16 else 1) * factor / 4
        require(abs(actual - target) / target < Fraction(1, 1000000), 'PLL factors disagree with independent arithmetic')
        ideal = Fraction(target * 4 * (2 if regs[0x34] & 16 else 1), source)
        require(abs(factor - ideal) <= Fraction(1, 2 << 24), 'PLL fraction was not rounded to the nearest representable value')
        cases.append('PLL factors ' + str((source, target)))
    # Arithmetic boundary probes only; these targets are not proposed chip clocks.
    for source, target, expected in ((40000007, 35000006, (1, 7, 0)),
                                     (160000001, 480000002, (0, 12, 0)),
                                     (40000001, 130000003, None)):
        m = ClockMachine(image, types)
        result = m.call('pll_factors', source, target, PARAMS)
        if expected is None:
            require(result == (-22 & 0xffffffff), 'PLL rounding admitted N=13')
        else:
            require(result == 0 and struct.unpack('<3I', bytes(m.cpu.mem_read(PARAMS, 12))) == expected,
                    'PLL rounding lost carry into N')
        require(not m.regtrace, 'factor calculation touched hardware')
        cases.append('PLL rounding boundary ' + str((source, target)))
    for source, target in ((0, 1), (1, 0), (1, 24576000), (19200000, 0xffffffff), (0xffffffff, 24576000)):
        m = ClockMachine(image, types)
        require(m.call('wm8960_set_pll', CODEC, source, target) == (-22 & 0xffffffff) and not m.regtrace,
                'invalid PLL input touched hardware')
        cases.append('invalid PLL pair ' + str((source, target)))
    for direction in (0, 1):
        sub = SUB if direction == 0 else CAPTURE
        m = ClockMachine(image, types)
        require(m.call('fsl_sai_hw_params', sub, PARAMS, CPU_DAI) == 0, 'SAI default fixture failed')
        require((m.sai_registers[(0 if direction == 0 else 0x80) + 0x14] >> 24) & 31 == 15,
                'SAI changed slot size without explicit request')
        require(m.call('fsl_sai_set_dai_tdm_slot', CPU_DAI, 0, 0, 2, 32) == 0 and
                m.call('fsl_sai_hw_params', sub, PARAMS, CPU_DAI) == 0, 'SAI explicit fixture failed')
        require((m.sai_registers[(0 if direction == 0 else 0x80) + 0x14] >> 24) & 31 == 31,
                'SAI ignored requested slot width')
        cases.append('SAI default and explicit slots ' + str(direction))
    return cases, matrix, count


def negative_controls(new, types, stock_path, codec_path, baseline_path, sai_path):
    old = Image(stock_path, True)
    old.ranges, old.board_writes, old.services = [], [], {}
    reference = Module(codec_path)
    codec_match = attach(old, reference)
    arithmetic(old)
    old_types = copy.copy(types)
    old_types.members = dict(types.members, **reference.members)
    old_types.sizes = dict(types.sizes, **reference.sizes)
    a, b = ClockMachine(old, old_types), ClockMachine(new, types)
    require(a.call('wm8960_set_dai_pll', DAI, 1, 0, 12000000, 24576000) == (-22 & 0xffffffff) and
            b.call('wm8960_set_dai_pll', DAI, 1, 0, 12000000, 24576000) == 0,
            'PLL negative control did not distinguish original inverted check')
    a = ClockMachine(old, old_types, width=20)
    for name, value in (('freq_in', 19200000), ('sysclk', 24576000), ('clk_id', 1)):
        a.field('wm8960_priv', PRIV, name, value)
    a.registers[7] = 0x42
    require(a.call('wm8960_hw_params', SUB, PARAMS, DAI) == (-22 & 0xffffffff) and
            a.flag('wm8960_priv', PRIV, 'is_stream_in_use', 0), 'stock active-flag defect not reproduced')
    baseline = Image(baseline_path)
    baseline.ranges, baseline.board_writes, baseline.services = [], [], {}
    raw = sai_path.read_bytes()
    sai_match = attach(baseline, SimpleNamespace(binary=raw, elf=ELFFile(io.BytesIO(raw))))
    arithmetic(baseline)
    a = ClockMachine(baseline, types)
    require(a.call('fsl_sai_set_dai_tdm_slot', CPU_DAI, 0, 0, 2, 32) == 0 and
            a.call('fsl_sai_hw_params', SUB, PARAMS, CPU_DAI) == 0 and
            (a.sai_registers[0x14] >> 24) & 31 == 15, 'original SAI did not reproduce ignored slot width')
    return ['original codec rejects valid PLL pair', 'original codec keeps active flag after failure',
            'original SAI ignores explicit slave slot width'], {'stock_codec': codec_match, 'baseline_sai': sai_match}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kernel', type=Path)
    parser.add_argument('board', type=Path)
    parser.add_argument('codec', type=Path)
    parser.add_argument('sai', type=Path)
    parser.add_argument('regmap', type=Path)
    parser.add_argument('stock_kernel', type=Path)
    parser.add_argument('matched_codec', type=Path)
    parser.add_argument('baseline_kernel', type=Path)
    parser.add_argument('baseline_sai', type=Path)
    args = parser.parse_args()
    board, codec, sai, regmap = (Module(p) for p in (args.board, args.codec, args.sai, args.regmap))
    image = load(args.kernel, board)
    linked = {'codec': attach(image, codec), 'sai': attach(image, sai)}
    forced = attach_force_helper(image, regmap)
    arithmetic(image)
    for module in (codec, sai, regmap):
        board.members.update(module.members)
        board.sizes.update(module.sizes)
    cases, matrix, io_count = verify(image, board)
    negative, references = negative_controls(image, board, args.stock_kernel, args.matched_codec,
                                             args.baseline_kernel, args.baseline_sai)
    cases.extend(negative)
    files = ('verify_wm8960_clocking.py', 'verify_wm8960_streams.py', 'verify_wm8960_lifetime.py',
             'verify_adc_module.py', 'verify_busfreq.py', 'verify_wm8960_sources.py',
             'verify_wm8960_board_sources.py', 'verify_ddr_preparation.py', 'verify_ddr_sources.py', 'arm_relocations.py')
    print(json.dumps({'kernel_sha256': sha(image.binary), 'board_sha256': sha(board.binary),
                      'codec_sha256': sha(codec.binary), 'sai_sha256': sha(sai.binary),
                      'regmap_sha256': sha(regmap.binary), 'force_helper': forced,
                      'linked_object_comparison': linked, 'passed_cases': len(cases), 'cases': cases,
                      'negative_controls': negative, 'reference_comparisons': references,
                      'reference_inputs': {n: sha(getattr(args, n).read_bytes()) for n in
                                           ('stock_kernel', 'matched_codec', 'baseline_kernel', 'baseline_sai')},
                      'clock_matrix': matrix, 'codec_setup_io_operations': io_count,
                      'verifier_sources': {n: sha((Path(__file__).parent / n).read_bytes()) for n in files},
                      'hardware_qualified': False,
                      'limits': 'Actual board/codec/SAI ARM setup and arithmetic; modeled register/clock I/O and ALSA unwind. No PCM DMA, scheduler, electrical timing or physical audio proof.'}, indent=2))


if __name__ == '__main__':
    main()
