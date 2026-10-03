#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Execute linked ASoC rollback and its connected PCM/SDMA retirement path.

Codec/CPU/machine trigger results, scheduler and MMIO are models. The connected
platform executes real ARM DMA submission, controls and retirement callbacks.
This does not qualify physical SAI triggering or ALSA/DPCM scheduling.
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
from verify_busfreq import require
from verify_pcm_lifetime import LifetimeMachine, load as load_lifetime, dependencies, GENERIC
from verify_pcm_trigger import START, STOP, PAUSE, RELEASE, SUSPEND, RESUME
from verify_pcm_dma import CHANNEL, DESCRIPTOR
from verify_wm8960_clocking import attach, linked_object
from verify_wm8960_lifetime import BoardMachine, DATA, load as load_board
from verify_wm8960_streams import RUNTIME, CPU_DAI, PCM
from verify_wm8960_sources import sha, compare, imm, signed16

SOC = 0x56000000
LINK, PLATFORM_DRIVER, PLATFORM_OPS, CODEC_ARRAY, MACHINE_OPS = (SOC + n * 0x1000 for n in range(5))
CODECS = (SOC + 0x5000, SOC + 0x6000)
DRIVERS = (SOC + 0x7000, SOC + 0x8000, SOC + 0x9000)
DAI_OPS = (SOC + 0xa000, SOC + 0xb000, SOC + 0xc000)
CALLBACKS = tuple(SOC + 0x10000 + 4 * n for n in range(5))
INVERSE = {START: STOP, RELEASE: PAUSE, RESUME: SUSPEND}
MASK = 0xffffffff


class SocMachine(LifetimeMachine):
    def __init__(self, image, types, *, failures=None, enabled=True,
                 platform_real=False, missing=(), **kwargs):
        super().__init__(image, types, real_control=True, **kwargs)
        self.cpu.mem_map(SOC, 0x20000)
        self.failures = failures or {}
        self.platform_real = platform_real
        self.trace, self.rollback_errors = [], []
        self.field('snd_soc_pcm_runtime', RUNTIME, 'dai_link', LINK)
        self.field('snd_soc_pcm_runtime', RUNTIME, 'dev', self.platform_device())
        self.field('snd_soc_pcm_runtime', RUNTIME, 'num_codecs', 2)
        self.field('snd_soc_pcm_runtime', RUNTIME, 'codec_dais', CODEC_ARRAY)
        if 'dreem_trigger_rollback' in types.members['snd_soc_dai_link']:
            self.field('snd_soc_dai_link', LINK, 'dreem_trigger_rollback', int(enabled), size=1)
        for n, dai in enumerate((*CODECS, CPU_DAI)):
            if n < 2:
                self.put(CODEC_ARRAY + n * 4, dai)
            stage = n if n < 2 else 3
            self.field('snd_soc_dai', dai, 'driver', DRIVERS[n])
            self.field('snd_soc_dai_driver', DRIVERS[n], 'ops', DAI_OPS[n])
            self.field('snd_soc_dai_ops', DAI_OPS[n], 'trigger', 0 if stage in missing else CALLBACKS[stage])
        platform = GENERIC + types.members['dmaengine_pcm']['platform']
        self.field('snd_soc_platform', platform, 'driver', PLATFORM_DRIVER)
        self.field('snd_soc_platform_driver', PLATFORM_DRIVER, 'ops', PLATFORM_OPS)
        self.field('snd_pcm_ops', PLATFORM_OPS, 'trigger', 0 if 2 in missing else CALLBACKS[2])
        self.field('snd_soc_dai_link', LINK, 'ops', MACHINE_OPS)
        self.field('snd_soc_ops', MACHINE_OPS, 'trigger', 0 if 4 in missing else CALLBACKS[4])
        for stage, address in enumerate(CALLBACKS):
            self.stub_addresses[address] = 'soc_stage_' + str(stage)

    def write(self, cpu, access, address, size, value, extra):
        if SOC <= address and address + size <= SOC + 0x20000:
            return
        return super().write(cpu, access, address, size, value, extra)

    def code(self, cpu, address, size, extra):
        name = self.stub_addresses.get(address, '')
        if name.startswith('soc_stage_'):
            stage = int(name.rsplit('_', 1)[1])
            sub, cmd, dai = (cpu.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2))
            require(sub == self.sub, 'trigger received wrong substream')
            if stage in (0, 1, 3):
                require(dai == (CODECS[stage] if stage < 2 else CPU_DAI), 'wrong trigger DAI')
            self.trace.append((stage, cmd))
            if stage == 2 and self.platform_real and (stage, cmd) not in self.failures:
                if cmd == START:
                    self.started, self.settled = True, False
                cpu.reg_write(UC_ARM_REG_PC, self.symbols['snd_dmaengine_pcm_trigger'])
                return
            result = self.failures.get((stage, cmd), 0)
            cpu.reg_write(UC_ARM_REG_R0, result & MASK)
            cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))
            return
        if name == 'dev_err':
            self.rollback_errors.append((cpu.reg_read(UC_ARM_REG_R2), cpu.reg_read(UC_ARM_REG_R3)))
        return super().code(cpu, address, size, extra)

    def soc(self, cmd):
        ret = self.call('soc_pcm_trigger', self.sub, cmd)
        require(self.preempt == 0 and not self.work_running, 'trigger left atomic or worker context')
        return ret


