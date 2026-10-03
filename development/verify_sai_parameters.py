#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Execute SAI configuration and clock ownership in linked ARM instructions.

Clock services, cached register I/O, locks and ALSA unwind ordering are modeled.
No physical registers, trigger, DMA or power transitions are exercised.
"""
import argparse
import itertools
import json
from pathlib import Path

from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_PC, UC_ARM_REG_LR
from verify_adc_module import Module
from verify_busfreq import require
from verify_wm8960_lifetime import load, DAI
from verify_wm8960_clocking import attach, attach_force_helper, arithmetic
from verify_sai_lifetime import SaiMachine, SAI_LOCK, SAI_UNLOCK
from verify_wm8960_streams import ARENA, SAI, SUB, CAPTURE, PARAMS, CPU_DAI
from verify_wm8960_sources import sha

MCLKS = tuple(ARENA + 0x7900 + i * 16 for i in range(4))
ERROR = -5 & 0xffffffff


class ParameterMachine(SaiMachine):
    def __init__(self, image, types, **kwargs):
        super().__init__(image, types, **kwargs)
        self.mclk_rates = [12288000, 24576000, 0, 0]
        self.mclk_prepared, self.mclk_enabled = [0] * 4, [0] * 4
        for i, clock in enumerate(MCLKS):
            self.put(SAI + types.members['fsl_sai']['mclk_clk'] + i * 4, clock)

    def code(self, cpu, address, size, extra):
        name = self.stub_addresses.get(address)
        a = cpu.reg_read(UC_ARM_REG_R0)
        if a not in MCLKS or name not in ('clk_get_rate', 'clk_prepare', 'clk_enable', 'clk_disable', 'clk_unprepare'):
            return super().code(cpu, address, size, extra)
        i = MCLKS.index(a)
        result = 0
        if name == 'clk_get_rate':
            result = self.mclk_rates[i]
        elif name == 'clk_prepare':
            result = -5 if self.hit('mclk_prepare') else 0
            if not result:
                self.mclk_prepared[i] += 1
        elif name == 'clk_enable':
            require(self.mclk_prepared[i] > self.mclk_enabled[i], 'MCLK enabled without preparation')
            result = -5 if self.hit('mclk_enable') else 0
            if not result:
                self.mclk_enabled[i] += 1
        elif name == 'clk_disable':
            require(self.mclk_enabled[i] > 0, 'unowned MCLK disabled')
            self.mclk_enabled[i] -= 1
        else:
            require(self.mclk_prepared[i] > self.mclk_enabled[i], 'unowned MCLK unprepared')
            self.mclk_prepared[i] -= 1
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def setup(self, direction=0, master=False, sync=(False, True)):
        require(self.cpu_open(direction) == 0, 'CPU open failed')
        self.cpu.mem_write(SAI + self.types.members['fsl_sai']['synchronous'], bytes(sync))
        require(self.call('fsl_sai_set_dai_fmt', CPU_DAI, 0x4001 if master else 0x1001) == 0,
                'format fixture failed')
        require(self.call('fsl_sai_set_dai_tdm_slot', CPU_DAI, 0, 0, 2, 32) == 0,
                'slot fixture failed')

    def configure_cpu(self, direction=0):
        return self.call('fsl_sai_hw_params', SUB if direction == 0 else CAPTURE, PARAMS, CPU_DAI)

    def configured(self, direction):
        return self.flag('fsl_sai', SAI, 'dreem_configured', direction)

    def free_cpu(self, direction=0):
        require(self.call('fsl_sai_hw_free', SUB if direction == 0 else CAPTURE, CPU_DAI) == 0,
                'CPU free failed')

    def released(self):
        require(not any(self.mclk_enabled + self.mclk_prepared) and
                not self.field('fsl_sai', SAI, 'mclk_streams'), 'CPU MCLK reference leaked')

    def registers_agree(self):
        require(self.sai_cache == self.sai_registers, 'SAI retry left cache/hardware disagreement')


def verify(image, types):
    cases = []
    for fmt, inv, mode, lsb in itertools.product((1, 3, 4, 5), (0, 2, 3, 4), (1, 2, 3, 4), (0, 1)):
        m = ParameterMachine(image, types)
        require(m.cpu_open(0) == 0, 'format open failed')
        m.field('fsl_sai', SAI, 'is_lsb_first', lsb, size=1)
        value = fmt | (inv << 8) | (mode << 12)
        require(m.call('fsl_sai_set_dai_fmt', CPU_DAI, value) == 0, 'supported format rejected')
        for base in (0, 0x80):
            cr2, cr4 = m.sai_registers[base + 8], m.sai_registers[base + 16]
            require(bool(cr2 & (1 << 25)) == (inv not in (3, 4)) and
                    bool(cr2 & (1 << 24)) == (mode in (2, 4)) and
                    bool(cr4 & 1) == (mode in (3, 4)) and
                    bool(cr4 & 2) == ((fmt == 1) != (inv in (2, 4))) and
                    bool(cr4 & 8) == (fmt in (1, 4)) and bool(cr4 & 16) == (not lsb),
                    'format register meaning differs')
        require(m.field('fsl_sai', SAI, 'is_dsp_mode', size=1) == (fmt in (4, 5)) and
                m.field('fsl_sai', SAI, 'is_slave_mode', size=1) == (mode in (1, 3)),
                'published mode differs')
        m.cpu_close(0)
        cases.append('format/inversion/master/bit-order ' + str((fmt, inv, mode, lsb)))
    m = ParameterMachine(image, types)
    m.setup()
    require(m.call('fsl_sai_set_dai_fmt', CPU_DAI, 0x1004) == 0 and
            m.call('fsl_sai_set_dai_fmt', CPU_DAI, 0x1001) == 0 and
            not m.field('fsl_sai', SAI, 'is_dsp_mode', size=1), 'DSP state persisted after I2S change')
    cases.append('DSP to I2S clears DSP state')
    for fmt in (0, 2, 0x1101, 0x5001):
        before = bytes(m.cpu.mem_read(SAI, types.sizes['fsl_sai']))
        registers = dict(m.sai_registers)
        require(m.call('fsl_sai_set_dai_fmt', CPU_DAI, fmt) == (-22 & 0xffffffff) and
                bytes(m.cpu.mem_read(SAI, len(before))) == before and m.sai_registers == registers,
                'invalid format changed state or registers')
        cases.append('invalid format ' + str(fmt))
    for rate, width, channels in ((0, 16, 2), (192001, 16, 2), (48000, 8, 2),
                                  (48000, 16, 0), (48000, 16, 3)):
        m = ParameterMachine(image, types)
        m.setup()
        m.params(rate, width, channels)
        m.sai_io_count = 0
        require(m.configure_cpu() == (-22 & 0xffffffff) and not m.sai_io_count and
                not m.configured(0), 'invalid parameters accessed hardware')
        m.released()
        cases.append('invalid parameters ' + str((rate, width, channels)))
    m = ParameterMachine(image, types)
    require(m.configure_cpu() == (-77 & 0xffffffff) and not m.sai_io_count,
            'unopened parameters accessed hardware')
    cases.append('unopened parameters rejected')
    for operation, io_count in (('format', 8), ('sysclk', 4)):
        for index, persistent in itertools.product(range(1, io_count + 1), (False, True)):
            m = ParameterMachine(image, types)
            m.setup()
            before = (m.field('fsl_sai', SAI, 'is_slave_mode', size=1),
                      m.field('fsl_sai', SAI, 'is_dsp_mode', size=1))
            m.sai_io_count = 0
            m.sai_fail_at, m.sai_persistent = index, persistent
            args = ('fsl_sai_set_dai_fmt', CPU_DAI, 0x4004) if operation == 'format' else (
                'fsl_sai_set_dai_sysclk', CPU_DAI, 2, 0, 1)
            require(m.call(*args) == ERROR and m.sai_failed, 'register setup error was discarded')
            require(before == (m.field('fsl_sai', SAI, 'is_slave_mode', size=1),
                               m.field('fsl_sai', SAI, 'is_dsp_mode', size=1)), 'partial format published')
            m.sai_fail_at = None
            require(m.call(*args) == 0, 'register setup retry failed')
            m.registers_agree()
            m.cpu_close(0)
            cases.append('checked setup and forced retry ' + str((operation, index, persistent)))
    for master, direction, sync in itertools.product((False, True), (0, 1), ((False, False), (False, True), (True, False))):
        good = ParameterMachine(image, types)
        good.setup(direction, master, sync)
        good.sai_io_count = 0
        require(good.configure_cpu(direction) == 0 and good.configured(direction), 'parameter fixture failed')
        count = good.sai_io_count
        require(sum(good.mclk_enabled) == int(master), 'wrong parameter clock count')
        require(good.cpu_open(direction) == (-16 & 0xffffffff) and good.configured(direction) and
                sum(good.mclk_enabled) == int(master), 'duplicate open released configured resources')
        if master:
            source_tx = not sync[1] if any(sync) else direction == 0
            cr2 = good.sai_registers[8 if source_tx else 0x88]
            rate = good.mclk_rates[(cr2 >> 26) & 3] // (2 * ((cr2 & 255) + 1))
            require(rate == 48000 * 64, 'CPU BCLK differs from frame rate')
        good.free_cpu(direction)
        good.free_cpu(direction)
        good.released()
        cases.append('parameter success/free ' + str((master, direction, sync)))
        for index, persistent in itertools.product(range(1, count + 1), (False, True)):
            m = ParameterMachine(image, types)
            m.setup(direction, master, sync)
            m.sai_io_count = 0
            m.sai_fail_at, m.sai_persistent = index, persistent
            require(m.configure_cpu(direction) == ERROR and m.sai_failed and not m.configured(direction),
                    'failed parameters falsely succeeded or published ownership')
            m.released()
            m.sai_fail_at = None
            require(m.configure_cpu(direction) == 0 and m.configured(direction), 'parameter retry failed')
            m.registers_agree()
            m.cpu_close(direction)  # Also covers close without a preceding hw_free.
            m.released()
            m.ownership(set())
            cases.append('parameter I/O failure/cleanup/retry ' + str((master, direction, sync, index, persistent)))
    for failure in ('mclk_prepare', 'mclk_enable'):
        m = ParameterMachine(image, types, fail=failure)
        m.setup(master=True)
        require(m.configure_cpu() == ERROR and m.failed and not m.configured(0), 'MCLK error was lost')
        m.released()
        m.fail = None
        require(m.configure_cpu() == 0, 'MCLK retry failed')
        m.free_cpu()
        m.released()
        cases.append('MCLK failure ' + failure)
    for first, master in itertools.product((0, 1), (False, True)):
        m = ParameterMachine(image, types)
        m.setup(first, master)
        require(m.cpu_open(1 - first) == 0 and m.configure_cpu(first) == 0, 'duplex fixture failed')
        snapshot = dict(m.sai_registers)
        m.params(44100, 16)
        require(m.configure_cpu(1 - first) == (-16 & 0xffffffff) and m.sai_registers == snapshot,
                'conflicting duplex setup changed peer registers')
        m.params(48000, 16)
        require(m.configure_cpu(1 - first) == 0 and m.configure_cpu(first) == 0,
                'matching duplex or repeated parameters failed')
        require(sum(m.mclk_enabled) == 2 * int(master), 'duplex/repeated parameters lost clock ownership')
        require(m.call('fsl_sai_set_dai_fmt', CPU_DAI, 0x1004) == (-16 & 0xffffffff) and
                m.call('fsl_sai_set_dai_tdm_slot', CPU_DAI, 0, 0, 2, 16) == (-16 & 0xffffffff),
                'live format/slot changes accepted')
        m.cpu_close(first)
        require(sum(m.mclk_enabled) == int(master) and m.configured(1 - first), 'peer ownership released')
        m.cpu_close(1 - first)
        m.released()
        cases.append('duplex configuration/clock isolation ' + str((first, master)))
    m = ParameterMachine(image, types)
    m.setup(master=True)
    require(m.configure_cpu() == 0, 'owned-clock fixture failed')
    m.field('fsl_sai', SAI, 'is_slave_mode', 1, size=1)
    m.put(SAI + types.members['fsl_sai']['mclk_id'] + 4, 1)
    m.free_cpu()
    m.released()
    cases.append('free releases owned clock despite mutable mode/selector')
    # Connected callbacks execute the board and codec's real unwind routines.
    m = ParameterMachine(image, types)
    require(m.startup() == 0, 'connected open failed')
    m.sai_io_count = 0
    require(m.configure() == (0, 0, 0), 'connected parameters failed')
    count = m.sai_io_count
    for index in range(1, count + 1):
        m = ParameterMachine(image, types)
        require(m.startup() == 0, 'connected failure fixture failed')
        m.sai_io_count, m.sai_fail_at = 0, index
        require(ERROR in m.configure() and m.sai_failed and not m.configured(0) and
                not m.board_flag('is_stream_in_use', 0), 'connected error did not unwind board/CPU')
        m.released()
        m.sai_fail_at = None
        require(m.configure() == (0, 0, 0), 'connected setup retry failed')
        m.free_cpu()
        m.call('wm8960_hw_free', SUB, DAI)
        m.call('imx_hifi_hw_free', SUB)
        m.close(0)
        m.released()
        m.ownership(set())
        require(m.enabled == m.prepared == 0, 'connected close leaked board MCLK')
        cases.append('connected board/codec/CPU error unwind/retry ' + str(index))
    return cases


def load_build(path, types=None):
    kernel = path / 'kernel'
    modules = [Module(kernel / name) for name in ('sound/soc/fsl/imx-wm8960.o',
               'sound/soc/codecs/wm8960.o', 'sound/soc/fsl/fsl_sai.o', 'drivers/base/regmap/regmap.o')]
    board, codec, sai, regmap = modules
    image = load(kernel / 'vmlinux', board)
    linked = {'codec': attach(image, codec), 'sai': attach(image, sai)}
    forced = attach_force_helper(image, regmap)
    arithmetic(image)
    image.services.update({SAI_LOCK: 'sai_map_lock', SAI_UNLOCK: 'sai_map_unlock'})
    for obj in modules[1:]:
        board.members.update(obj.members)
        board.sizes.update(obj.sizes)
    return image, types or board, linked, forced


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build', type=Path)
    parser.add_argument('previous_build', type=Path)
    args = parser.parse_args()
    image, types, linked, forced = load_build(args.build)
    cases = verify(image, types)
    old, _, previous, _ = load_build(args.previous_build, types)
    negative = []
    m = ParameterMachine(old, types)
    m.setup()
    m.sai_io_count, m.sai_fail_at = 0, 1
    require(m.configure_cpu() == 0 and m.sai_failed, 'old ignored parameter error not reproduced')
    negative.append('old SAI discards parameter register error')
    m = ParameterMachine(old, types)
    m.setup()
    require(m.call('fsl_sai_set_dai_fmt', CPU_DAI, 0x1004) == 0 and
            m.call('fsl_sai_set_dai_fmt', CPU_DAI, 0x1001) == 0 and
            m.field('fsl_sai', SAI, 'is_dsp_mode', size=1), 'old stale DSP mode not reproduced')
    negative.append('old SAI retains DSP mode after I2S selection')
    m = ParameterMachine(old, types)
    m.setup(master=True)
    require(m.configure_cpu() == 0 and sum(m.mclk_enabled) == 1, 'old MCLK fixture failed')
    require(m.call('fsl_sai_set_dai_fmt', CPU_DAI, 0x1001) == 0, 'old format change failed')
    m.free_cpu()
    require(sum(m.mclk_enabled) == 1, 'old mode-dependent clock leak not reproduced')
    negative.append('old free leaks MCLK after mode change')
    cases.extend(negative)
    files = ('verify_sai_parameters.py', 'verify_sai_lifetime.py', 'verify_wm8960_clocking.py',
             'verify_wm8960_streams.py', 'verify_wm8960_lifetime.py', 'verify_adc_module.py',
             'verify_busfreq.py', 'verify_wm8960_sources.py', 'verify_wm8960_board_sources.py',
             'verify_ddr_preparation.py', 'verify_ddr_sources.py', 'arm_relocations.py')
    print(json.dumps({'kernel_sha256': sha(image.binary), 'previous_kernel_sha256': sha(old.binary),
                      'passed_cases': len(cases), 'cases': cases, 'negative_controls': negative,
                      'linked_objects': linked, 'previous_linked_objects': previous, 'force_helper': forced,
                      'verifier_sources': {n: sha((Path(__file__).parent / n).read_bytes()) for n in files},
                      'hardware_qualified': False,
                      'limits': 'Compiled SAI setup/free and connected callbacks; modeled register bus/cache, clocks, locks and ALSA unwind. No trigger, DMA, concurrent scheduler or physical audio.'}, indent=2))


if __name__ == '__main__':
    main()
