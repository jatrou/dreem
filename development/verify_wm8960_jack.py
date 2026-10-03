#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Execute the source-matched board's jack interface and early probe in Unicorn.

Kernel/device services are models. This records existing defects as regression
inputs for the replacement, not as acceptable behavior or physical validation.
Full sound-card initialization, workqueues and real VFS concurrency are outside
this bounded check. No firmware or driver is installed.
"""
import argparse
import itertools
import json
from pathlib import Path
import struct

from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_SP, UC_ARM_REG_PC, UC_ARM_REG_LR
from verify_busfreq import Image, Machine, FIXTURE, STACK, STOP, RAW_HASH, require
from verify_wm8960_sources import compare, section_bases, names_in, sha
from verify_wm8960_board_sources import board_object

CARD, RUNTIME, DAI, CODEC, SOUND, CONTROL, PLATFORM, CLASS, DEVICE = (
    FIXTURE + n * 0x1000 for n in range(1, 10))
NUMBER = 0x12300000
SERVICES = ('get_dreem_hardware_version', 'alloc_chrdev_region', 'unregister_chrdev_region',
            'cdev_init', 'cdev_add', 'cdev_del', '__class_create', 'class_destroy',
            'device_create', 'device_destroy', 'of_parse_phandle', 'driver_remove_file',
            'snd_soc_dapm_disable_pin', 'snd_soc_dapm_enable_pin', 'snd_kctl_jack_report',
            'snd_soc_jack_report', 'snd_soc_update_bits', 'printk', 'dev_err')


class BoardImage(Image):
    def __init__(self, path, obj):
        super().__init__(path, True)
        self.source_match = compare(obj, self)
        self.bases = bases = section_bases(obj, names_in(self))
        for s in obj.symbols:
            if s.name and s['st_shndx'] in bases:
                self.symbols[s.name] = bases[s['st_shndx']] + s['st_value']
        self.ranges = [(self.symbols[n], self.symbols[n] + obj.named[n]['st_size'])
                       for n in ('set_jack_status', 'jack_ioctl', 'imx_wm8960_late_probe',
                                 'imx_wm8960_probe', 'imx_wm8960_remove')]
        self.bss = bases[obj.elf.get_section_index('.bss')]


class JackMachine(Machine):
    def __init__(self, image, *, low=0, report=1, card=True, hardware=1, failure=None):
        super().__init__(image)
        self.stub_addresses = {self.symbols[n]: n for n in SERVICES}
        self.hardware, self.inject = hardware, failure
        self.allocated = self.registered = self.class_created = self.node_created = False
        self.cdev_refs = 0
        self.events, self.audio = [], []
        self.low, self.report = low, report
        self.word_put(self.symbols['card_priv'], low)
        self.word_put(self.symbols['card_priv'] + 12, CONTROL)
        self.word_put(self.symbols['card_priv'] + 24, SOUND if card else 0)
        self.word_put(self.symbols['imx_hp_jack'] + 16, CARD)
        self.word_put(self.symbols['imx_hp_jack_gpio'] + 16, report)
        self.word_put(CARD + 0x64, RUNTIME)
        self.word_put(RUNTIME + 0x574, DAI)
        self.word_put(DAI + 0x38, CODEC)
        self.word_put(PLATFORM + 0x128, FIXTURE + 0xa000)
        self.word_put(PLATFORM + 0x54, FIXTURE + 0xb000)

    def word_put(self, address, value):
        self.cpu.mem_write(address, struct.pack('<I', value & 0xffffffff))

    def write(self, cpu, access, address, size, value, extra):
        require((STACK <= address and address + size <= STOP) or
                (self.image.bss <= address and address + size <= self.image.bss + 208),
                f'unexpected board write at {address:#x}')

    def resources(self):
        return self.allocated, self.cdev_refs, self.registered, self.class_created, self.node_created

    def code(self, cpu, address, size, extra):
        name = self.stub_addresses.get(address)
        if name is None:
            require(any(a <= address < b for a, b in self.image.ranges),
                    f'board execution left allowlist at {address:#x}')
            return
        a, b, c, d = [cpu.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
        result = 0
        if name in ('printk', 'dev_err'):
            pass
        elif name == 'get_dreem_hardware_version':
            self.events.append(name)
            result = self.hardware
        elif name == 'alloc_chrdev_region':
            require(a == self.symbols['jack_number'] and b == 0 and c == 1 and self.string(d) == 'jack',
                    'unexpected jack allocation')
            self.events.append(name)
            result = -28 if self.inject == name else 0
            if not result:
                self.word_put(a, NUMBER)
                self.allocated = True
        elif name == 'unregister_chrdev_region':
            require(a == NUMBER and b == 1 and self.allocated and not self.registered, 'invalid number release')
            self.events.append(name)
            self.allocated = False
        elif name == '__class_create':
            require(a == 0 and self.string(b) == 'jack' and self.allocated, 'unexpected class creation')
            self.events.append(name)
            result = -12 if self.inject == name else CLASS
            self.class_created = result == CLASS
        elif name == 'device_create':
            require(b == 0 and c == NUMBER and d == 0 and
                    self.string(self.word(cpu.reg_read(UC_ARM_REG_SP))) == 'jack', 'wrong node identity')
            self.events.append([name, 'cdev_registered', self.registered])
            # Public device_create_groups_vargs rejects an error-pointer class.
            result = -19 if a != CLASS else (-12 if self.inject == name else DEVICE)
            self.node_created = result == DEVICE
        elif name == 'device_destroy':
            require(a == CLASS and b == NUMBER and self.node_created, 'invalid node removal')
            self.events.append(name)
            self.node_created = False
        elif name == 'class_destroy':
            require(a == CLASS and self.class_created and not self.node_created, 'invalid class removal')
            self.events.append(name)
            self.class_created = False
        elif name == 'cdev_init':
            require(a == self.symbols['jack_inst'] and b == self.symbols['fops'], 'wrong character interface')
            fields = [self.word(b + i * 4) for i in range(28)]
            require(fields[8] == self.symbols['jack_ioctl'] and
                    all(v == 0 for i, v in enumerate(fields) if i != 8), 'unexpected file operations')
            self.events.append(name)
            self.cdev_refs = 1
        elif name == 'cdev_add':
            require(a == self.symbols['jack_inst'] and b == NUMBER and c == 1 and self.cdev_refs == 1,
                    'invalid cdev registration')
            self.events.append(name)
            result = -5 if self.inject == name else 0
            self.registered = result == 0
        elif name == 'cdev_del':
            require(a == self.symbols['jack_inst'] and self.registered, 'invalid cdev removal')
            self.events.append(name)
            self.registered = False
            self.cdev_refs = 0
        elif name == 'of_parse_phandle':
            require(a == FIXTURE + 0xa000 and self.string(b) == 'cpu-dai' and c == 0,
                    'unexpected probe extent')
            self.events.append('missing cpu-dai')
            result = 0  # Bound the check at the first audio initialization failure.
        elif name == 'driver_remove_file':
            require(a == FIXTURE + 0xb000 and b in (self.symbols['driver_attr_micphone'],
                                                    self.symbols['driver_attr_headphone']), 'wrong attribute removal')
            self.events.append(name)
        elif name in ('snd_soc_dapm_disable_pin', 'snd_soc_dapm_enable_pin'):
            require(a == CARD + 0xe8 and self.string(b) == 'Ext Spk', 'wrong speaker route')
            self.audio.append([name, 'Ext Spk'])
        elif name == 'snd_kctl_jack_report':
            require(a == SOUND and b == CONTROL and c in (0, 1), 'wrong control report')
            self.audio.append([name, c])
        elif name == 'snd_soc_jack_report':
            require(a == self.symbols['imx_hp_jack'] and c == 1 and b in (0, 1), 'wrong jack report')
            self.audio.append([name, b, c])
        elif name == 'snd_soc_update_bits':
            require(a == CODEC, 'wrong late-probe codec')
            self.audio.append([name, b, c, d])
            result = -5 if self.inject == name else 0
        else:
            raise ValueError('unmodeled board service: ' + name)
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))


def verify(image):
    cases = []
    for low, report, requested in itertools.product((0, 1), (1, 3), (0, 1, 2)):
        m = JackMachine(image, low=low, report=report)
        active = requested != low
        result = m.call('set_jack_status', requested)
        require(result == (report if active else 0), 'helper return differs')
        require(m.audio == [['snd_soc_dapm_disable_pin' if active else 'snd_soc_dapm_enable_pin', 'Ext Spk'],
                            ['snd_kctl_jack_report', int(active)], ['snd_soc_jack_report', int(active), 1]],
                'helper route or report differs')
        cases.append('jack helper ' + repr((low, report, requested)))
    for low, card, command, argument in itertools.product((0, 1), (False, True),
                                                         (0, 1, 4, 5, 6, 7, 0xffffffff), (0, 0xdeadbeef)):
        m = JackMachine(image, low=low, card=card)
        result = m.call('jack_ioctl', 0, command, argument)
        require(result == (0 if card else 0xffffffff), 'ioctl result differs')
        if card and command in (5, 6):
            active = int(command == 5) != low
            require(m.audio == [['snd_soc_dapm_disable_pin' if active else 'snd_soc_dapm_enable_pin', 'Ext Spk'],
                                ['snd_kctl_jack_report', int(active)], ['snd_soc_jack_report', int(active), 1]],
                    'ioctl did not route the requested status')
        else:
            require(not m.audio, 'missing-card or unknown command touched audio')
        cases.append('ioctl ' + repr((low, card, command, argument)))
    for failed in (False, True):
        m = JackMachine(image, failure='snd_soc_update_bits' if failed else None)
        require(m.call('imx_wm8960_late_probe', CARD) == 0 and
                m.audio == [['snd_soc_update_bits', 9, 64, 64]], 'late-probe behavior differs')
        cases.append('late probe ignores register failure ' + str(failed))
    m = JackMachine(image, hardware=0)
    require(m.call('imx_wm8960_probe', PLATFORM) == 0xffffffea and
            m.resources() == (False, 0, False, False, False), 'zero hardware version accepted')
    cases.append('zero hardware version rejected')
    for hardware in (1, 2, -1):
        m = JackMachine(image, hardware=hardware)
        require(m.call('imx_wm8960_probe', PLATFORM) == 0xffffffea and
                m.resources() == (True, 1, True, True, True), 'early-probe leak no longer reproduced')
        require(['device_create', 'cdev_registered', False] in m.events, 'early publication not reproduced')
        cases.append('early publication and audio-failure leak with hardware ' + str(hardware))
    for failure, error, remaining in [
        ('alloc_chrdev_region', -28, (False, 0, False, False, False)),
        ('__class_create', -22, (True, 1, True, False, False)),
        ('device_create', -22, (True, 1, True, True, False)),
        ('cdev_add', -5, (False, 1, False, False, False))]:
        m = JackMachine(image, failure=failure)
        require(m.call('imx_wm8960_probe', PLATFORM) == error & 0xffffffff and
                m.resources() == remaining, 'registration failure behavior differs: ' + failure)
        cases.append('registration failure ' + failure)
    m = JackMachine(image)
    m.allocated = m.registered = m.class_created = m.node_created = True
    m.cdev_refs = 1
    m.word_put(m.symbols['jack_number'], NUMBER)
    m.word_put(m.symbols['jack_class'], CLASS)
    require(m.call('imx_wm8960_remove', PLATFORM) == 0 and
            m.resources() == (False, 0, False, False, False), 'character resource removal differs')
    require(m.call('jack_ioctl', 0, 5, 0) == 0 and len(m.audio) == 3,
            'retained ioctl no longer follows uncleared card pointers')
    cases.append('remove leaves ioctl able to use old card pointers')
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stock_kernel', type=Path)
    parser.add_argument('matched_board_object', type=Path)
    args = parser.parse_args()
    obj = board_object(args.matched_board_object.read_bytes())
    image = BoardImage(args.stock_kernel, obj)
    cases = verify(image)
    print(json.dumps({'stock_kernel_raw_sha256': RAW_HASH, 'matched_object_sha256': sha(obj.binary),
                      'passed_cases': len(cases), 'cases': cases,
                      'source_match_bytes': image.source_match['compared_bytes'],
                      'verifier_sources': {n: sha((Path(__file__).parent / n).read_bytes()) for n in
                          ('verify_wm8960_jack.py', 'verify_wm8960_board_sources.py',
                           'verify_wm8960_sources.py', 'verify_busfreq.py', 'verify_ddr_sources.py',
                           'arm_relocations.py')},
                      'known_defects_are_reproduced_not_fixed': True,
                      'hardware_qualified': False,
                      'limits': 'Modeled kernel services; probe ends at missing cpu-dai. No full sound-card initialization, GPIO work, VFS lifetime or physical audio verification.'}, indent=2))


if __name__ == '__main__':
    main()
