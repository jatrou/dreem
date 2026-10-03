#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Execute SAI DMA retirement and PCM reuse/free/close with modeled scheduling.

The upstream 1 ms settling assumption is modeled, not measured on hardware.
Workqueue/tasklet dispatch, allocation and MMIO are models; their callbacks,
descriptor retirement and client ordering execute linked ARM instructions.
"""
import argparse
import ast
import io
import itertools
import json
from pathlib import Path
from types import SimpleNamespace

from elftools.elf.elffile import ELFFile
from unicorn import UC_HOOK_MEM_READ
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_PC, UC_ARM_REG_LR
from verify_adc_module import Module
from verify_busfreq import STACK, STOP, require
from verify_pcm_trigger import TriggerMachine, load_build, DEVICE, CONTROL, START, PAUSE, RESUME
from verify_pcm_dma import BASE, ENGINE, CHANNEL, PRUNTIME, DESCRIPTOR, BD_ARRAY, CONFIG
from verify_wm8960_clocking import attach
from verify_wm8960_streams import SUB, CAPTURE, PARAMS, RUNTIME
from verify_wm8960_sources import sha

GENERIC, CLOCK_A, CLOCK_B = BASE + 0x4000, BASE + 0x6000, BASE + 0x6004
WORK_RETURN, TASKLET_RETURN = BASE + 0xfc00, BASE + 0xfc04


class LifetimeMachine(TriggerMachine):
    def __init__(self, image, types, *, strict=True, **kwargs):
        super().__init__(image, types, **kwargs)
        self.strict = strict
        self.history = []
        self.started = False
        self.runtime_live = True
        self.work_pending = self.work_running = False
        self.queued_tasklet = self.captured_callback = False
        self.settled = False
        self.periods = self.schedules = self.freed_pages = self.released = 0
        self.continuations = {}
        self.sub = SUB if self.direction == 0 else CAPTURE
        self.task = self.vc + types.members['virt_dma_chan']['task']
        self.worker = CHANNEL + types.members['sdma_channel'].get('dreem_retire_work', 0)
        if 'dreem_retired' in types.members['sdma_channel']:
            self.list_init(CHANNEL + types.members['sdma_channel']['dreem_retired'])
            self.field('work_struct', self.worker, 'func', image.symbols['dreem_sdma_audio_retire'])
        self.field('virt_dma_chan', self.vc, 'desc_free', image.symbols['sdma_desc_free'])
        self.field('dma_device', DEVICE, 'device_terminate_all', image.symbols['sdma_terminate_all'])
        self.field('dma_device', DEVICE, 'device_wait_tasklet', image.symbols['sdma_wait_tasklet'])
        self.field('dma_device', DEVICE, 'device_config', image.symbols['sdma_config'])
        self.field('sdma_engine', ENGINE, 'clk_ipg', CLOCK_A)
        self.field('sdma_engine', ENGINE, 'clk_ahb', CLOCK_B)
        if 'dmaengine_pcm' in types.members:
            require(GENERIC + types.sizes['dmaengine_pcm'] < CLOCK_A, 'overlapping PCM fixture')
            self.field('snd_soc_pcm_runtime', RUNTIME, 'platform',
                       GENERIC + types.members['dmaengine_pcm']['platform'])
        self.cpu.hook_add(UC_HOOK_MEM_READ, self.read)

    def read(self, cpu, access, address, size, value, extra):
        if DESCRIPTOR <= address < DESCRIPTOR + self.types.sizes['sdma_desc']:
            require(self.descriptor_live, 'read of freed DMA descriptor')
        if PRUNTIME <= address < PRUNTIME + self.types.sizes['dmaengine_pcm_runtime_data']:
            require(self.runtime_live, 'read of freed PCM runtime')

    def write(self, cpu, access, address, size, value, extra):
        if address == 0x020ec008 and value == 4:
            self.history.append('stop request')
        return super().write(cpu, access, address, size, value, extra)

    def enter(self, target, argument, marker):
        self.continuations[marker] = self.cpu.reg_read(UC_ARM_REG_LR)
        self.cpu.reg_write(UC_ARM_REG_R0, argument)
        self.cpu.reg_write(UC_ARM_REG_LR, marker)
        self.cpu.reg_write(UC_ARM_REG_PC, self.symbols[target])

    def code(self, cpu, address, size, extra):
        if address in (WORK_RETURN, TASKLET_RETURN):
            if address == WORK_RETURN:
                self.work_pending = self.work_running = False
                self.history.append('worker finished')
            else:
                self.history.append('callback drained')
            cpu.reg_write(UC_ARM_REG_R0, 0)
            cpu.reg_write(UC_ARM_REG_PC, self.continuations.pop(address))
            return
        name = self.stub_addresses.get(address)
        a, b, c = [cpu.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2)]
        result = 0
        if name == 'queue_work_on':
            require(c == self.worker and not self.work_pending, 'wrong or duplicate retirement work')
            require('stop request' in self.history, 'retirement queued before stop request')
            self.work_pending = True
            self.schedules += 1
            self.history.append('queue retirement')
            result = 1
        elif name == 'flush_work':
            require(a == self.worker and self.preempt == 0, 'wrong or atomic retirement synchronization')
            if self.work_pending:
                require(not self.work_running, 'recursive retirement')
                self.work_running = True
                self.enter('dreem_sdma_audio_retire', self.worker, WORK_RETURN)
                return
        elif name == 'tasklet_kill':
            require(a == self.task and self.preempt == 0, 'wrong or atomic tasklet drain')
            self.history.append('drain tasklet')
            if self.queued_tasklet:
                self.queued_tasklet = False
                self.enter('vchan_complete', self.vc, TASKLET_RETURN)
                return
            if self.captured_callback:
                self.captured_callback = False
                self.enter('imx_pcm_dma_complete', self.sub, TASKLET_RETURN)
                return
        elif name == 'usleep_range':
            require(a >= 1000 and b >= a and self.preempt == 0 and self.work_running,
                    'missing settling interval or sleep in atomic context')
            require(not self.queued_tasklet and not self.captured_callback, 'delay before callback drain')
            self.settled = True
            self.history.append('settling interval')
        elif name == 'dma_sync_wait_tasklet':
            require(a == self.chan, 'wrong synchronization channel')
            cpu.reg_write(UC_ARM_REG_PC, self.symbols['sdma_wait_tasklet'])
            return
        elif name == 'snd_pcm_period_elapsed':
            require(a == self.sub and self.runtime_live, 'period callback after runtime release')
            self.periods += 1
            self.history.append('period callback')
        elif name in ('snd_pcm_lib_free_pages', 'snd_pcm_lib_malloc_pages'):
            require(a == self.sub and not self.descriptor_live and not self.work_pending,
                    'PCM buffer released/reused before DMA retirement')
            require(not self.queued_tasklet and not self.captured_callback, 'PCM buffer changed before callback drain')
            self.history.append('free pages' if name.endswith('free_pages') else 'allocate pages')
            self.freed_pages += name.endswith('free_pages')
        elif name == 'kfree' and a == PRUNTIME:
            if self.strict:
                require(not self.descriptor_live and not self.work_pending and
                        not self.queued_tasklet and not self.captured_callback,
                        'PCM runtime released before retirement/callback drain')
            self.runtime_live = False
            self.history.append('free runtime')
        elif name == 'gen_pool_free':
            if self.started and self.strict:
                require('stop request' in self.history and self.settled and self.work_running,
                        'DMA storage freed before stop, drain and settling interval')
            self.history.append('free DMA storage')
            return super().code(cpu, address, size, extra)
        elif name == 'dma_release_channel':
            require(a == self.chan and not self.runtime_live, 'channel released before closing runtime')
            self.released += 1
            self.history.append('release channel')
            cpu.reg_write(UC_ARM_REG_PC, self.symbols['sdma_free_chan_resources'])
            return
        elif name == 'clk_disable' and a in (CLOCK_A, CLOCK_B):
            require(not self.descriptor_live and not self.work_pending, 'DMA clock released before retirement')
            self.history.append('disable DMA clock')
        elif name == '__memzero' and STACK <= a and a + b < STOP:
            cpu.mem_write(a, bytes(b))
        else:
            return super().code(cpu, address, size, extra)
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def start(self):
        self.prepare_config()
        require(self.trigger(START) == 0, 'start failed')
        self.started = True
        self.settled = False

    def queue_period(self):
        self.field('virt_dma_chan', self.vc, 'cyclic',
                   DESCRIPTOR + self.types.members['sdma_desc']['vd'])
        self.queued_tasklet = True


def load(path, generic_types=None):
    image, types, matches = load_build(path)
    p = path / 'kernel/sound/soc/soc-generic-dmaengine-pcm.o'
    if generic_types is None:
        generic_types = Module(p)
    types.members.update(generic_types.members)
    types.sizes.update(generic_types.sizes)
    raw = p.read_bytes()
    matches['generic_pcm'] = attach(image, SimpleNamespace(binary=raw, elf=ELFFile(io.BytesIO(raw))))
    for start, _ in image.ranges:
        image.services.pop(start, None)
    image.services[image.symbols['sdma_run_channel0']] = 'sdma_run_channel0'
    return image, types, matches, generic_types


def verify(image, types):
    cases = []
    for direction, width, channels in itertools.product((0, 1), (16, 20, 24, 32), (1, 2)):
        m = LifetimeMachine(image, types, direction=direction, width=width, channels=channels, real_control=True)
        m.start()
        m.queue_period()
        require(m.trigger(0) == 0 and m.descriptor_live and m.bd_length and m.work_pending,
                'stop did not retain DMA storage')
        require(not m.field('sdma_channel', CHANNEL, 'desc') and
                not m.field('virt_dma_chan', m.vc, 'cyclic'), 'stop retained callback publication')
        require(m.trigger(0) == 0 and m.schedules == 1, 'duplicate stop queued another worker')
        before = len(m.mmio_writes)
        require(m.call('sdma_config', m.chan, CONFIG) == (-16 & 0xffffffff), 'configuration raced retirement')
        require(m.trigger(START) == (-12 & 0xffffffff) and len(m.mmio_writes) == before,
                'start before synchronization reused active DMA storage')
        require(m.trigger(RESUME) == (-22 & 0xffffffff), 'resume after stop reenabled retired descriptors')
        require(m.call('snd_dmaengine_pcm_sync_stop', m.sub) == 0 and
                not m.descriptor_live and not m.bd_length and not m.work_pending and m.periods == 0,
                'synchronization failed or stale queued callback ran')
        m.start()
        require(m.published()[0] == 9, 'retry did not submit a new cookie')
        require(m.call('snd_dmaengine_pcm_close_release_chan', m.sub) == 0 and
                not m.runtime_live and m.released == 1 and not m.descriptor_live,
                'close/release did not synchronize provider resources')
        cases.append('start, queued callback, repeated stop, sync, restart, close/release ' + str((direction, width, channels)))
    for direction, state in itertools.product((0, 1), ('running', 'queued', 'captured', 'paused')):
        m = LifetimeMachine(image, types, direction=direction, real_control=True)
        m.start()
        if state == 'queued':
            m.queue_period()
        elif state == 'captured':
            m.captured_callback = True
        elif state == 'paused':
            require(m.trigger(PAUSE) == 0, 'pause failed')
        require(m.call('dreem_dmaengine_pcm_hw_free', m.sub) == 0 and m.freed_pages == 1,
                'hardware free did not release pages after synchronization')
        require(m.periods == (1 if state == 'captured' else 0), 'callback drain count differs')
        require(m.call('snd_dmaengine_pcm_close', m.sub) == 0 and not m.runtime_live,
                'close after hardware free failed')
        cases.append('buffer and runtime release with ' + str((direction, state)))
    for direction in (0, 1):
        m = LifetimeMachine(image, types, direction=direction)
        m.prepare_config()
        tx = m.cyclic(768, 256, direction=1 + direction)
        require(tx and m.call('vchan_tx_submit', tx) == 8 and m.issue_count == 0,
                'unissued submission fixture failed')
        require(m.call('snd_dmaengine_pcm_sync_stop', m.sub) == 0 and
                not m.descriptor_live and not m.bd_length and m.issue_count == 0,
                'unissued descriptor was leaked or started during retirement')
        cases.append('retire submitted descriptor without issuing DMA ' + str(direction))
        m = LifetimeMachine(image, types, direction=direction)
        m.start()
        m.queue_period()
        m.queued_tasklet = False
        status = bytes(m.cpu.mem_read(BD_ARRAY + 2, 1))[0]
        m.cpu.mem_write(BD_ARRAY + 2, bytes([status & ~1]))
        m.call('vchan_complete', m.vc)
        require(m.periods == 1 and bytes(m.cpu.mem_read(BD_ARRAY + 2, 1))[0] & 1,
                'normal cyclic callback did not recycle the completed period')
        require(m.call('dreem_dmaengine_pcm_hw_free', m.sub) == 0 and m.periods == 1,
                'retirement repeated a completed callback')
        cases.append('actual vchan, SDMA loop and PCM period callback before retirement ' + str(direction))
    for direction in (0, 1):
        m = LifetimeMachine(image, types, direction=direction, width=20, channels=1)
        m.start()
        require(m.trigger(0) == 0, 'stop before parameters failed')
        require(m.call('dmaengine_pcm_hw_params', m.sub, PARAMS) == 0 and
                'allocate pages' in m.history and not m.work_pending,
                'parameter setup did not synchronize before configuration/allocation')
        cases.append('parameters synchronize stopped packed-20 stream ' + str(direction))
    for entry, args in (('dreem_dmaengine_pcm_hw_free', ()),
                        ('dmaengine_pcm_hw_params', (PARAMS,)),
                        ('snd_dmaengine_pcm_close', ()),
                        ('snd_dmaengine_pcm_close_release_chan', ())):
        m = LifetimeMachine(image, types, control_result=-110)
        # Return injection uses the inherited modeled provider callback.
        m.field('dma_device', DEVICE, 'device_terminate_all', CONTROL['device_terminate_all'])
        require(m.call(entry, m.sub, *args) == (-110 & 0xffffffff) and
                m.runtime_live and not m.freed_pages and not m.released and not m.history,
                'provider error was ignored during client resource release')
        cases.append('termination error prevents client release: ' + entry)
    m = LifetimeMachine(image, types)
    ops = image.symbols['dmaengine_pcm_ops']
    require(m.field('snd_pcm_ops', ops, 'prepare') == image.symbols['snd_dmaengine_pcm_sync_stop'] and
            m.field('snd_pcm_ops', ops, 'hw_free') == image.symbols['dreem_dmaengine_pcm_hw_free'] and
            m.field('snd_pcm_ops', ops, 'close') == image.symbols['snd_dmaengine_pcm_close'],
            'PCM operation table does not select synchronization')
    cases.append('PCM callback registration')
    return cases


def dependencies(name, seen=None):
    seen = set() if seen is None else seen
    if name in seen:
        return seen
    seen.add(name)
    for node in ast.walk((ast.parse((Path(__file__).parent / name).read_text()))):
        modules = [node.module] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names] if isinstance(node, ast.Import) else []
        for module in modules:
            child = (module or '').split('.')[0] + '.py'
            if (Path(__file__).parent / child).is_file():
                dependencies(child, seen)
    return seen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build', type=Path)
    parser.add_argument('previous_build', type=Path)
    args = parser.parse_args()
    image, types, matches, generic = load(args.build)
    cases = verify(image, types)
    old, old_types, old_matches, _ = load(args.previous_build, generic)
    m = LifetimeMachine(old, old_types, strict=False)
    m.start()
    m.queue_period()
    require(m.trigger(0) == 0 and not m.descriptor_live and
            m.history.index('free DMA storage') < m.history.index('stop request', 1),
            'old free-before-stop was not reproduced')
    negative = ['old termination frees descriptor storage before issuing stop']
    try:
        m.call('vchan_complete', m.vc)
    except ValueError as error:
        require(str(error) == 'read of freed DMA descriptor', 'unexpected negative-control failure: ' + str(error))
    else:
        raise ValueError('old pending cyclic callback did not read freed descriptor')
    negative.append('old pending cyclic tasklet reads freed descriptor')
    cases.extend(negative)
    print(json.dumps({'kernel_sha256': sha(image.binary), 'previous_kernel_sha256': sha(old.binary),
                      'passed_cases': len(cases), 'cases': cases, 'negative_controls': negative,
                      'linked_objects': matches, 'previous_linked_objects': old_matches,
                      'verifier_sources': {n: sha((Path(__file__).parent / n).read_bytes())
                                           for n in sorted(dependencies(Path(__file__).name))},
                      'hardware_qualified': False,
                      'limits': 'Linked ARM lifecycle callbacks; modeled worker/tasklet scheduling, DMA API dispatch, allocation and MMIO. The upstream 1 ms stop interval is assumed, not measured. No physical DMA completion, concurrent scheduler, full ALSA core or SAI trigger qualification.'}, indent=2))


if __name__ == '__main__':
    main()
