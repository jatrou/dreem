#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Execute connected board/codec ARM routines with modeled registers and SAI.

Checks clock ownership, failed board setup, duplex configuration, and exact
codec behavior against the saved firmware. Codec defects are reported, not
treated as hardware qualification. No headset code is installed or executed.
"""
import argparse
import io
import itertools
import json
from pathlib import Path

from elftools.elf.elffile import ELFFile
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_PC, UC_ARM_REG_LR, UC_ARM_REG_SP
from verify_adc_module import Module
from verify_busfreq import Image, STOP, require, RAW_HASH
from verify_wm8960_lifetime import BoardMachine, load, DATA, CPU, CODEC, DAI, RUNTIME, CLOCK
from verify_wm8960_sources import Object, compare, section_bases, names_in, sha
from verify_wm8960_board_sources import board_object

ARENA = 0x52000000
SUB, CAPTURE, PCM, PARAMS, SAI, CPU_DAI, PRIV = (ARENA + n * 0x1000 for n in range(7))
FORMATS = {16: 2, 20: 36, 24: 6, 32: 10}


def attach(image, obj):
    result = compare(obj, image)
    bases = section_bases(obj, names_in(image))
    for s in obj.symbols:
        if s.name and s['st_shndx'] in bases:
            image.symbols[s.name] = bases[s['st_shndx']] + s['st_value']
            if s['st_info']['type'] == 'STT_FUNC':
                image.ranges.append((image.symbols[s.name], image.symbols[s.name] + s['st_size']))
    for name in ('.data', '.bss'):
        index = obj.elf.get_section_index(name)
        image.board_writes.append((bases[index], bases[index] + obj.elf.get_section(index)['sh_size']))
    image.services.update({image.symbols[s.name]: s.name for s in obj.symbols
                           if s['st_shndx'] == 'SHN_UNDEF' and s.name in image.symbols})
    return result


def load_images(stock, kernel, board, codec, matched_board):
    new = load(kernel, board)
    old = Image(stock, True)
    old.ranges, old.board_writes, old.services = [], [], {}
    attach(old, board_object(matched_board.read_bytes()))
    codec_object = Object(codec.binary)
    matches = {'stock_codec': attach(old, codec_object), 'linked_codec': attach(new, codec_object)}
    for image in (old, new):
        elf = ELFFile(io.BytesIO(image.binary))
        table = list(elf.get_section_by_name('.symtab').iter_symbols())
        addresses = sorted({s['st_value'] for s in table})
        for name in ('__aeabi_idiv', '__aeabi_uidiv', '__aeabi_uidivmod', '__do_div64'):
            sym = next(s for s in table if s.name == name)
            start = sym['st_value']
            end = start + sym['st_size'] if sym['st_size'] else next(a for a in addresses if a > start)
            image.ranges.append((start, end))
            image.services.pop(start, None)  # Execute real integer arithmetic helpers.
    return old, new, matches


class StreamMachine(BoardMachine):
    def __init__(self, image, types, *, master=True, fail=None, real_codec=True, rate=48000,
                 width=16, channels=2, mclk=19200000):
        super().__init__(image, types, fail=fail)
        self.cpu.mem_map(ARENA, 0x8000)
        self.cpu.mem_map(DATA, 0x1000)
        self.allocated = True
        self.master, self.real_codec, self.mclk = master, real_codec, mclk
        self.prepared = self.enabled = 0
        self.registers, self.regtrace, self.calls = {}, [], []
        self.field('snd_soc_card', DATA, 'drvdata', DATA)
        self.field('snd_soc_card', DATA, 'dev', self.platform_device())
        self.field('imx_wm8960_data', DATA, 'codec_clk', CLOCK)
        self.field('imx_wm8960_data', DATA, 'is_codec_master', int(master), size=1)
        self.field('snd_soc_pcm_runtime', RUNTIME, 'card', DATA)
        self.field('snd_soc_pcm_runtime', RUNTIME, 'codec_dai', DAI)
        self.field('snd_soc_pcm_runtime', RUNTIME, 'cpu_dai', CPU_DAI)
        self.field('snd_soc_dai', DAI, 'codec', CODEC)
        self.field('snd_soc_dai', CPU_DAI, 'dev', self.platform_device(CPU))
        self.field('device', self.platform_device(CPU), 'driver_data', SAI)
        self.field('snd_soc_codec', CODEC, 'dev', self.codec_device())
        component = CODEC + self.types.members['snd_soc_codec']['component']
        self.field('snd_soc_component', component, 'dev', self.codec_device())
        self.field('device', self.codec_device(), 'driver_data', PRIV)
        self.field('wm8960_priv', PRIV, 'mclk', CLOCK)
        for stream, p in enumerate((SUB, CAPTURE)):
            self.field('snd_pcm_substream', p, 'stream', stream)
            self.field('snd_pcm_substream', p, 'private_data', RUNTIME)
            self.field('snd_pcm_substream', p, 'runtime', PCM)
        self.params(rate, width, channels)

    def params(self, rate, width, channels=2):
        self.rate, self.width, self.channels = rate, width, channels
        self.cpu.mem_write(PARAMS, bytes(self.types.sizes['snd_pcm_hw_params']))
        masks = PARAMS + self.types.members['snd_pcm_hw_params']['masks']
        fmt = FORMATS.get(width, 0)
        self.put(masks + self.types.sizes['snd_mask'] + (fmt // 32) * 4, 1 << (fmt % 32))
        intervals = PARAMS + self.types.members['snd_pcm_hw_params']['intervals']
        for index, value in ((2, channels), (3, rate)):
            p = intervals + index * self.types.sizes['snd_interval']
            self.field('snd_interval', p, 'min', value)
            self.field('snd_interval', p, 'max', value)

    def flag(self, kind, address, name, direction, value=None):
        p = address + self.types.members[kind][name] + (1 if direction == 0 else 0)
        if value is None:
            return int(self.cpu.mem_read(p, 1)[0])
        self.cpu.mem_write(p, bytes([value]))

    def board_flag(self, name, direction, value=None):
        return self.flag('imx_wm8960_data', DATA, name, direction, value)

    def write(self, cpu, access, address, size, value, extra):
        if ARENA <= address and address + size <= ARENA + 0x8000:
            return
        super().write(cpu, access, address, size, value, extra)

    def code(self, cpu, address, size, extra):
        name = self.stub_addresses.get(address)
        a, b, c, d = [cpu.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
        result = 0
        if name in ('clk_prepare', 'clk_enable', 'clk_disable', 'clk_unprepare'):
            require(a == CLOCK, 'wrong stream clock')
            if name == 'clk_prepare':
                result = -5 if self.hit(name) else 0
                if not result:
                    self.prepared += 1
            elif name == 'clk_enable':
                require(self.prepared > self.enabled, 'enable without prepare')
                result = -5 if self.hit(name) else 0
                if not result:
                    self.enabled += 1
            elif name == 'clk_disable':
                require(self.enabled > 0, 'unowned clock disable')
                self.enabled -= 1
            else:
                require(self.prepared > self.enabled, 'unowned clock unprepare')
                self.prepared -= 1
        elif name == 'clk_get_rate':
            require(a == CLOCK and self.enabled > 0, 'rate query without owned MCLK')
            result = self.mclk
        elif name == 'snd_pcm_hw_constraint_list':
            require(a == PCM and b == 0 and c == 11, 'wrong rate constraint')
            result = -22 if self.hit('constraint') else 0
        elif name == 'snd_pcm_format_width':
            result = {v: k for k, v in FORMATS.items()}.get(a, -22)
        elif name == 'snd_soc_params_to_bclk':
            require(a == PARAMS, 'wrong codec parameters')
            result = self.rate * self.width * self.channels
        elif name in ('snd_soc_dai_set_fmt', 'snd_soc_dai_set_pll', 'snd_soc_dai_set_sysclk', 'snd_soc_dai_set_tdm_slot'):
            require(a in (DAI, CPU_DAI), 'unknown audio DAI')
            label = name + (':codec' if a == DAI else ':cpu')
            self.calls.append(label)
            if self.hit(label):
                result = -5
            elif a == DAI and self.real_codec:
                target = {'snd_soc_dai_set_fmt': 'wm8960_set_dai_fmt',
                          'snd_soc_dai_set_pll': 'wm8960_set_dai_pll',
                          'snd_soc_dai_set_sysclk': 'wm8960_set_dai_sysclk'}[name]
                # Tail-dispatch as the ASoC wrapper does; preserve the real fifth
                # PLL argument on the ARM stack and the caller's LR.
                cpu.reg_write(UC_ARM_REG_PC, self.symbols[target])
                return
        elif name in ('snd_soc_read', 'snd_soc_write', 'snd_soc_update_bits'):
            require(a == CODEC and b <= 0x37, 'wrong codec register access')
            if name == 'snd_soc_read':
                result = self.registers.get(b, 0)
                self.regtrace.append(['read', b, result])
            elif name == 'snd_soc_write':
                self.regtrace.append(['write', b, c])
                self.registers[b] = c
            else:
                old = self.registers.get(b, 0)
                new = (old & ~c) | (d & c)
                self.regtrace.append(['update', b, c, d])
                self.registers[b] = new
                result = int(old != new)
        elif name in ('printk', 'dev_err', 'dev_warn'):
            pass
        elif name == 'msleep':
            self.regtrace.append(['sleep', a])
        else:
            return super().code(cpu, address, size, extra)
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def startup(self, direction=0):
        self.flag('fsl_sai', SAI, 'is_stream_opened', direction, 1)
        return self.call('imx_hifi_startup', SUB if direction == 0 else CAPTURE)

    def configure(self, direction=0):
        sub = SUB if direction == 0 else CAPTURE
        board = self.call('imx_hifi_hw_params', sub, PARAMS)
        codec = self.call('wm8960_hw_params', sub, PARAMS, DAI) if board == 0 else None
        return board, codec


def verify(old, new, types):
    cases, matrix = [], []
    for direction in (0, 1):
        sub = SUB if direction == 0 else CAPTURE
        m = StreamMachine(new, types)
        require(m.call('imx_hifi_hw_params', sub, PARAMS) == (-77 & 0xffffffff) and
                not m.calls and not m.regtrace, 'unopened stream touched audio hardware')
        cases.append('unopened stream parameters rejected ' + str(direction))
        for own_cpu, other_cpu, other_board in ((0, 0, 0), (1, 1, 0), (1, 0, 1)):
            m = StreamMachine(new, types)
            m.flag('fsl_sai', SAI, 'is_stream_opened', direction, own_cpu)
            m.flag('fsl_sai', SAI, 'is_stream_opened', 1 - direction, other_cpu)
            m.board_flag('is_stream_opened', 1 - direction, other_board)
            require(m.call('imx_hifi_startup', sub) == (-16 & 0xffffffff) and
                    m.prepared == m.enabled == 0 and not m.board_flag('is_stream_opened', direction),
                    'inconsistent CPU/board ownership acquired a clock')
            cases.append('CPU/board ownership mismatch ' + str((direction, own_cpu, other_cpu, other_board)))
        for rate, width in ((0, 16), (7999, 16), (48001, 16), (0xffffffff, 16), (48000, 8)):
            m = StreamMachine(new, types, rate=rate, width=width)
            require(m.startup(direction) == 0, 'invalid-parameter fixture failed to start')
            require(m.call('imx_hifi_hw_params', sub, PARAMS) == (-22 & 0xffffffff) and
                    not m.calls and not m.regtrace and not m.board_flag('is_stream_in_use', direction),
                    'invalid parameters touched audio hardware')
            m.call('imx_hifi_shutdown', sub)
            require(m.prepared == m.enabled == 0, 'invalid-parameter fixture retained clock')
            cases.append('invalid rate/format rejected ' + str((direction, rate, width)))
        m = StreamMachine(new, types)
        require(m.startup(direction) == 0 and m.configure(direction) == (0, 0), 'free-failure fixture failed')
        m.fail = 'snd_soc_dai_set_fmt:codec'
        require(m.call('imx_hifi_hw_free', sub) == (-5 & 0xffffffff) and m.failed and
                not m.board_flag('is_stream_in_use', direction), 'free discarded codec error or retained active state')
        m.call('imx_hifi_shutdown', sub)
        require(m.prepared == m.enabled == 0, 'failed free retained clock at shutdown')
        cases.append('free error propagates and shutdown balances clock ' + str(direction))
    for direction, master in itertools.product((0, 1), (False, True)):
        for failure in (('constraint',) if not master else ()) + ('clk_prepare', 'clk_enable'):
            m = StreamMachine(new, types, master=master, fail=failure)
            require(m.startup(direction) & 0x80000000 and m.failed, 'startup failure not injected')
            require(m.board_flag('is_stream_opened', direction) == 0 and m.prepared == m.enabled == 0,
                    'failed startup retained stream or clock ownership')
            m.fail = None
            require(m.startup(direction) == 0 and m.prepared == m.enabled == 1, 'startup retry failed')
            require(m.startup(direction) == (-16 & 0xffffffff) and m.enabled == 1, 'duplicate startup acquired MCLK')
            for _ in range(2):
                m.call('imx_hifi_shutdown', SUB if direction == 0 else CAPTURE)
            require(m.prepared == m.enabled == 0 and not m.board_flag('is_stream_opened', direction),
                    'shutdown was not balanced/idempotent')
            cases.append('startup rollback/retry/idempotent close ' + str((direction, master, failure)))
    for master in (False, True):
        failures = ['snd_soc_dai_set_fmt:cpu', 'snd_soc_dai_set_fmt:codec', 'snd_soc_dai_set_sysclk:cpu']
        failures += ['snd_soc_dai_set_pll:codec', 'snd_soc_dai_set_sysclk:codec'] if master else ['snd_soc_dai_set_tdm_slot:cpu']
        for failure in failures:
            m = StreamMachine(new, types, master=master, fail=failure, real_codec=False)
            require(m.startup() == 0, 'parameter fixture failed to start')
            require(m.call('imx_hifi_hw_params', SUB, PARAMS) == (-5 & 0xffffffff) and m.failed and
                    not m.board_flag('is_stream_in_use', 0), 'failed parameters published active stream')
            m.fail = None
            require(m.call('imx_hifi_hw_params', SUB, PARAMS) == 0 and m.board_flag('is_stream_in_use', 0),
                    'parameter retry failed')
            require(m.call('imx_hifi_hw_free', SUB) == 0, 'parameter fixture cannot free')
            m.call('imx_hifi_shutdown', SUB)
            require(m.prepared == m.enabled == 0, 'parameter fixture retained clock')
            cases.append('board parameter failure and retry ' + str((master, failure)))
    for rate, width, channels, direction in itertools.product((8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000),
                                                              (16, 20, 24, 32), (1, 2), (0, 1)):
        a = StreamMachine(old, types, rate=rate, width=width, channels=channels)
        b = StreamMachine(new, types, rate=rate, width=width, channels=channels)
        require(a.startup(direction) == b.startup(direction) == 0, 'clock matrix startup failed')
        ar, br = a.configure(direction), b.configure(direction)
        require(ar == br and a.regtrace == b.regtrace and a.registers == b.registers,
                'codec behavior diverged from saved firmware: ' + str((rate, width, channels, direction)))
        if br[0]:
            require(a.board_flag('is_stream_in_use', direction) == 1 and b.board_flag('is_stream_in_use', direction) == 0,
                    'failed board setup state was not repaired')
        if channels == 2 and direction == 0:
            matrix.append({'rate': rate, 'width': width, 'board_result': br[0], 'codec_result': br[1]})
        cases.append('connected stock/research clock trace ' + str((rate, width, channels, direction)))
    m = StreamMachine(new, types)
    require(m.startup() == 0 and m.configure() == (0, 0), '48 kHz 16-bit fixture failed')
    require({k: m.registers[k] for k in (4, 8, 0x34, 0x35, 0x36, 0x37)} ==
            {4: 5, 8: 7, 0x34: 0x3a, 0x35: 0x3d, 0x36: 0x70, 0x37: 0xa4},
            'unexpected PLL/SYSCLK/BCLK divisors for the recorded 19.2 MHz clock')
    require(m.startup(1) == 0 and m.prepared == m.enabled == 2, 'duplex start did not own two clock references')
    for rate, width in ((44100, 16), (48000, 24)):
        before = list(m.regtrace)
        m.params(rate, width)
        require(m.call('imx_hifi_hw_params', CAPTURE, PARAMS) == (-22 & 0xffffffff) and
                not m.board_flag('is_stream_in_use', 1) and m.regtrace == before,
                'conflicting duplex setup altered active stream')
    m.params(48000, 16)
    require(m.configure(1) == (0, 0), 'matching duplex setup failed')
    for direction in (0, 1):
        sub = SUB if direction == 0 else CAPTURE
        require(m.call('imx_hifi_hw_free', sub) == m.call('wm8960_hw_free', sub, DAI) == 0,
                'duplex free failed')
        m.call('imx_hifi_shutdown', sub)
    require(m.prepared == m.enabled == 0, 'duplex shutdown retained clocks')
    cases.append('known divisors, duplex conflict isolation and balanced stop')
    m = StreamMachine(new, types, mclk=0)
    require(m.startup() == 0 and m.call('imx_hifi_hw_params', SUB, PARAMS) == (-22 & 0xffffffff) and
            not m.board_flag('is_stream_in_use', 0), 'missing MCLK treated as a PLL-disable request')
    cases.append('zero MCLK rejected before PLL programming')
    for failure in ('clk_prepare', 'clk_enable'):
        m = StreamMachine(old, types, fail=failure)
        require(m.startup() == (-5 & 0xffffffff) and m.board_flag('is_stream_opened', 0) == 1,
                'stock negative control failed to reproduce poisoned startup state')
        cases.append('stock negative control retains failed startup state ' + failure)
    # The source-matched codec still marks itself active on clock-configuration
    # failure; ASoC's failing-codec path does not call that codec's hw_free.
    m = StreamMachine(new, types, width=20)
    require(m.startup() == 0 and m.configure() == (0, -22 & 0xffffffff) and
            m.flag('wm8960_priv', PRIV, 'is_stream_in_use', 0) == 1,
            'remaining codec failure state was not reproduced')
    cases.append('remaining codec defect: failed clock setup retains active flag')
    return cases, matrix


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stock_kernel', type=Path)
    parser.add_argument('research_kernel', type=Path)
    parser.add_argument('board_object', type=Path)
    parser.add_argument('codec_object', type=Path)
    parser.add_argument('matched_board_object', type=Path)
    args = parser.parse_args()
    board, codec = Module(args.board_object), Module(args.codec_object)
    old, new, matches = load_images(args.stock_kernel, args.research_kernel, board, codec, args.matched_board_object)
    board.members.update(codec.members)
    board.sizes.update(codec.sizes)
    cases, matrix = verify(old, new, board)
    files = ('verify_wm8960_streams.py', 'verify_wm8960_lifetime.py', 'verify_adc_module.py',
             'verify_busfreq.py', 'verify_wm8960_sources.py', 'verify_wm8960_board_sources.py',
             'verify_ddr_preparation.py', 'verify_ddr_sources.py', 'arm_relocations.py')
    print(json.dumps({'stock_raw_sha256': RAW_HASH, 'kernel_sha256': sha(new.binary),
                      'board_object_sha256': sha(board.binary), 'codec_object_sha256': sha(codec.binary),
                      'matched_board_object_sha256': sha(args.matched_board_object.read_bytes()),
                      'codec_source_matches': matches, 'passed_cases': len(cases), 'cases': cases,
                      'clock_matrix_at_19_2_mhz': matrix,
                      'verifier_sources': {n: sha((Path(__file__).parent / n).read_bytes()) for n in files},
                      'hardware_qualified': False,
                      'limits': 'Modeled SAI, clocks and register I/O; real board/codec and integer division instructions. No ALSA core, scheduler, analog audio or electrical timing proof. Codec defects remain.'}, indent=2))


if __name__ == '__main__':
    main()