def load(path, *, old=False):
    image, types, matches, _ = load_lifetime(path)
    objpath = path / 'kernel/sound/soc/soc-pcm.o'
    if not old:
        obj = Module(objpath)
        types.members.update(obj.members)
        types.sizes.update(obj.sizes)
    raw = objpath.read_bytes()
    matches['soc_pcm'] = attach(image, SimpleNamespace(binary=raw, elf=ELFFile(io.BytesIO(raw))))
    for start, _ in image.ranges:
        image.services.pop(start, None)
    image.services[image.symbols['sdma_run_channel0']] = 'sdma_run_channel0'
    return image, types, matches


def verify(image, types):
    cases = []
    for direction, cmd in itertools.product((0, 1), (*INVERSE, STOP, PAUSE, SUSPEND)):
        m = SocMachine(image, types, direction=direction)
        require(m.soc(cmd) == 0 and m.trace == [(i, cmd) for i in range(5)], 'success order changed')
        cases.append('all trigger stages succeed ' + str((direction, cmd)))
    for direction, cmd, failed, error in itertools.product((0, 1), INVERSE, range(5), (-5, -110)):
        m = SocMachine(image, types, direction=direction, failures={(failed, cmd): error})
        expected = [(i, cmd) for i in range(failed + 1)] + [(i, INVERSE[cmd]) for i in range(failed, -1, -1)]
        require(m.soc(cmd) == (error & MASK) and m.trace == expected, 'start failure did not unwind attempted stages')
        cases.append('reverse rollback includes failing stage ' + str((direction, cmd, failed, error)))
    for cmd, failed_cleanup in itertools.product(INVERSE, range(5)):
        m = SocMachine(image, types, failures={(4, cmd): -5, (failed_cleanup, INVERSE[cmd]): -110})
        require(m.soc(cmd) == (-5 & MASK) and
                m.trace == [(i, cmd) for i in range(5)] + [(i, INVERSE[cmd]) for i in range(4, -1, -1)] and
                m.rollback_errors == [(failed_cleanup, -110 & MASK)], 'cleanup error hid primary error or stopped unwind')
        cases.append('rollback error reported and remaining cleanup attempted ' + str((cmd, failed_cleanup)))
    for cmd, failed in itertools.product((STOP, PAUSE, SUSPEND), range(5)):
        m = SocMachine(image, types, failures={(failed, cmd): -110, ((failed + 1) % 5, cmd): -5})
        first = -5 if failed == 4 else -110
        require(m.soc(cmd) == (first & MASK) and m.trace == [(i, cmd) for i in range(5)],
                'stop skipped a stage or lost first error')
        cases.append('stop attempts all stages and preserves first error ' + str((cmd, failed)))
    for mask in range(32):
        missing = {n for n in range(5) if mask & (1 << n)}
        m = SocMachine(image, types, missing=missing, failures={(4, START): -5})
        expected = [(i, START) for i in range(5) if i not in missing]
        if 4 not in missing:
            expected += [(i, STOP) for i in range(4, -1, -1) if i not in missing]
        require(m.soc(START) == (0 if 4 in missing else -5 & MASK) and m.trace == expected,
                'absent callback handling differs')
        cases.append('optional trigger callbacks ' + str(mask))
    for cmd in (2, 7, MASK):
        m = SocMachine(image, types)
        require(m.soc(cmd) == (-22 & MASK) and not m.trace, 'invalid command reached a callback')
        cases.append('invalid command ' + str(cmd))
    for kind in ('empty-ops', 'no-codecs', 'positive-result'):
        m = SocMachine(image, types)
        expected = [(i, START) for i in range(5)]
        if kind == 'empty-ops':
            for driver in DRIVERS:
                m.field('snd_soc_dai_driver', driver, 'ops', 0)
            m.field('snd_soc_platform_driver', PLATFORM_DRIVER, 'ops', 0)
            m.field('snd_soc_dai_link', LINK, 'ops', 0)
            expected = []
        elif kind == 'no-codecs':
            m.field('snd_soc_pcm_runtime', RUNTIME, 'num_codecs', 0)
            expected = [(i, START) for i in (2, 3, 4)]
        else:
            m.failures = {(i, START): 1 for i in range(5)}
        require(m.soc(START) == 0 and m.trace == expected, 'optional callback container/result handling differs')
        cases.append('optional trigger structure ' + kind)
    for cmd in (START, STOP):
        m = SocMachine(image, types, enabled=False, failures={(2, cmd): -5})
        require(m.soc(cmd) == (-5 & MASK) and m.trace == [(i, cmd) for i in range(3)],
                'unselected link behavior changed')
        cases.append('unselected link preserves legacy dispatch ' + str(cmd))
    for direction, width, channels, failure in itertools.product((0, 1), (16, 20, 24, 32), (1, 2), (3, 4)):
        m = SocMachine(image, types, direction=direction, width=width, channels=channels,
                       platform_real=True, failures={(failure, START): -5})
        m.prepare_config()
        require(m.soc(START) == (-5 & MASK) and m.issue_count == 1 and m.work_pending and
                m.descriptor_live and m.field('sdma_channel', CHANNEL, 'desc') == 0 and
                m.field('sdma_channel', CHANNEL, 'status') == 3 and m.mmio_writes[-1] == (8, 4),
                'late start failure left DMA active or freed it in trigger')
        require(m.call('snd_dmaengine_pcm_sync_stop', m.sub) == 0 and
                not m.work_pending and not m.descriptor_live, 'failed-start retirement was not drained')
        m.failures = {}
        require(m.soc(START) == 0 and m.published()[0] == 9 and m.issue_count == 2,
                'retry after synchronized rollback did not start DMA')
        require(m.soc(STOP) == 0 and m.call('dreem_dmaengine_pcm_hw_free', m.sub) == 0 and
                m.freed_pages == 1, 'retry cleanup failed')
        cases.append('connected PCM/SDMA late failure, retirement and retry ' + str((direction, width, channels, failure)))
    for direction, cmd, failure in itertools.product((0, 1), (RELEASE, RESUME), (3, 4)):
        m = SocMachine(image, types, direction=direction, platform_real=True)
        m.prepare_config()
        m.field('snd_pcm_runtime', PCM, 'info', 0x80000)
        require(m.soc(START) == 0 and m.soc(INVERSE[cmd]) == 0, 'pause fixture failed')
        m.failures = {(failure, cmd): -110}
        require(m.soc(cmd) == (-110 & MASK) and m.descriptor_live and not m.work_pending and
                m.field('sdma_channel', CHANNEL, 'desc') == DESCRIPTOR and
                m.field('sdma_channel', CHANNEL, 'status') == 2 and m.mmio_writes[-1] == (8, 4),
                'failed release/resume did not retain paused DMA')
        m.failures = {}
        require(m.soc(cmd) == 0 and m.field('sdma_channel', CHANNEL, 'status') == 1,
                'release/resume retry failed')
        require(m.call('dreem_dmaengine_pcm_hw_free', m.sub) == 0, 'paused path cleanup failed')
        cases.append('connected failed release/resume returns to paused state ' + str((direction, cmd, failure)))
    for direction in (0, 1):
        m = SocMachine(image, types, direction=direction, platform_real=True, failures={(0, STOP): -5})
        m.prepare_config()
        require(m.soc(START) == 0, 'stop-failure fixture failed')
        m.trace = []
        require(m.soc(STOP) == (-5 & MASK) and m.trace == [(i, STOP) for i in range(5)] and
                m.work_pending and m.field('sdma_channel', CHANNEL, 'desc') == 0,
                'codec stop error prevented DMA stop or later callbacks')
        require(m.call('dreem_dmaengine_pcm_hw_free', m.sub) == 0, 'stop-failure cleanup failed')
        cases.append('connected codec stop failure still retires DMA ' + str(direction))
    return cases


