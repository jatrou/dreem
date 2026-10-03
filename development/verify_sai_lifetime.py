#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Execute linked SAI startup/shutdown and connected audio open/unwind callbacks.

Clocks, runtime-PM services, register cache/bus and ALSA call ordering are models.
This does not execute the ALSA core, scheduler, PCM DMA or physical headset.
"""
import argparse
import io
import itertools
import json
from pathlib import Path
from types import SimpleNamespace

from elftools.elf.elffile import ELFFile
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_PC, UC_ARM_REG_LR
from verify_adc_module import Module
from verify_busfreq import Image, require
from verify_wm8960_lifetime import load, CPU, DAI
from verify_wm8960_clocking import ClockMachine, attach, attach_force_helper, arithmetic, clocks, REGMAP
from verify_wm8960_streams import ARENA, SUB, CAPTURE, PCM, PARAMS, SAI, CPU_DAI
from verify_wm8960_sources import sha

BUS_CLOCK, SAI_LOCK, SAI_UNLOCK = ARENA + 0x7e00, ARENA + 0x7f08, ARENA + 0x7f0c


class ProbeInitialized(Exception):
    """Stop the bounded probe check before device-tree/hardware setup."""


class SaiMachine(ClockMachine):
    def __init__(self, image, types, *, sai_fail_at=None, sai_persistent=False,
                 resume_result=0, idle_result=0, external_pm_references=0, **kwargs):
        super().__init__(image, types, **kwargs)
        self.bus_prepared = self.bus_enabled = 0
        self.stream_locked = self.sai_map_locked = False
        self.sai_fail_at, self.sai_persistent = sai_fail_at, sai_persistent
        self.sai_io_count, self.sai_failed = 0, False
        self.sai_cache, self.sai_events = {}, []
        self.diagnostics = []
        self.resume_result, self.idle_result = resume_result, idle_result
        self.field('fsl_sai', SAI, 'pdev', CPU)
        self.field('fsl_sai', SAI, 'bus_clk', BUS_CLOCK)
        self.field('regmap', REGMAP, 'lock', SAI_LOCK)
        self.field('regmap', REGMAP, 'unlock', SAI_UNLOCK)
        self.field('regmap', REGMAP, 'lock_arg', REGMAP)
        self.pm = self.platform_device(CPU) + self.types.members['device']['power']
        self.pm_usage = self.pm + self.types.members['dev_pm_info']['usage_count']
        self.external_pm_references = external_pm_references
        self.put(self.pm_usage, external_pm_references)
        self.lock_address = SAI + self.types.members['fsl_sai']['dreem_stream_lock']

    def usage(self):
        return int.from_bytes(self.cpu.mem_read(self.pm_usage, 4), 'little', signed=True)

    def code(self, cpu, address, size, extra):
        name = self.stub_addresses.get(address)
        a, b, c, d = [cpu.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
        result = 0
        if name == 'devm_kmalloc':
            require(a == self.platform_device(CPU) and b == self.types.sizes['fsl_sai'] and c & 0x8000,
                    'SAI probe allocation differs')
            result = 0 if self.hit('sai_allocation') else SAI
            if result:
                cpu.mem_write(SAI, bytes(b))
        elif name == '__mutex_init':
            require(a == self.lock_address and self.field('fsl_sai', SAI, 'pdev') == CPU and
                    self.string(b) == '&sai->dreem_stream_lock', 'SAI probe did not initialize its per-device lock')
            raise ProbeInitialized()
        elif name == 'dev_err':
            self.diagnostics.append(self.string(b))
        elif name in ('mutex_lock', 'mutex_unlock') and a == self.lock_address:
            require(self.stream_locked == (name == 'mutex_unlock'), 'SAI stream lock imbalance')
            self.stream_locked = name == 'mutex_lock'
            self.sai_events.append(name)
        elif name in ('sai_map_lock', 'sai_map_unlock'):
            require(a == REGMAP and self.sai_map_locked == (name == 'sai_map_unlock'), 'SAI regmap lock imbalance')
            self.sai_map_locked = name == 'sai_map_lock'
        elif name in ('__pm_runtime_resume', '__pm_runtime_idle'):
            require(a == self.platform_device(CPU) and b == 4, 'wrong SAI PM call')
            self.sai_events.append(name)
            usage = self.usage() + (1 if name == '__pm_runtime_resume' else -1)
            require(usage >= 0, 'SAI released an unowned PM reference')
            self.put(self.pm_usage, usage)
            if name == '__pm_runtime_resume':
                result = -5 if self.hit('pm_resume') else self.resume_result
            else:
                require(self.bus_enabled == self.bus_prepared, 'PM release before clock cleanup')
                result = self.idle_result
        elif name in ('clk_prepare', 'clk_enable', 'clk_disable', 'clk_unprepare') and a == BUS_CLOCK:
            self.sai_events.append(name)
            require(self.usage() > 0, 'bus clock access without PM reference')
            if name == 'clk_prepare':
                result = -5 if self.hit('bus_prepare') else 0
                if not result:
                    self.bus_prepared += 1
            elif name == 'clk_enable':
                require(self.bus_prepared > self.bus_enabled, 'SAI enable without prepare')
                result = -5 if self.hit('bus_enable') else 0
                if not result:
                    self.bus_enabled += 1
            elif name == 'clk_disable':
                require(self.bus_enabled > 0, 'SAI disabled an unowned clock')
                self.bus_enabled -= 1
            else:
                require(self.bus_prepared > self.bus_enabled, 'SAI unprepared an unowned clock')
                self.bus_prepared -= 1
        elif name == 'snd_pcm_hw_constraint_list' and d == self.symbols.get('fsl_sai_rate_constraints'):
            require(a == PCM and b == 0 and c == 11 and self.bus_enabled > 0, 'wrong SAI constraint call')
            self.sai_events.append('rate_constraint')
            require(self.field('snd_pcm_hw_constraint_list', d, 'count') == 14, 'SAI rate count changed')
            result = -22 if self.hit('sai_constraint') else 0
        elif a == REGMAP and name in ('_regmap_read', '_regmap_write', 'regmap_update_bits', 'regmap_write', 'regmap_read'):
            require(self.bus_enabled > 0 and b < 0xe4 and b % 4 == 0, 'SAI I/O without bus clock or invalid register')
            raw = name.startswith('_regmap')
            require(not raw or self.sai_map_locked, 'SAI forced RMW without map lock')
            self.sai_io_count += 1
            self.sai_events.append((name, b))
            old = self.sai_cache.get(b, 0)
            changed = False
            if name in ('regmap_read', '_regmap_read'):
                self.put(c, old)
            elif name in ('regmap_write', '_regmap_write'):
                self.sai_cache[b] = c
                changed = True
            else:
                self.sai_cache[b] = (old & ~c) | (d & c)
                changed = self.sai_cache[b] != old
            if (self.sai_fail_at is not None and self.sai_io_count >= self.sai_fail_at and
                    (not self.sai_failed or self.sai_persistent)):
                self.sai_failed = True
                result = -5
            elif changed:
                self.sai_registers[b] = self.sai_cache[b]
        else:
            return super().code(cpu, address, size, extra)
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def call(self, name, *args):
        result = super().call(name, *args)
        require(not self.stream_locked and not self.sai_map_locked, 'SAI callback retained a lock')
        return result

    def cpu_open(self, direction):
        return self.call('fsl_sai_startup', SUB if direction == 0 else CAPTURE, CPU_DAI)

    def cpu_close(self, direction):
        self.call('fsl_sai_shutdown', SUB if direction == 0 else CAPTURE, CPU_DAI)

    def ownership(self, opened):
        require(self.bus_prepared == self.bus_enabled == len(opened) and
                self.usage() == len(opened) + self.external_pm_references,
                'SAI resources leaked or under-released')
        for direction in (0, 1):
            require(self.flag('fsl_sai', SAI, 'is_stream_opened', direction) == int(direction in opened), 'SAI ownership differs from resources')

    def startup(self, direction=0):
        # CPU -> platform -> codec -> board, matching public soc_pcm_open.
        # A failing callback owns its own unwind; completed callbacks close.
        sub = SUB if direction == 0 else CAPTURE
        ret = self.cpu_open(direction)
        if ret & 0x80000000:
            return ret
        ret = (-5 & 0xffffffff) if self.hit('platform_open') else self.call('wm8960_startup', sub, DAI)
        if not ret:
            ret = self.call('imx_hifi_startup', sub)
        if ret:
            self.cpu_close(direction)
        return ret

    def close(self, direction):
        sub = SUB if direction == 0 else CAPTURE
        # Public soc_pcm_close invokes CPU shutdown before board shutdown.
        self.cpu_close(direction)
        self.call('imx_hifi_shutdown', sub)


def verify(image, types):
    cases = []
    m = SaiMachine(image, types)
    try:
        m.call('fsl_sai_probe', CPU)
    except ProbeInitialized:
        pass
    else:
        raise ValueError('SAI probe passed lock initialization without being observed')
    cases.append('compiled probe initializes the per-device stream mutex before hardware setup')
    m = SaiMachine(image, types, fail='sai_allocation')
    require(m.call('fsl_sai_probe', CPU) == (-12 & 0xffffffff) and m.failed, 'SAI probe allocation failure lost')
    cases.append('probe allocation failure does not initialize or publish a lock')
    for failure in (None, 'pm_resume'):
        m = SaiMachine(image, types, external_pm_references=2, fail=failure)
        require(m.cpu_open(0) == ((-5 & 0xffffffff) if failure else 0), 'shared PM fixture failed')
        m.cpu_close(0)
        m.ownership(set())
        cases.append('SAI releases only its own PM reference ' + str(failure))
    for direction, resume in itertools.product((0, 1), (0, 1)):
        m = SaiMachine(image, types, resume_result=resume)
        m.cpu_close(direction)
        require(not m.sai_events[2:], 'unopened shutdown accessed hardware')
        require(m.cpu_open(direction) == 0, 'SAI open failed')
        m.ownership({direction})
        mark = len(m.sai_events)
        require(m.cpu_open(direction) == (-16 & 0xffffffff), 'duplicate SAI open accepted')
        require(m.sai_events[mark:] == ['mutex_lock', 'mutex_unlock'], 'duplicate open touched resources')
        m.cpu_close(direction)
        m.cpu_close(direction)
        m.ownership(set())
        require(not m.sai_registers.get(0x0c + direction * 0x80, 0) & 0x10000, 'closed SAI channel still enabled')
        cases.append('CPU open/duplicate/close with PM result ' + str((direction, resume)))
    for direction, failure in itertools.product((0, 1), ('pm_resume', 'bus_prepare', 'bus_enable', 'sai_constraint')):
        m = SaiMachine(image, types, fail=failure)
        expected = -22 if failure == 'sai_constraint' else -5
        require(m.cpu_open(direction) == (expected & 0xffffffff) and m.failed, 'CPU startup failure lost')
        m.ownership(set())
        if failure == 'pm_resume':
            require(m.sai_events == ['mutex_lock', '__pm_runtime_resume', 'mutex_unlock'], 'failed resume invoked idle or hardware')
        m.fail = None
        require(m.cpu_open(direction) == 0, 'CPU startup failure poisoned retry')
        m.cpu_close(direction)
        m.ownership(set())
        cases.append('CPU startup rollback/retry ' + str((direction, failure)))
    for direction, operation, persistent in itertools.product((0, 1), (1, 2), (False, True)):
        m = SaiMachine(image, types, sai_fail_at=operation, sai_persistent=persistent, idle_result=-16)
        require(m.cpu_open(direction) == (-5 & 0xffffffff) and m.sai_failed, 'SAI register failure lost')
        m.ownership(set())
        m.sai_fail_at = None
        require(m.cpu_open(direction) == 0, 'SAI register failure poisoned reopen')
        require(m.sai_registers[0x0c + direction * 0x80] & 0x10000, 'SAI reopen skipped a required hardware write')
        m.cpu_close(direction)
        m.ownership(set())
        cases.append('register startup failure and forced retry ' + str((direction, operation, persistent)))
    for direction, operation in itertools.product((0, 1), (3, 4)):
        m = SaiMachine(image, types, fail='sai_constraint', sai_fail_at=operation)
        require(m.cpu_open(direction) == (-22 & 0xffffffff) and m.failed and m.sai_failed, 'cleanup hid original constraint failure')
        require(m.diagnostics == ['SAI startup channel cleanup failed\n'], 'startup cleanup failure was not reported')
        m.ownership(set())
        m.fail, m.sai_fail_at = None, None
        require(m.cpu_open(direction) == 0, 'failed cleanup prevented retry')
        m.cpu_close(direction)
        m.ownership(set())
        cases.append('startup cleanup error preserves primary failure ' + str((direction, operation)))
    for direction, operation in itertools.product((0, 1), (3, 4)):
        m = SaiMachine(image, types, sai_fail_at=operation)
        require(m.cpu_open(direction) == 0, 'shutdown fixture failed')
        m.cpu_close(direction)
        require(m.sai_failed, 'shutdown error not injected')
        require(m.diagnostics == ['SAI shutdown channel cleanup failed\n'], 'shutdown cleanup failure was not reported')
        m.ownership(set())
        m.sai_fail_at = None
        require(m.cpu_open(direction) == 0, 'failed shutdown poisoned next open')
        m.cpu_close(direction)
        m.ownership(set())
        require(not m.sai_registers[0x0c + direction * 0x80] & 0x10000, 'shutdown retry trusted stale cache')
        cases.append('shutdown failure/reopen/close ' + str((direction, operation)))
    for first in (0, 1):
        m = SaiMachine(image, types)
        require(m.cpu_open(first) == m.cpu_open(1 - first) == 0, 'duplex SAI open failed')
        m.ownership({0, 1})
        m.cpu_close(first)
        m.ownership({1 - first})
        require(m.sai_registers[0x0c + (1 - first) * 0x80] & 0x10000, 'closing one direction disabled its peer')
        m.cpu_close(1 - first)
        m.ownership(set())
        cases.append('duplex independent ownership ' + str(first))
    for first, failure in itertools.product((0, 1), ('pm_resume', 'bus_prepare', 'bus_enable', 'sai_constraint')):
        m = SaiMachine(image, types)
        require(m.cpu_open(first) == 0, 'peer fixture failed to open')
        m.fail = failure
        require(m.cpu_open(1 - first) & 0x80000000 and m.failed, 'duplex second-open failure missing')
        m.ownership({first})
        require(m.sai_registers[0x0c + first * 0x80] & 0x10000, 'failed peer open disabled the first direction')
        m.cpu_close(first)
        m.ownership(set())
        cases.append('second direction failure preserves existing CPU stream ' + str((first, failure)))
    for direction, failure in itertools.product((0, 1), ('platform_open', 'codec_constraint', 'clk_prepare', 'clk_enable')):
        m = SaiMachine(image, types, fail=failure)
        require(m.startup(direction) & 0x80000000 and m.failed, 'connected startup did not fail')
        m.ownership(set())
        require(m.prepared == m.enabled == 0 and not m.board_flag('is_stream_opened', direction), 'connected unwind retained board clock')
        m.fail = None
        require(m.startup(direction) == 0 and m.configure(direction) == (0, 0, 0), 'connected retry/configuration failed')
        clocks(m, direction)
        sub = SUB if direction == 0 else CAPTURE
        m.call('fsl_sai_hw_free', sub, CPU_DAI)
        m.call('wm8960_hw_free', sub, DAI)
        m.call('imx_hifi_hw_free', sub)
        m.close(direction)
        m.ownership(set())
        require(m.prepared == m.enabled == 0, 'connected close retained board clock')
        cases.append('connected open/rollback/retry/configure/free/close ' + str((direction, failure)))
    for first in (0, 1):
        m = SaiMachine(image, types)
        for direction in (first, 1 - first):
            require(m.startup(direction) == 0 and m.configure(direction) == (0, 0, 0), 'connected duplex setup failed')
        m.ownership({0, 1})
        for index, direction in enumerate((first, 1 - first)):
            sub = SUB if direction == 0 else CAPTURE
            for name, args in (('fsl_sai_hw_free', (sub, CPU_DAI)), ('wm8960_hw_free', (sub, DAI)),
                               ('imx_hifi_hw_free', (sub,))):
                require(m.call(name, *args) == 0, 'connected duplex hw_free failed')
            m.close(direction)
            m.ownership({1 - first} if index == 0 else set())
            require(m.enabled == m.prepared == 1 - index, 'connected duplex close released peer MCLK')
        cases.append('connected duplex configure/free/close ' + str(first))
    m = SaiMachine(image, types)
    ops = image.symbols['fsl_sai_pcm_dai_ops']
    for name, callback in (('startup', 'fsl_sai_startup'), ('shutdown', 'fsl_sai_shutdown')):
        require(m.field('snd_soc_dai_ops', ops, name) == image.symbols[callback], 'SAI lifecycle callback not registered')
    cases.append('SAI lifecycle callback registration')
    return cases


def negative_controls(path, obj, types):
    image = Image(path)
    image.ranges, image.board_writes, image.services = [], [], {}
    raw = obj.read_bytes()
    comparison = attach(image, SimpleNamespace(binary=raw, elf=ELFFile(io.BytesIO(raw))))
    cases = []
    for failure in ('bus_prepare', 'bus_enable', 'sai_constraint'):
        m = SaiMachine(image, types, fail=failure)
        require(m.cpu_open(0) & 0x80000000 and m.failed and m.usage() == 1 and
                m.flag('fsl_sai', SAI, 'is_stream_opened', 0) == 1, 'original SAI startup leak not reproduced')
        if failure == 'sai_constraint':
            require(m.bus_enabled == m.bus_prepared == 1, 'original constraint failure did not leak bus clock')
        cases.append('original SAI leaks ownership after ' + failure)
    m = SaiMachine(image, types, fail='pm_resume')
    require(m.cpu_open(0) == 0 and m.failed, 'original ignored resume error not reproduced')
    cases.append('original SAI ignores runtime resume error')
    m = SaiMachine(image, types, sai_fail_at=1)
    require(m.cpu_open(0) == 0 and m.sai_failed and not m.sai_registers.get(0x0c, 0), 'original ignored register failure not reproduced')
    cases.append('original SAI ignores channel register error')
    return cases, comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('kernel', 'board', 'codec', 'sai', 'regmap', 'baseline_kernel', 'baseline_sai'):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    board, codec, sai, regmap = (Module(p) for p in (args.board, args.codec, args.sai, args.regmap))
    image = load(args.kernel, board)
    linked = {'codec': attach(image, codec), 'sai': attach(image, sai)}
    forced = attach_force_helper(image, regmap)
    image.services.update({SAI_LOCK: 'sai_map_lock', SAI_UNLOCK: 'sai_map_unlock'})
    arithmetic(image)
    for module in (codec, sai, regmap):
        board.members.update(module.members)
        board.sizes.update(module.sizes)
    cases = verify(image, board)
    negative, reference = negative_controls(args.baseline_kernel, args.baseline_sai, board)
    cases.extend(negative)
    files = ('verify_sai_lifetime.py', 'verify_wm8960_clocking.py', 'verify_wm8960_streams.py',
             'verify_wm8960_lifetime.py', 'verify_adc_module.py', 'verify_busfreq.py',
             'verify_wm8960_sources.py', 'verify_wm8960_board_sources.py',
             'verify_ddr_preparation.py', 'verify_ddr_sources.py', 'arm_relocations.py')
    print(json.dumps({'kernel_sha256': sha(image.binary), 'sai_sha256': sha(sai.binary),
                      'linked_object_comparison': linked, 'force_helper': forced,
                      'passed_cases': len(cases), 'cases': cases, 'negative_controls': negative,
                      'baseline_comparison': reference,
                      'inputs': {n: sha(getattr(args, n).read_bytes()) for n in vars(args)},
                      'verifier_sources': {n: sha((Path(__file__).parent / n).read_bytes()) for n in files},
                      'hardware_qualified': False,
                      'limits': 'Compiled SAI open/close and connected board/codec callbacks; modeled PM, clocks, cache/bus and ALSA ordering. No ALSA core, scheduler, PCM DMA, trigger or physical audio proof.'}, indent=2))


if __name__ == '__main__':
    main()
