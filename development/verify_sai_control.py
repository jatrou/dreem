#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Execute linked SAI trigger/IRQ/close and connected ASoC -> PCM rollback.

The CSR model implements W1C flags, reset strobes, BCE auto-enable and delayed
enable clearing. It is not a model of sample transfer or physical frame timing.
"""
import argparse
import itertools
import json
from pathlib import Path

from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_PC, UC_ARM_REG_LR, UC_ARM_REG_CPSR
from verify_busfreq import require
from verify_soc_trigger import SocMachine, load as load_soc, DAI_OPS, START, STOP, PAUSE, RELEASE, RESUME, SUSPEND, MASK
from verify_adc_module import Module
from verify_pcm_lifetime import dependencies
from verify_sai_parameters import ParameterMachine
from verify_sai_lifetime import SAI_LOCK, SAI_UNLOCK
from verify_wm8960_clocking import REGMAP, attach, attach_force_helper
from verify_wm8960_streams import SAI, CPU_DAI, SUB, CAPTURE, PCM
from verify_wm8960_sources import sha

TE, BCE, FR, SR = 1 << 31, 1 << 28, 1 << 25, 1 << 24
FEF, SEF, WSF, FWF, FRF = (1 << n for n in (18, 19, 20, 17, 16))
FLAGS, W1C, IE, DMA = 0x1f0000, 0x1c0000, 0x1f00, 1
ENABLE, REQUEST = TE | BCE, IE | DMA
MODES = ((False, False), (True, False), (False, True))  # RX, TX


class ControlModel:
    def __init__(self, image, types, *, strict_control=True, stop_reads=2,
                 control_fail_at=None, persistent_control=False, applied_error=False, **kwargs):
        super().__init__(image, types, **kwargs)
        self.strict_control = strict_control
        self.stop_reads = stop_reads
        self.control_fail_at, self.persistent_control = control_fail_at, persistent_control
        self.applied_error = applied_error
        self.control_count, self.control_failed, self.control_active = 0, False, False
        self.csr_trace, self.control_diagnostics, self.unsafe_resets = [], [], []
        self.pending_clear, self.remaining_reads, self.stuck = {}, {}, {}
        self.delays, self.preempt = 0, 0
        self.field('snd_pcm_runtime', PCM, 'channels', self.channels)
        self.cpu.reg_write(UC_ARM_REG_CPSR, self.cpu.reg_read(UC_ARM_REG_CPSR) & ~0x80)

    def running(self, direction):
        return self.flag('fsl_sai', SAI, 'dreem_running', direction)

    def fault(self):
        return self.field('fsl_sai', SAI, 'dreem_control_error')

    def control_read(self, reg):
        if reg in self.pending_clear:
            self.remaining_reads[reg] -= 1
            if self.remaining_reads[reg] <= 0:
                clear = self.pending_clear[reg] & ~self.stuck.get(reg, 0)
                self.sai_registers[reg] &= ~clear
                self.pending_clear[reg] &= ~clear
                if not self.pending_clear[reg]:
                    del self.pending_clear[reg]
        return self.sai_registers.get(reg, 0)

    def control_write(self, reg, value):
        old = self.sai_registers.get(reg, 0)
        if reg not in (0, 0x80):
            if reg == 0x20:
                require(not self.strict_control or not (self.sai_registers.get(0, 0) & DMA),
                        'CPU primed FIFO after enabling DMA requests')
            self.sai_registers[reg] = value
            return
        unsafe = bool(value & FR and old & TE and not old & FEF) or bool(value & SR and old & ENABLE)
        if unsafe:
            self.unsafe_resets.append((reg, old, value))
            require(not self.strict_control, 'reset before confirmed disable or FIFO error')
        if value & SR:
            self.sai_registers[reg] = SR
            self.pending_clear.pop(reg, None)
            return
        requested = value | (BCE if value & TE else 0)
        delayed = old & ENABLE & ~requested
        if delayed:
            if reg not in self.pending_clear:
                self.remaining_reads[reg] = self.stop_reads
            self.pending_clear[reg] = delayed
        else:
            self.pending_clear.pop(reg, None)
        self.sai_registers[reg] = ((requested & ~(FLAGS | FR | SR)) | delayed |
                                   (old & FLAGS & ~(value & W1C)))

    def code(self, cpu, address, size, extra):
        name = self.stub_addresses.get(address, '')
        a, b, c, d = [cpu.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
        if name in ('preempt_count_add', 'preempt_count_sub'):
            self.preempt += a if name.endswith('add') else -a
            require(self.preempt >= 0, 'negative control preemption depth')
            result = 0
        elif name == 'preempt_schedule':
            require(not self.preempt, 'schedule inside SAI control')
            result = 0
        elif name in ('__const_udelay', '__udelay', '__loop_const_udelay'):
            self.delays += 1
            require(not self.strict_control or self.preempt > 0, 'SAI delay outside atomic control')
            result = 0
        elif (self.control_active and a == REGMAP and
              (b in (0, 0x80) or self.control_entry in ('fsl_sai_trigger', 'fsl_sai_isr', 'soc_pcm_trigger')) and
              name in ('regmap_read', 'regmap_write', 'regmap_update_bits', '_regmap_read', '_regmap_write')):
            require(not self.strict_control or (self.preempt > 0 and cpu.reg_read(UC_ARM_REG_CPSR) & 0x80),
                    'SAI register operation without IRQ/preemption exclusion')
            self.control_count += 1
            failed = self.control_fail_at is not None and self.control_count >= self.control_fail_at and (
                not self.control_failed or self.persistent_control)
            self.control_failed |= failed
            self.csr_trace.append((name, b, c, d, failed))
            read = name in ('regmap_read', '_regmap_read')
            result = -5 if failed else 0
            if read and not failed:
                value = self.control_read(b) if b in (0, 0x80) else getattr(self, 'sai_cache', self.sai_registers).get(b, 0)
                self.put(c, value)
            elif not read:
                old = self.sai_registers.get(b, 0)
                value = ((old & ~c) | (d & c)) if name == 'regmap_update_bits' else c
                if b not in (0, 0x80) and hasattr(self, 'sai_cache'):
                    self.sai_cache[b] = value
                if not failed or self.applied_error:
                    self.control_write(b, value)
        elif name == 'dev_err' and self.control_active:
            self.control_diagnostics.append((self.string(b), c, d))
            result = 0
        else:
            return super().code(cpu, address, size, extra)
        cpu.reg_write(UC_ARM_REG_R0, result & MASK)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def call(self, name, *args):
        before = self.cpu.reg_read(UC_ARM_REG_CPSR) & 0x80
        active = name in ('fsl_sai_trigger', 'fsl_sai_isr', 'soc_pcm_trigger',
                          'fsl_sai_hw_free', 'fsl_sai_shutdown', 'fsl_sai_startup')
        self.control_active = active
        self.control_entry = name
        try:
            ret = super().call(name, *args)
            require(not self.preempt and self.cpu.reg_read(UC_ARM_REG_CPSR) & 0x80 == before,
                    'control callback retained IRQ/preemption state')
            return ret
        finally:
            self.control_active = False

    def trigger_cpu(self, direction, cmd):
        return self.call('fsl_sai_trigger', SUB if direction == 0 else CAPTURE, cmd, CPU_DAI)

    def irq(self):
        return self.call('fsl_sai_isr', 17, SAI)


class ControlMachine(ControlModel, SocMachine):
    def configure_fixture(self, sync=(True, False), opened=(0, 1)):
        self.cpu.mem_write(SAI + self.types.members['fsl_sai']['synchronous'], bytes(sync))
        for direction in (0, 1):
            for name in ('is_stream_opened', 'dreem_configured'):
                self.flag('fsl_sai', SAI, name, direction, int(direction in opened))


class ControlParameters(ControlModel, ParameterMachine):
    pass


def load(path):
    image, types, matches = load_soc(path)
    matches['sai'] = attach(image, Module(path / 'kernel/sound/soc/fsl/fsl_sai.o'))
    matches['force_helper'] = attach_force_helper(image, Module(path / 'kernel/drivers/base/regmap/regmap.o'))
    image.services.update({SAI_LOCK: 'sai_map_lock', SAI_UNLOCK: 'sai_map_unlock'})
    for start, _ in image.ranges:
        image.services.pop(start, None)
    image.services[image.symbols['sdma_run_channel0']] = 'sdma_run_channel0'
    image.services[image.symbols['__loop_const_udelay']] = '__loop_const_udelay'
    return image, types, matches


def verify(image, types):
    cases = []
    for sync, direction, cmd in itertools.product(MODES, (0, 1), (START, RESUME, RELEASE)):
        m = ControlMachine(image, types)
        m.configure_fixture(sync)
        reg = 0 if direction == 0 else 0x80
        m.sai_registers[reg] = FEF | SEF | WSF
        require(m.trigger_cpu(direction, cmd) == 0 and m.running(direction) and not m.fault(), 'start failed')
        tx = direction == 0
        for hardware in (False, True):
            r = 0 if hardware else 0x80
            needed = hardware == tx or sync[int(tx)]
            require(bool(m.sai_registers.get(r, 0) & TE) == bool(needed), 'wrong synchronous enables')
        require(m.sai_registers[reg] & W1C == W1C, 'trigger acknowledged pending status')
        if sync[int(tx)]:
            enables = [b for name, b, c, _, failed in m.csr_trace
                       if name == 'regmap_write' and b in (0, 0x80) and c & ENABLE == ENABLE and not failed]
            require(enables[:2] == [reg, reg ^ 0x80], 'clock source was not enabled last')
        count = len(m.csr_trace)
        require(m.trigger_cpu(direction, cmd) == 0 and len(m.csr_trace) == count, 'repeated start was not idempotent')
        inverse = {START: STOP, RESUME: SUSPEND, RELEASE: PAUSE}[cmd]
        before_stop = len(m.csr_trace)
        require(m.trigger_cpu(direction, inverse) == 0 and not m.running(direction) and not m.fault() and
                all(not m.sai_registers.get(r, 0) & (ENABLE | REQUEST) for r in (0, 0x80)), 'stop failed')
        if sync[int(tx)]:
            disables = [b for name, b, c, _, failed in m.csr_trace[before_stop:]
                        if name == 'regmap_write' and b in (0, 0x80) and not c & (ENABLE | SR) and not failed]
            require(disables[:2] == [reg ^ 0x80, reg], 'clock source was not disabled first')
        cases.append('start/status/idempotence/stop ' + str((sync, direction, cmd)))
    for sync, first in itertools.product(MODES, (0, 1)):
        m = ControlMachine(image, types)
        m.configure_fixture(sync)
        require(m.trigger_cpu(first, START) == 0 and m.trigger_cpu(1 - first, START) == 0, 'duplex start failed')
        peer_reg = 0x80 if first == 0 else 0
        before = m.sai_registers[peer_reg]
        require(m.trigger_cpu(first, STOP) == 0 and m.running(1 - first) and
                m.sai_registers[peer_reg] == before and not m.fault(), 'stop disturbed active peer')
        require(m.trigger_cpu(1 - first, STOP) == 0 and not m.running(1 - first), 'last peer stop failed')
        cases.append('duplex stop preserves peer ' + str((sync, first)))
    for sync, peer in itertools.product(MODES, (0, 1)):
        good = ControlMachine(image, types)
        good.configure_fixture(sync)
        require(good.trigger_cpu(peer, START) == 0, 'peer fault count setup failed')
        good.control_count = 0
        require(good.trigger_cpu(1 - peer, START) == 0, 'peer fault count failed')
        for index, persistent in itertools.product(range(1, good.control_count + 1), (False, True)):
            m = ControlMachine(image, types, applied_error=True)
            m.configure_fixture(sync)
            require(m.trigger_cpu(peer, START) == 0, 'active peer fixture failed')
            reg = 0 if peer == 0 else 0x80
            before = m.sai_registers[reg]
            m.control_count, m.control_fail_at, m.persistent_control = 0, index, persistent
            require(m.trigger_cpu(1 - peer, START) == (-5 & MASK) and m.running(peer) and
                    not m.running(1 - peer) and m.sai_registers[reg] == before,
                    'failed start disturbed active peer or published itself')
            m.control_fail_at = None
            require(m.trigger_cpu(1 - peer, STOP) == 0 and m.trigger_cpu(peer, STOP) == 0 and not m.fault(),
                    'peer fault did not recover after both streams stopped')
            cases.append('start failure preserves active peer ' + str((sync, peer, index, persistent)))
    for direction, sync in itertools.product((0, 1), MODES):
        good = ControlMachine(image, types)
        good.configure_fixture(sync)
        require(good.trigger_cpu(direction, START) == 0, 'count fixture failed')
        for index, persistent, applied in itertools.product(range(1, good.control_count + 1), (False, True), (False, True)):
            m = ControlMachine(image, types, control_fail_at=index, persistent_control=persistent, applied_error=applied)
            m.configure_fixture(sync)
            require(m.trigger_cpu(direction, START) == (-5 & MASK) and m.control_failed and not m.running(direction),
                    'start I/O error was lost or published running state')
            if persistent:
                require(m.fault() and m.control_diagnostics, 'failed cleanup did not retain fault')
            else:
                require(not m.fault() and all(not m.sai_registers.get(r, 0) & (ENABLE | REQUEST) for r in (0, 0x80)),
                        'transient failure did not clean up hardware')
            m.control_fail_at = None
            require(m.trigger_cpu(direction, STOP) == 0 and not m.fault() and m.trigger_cpu(direction, START) == 0,
                    'stop and retry did not recover')
            cases.append('start I/O failure and retry ' + str((direction, sync, index, persistent, applied)))
    for direction in (0, 1):
        good = ControlMachine(image, types)
        good.configure_fixture()
        require(good.trigger_cpu(direction, START) == 0, 'stop count fixture failed')
        good.control_count = 0
        require(good.trigger_cpu(direction, STOP) == 0, 'stop count failed')
        for index, applied in itertools.product(range(1, good.control_count + 1), (False, True)):
            m = ControlMachine(image, types, applied_error=applied)
            m.configure_fixture()
            require(m.trigger_cpu(direction, START) == 0, 'stop fault fixture failed')
            m.control_count, m.control_fail_at = 0, index
            require(m.trigger_cpu(direction, STOP) == (-5 & MASK) and m.fault(), 'stop error was discarded')
            before = len(m.csr_trace)
            require(m.trigger_cpu(direction, START) == (-5 & MASK) and len(m.csr_trace) == before,
                    'faulted controller restarted')
            m.control_fail_at = None
            require(m.trigger_cpu(direction, STOP) == 0 and not m.fault(), 'stop retry failed')
            cases.append('stop I/O failure retains fault ' + str((direction, index, applied)))
    for reg, stuck in itertools.product((0, 0x80), (TE, BCE, ENABLE)):
        m = ControlMachine(image, types)
        m.configure_fixture()
        require(m.trigger_cpu(1, START) == 0, 'clock-dependent capture fixture failed')
        m.stuck[reg] = stuck
        m.delays = 0
        require(m.trigger_cpu(1, STOP) == (-110 & MASK) and m.fault() == (-110 & MASK) and
                m.delays == 100 and not m.unsafe_resets, 'stop timeout was lost or reset active hardware')
        m.stuck = {}
        require(m.trigger_cpu(1, STOP) == 0 and not m.fault(), 'timeout retry failed')
        cases.append('both direction/enable bits have bounded stop ' + str((reg, stuck)))
    for delay in (1, 99, 100, 101):
        m = ControlMachine(image, types, stop_reads=delay)
        m.configure_fixture()
        require(m.trigger_cpu(1, START) == 0, 'stop-boundary fixture failed')
        m.delays = 0
        require(m.trigger_cpu(1, STOP) == (0 if delay <= 100 else -110 & MASK) and
                m.delays == min(delay, 100) and not m.unsafe_resets, 'stop poll boundary differs')
        cases.append('stop poll completion boundary ' + str(delay))
    for direction, flag, enabled in itertools.product((0, 1), (FEF, SEF, WSF, FWF, FRF), (False, True)):
        m = ControlMachine(image, types)
        reg = 0 if direction == 0 else 0x80
        enable = flag >> 8 if enabled else 0
        m.sai_registers[reg] = ENABLE | DMA | flag | enable
        require(m.irq() == int(enabled), 'IRQ ownership ignored actual enables')
        require(bool(m.sai_registers[reg] & flag) == (not enabled or not flag & W1C), 'IRQ status acknowledgement differs')
        require(m.sai_registers[reg] & (ENABLE | DMA) == ENABLE | DMA, 'IRQ changed data path controls')
        if enabled and flag & (FWF | FRF):
            require(not m.sai_registers[reg] & enable, 'unsupported level IRQ remained enabled')
        cases.append('IRQ enable/status handling ' + str((direction, flag, enabled)))
    for index, persistent in itertools.product(range(1, 5), (False, True)):
        m = ControlMachine(image, types, control_fail_at=index, persistent_control=persistent)
        for reg in (0, 0x80):
            m.sai_registers[reg] = ENABLE | DMA | FEF | (FEF >> 8) | WSF
        m.irq()
        require(m.fault() == (-5 & MASK) and not m.unsafe_resets, 'IRQ I/O failure ignored')
        require(all(value & WSF for value in m.sai_registers.values()), 'IRQ cleared an unenabled flag')
        failed_reads = {b for name, b, _, _, failed in m.csr_trace if name == 'regmap_read' and failed}
        require(not any(name == 'regmap_write' and b in failed_reads for name, b, _, _, _ in m.csr_trace),
                'IRQ used a CSR value after a failed read')
        cases.append('IRQ read/write failure ' + str((index, persistent)))
    for cmd in (2, 7, MASK):
        m = ControlMachine(image, types)
        require(m.trigger_cpu(0, cmd) == (-22 & MASK) and not m.csr_trace, 'invalid command touched hardware')
        cases.append('invalid trigger ' + str(cmd))
    for fault in ('closed', 'unconfigured', 'both-sync', 'zero-channels', 'too-many-channels'):
        m = ControlMachine(image, types)
        m.configure_fixture()
        if fault in ('closed', 'unconfigured'):
            m.flag('fsl_sai', SAI, 'is_stream_opened' if fault == 'closed' else 'dreem_configured', 0, 0)
        elif fault == 'both-sync':
            m.cpu.mem_write(SAI + types.members['fsl_sai']['synchronous'], b'\1\1')
        else:
            m.field('snd_pcm_runtime', PCM, 'channels', 0 if fault == 'zero-channels' else 3)
        require(m.trigger_cpu(0, START) == ((-77 if fault in ('closed', 'unconfigured') else -22) & MASK) and
                not m.csr_trace, 'invalid stream state touched hardware')
        cases.append('invalid stream state ' + fault)
    for direction, width, channels in itertools.product((0, 1), (16, 20, 24, 32), (1, 2)):
        m = ControlMachine(image, types, direction=direction, width=width, channels=channels, platform_real=True)
        m.configure_fixture()
        m.field('snd_soc_dai_ops', DAI_OPS[2], 'trigger', image.symbols['fsl_sai_trigger'])
        m.prepare_config()
        m.control_fail_at = 1
        require(m.soc(START) == (-5 & MASK) and m.issue_count == 1 and m.work_pending and m.descriptor_live and
                not m.running(direction) and not m.fault(), 'real SAI failure did not unwind real DMA')
        require(m.call('snd_dmaengine_pcm_sync_stop', m.sub) == 0 and not m.descriptor_live,
                'connected failed-start retirement did not complete')
        m.control_fail_at = None
        require(m.soc(START) == 0 and m.running(direction), 'connected start retry failed')
        require(m.soc(STOP) == 0 and not m.running(direction) and
                m.call('dreem_dmaengine_pcm_hw_free', m.sub) == 0, 'connected stop/free failed')
        cases.append('real ASoC/PCM/SAI failure rollback and retry ' + str((direction, width, channels)))
    return cases


def lifetime(image, types):
    cases = []
    for direction in (0, 1):
        m = ControlParameters(image, types)
        m.setup(direction, master=True)
        require(m.configure_cpu(direction) == 0 and m.trigger_cpu(direction, START) == 0, 'clock-retention fixture failed')
        reg = 0 if direction == 0 else 0x80
        m.stuck[reg] = ENABLE
        require(m.call('fsl_sai_hw_free', SUB if direction == 0 else CAPTURE, CPU_DAI) == (-110 & MASK) and
                m.configured(direction) and sum(m.mclk_enabled) == 1, 'failed free released active clock')
        m.cpu_close(direction)
        require(m.flag('fsl_sai', SAI, 'dreem_orphaned', direction) and sum(m.mclk_enabled) == 1,
                'failed void shutdown discarded clock ownership')
        m.ownership({direction})
        require(m.cpu_open(direction) == (-110 & MASK), 'reopen reused a faulted direction')
        m.stuck = {}
        require(m.cpu_open(direction) == 0 and not m.fault() and not m.configured(direction) and
                not m.flag('fsl_sai', SAI, 'dreem_orphaned', direction), 'reopen did not drain orphaned resources')
        m.released()
        m.ownership({direction})
        m.cpu_close(direction)
        m.ownership(set())
        cases.append('failed free/close retains clocks and reopen recovers ' + str(direction))
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build', type=Path)
    parser.add_argument('previous_build', type=Path)
    args = parser.parse_args()
    image, types, matches = load(args.build)
    cases = verify(image, types) + lifetime(image, types)
    old, old_types, old_matches = load(args.previous_build)
    negative = []
    m = ControlMachine(old, old_types, strict_control=False, control_fail_at=1)
    m.configure_fixture()
    require(m.trigger_cpu(0, START) == 0 and m.control_failed, 'old ignored start error not reproduced')
    negative.append('old trigger returns success after register failure')
    m = ControlMachine(old, old_types, strict_control=False)
    m.configure_fixture()
    m.sai_registers[0] = FEF | SEF | WSF
    require(m.trigger_cpu(0, START) == 0 and not m.sai_registers[0] & W1C,
            'old trigger W1C acknowledgement not reproduced')
    negative.append('old trigger RMW acknowledges unrelated pending flags')
    m.stuck[0] = ENABLE
    require(m.trigger_cpu(0, STOP) == 0 and m.unsafe_resets, 'old reset after stop timeout not reproduced')
    negative.append('old stop times out silently and resets active hardware')
    m = ControlMachine(old, old_types, strict_control=False)
    m.sai_registers[0] = ENABLE | FEF
    require(m.irq() == 1 and not m.sai_registers[0] & FEF, 'old masked IRQ claim not reproduced')
    negative.append('old ISR claims and clears a disabled interrupt flag')
    cases.extend(negative)
    print(json.dumps({'kernel_sha256': sha(image.binary), 'previous_kernel_sha256': sha(old.binary),
                      'passed_cases': len(cases), 'cases': cases, 'negative_controls': negative,
                      'linked_objects': matches, 'previous_linked_objects': old_matches,
                      'verifier_sources': {n: sha((Path(__file__).parent / n).read_bytes())
                                           for n in sorted(dependencies(Path(__file__).name))},
                      'hardware_qualified': False,
                      'limits': 'Linked ARM SAI trigger/IRQ/free/close and ASoC/PCM/SDMA callbacks; modeled CSR semantics, frame completion, regmap/clock services and scheduler. No physical audio transfer, concurrent IRQ scheduler, PM or independent unbind qualification.'}, indent=2))


if __name__ == '__main__':
    main()
