#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Execute PCM submission/control, virtual DMA submission and SDMA issue in ARM.

Allocation, MMIO and channel-zero transactions are modeled. Error callbacks
exercise generic DMA return handling. No ROM script, real transfer, completion,
termination quiescence, SAI trigger or physical audio is qualified.
"""
import argparse
import io
import itertools
import json
from pathlib import Path
from types import SimpleNamespace

from elftools.elf.elffile import ELFFile
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_PC, UC_ARM_REG_LR
from verify_adc_module import Module
from verify_busfreq import require
from verify_wm8960_clocking import attach
from verify_pcm_dma import (PcmMachine, load_objects, OBJECTS, BASE, ENGINE,
                            CHANNEL, PRUNTIME, DESCRIPTOR, BUFFER)
from verify_wm8960_streams import SUB, CAPTURE, PCM
from verify_wm8960_sources import sha

DEVICE, CONTROL_BLOCKS = BASE + 0xb400, BASE + 0xf000
CONTROL = {'device_pause': BASE + 0xf800, 'device_resume': BASE + 0xf804,
           'device_terminate_all': BASE + 0xf808}
START, STOP, PAUSE, RELEASE, SUSPEND, RESUME = 1, 0, 3, 4, 5, 6


class TriggerMachine(PcmMachine):
    def __init__(self, image, types, *, submit_error=0, control_result=0,
                 missing_control=False, real_control=False, **kwargs):
        super().__init__(image, types, **kwargs)
        # Each fixture may override a callback without mutating another image.
        self.stub_addresses = dict(self.stub_addresses)
        self.submit_error, self.control_result = submit_error, control_result
        self.control_calls = []
        self.issue_count = 0
        self.tx = DESCRIPTOR + types.members['sdma_desc']['vd'] + types.members['virt_dma_desc']['tx']
        self.vc = CHANNEL + types.members['sdma_channel']['vc']
        self.field('dma_chan', self.chan, 'device', DEVICE)
        self.field('dma_chan', self.chan, 'cookie', 7)
        self.field('dma_device', DEVICE, 'device_prep_dma_cyclic', self.symbols['sdma_prep_dma_cyclic'])
        self.field('dma_device', DEVICE, 'device_issue_pending', self.symbols['sdma_issue_pending'])
        self.field('sdma_engine', ENGINE, 'channel_control', CONTROL_BLOCKS)
        for name in ('desc_submitted', 'desc_issued', 'desc_completed'):
            self.list_init(self.vc + types.members['virt_dma_chan'][name])
        self.list_init(CHANNEL + types.members['sdma_channel']['pending'])
        self.stub_addresses.pop(self.symbols['vchan_tx_submit'], None)
        if submit_error:
            self.stub_addresses[self.symbols['vchan_tx_submit']] = 'failed_submit'
        for name, address in CONTROL.items():
            self.stub_addresses[address] = name
            self.field('dma_device', DEVICE, name, 0 if missing_control else address)
        if real_control:
            self.field('dma_device', DEVICE, 'device_pause', self.symbols['sdma_channel_pause'])
            self.field('dma_device', DEVICE, 'device_resume', self.symbols['sdma_channel_resume'])
        physical = {16: 2, 20: 3, 24: 4, 32: 4}[self.width]
        for name, value in (('frame_bits', physical * self.channels * 8),
                            ('period_size', 64), ('buffer_size', 192), ('dma_addr', BUFFER)):
            self.field('snd_pcm_runtime', PCM, name, value)
        self.field('dmaengine_pcm_runtime_data', PRUNTIME, 'cookie', 6)
        self.field('dmaengine_pcm_runtime_data', PRUNTIME, 'pos', 99)

    def list_init(self, address):
        self.put(address, address)
        self.put(address + 4, address)

    def code(self, cpu, address, size, extra):
        if address == self.symbols['sdma_issue_pending']:
            self.issue_count += 1
        name = self.stub_addresses.get(address)
        a = cpu.reg_read(UC_ARM_REG_R0)
        if name == 'failed_submit':
            require(a == self.tx, 'wrong submitted descriptor')
            self.control_calls.append('failed_submit')
            result = self.submit_error
        elif name in CONTROL:
            require(a == self.chan, 'wrong DMA channel control')
            self.control_calls.append(name)
            result = self.control_result
        else:
            return super().code(cpu, address, size, extra)
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def trigger(self, command):
        result = self.call('snd_dmaengine_pcm_trigger', SUB if self.direction == 0 else CAPTURE, command)
        require(self.preempt == 0, 'DMA trigger retained preemption disable')
        return result

    def published(self):
        return tuple(self.field('dmaengine_pcm_runtime_data', PRUNTIME, name) for name in ('cookie', 'pos'))


def load_build(path):
    kernel = path / 'kernel'
    types = Module(kernel / 'sound/soc/fsl/imx-wm8960.o')
    for name in ('sound/soc/codecs/wm8960.o', 'sound/soc/fsl/fsl_sai.o',
                 'drivers/base/regmap/regmap.o', *OBJECTS.values()):
        obj = Module(kernel / name)
        types.members.update(obj.members)
        types.sizes.update(obj.sizes)
    image, comparisons = load_objects(kernel / 'vmlinux', kernel / 'sound/soc/fsl/imx-wm8960.o',
                                      {k: kernel / v for k, v in OBJECTS.items()}, types)
    raw = (kernel / 'drivers/dma/virt-dma.o').read_bytes()
    comparisons['virtual_dma'] = attach(image, SimpleNamespace(binary=raw, elf=ELFFile(io.BytesIO(raw))))
    for start, _ in image.ranges:
        image.services.pop(start, None)
    image.services[image.symbols['sdma_run_channel0']] = 'sdma_run_channel0'
    return image, types, comparisons


def verify(image, types):
    cases = []
    for direction, width, channels in itertools.product((0, 1), (16, 20, 24, 32), (1, 2)):
        m = TriggerMachine(image, types, direction=direction, width=width, channels=channels)
        physical = m.prepare_config()
        require(m.trigger(START) == 0 and m.published() == (8, 0) and m.issue_count == 1,
                'PCM start failed to publish submitted cookie or issue once')
        require(m.field('dma_async_tx_descriptor', m.tx, 'callback') == image.symbols['imx_pcm_dma_complete'] and
                m.field('dma_async_tx_descriptor', m.tx, 'callback_param') == (SUB if direction == 0 else CAPTURE),
                'PCM callback or callback parameter differs')
        require(m.field('dma_async_tx_descriptor', m.tx, 'flags') == 3 and
                m.field('sdma_channel', CHANNEL, 'period_len') == physical * channels * 64,
                'PCM byte conversion or interrupt flags differ')
        node = DESCRIPTOR + types.members['sdma_desc']['vd'] + types.members['virt_dma_desc']['node']
        issued = m.vc + types.members['virt_dma_chan']['desc_issued']
        require(m.field('list_head', issued, 'next') == node and
                m.field('list_head', node, 'next') == issued and
                m.field('sdma_channel', CHANNEL, 'desc') == DESCRIPTOR,
                'submitted descriptor did not reach SDMA active list')
        control = CONTROL_BLOCKS + 2 * types.sizes['sdma_channel_control']
        require(m.field('sdma_channel_control', control, 'base_bd_ptr') == 0x91000000 and
                m.field('sdma_channel_control', control, 'current_bd_ptr') == 0x91000000 and
                m.mmio_writes[-1] == (0x0c, 4), 'SDMA descriptor pointers or final enable write differ')
        # DMA owns this descriptor after submission; no synthetic unsafe free.
        cases.append('PCM -> cyclic preparation -> vchan submit -> SDMA issue ' + str((direction, width, channels)))
    for direction, error in itertools.product((0, 1), (-5, -22, -110)):
        m = TriggerMachine(image, types, direction=direction, submit_error=error)
        m.prepare_config()
        before = len(m.mmio_writes)
        require(m.trigger(START) == (error & 0xffffffff) and m.published() == (6, 99) and
                len(m.mmio_writes) == before and m.issue_count == 0 and m.control_calls == ['failed_submit'],
                'failed submission published state, issued DMA or lost error')
        cases.append('submission error leaves cookie/position and does not issue ' + str((direction, error)))
    for direction in (0, 1):
        m = TriggerMachine(image, types, direction=direction, fail_allocation=True)
        m.prepare_config()
        before = len(m.mmio_writes)
        require(m.trigger(START) == (-12 & 0xffffffff) and m.published() == (6, 99) and
                len(m.mmio_writes) == before and m.issue_count == 0 and not m.control_calls,
                'failed preparation issued or changed runtime')
        cases.append('preparation allocation failure ' + str(direction))
    commands = ((STOP, 'device_terminate_all', False), (PAUSE, 'device_pause', False),
                (RELEASE, 'device_resume', False), (RESUME, 'device_resume', False),
                (SUSPEND, 'device_terminate_all', False), (SUSPEND, 'device_pause', True))
    for (cmd, expected, pause), error, missing in itertools.product(commands, (0, -5, -110), (False, True)):
        m = TriggerMachine(image, types, control_result=error, missing_control=missing)
        m.field('snd_pcm_runtime', PCM, 'info', 0x80000 if pause else 0)
        require(m.trigger(cmd) == ((-38 if missing else error) & 0xffffffff) and
                m.control_calls == ([] if missing else [expected]) and m.published() == (6, 99),
                'DMA control dispatch/error/unsupported-operation result differs')
        cases.append('DMA control result ' + str((cmd, pause, error, missing)))
    for cmd in (2, 7, 0xffffffff):
        m = TriggerMachine(image, types)
        require(m.trigger(cmd) == (-22 & 0xffffffff) and not m.control_calls and
                not m.mmio_writes and m.published() == (6, 99), 'invalid trigger touched DMA')
        cases.append('invalid trigger ' + str(cmd))
    for direction in (0, 1):
        m = TriggerMachine(image, types, direction=direction, real_control=True)
        m.prepare_config()
        require(m.trigger(START) == 0 and m.trigger(PAUSE) == 0 and m.mmio_writes[-1] == (8, 4) and
                m.field('sdma_channel', CHANNEL, 'status') == 2, 'real SDMA pause dispatch differs')
        require(m.trigger(RELEASE) == 0 and m.mmio_writes[-1] == (12, 4) and
                m.field('sdma_channel', CHANNEL, 'status') == 1, 'real SDMA resume dispatch differs')
        require(m.trigger(PAUSE) == 0, 'second pause failed')
        m.field('sdma_engine', ENGINE, 'suspend_off', 1, size=1)
        m.field('sdma_channel', CHANNEL, 'context_loaded', 0, size=1)
        m.context_error = -110
        before = len(m.mmio_writes)
        require(m.trigger(RESUME) == (-22 & 0xffffffff) and len(m.mmio_writes) == before and
                m.field('sdma_channel', CHANNEL, 'status') == 2, 'failed SDMA restore was ignored or enabled channel')
        cases.append('real SDMA pause/resume and failed context restore ' + str(direction))
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build', type=Path)
    parser.add_argument('previous_build', type=Path)
    args = parser.parse_args()
    image, types, matches = load_build(args.build)
    cases = verify(image, types)
    old, old_types, previous = load_build(args.previous_build)
    negative = []
    m = TriggerMachine(old, old_types, submit_error=-5)
    m.prepare_config()
    require(m.trigger(START) == 0 and m.published() == (-5 & 0xffffffff, 0) and m.issue_count == 1,
            'old failed-submit success/issue not reproduced')
    negative.append('old PCM calls issue_pending after failed submission and publishes the error cookie')
    for cmd in (STOP, PAUSE, RESUME):
        m = TriggerMachine(old, old_types, control_result=-110)
        require(m.trigger(cmd) == 0 and len(m.control_calls) == 1, 'old ignored DMA error not reproduced')
        negative.append('old PCM ignores DMA control failure ' + str(cmd))
    cases.extend(negative)
    dependencies = ('verify_pcm_trigger.py', 'verify_pcm_dma.py', 'verify_wm8960_clocking.py',
                    'verify_wm8960_streams.py', 'verify_wm8960_lifetime.py', 'verify_adc_module.py',
                    'verify_busfreq.py', 'verify_wm8960_sources.py', 'verify_ddr_preparation.py',
                    'verify_ddr_sources.py', 'arm_relocations.py', 'verify_wm8960_board_sources.py',
                    'verify_adc_control.py', 'verify_adc_init.py', 'verify_adc_read.py',
                    'verify_adc_test_signal.py')
    print(json.dumps({'kernel_sha256': sha(image.binary), 'previous_kernel_sha256': sha(old.binary),
                      'passed_cases': len(cases), 'cases': cases, 'negative_controls': negative,
                      'linked_objects': matches, 'previous_linked_objects': previous,
                      'verifier_sources': {n: sha((Path(__file__).parent / n).read_bytes()) for n in dependencies},
                      'hardware_qualified': False,
                      'limits': 'Actual PCM/cyclic SDMA/vchan submit/SDMA issue instructions; modeled MMIO, allocation, control errors and channel-zero transactions. No ROM transfers, DMA quiescence, scheduler, SAI trigger, ISR or physical audio.'}, indent=2))


if __name__ == '__main__':
    main()