def relocation_controls(path, image):
    raw = path.read_bytes()
    obj = linked_object(SimpleNamespace(binary=raw, elf=ELFFile(io.BytesIO(raw))))
    index = obj.elf.get_section_index('.text')
    section = obj.elf.get_section(index)
    groups, registers = {}, {}
    for reloc, _ in obj.relocations[index]:
        kind = reloc['r_info_type']
        target = obj.table.get_symbol(reloc['r_info_sym'])
        if kind not in (43, 44) or not isinstance(target['st_shndx'], int):
            continue
        if not obj.elf.get_section(target['st_shndx'])['sh_flags'] & 0x20:
            continue
        offset = reloc['r_offset']
        word = int.from_bytes(section.data()[offset:offset + 4], 'little')
        key = (reloc['r_info_sym'], signed16(imm(word)))
        groups.setdefault(key, {43: [], 44: []})[kind].append(offset)
        registers.setdefault((key, kind), set()).add((word >> 12) & 15)
    reused = next((key, value) for key, value in groups.items()
                  if registers.get((key, 44), set()) - registers.get((key, 43), set()))
    controls = []
    target = obj.table.get_symbol(reused[0][0])
    changes = [
        (section['sh_offset'] + reused[1][44][0] + 1, 0x10, 'copied-low MOVT destination mutation', 'section bytes differ'),
        (obj.elf.get_section(target['st_shndx'])['sh_offset'] + target['st_value'] + reused[0][1],
         1, 'copied-low referenced string mutation', 'merged string contents differ'),
    ]
    for offset, bit, label, expected in changes:
        changed = bytearray(raw)
        changed[offset] ^= bit
        mutated = linked_object(SimpleNamespace(binary=bytes(changed), elf=ELFFile(io.BytesIO(changed))))
        try:
            compare(mutated, image)
        except ValueError as error:
            require(expected in str(error), 'unexpected relocation-control rejection: ' + str(error))
        else:
            raise ValueError('relocation mutation accepted: ' + label)
        controls.append(label)
    return controls


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build', type=Path)
    parser.add_argument('previous_build', type=Path)
    args = parser.parse_args()
    image, types, matches = load(args.build)
    cases = verify(image, types)
    board = Module(args.build / 'kernel/sound/soc/fsl/imx-wm8960.o')
    board_image = load_board(args.build / 'kernel/vmlinux', board)
    for asrc in (False, True):
        m = BoardMachine(board_image, board, asrc=asrc)
        require(m.probe() == 0, 'board opt-in fixture failed')
        links = DATA + board.members['imx_wm8960_data']['links']
        require([m.field('snd_soc_dai_link', links + n * board.sizes['snd_soc_dai_link'],
                         'dreem_trigger_rollback', size=1) for n in range(3)] == [1, 0, 0],
                'board did not opt in only the direct link')
        m.call('imx_wm8960_remove', m.platform_device() - board.members['platform_device']['dev'])
        m.clean()
        cases.append('actual board probe opts in direct link only, ASRC present=' + str(asrc))
    old, old_types, old_matches = load(args.previous_build, old=True)
    negative = []
    m = SocMachine(old, old_types, platform_real=True, failures={(3, START): -5})
    m.prepare_config()
    require(m.soc(START) == (-5 & MASK) and not m.work_pending and m.descriptor_live and
            m.field('sdma_channel', CHANNEL, 'desc') == DESCRIPTOR and m.mmio_writes[-1] == (12, 4),
            'old late-start DMA leak not reproduced')
    negative.append('old CPU start failure leaves actual SDMA issued and active')
    m = SocMachine(old, old_types, platform_real=True, failures={(0, STOP): -110})
    m.prepare_config()
    require(m.soc(START) == 0, 'old stop-failure fixture failed')
    m.trace = []
    require(m.soc(STOP) == (-110 & MASK) and m.trace == [(0, STOP)] and not m.work_pending and
            m.field('sdma_channel', CHANNEL, 'desc') == DESCRIPTOR,
            'old early-stop failure did not skip DMA cleanup')
    negative.append('old codec stop failure skips actual DMA stop')
    cases.extend(negative)
    relocation_negative = relocation_controls(args.build / 'kernel/sound/soc/soc-pcm.o', image)
    cases.extend(relocation_negative)
    print(json.dumps({'kernel_sha256': sha(image.binary), 'previous_kernel_sha256': sha(old.binary),
                      'passed_cases': len(cases), 'cases': cases, 'negative_controls': negative,
                      'relocation_negative_controls': relocation_negative,
                      'linked_objects': matches, 'previous_linked_objects': old_matches,
                      'verifier_sources': {n: sha((Path(__file__).parent / n).read_bytes())
                                           for n in sorted(dependencies(Path(__file__).name))},
                      'hardware_qualified': False,
                      'limits': 'Linked ARM ASoC sequencing and connected PCM/SDMA callbacks; modeled codec, CPU and machine results, scheduling, MMIO and allocation. No physical SAI, full ALSA scheduler, DPCM/ASRC or board deployment qualification.'}, indent=2))


if __name__ == '__main__':
    main()
