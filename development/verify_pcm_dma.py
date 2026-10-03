#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Execute PCM configuration and cyclic SAI descriptor preparation in ARM code.

Allocation, channel-zero transactions and kernel services are models. This does not
run the ROM script, transfer sample bytes, submit DMA, or qualify physical audio.
"""
import argparse
import io
import itertools
import json
from pathlib import Path
import struct
from types import SimpleNamespace

from elftools.elf.elffile import ELFFile
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_PC, UC_ARM_REG_LR
from verify_adc_module import Module
from verify_busfreq import require
from verify_wm8960_lifetime import load
from verify_wm8960_clocking import ClockMachine, attach, arithmetic
from verify_wm8960_streams import SUB, CAPTURE, PCM, PARAMS, SAI, CPU_DAI
from verify_wm8960_sources import sha

BASE, ENGINE, CHANNEL, CONFIG, PRUNTIME, DESCRIPTOR, BD_ARRAY = (
    0x54000000, 0x54000000, 0x54007000, 0x54008000, 0x54009000, 0x5400a000, 0x54010000)
BUFFER = 0x90000000
MMIO, DRVDATA, SCRIPTS = 0x020ec000, BASE + 0xb000, BASE + 0xc000
CONTEXT, COMMAND = BASE + 0xd000, BASE + 0xe000
OBJECTS = {'sdma': 'drivers/dma/imx-sdma.o', 'pcm': 'sound/core/pcm_dmaengine.o',
           'imx_pcm': 'sound/soc/fsl/imx-pcm-dma.o'}


class PcmMachine(ClockMachine):
    def __init__(self, image, types, *, direction=0, fail_allocation=False, context_error=0, **kwargs):
        super().__init__(image, types, **kwargs)
        self.cpu.mem_map(BASE, 0x20000)
        self.cpu.mem_map(MMIO, 0x1000)
        self.direction, self.fail_allocation = direction, fail_allocation
        self.context_error = context_error
        self.descriptor_live, self.bd_length, self.preempt = False, 0, 0
        self.dma_events = []
        self.mmio_writes = []
        self.chan = CHANNEL + types.members['sdma_channel']['vc'] + types.members['virt_dma_chan']['chan']
        self.field('sdma_channel', CHANNEL, 'sdma', ENGINE)
        self.field('sdma_channel', CHANNEL, 'channel', 2)
        self.field('sdma_channel', CHANNEL, 'peripheral_type', 24)
        self.field('sdma_channel', CHANNEL, 'direction', 1 + direction)
        self.field('sdma_channel', CHANNEL, 'event_id0', 38 if direction == 0 else 37)
        for name, value in (('regs', MMIO), ('drvdata', DRVDATA), ('script_addrs', SCRIPTS),
                            ('context', CONTEXT), ('context_phys', 0x9100d000), ('bd0', COMMAND)):
            self.field('sdma_engine', ENGINE, name, value)
        self.field('sdma_driver_data', DRVDATA, 'num_events', 48)
        self.field('sdma_driver_data', DRVDATA, 'chnenbl0', 0x200)
        for name, value in (('app_2_mcu_addr', 683), ('mcu_2_app_addr', 747)):
            self.field('sdma_script_start_addrs', SCRIPTS, name, value)
        self.field('snd_pcm_runtime', PCM, 'private_data', PRUNTIME)
        self.field('dmaengine_pcm_runtime_data', PRUNTIME, 'dma_chan', self.chan)
        for stream, member in enumerate(('playback_dma_data', 'capture_dma_data')):
            data = SAI + types.members['fsl_sai']['dma_params_tx' if stream == 0 else 'dma_params_rx']
            self.field('snd_soc_dai', CPU_DAI, member, data)
            self.field('snd_dmaengine_dai_dma_data', data, 'addr', 0x0202c020 + stream * 0x80)
            self.field('snd_dmaengine_dai_dma_data', data, 'maxburst', 6)

    def write(self, cpu, access, address, size, value, extra):
        if MMIO <= address < MMIO + 0x1000:
            require(size == 4, 'unexpected SDMA register width')
            self.mmio_writes.append((address - MMIO, value))
            return
        if BD_ARRAY <= address < BASE + 0x20000:
            require(self.bd_length and address + size <= BD_ARRAY + self.bd_length,
                    'descriptor allocation overrun or access after free')
            return
        if BASE <= address and address + size <= BD_ARRAY:
            return
        return super().write(cpu, access, address, size, value, extra)

    def code(self, cpu, address, size, extra):
        name = self.stub_addresses.get(address)
        a, b, c, d = [cpu.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
        result = 0
        if name == 'snd_pcm_format_physical_width':
            result = {2: 16, 36: 24, 6: 32, 10: 32}.get(a, -22)
        elif name == 'sdma_run_channel0':
            require(a == ENGINE, 'wrong context engine')
            self.dma_events.append('load context')
            result = self.context_error
        elif name == 'kmem_cache_alloc':
            require(not self.descriptor_live, 'duplicate descriptor allocation')
            self.dma_events.append('allocate descriptor')
            if not self.fail_allocation:
                self.descriptor_live = True
                cpu.mem_write(DESCRIPTOR, bytes(self.types.sizes['sdma_desc']))
                result = DESCRIPTOR
        elif name == 'gen_pool_dma_alloc':
            require(self.descriptor_live and not self.bd_length and 0 < b <= 0x10000,
                    'bad descriptor buffer allocation')
            self.bd_length = b
            self.put(c, 0x91000000)
            result = BD_ARRAY
            self.dma_events.append('allocate buffer')
        elif name == 'gen_pool_free':
            require(b == BD_ARRAY and c == self.bd_length and self.bd_length > 0, 'wrong descriptor buffer release')
            self.bd_length = 0
            self.dma_events.append('free buffer')
        elif name == 'kfree':
            require(a == DESCRIPTOR and self.descriptor_live and not self.bd_length, 'descriptor released before its buffer')
            self.descriptor_live = False
            self.dma_events.append('free descriptor')
        elif name == 'dma_async_tx_descriptor_init':
            require(a == DESCRIPTOR + self.types.members['sdma_desc']['vd'] + self.types.members['virt_dma_desc']['tx'] and
                    b == self.chan, 'wrong DMA transaction initializer')
            self.field('dma_async_tx_descriptor', a, 'chan', b)
        elif name in ('preempt_count_add', 'preempt_count_sub'):
            self.preempt += a if name.endswith('add') else -a
            require(self.preempt >= 0, 'unbalanced preemption')
        elif name == 'preempt_schedule':
            require(self.preempt == 0, 'scheduled inside atomic region')
        elif name == '__memzero':
            require(BASE <= a and a + b <= BASE + 0x20000, 'unexpected DMA zeroing')
            cpu.mem_write(a, bytes(b))
        else:
            return super().code(cpu, address, size, extra)
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def prepare_config(self):
        sub = SUB if self.direction == 0 else CAPTURE
        require(self.call('imx_pcm_dma_prepare_slave_config', sub, PARAMS, CONFIG) == 0,
                'PCM slave configuration failed')
        prefix = 'dst_' if self.direction == 0 else 'src_'
        physical = {16: 2, 20: 3, 24: 4, 32: 4}[self.width]
        require(self.field('dma_slave_config', CONFIG, 'direction') == self.direction + 1 and
                self.field('dma_slave_config', CONFIG, prefix + 'addr_width') == physical and
                self.field('dma_slave_config', CONFIG, prefix + 'addr') == 0x0202c020 + self.direction * 0x80 and
                self.field('dma_slave_config', CONFIG, prefix + 'maxburst') == 6,
                'PCM direction, physical width, FIFO or burst count differs')
        require(self.field('dmaengine_pcm_runtime_data', PRUNTIME, 'callback') == self.symbols['imx_pcm_dma_complete'],
                'PCM period callback missing')
        require(self.call('sdma_config', self.chan, CONFIG) == 0, 'SDMA configuration failed')
        require(self.field('sdma_channel', CHANNEL, 'word_size') == physical and
                self.field('sdma_channel', CHANNEL, 'watermark_level') == physical * 6 and
                self.field('sdma_channel', CHANNEL, 'per_address') == 0x0202c020 + self.direction * 0x80,
                'SDMA lost the PCM transfer configuration')
        return physical

    def cyclic(self, length, period, address=BUFFER, direction=None):
        result = self.call('sdma_prep_dma_cyclic', self.chan, address, length, period,
                           1 + self.direction if direction is None else direction, 3)
        require(self.preempt == 0, 'cyclic preparation retained preemption disable')
        return result

    def release(self):
        self.call('sdma_desc_free', DESCRIPTOR + self.types.members['sdma_desc']['vd'])
        require(not self.descriptor_live and not self.bd_length and not self.preempt and
                self.field('sdma_channel', CHANNEL, 'bd_size_sum') == 0, 'descriptor resources leaked')


def verify(image, types):
    cases = []
    for direction, width, channels, frames in itertools.product((0, 1), (16, 20, 24, 32), (1, 2), (64, 255)):
        m = PcmMachine(image, types, direction=direction, width=width, channels=channels)
        physical = m.prepare_config()
        period, periods = frames * channels * physical, 3
        transaction = m.cyclic(period * periods, period)
        expected = DESCRIPTOR + types.members['sdma_desc']['vd'] + types.members['virt_dma_desc']['tx']
        require(transaction == expected and m.bd_length == periods * 12, 'cyclic transaction/descriptor count differs')
        require(m.field('dma_async_tx_descriptor', transaction, 'flags') == 3, 'DMA flags lost')
        for i in range(periods):
            count, status, command, address, extra = struct.unpack('<HBBII', bytes(m.cpu.mem_read(BD_ARRAY + i * 12, 12)))
            require((count, status, command, address, extra) ==
                    (period, 0x8f if i == periods - 1 else 0x8d, physical % 4, BUFFER + i * period, 0),
                    'cyclic descriptor count, command, wrap or address differs')
        m.release()
        cases.append('PCM -> SDMA -> cyclic descriptors ' + str((direction, width, channels, frames)))
    for width in (16, 20, 24, 32):
        m = PcmMachine(image, types, width=width)
        physical = m.prepare_config()
        require(m.cyclic(65532 * 2, 65532) != 0, 'maximum valid period rejected')
        m.release()
        cases.append('maximum period ' + str(width))
    for direction in (0, 1):
        m = PcmMachine(image, types, direction=direction, context_error=-110)
        sub = SUB if direction == 0 else CAPTURE
        require(m.call('imx_pcm_dma_prepare_slave_config', sub, PARAMS, CONFIG) == 0 and
                m.call('sdma_config', m.chan, CONFIG) == (-110 & 0xffffffff),
                'context setup error was discarded')
        require(not m.field('sdma_channel', CHANNEL, 'context_loaded', size=1),
                'failed context was marked loaded')
        m.context_error = 0
        m.prepare_config()
        require(m.field('sdma_channel', CHANNEL, 'context_loaded', size=1), 'context retry did not succeed')
        m.field('sdma_channel', CHANNEL, 'context_loaded', 0, size=1)
        m.context_error = -110
        require(m.cyclic(768, 256) == 0 and not m.descriptor_live and not m.bd_length and
                m.field('sdma_channel', CHANNEL, 'bd_size_sum') == 0,
                'context failure during descriptor preparation leaked allocations')
        require(m.cyclic(768, 256) == 0 and not m.descriptor_live and not m.bd_length,
                'persistent context error falsely succeeded or leaked allocations')
        m.context_error = 0
        require(m.cyclic(768, 256), 'context failure poisoned cyclic retry')
        m.release()
        cases.append('context failure cleanup and retry ' + str(direction))
    malformed = [(0, 128, BUFFER, 1, 2), (256, 0, BUFFER, 1, 2),
                 (640, 384, BUFFER, 1, 2), (128, 256, BUFFER, 1, 2),
                 (131070, 65535, BUFFER, 1, 3), (512, 256, 0xffffff00, 1, 2),
                 (0x80000000, 128, 0, 1, 2), (0x7fffffff, 1, 0, 1, 1),
                 (512, 256, BUFFER, 1, 0), (512, 256, BUFFER, 1, 8),
                 (510, 255, BUFFER, 1, 2), (512, 256, BUFFER, 1, 3),
                 (512, 256, BUFFER, 0, 2), (512, 256, BUFFER, 3, 2)]
    for length, period, address, direction, width in malformed:
        m = PcmMachine(image, types)
        m.field('sdma_channel', CHANNEL, 'word_size', width)
        before = bytes(m.cpu.mem_read(CHANNEL, types.sizes['sdma_channel']))
        require(m.cyclic(length, period, address, direction) == 0 and not m.dma_events,
                'malformed cyclic request allocated or configured resources')
        require(bytes(m.cpu.mem_read(CHANNEL, len(before))) == before, 'malformed cyclic request changed channel state')
        cases.append('malformed cyclic request rejected before allocation ' + str((length, period, address, direction, width)))
    m = PcmMachine(image, types, fail_allocation=True)
    m.prepare_config()
    require(m.cyclic(768, 256) == 0 and not m.descriptor_live and not m.bd_length, 'descriptor allocation failure mishandled')
    cases.append('descriptor allocation failure')
    m = PcmMachine(image, types)
    require(m.field('snd_pcm_hardware', image.symbols['imx_pcm_hardware'], 'period_bytes_max') == 65532,
            'ALSA advertises an oversized SDMA period')
    cases.append('ALSA period bound matches descriptor limit')
    return cases


def load_objects(kernel, board, extras, types=None):
    if types is None:
        types = Module(board)
    image = load(kernel, types)
    comparisons = {}
    for name, path in extras.items():
        raw = path.read_bytes()
        obj = SimpleNamespace(binary=raw, elf=ELFFile(io.BytesIO(raw)))
        comparisons[name] = attach(image, obj)
    # Calls between the attached objects execute their linked instructions.
    for start, _ in image.ranges:
        image.services.pop(start, None)
    # Context preparation executes; the channel-zero hardware transaction
    # remains a modeled service with explicit success/failure results.
    image.services[image.symbols['sdma_run_channel0']] = 'sdma_run_channel0'
    arithmetic(image)
    return image, comparisons


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build', type=Path)
    parser.add_argument('previous_build', type=Path)
    args = parser.parse_args()
    kernel = args.build / 'kernel'
    board = Module(kernel / 'sound/soc/fsl/imx-wm8960.o')
    for name in ['sound/soc/codecs/wm8960.o', 'sound/soc/fsl/fsl_sai.o',
                 'drivers/base/regmap/regmap.o', *OBJECTS.values()]:
        obj = Module(kernel / name)
        board.members.update(obj.members)
        board.sizes.update(obj.sizes)
    image, comparisons = load_objects(kernel / 'vmlinux', kernel / 'sound/soc/fsl/imx-wm8960.o',
                                      {k: kernel / v for k, v in OBJECTS.items()}, board)
    cases = verify(image, board)
    old = args.previous_build / 'kernel'
    # SDMA's channel array changes the engine layout when fields are added.
    # Negative controls must use their own DWARF, not the rebuilt provider's
    # offsets. Older PCM objects lack debug info; unchanged PCM-only layouts
    # fall back to the current header definitions.
    old_board = Module(old / 'sound/soc/fsl/imx-wm8960.o')
    for name in ['sound/soc/codecs/wm8960.o', 'sound/soc/fsl/fsl_sai.o',
                 'drivers/base/regmap/regmap.o', *OBJECTS.values()]:
        raw = (old / name).read_bytes()
        if ELFFile(io.BytesIO(raw)).has_dwarf_info():
            obj = Module(old / name)
            old_board.members.update(obj.members)
            old_board.sizes.update(obj.sizes)
    for name, members in board.members.items():
        if name not in old_board.members:
            old_board.members[name] = members
            old_board.sizes[name] = board.sizes[name]
    previous, old_comparisons = load_objects(old / 'vmlinux', old / 'sound/soc/fsl/imx-wm8960.o',
                                             {k: old / v for k, v in OBJECTS.items()}, old_board)
    m = PcmMachine(previous, old_board)
    m.prepare_config()
    try:
        m.cyclic(640, 384)
    except ValueError as error:
        require('descriptor allocation overrun' in str(error), 'wrong original failure: ' + str(error))
    else:
        raise ValueError('original descriptor overrun not reproduced')
    m = PcmMachine(previous, old_board, width=20, channels=1)
    m.prepare_config()
    require(m.field('snd_pcm_hardware', previous.symbols['imx_pcm_hardware'], 'period_bytes_max') == 65535 and
            m.cyclic(131070, 65535) == 0, 'original advertised-but-rejected period not reproduced')
    negative = ['original cyclic descriptor array overrun', 'original PCM advertises a period SDMA rejects']
    m = PcmMachine(previous, old_board, context_error=-110)
    require(m.call('imx_pcm_dma_prepare_slave_config', SUB, PARAMS, CONFIG) == 0 and
            m.call('sdma_config', m.chan, CONFIG) == (-110 & 0xffffffff) and
            m.field('sdma_channel', CHANNEL, 'context_loaded', size=1) == 1,
            'original failed-context flag not reproduced')
    negative.append('original context failure is marked loaded')
    m.field('sdma_channel', CHANNEL, 'context_loaded', 0, size=1)
    try:
        m.cyclic(768, 256)
    except ValueError as error:
        require('descriptor released before its buffer' in str(error), 'wrong original allocation failure')
    else:
        raise ValueError('original context-failure buffer leak not reproduced')
    negative.append('original context failure leaks descriptor buffer')
    cases.extend(negative)
    dependencies = ('verify_pcm_dma.py', 'verify_wm8960_clocking.py', 'verify_wm8960_streams.py',
                    'verify_wm8960_lifetime.py', 'verify_adc_module.py', 'verify_busfreq.py',
                    'verify_wm8960_sources.py', 'verify_ddr_preparation.py', 'verify_ddr_sources.py', 'arm_relocations.py')
    print(json.dumps({'kernel_sha256': sha(image.binary), 'previous_kernel_sha256': sha(previous.binary),
                      'passed_cases': len(cases), 'cases': cases, 'negative_controls': negative,
                      'linked_objects': comparisons, 'previous_linked_objects': old_comparisons,
                      'objects': {k: sha((kernel / v).read_bytes()) for k, v in OBJECTS.items()},
                      'verifier_sources': {n: sha((Path(__file__).parent / n).read_bytes()) for n in dependencies},
                      'hardware_qualified': False,
                      'limits': 'Actual PCM configuration, context preparation and cyclic descriptor ARM instructions; modeled MMIO, allocation and channel-zero transactions. No ROM script, submitted DMA, sample transfer, IRQ scheduling, or physical audio.'}, indent=2))


if __name__ == '__main__':
    main()
