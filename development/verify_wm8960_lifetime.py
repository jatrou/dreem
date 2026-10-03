#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Execute the linked research board's probe, jack and removal ARM code.

Kernel services and hardware are models. Checks cover resource failure/retry,
publication, stale descriptors and draining a modeled pending GPIO callback.
This is not physical audio, scheduler, ALSA internals or codec hot-unplug proof.
"""
import argparse
import json
from pathlib import Path
import struct

from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_PC, UC_ARM_REG_LR, UC_ARM_REG_SP
from verify_adc_module import Module
from verify_busfreq import Image, Machine, FIXTURE, STACK, STOP, require
from verify_ddr_preparation import resolve_probe_globals
from verify_wm8960_sources import names_in, sha
from unicorn import UcError, UC_ERR_READ_UNMAPPED

BASE = 0x51000000
PDEV, OTHER, CPU, CODEV, ASRC, NODE, CPUNODE, CODENODE, ASRCNODE, GPRNODE, GPR, CLOCK, DRIVER, RUNTIME, DAI, CODEC, SOUND, CONTROL, FILE, OLD_FILE, BUFFER = (
    BASE + i * 0x1000 for i in range(21))
DATA = BASE + 0x20000
CALLBACK_RETURN = FIXTURE + 0xfff0


def load(path, obj):
    image = Image(path, extra_routines=('imx_wm8960_probe',))
    image.named_addresses = names_in(image)
    resolve_probe_globals(image, obj)
    table = list(obj.elf.get_section_by_name('.symtab').iter_symbols())
    image.ranges = []
    for index, section in enumerate(obj.elf.iter_sections()):
        if not section.name.startswith('.text'):
            continue
        functions = [s for s in table if s['st_shndx'] == index and s['st_info']['type'] == 'STT_FUNC']
        if not functions:
            continue
        choices = None
        for s in functions:
            values = {a - s['st_value'] for a in image.named_addresses.get(s.name, set())}
            choices = values if choices is None else choices & values
        require(len(choices) == 1, 'board text layout is ambiguous: ' + section.name)
        base = choices.pop()
        for s in functions:
            address = base + s['st_value']
            require(address in image.named_addresses.get(s.name, set()), 'board text layout changed: ' + s.name)
            image.symbols[s.name] = address
            image.ranges.append((address, address + s['st_size']))
    image.board_writes = []
    for name in ('.data', '.bss'):
        section = obj.elf.get_section_index(name)
        first = next(s for s in table if s['st_shndx'] == section and s['st_info']['type'] == 'STT_OBJECT')
        address = image.symbols[first.name] - first['st_value']
        image.board_writes.append((address, address + obj.elf.get_section(section)['sh_size']))
    image.services = {image.symbols[s.name]: s.name for s in table
                      if s['st_shndx'] == 'SHN_UNDEF' and s.name in image.symbols}
    return image


class BoardMachine(Machine):
    def __init__(self, image, types, *, fail=None, asrc=True, mic='separate', low=0,
                 hardware=1, identity_error=0, width=24, pending=True):
        super().__init__(image)
        self.types, self.fail = types, fail
        self.asrc, self.mic, self.low = asrc, mic, low
        self.hardware, self.identity_error, self.width = hardware, identity_error, width
        self.pending_work = pending
        self.failed = False
        self.allocated = self.card_live = self.control_live = self.clock = self.node_live = False
        self.devices, self.gpios, self.attributes = set(), set(), set()
        self.events, self.audio = [], []
        self.callback = None
        self.gpr_value = 0x12345678
        self.cpu.mem_map(BASE, 0x18000)
        self.stub_addresses = image.services
        for dev in (PDEV, OTHER, CPU, CODEV, ASRC):
            self.field('device', dev + self.types.members['platform_device']['dev'], 'of_node', NODE)
            self.field('device', dev + self.types.members['platform_device']['dev'], 'driver', DRIVER)
        # i2c_client.dev is at a different offset from platform_device.dev.
        self.field('device', self.codec_device(), 'driver', DRIVER)
        for dev in (CPU, ASRC):
            addr = self.platform_device(dev)
            self.field('device', addr, 'init_name', BUFFER)
        self.cpu.mem_write(BUFFER, b'synthetic-sai\0')
        self.cpu.mem_write(FILE, bytes(types.sizes['file']))
        self.cpu.mem_write(OLD_FILE, bytes(types.sizes['file']))

    def platform_device(self, p=PDEV):
        return p + self.types.members['platform_device']['dev']

    def codec_device(self):
        return CODEV + self.types.members['i2c_client']['dev']

    def put(self, p, v):
        self.cpu.mem_write(p, struct.pack('<I', v & 0xffffffff))

    def field(self, kind, p, name, value=None, size=4):
        p += self.types.members[kind][name]
        if value is None:
            return int.from_bytes(self.cpu.mem_read(p, size), 'little')
        self.cpu.mem_write(p, (value & ((1 << (size * 8)) - 1)).to_bytes(size, 'little'))

    def ready(self):
        return bool(self.cpu.mem_read(self.symbols['dreem_jack_ready'], 1)[0])

    def hit(self, stage):
        self.events.append(stage)
        if self.fail == stage and not self.failed:
            self.failed = True
            return True
        return False

    def write(self, cpu, access, address, size, value, extra):
        allowed = [(STACK, STOP), (BASE, BASE + 0x18000), *self.image.board_writes]
        if self.allocated:
            allowed.append((DATA, DATA + self.types.sizes['imx_wm8960_data']))
        require(any(a <= address and address + size <= b for a, b in allowed),
                f'board write outside live state at {address:#x}')

    def audio_valid(self):
        require(self.allocated and self.card_live and self.control_live,
                'audio access after card/control release')

    def code(self, cpu, address, size, extra):
        if address == CALLBACK_RETURN:
            require(self.callback is not None, 'unowned callback continuation')
            ret, jack = self.callback
            self.callback = None
            if jack == 'opening':
                require(cpu.reg_read(UC_ARM_REG_R0) == (-19 & 0xffffffff),
                        'open succeeded while misc registration was still publishing')
                self.events.append('early-open-rejected')
            else:
                self.gpios.remove(jack)
                self.events.append('gpio-work-drained:' + jack)
            cpu.reg_write(UC_ARM_REG_R0, 0)
            cpu.reg_write(UC_ARM_REG_PC, ret)
            return
        name = self.stub_addresses.get(address)
        if name is None:
            require(any(a <= address < b for a, b in self.image.ranges),
                    f'board execution left allowlist at {address:#x}')
            return
        a, b, c, d = [cpu.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
        result = 0
        if name in ('mutex_lock', 'mutex_unlock'):
            require(a == self.symbols['dreem_jack_mutex'] and self.locked == (name == 'mutex_unlock'),
                    'wrong or recursively held jack mutex')
            self.locked = name == 'mutex_lock'
        elif name == 'dreem_get_hardware_version':
            result = self.identity_error
            if not result:
                self.put(a, self.hardware)
        elif name == 'kmem_cache_alloc':
            require(not self.allocated, 'duplicate board allocation')
            if not self.hit('allocation'):
                self.cpu.mem_map(DATA, 0x1000)
                self.allocated = True
                result = DATA
        elif name == 'kfree':
            if a:
                require(a == DATA and self.allocated and not self.card_live and not self.clock and
                        not self.node_live and not self.gpios and not self.devices and not self.attributes,
                        'board freed before its resources drained')
                self.cpu.mem_unmap(DATA, 0x1000)
                self.allocated = False
        elif name == '__memzero':
            cpu.mem_write(a, bytes(b))
        elif name == 'memcpy':
            require(self.allocated and a == DATA + self.types.members['imx_wm8960_data']['links'] and
                    b == self.symbols['imx_wm8960_dai'] and c == 3 * self.types.sizes['snd_soc_dai_link'],
                    'invalid per-instance DAI copy')
            cpu.mem_write(a, bytes(cpu.mem_read(b, c)))
            result = a
        elif name == 'of_parse_phandle':
            require(a == NODE and c == 0, 'unexpected board phandle lookup')
            prop = self.string(b)
            result = {'cpu-dai': CPUNODE, 'audio-codec': CODENODE,
                      'asrc-controller': ASRCNODE if self.asrc else 0}[prop]
            if self.hit(prop):
                result = 0
        elif name in ('of_find_device_by_node', 'of_find_i2c_device_by_node'):
            dev, label = {CPUNODE: (CPU, 'cpu-device'), CODENODE: (CODEV, 'codec-device'),
                          ASRCNODE: (ASRC, 'asrc-device')}[a]
            if not self.hit(label):
                result = dev
                self.devices.add(self.codec_device() if dev == CODEV else self.platform_device(dev))
            if label == 'codec-device' and self.hit('codec-unbound'):
                self.field('device', self.codec_device(), 'driver', 0)
        elif name == 'put_device':
            require(a in self.devices, 'unbalanced device reference')
            self.devices.remove(a)
        elif name == 'of_find_property':
            require(a == NODE and self.string(b) == 'codec-master', 'unexpected boolean property')
            result = BUFFER
        elif name == 'clk_get':
            require(a == self.codec_device() and self.string(b) == 'mclk', 'wrong clock consumer')
            result = -517 if self.hit('clock') else CLOCK
            self.clock = result == CLOCK
        elif name == 'clk_put':
            require(a == CLOCK and self.clock and not self.card_live, 'clock released while card lives')
            self.clock = False
        elif name == 'of_parse_phandle_with_fixed_args':
            require(a == NODE and self.string(b) == 'gpr' and c == 3 and d == 0, 'wrong GPR arguments')
            result = -22 if self.hit('gpr-phandle') else 0
            p = self.word(cpu.reg_read(UC_ARM_REG_SP))
            if not result:
                self.field('of_phandle_args', p, 'np', GPRNODE)
                self.field('of_phandle_args', p, 'args_count', 3)
                for i, v in enumerate((4, 0x100000, 0x100000)):
                    self.put(p + self.types.members['of_phandle_args']['args'] + 4 * i, v)
        elif name == 'syscon_node_to_regmap':
            require(a == GPRNODE, 'wrong syscon node')
            result = -517 if self.hit('regmap') else GPR
        elif name == 'regmap_read':
            require(a == GPR and b == 4, 'wrong GPR read')
            result = -5 if self.hit('gpr-read') else 0
            if not result:
                self.put(c, self.gpr_value)
        elif name == 'regmap_update_bits':
            require(a == GPR and b == 4 and c == 0x100000, 'wrong GPR field')
            result = -5 if self.hit('gpr-write') else 0
            if not result:
                self.gpr_value = (self.gpr_value & ~c) | (d & c)
        elif name == 'of_property_read_u32_array':
            prop = self.string(b)
            if prop == 'hp-det':
                result = -22  # Absent in the reviewed device tree.
            else:
                require(a == ASRCNODE and d == 1, 'wrong ASRC property')
                result = -22 if self.hit(prop) else 0
                if not result:
                    self.put(c, 48000 if prop == 'fsl,asrc-rate' else self.width)
        elif name in ('snd_soc_of_parse_card_name', 'snd_soc_of_parse_audio_routing'):
            require(a == DATA, 'wrong sound card')
            result = -22 if self.hit(name) else 0
        elif name == 'snd_soc_register_card':
            require(a == DATA and self.clock and not self.card_live and not self.ready(), 'early sound publication')
            require(self.field('snd_soc_card', a, 'late_probe') == self.symbols['imx_wm8960_late_probe'],
                    'wrong late probe callback')
            require(self.field('snd_soc_card', a, 'dai_link') ==
                    DATA + self.types.members['imx_wm8960_data']['links'], 'DAI links still shared between bindings')
            result = -517 if self.hit('card-register') else 0
            if not result:
                self.card_live = True
                self.field('snd_soc_card', a, 'snd_card', SOUND)
                self.field('snd_soc_card', a, 'rtd', RUNTIME)
                self.field('snd_soc_pcm_runtime', RUNTIME, 'codec_dai', DAI)
                self.field('snd_soc_dai', DAI, 'codec', CODEC)
        elif name == 'snd_soc_unregister_card':
            require(a == DATA and self.card_live and not self.ready() and not self.node_live and
                    not self.gpios and not self.attributes and not self.locked, 'sound freed before jack cleanup')
            self.card_live = self.control_live = False
            self.events.append('card-unregister')
        elif name == 'of_get_named_gpio_flags':
            require(a == NODE and c == 0, 'wrong GPIO node')
            prop = self.string(b)
            result = 132 if prop == 'hp-det-gpios' else {'absent': -2, 'shared': 132, 'separate': 133}[self.mic]
            if self.hit(prop):
                result = -517
            self.put(d, self.low)
        elif name == 'snd_kctl_jack_new':
            require(self.card_live and self.string(a) == 'Headphone' and b == c == 0, 'wrong kcontrol')
            result = 0 if self.hit('control-allocation') else CONTROL
            self.control_live = bool(result)
        elif name == 'snd_ctl_add':
            self.audio_valid()
            require(a == SOUND and b == CONTROL, 'wrong control owner')
            result = -12 if self.hit('control-add') else 0
            if result:
                self.control_live = False
        elif name == 'snd_soc_card_jack_new':
            self.audio_valid()
            jack = 'hp' if d == self.symbols['imx_hp_jack'] else 'mic'
            require(a == DATA and d == self.symbols['imx_' + jack + '_jack'], 'wrong jack owner')
            result = -12 if self.hit(jack + '-jack') else 0
            if not result:
                self.field('snd_soc_jack', d, 'card', DATA)
        elif name in ('snd_soc_jack_add_gpios', 'snd_soc_jack_free_gpios'):
            jack = 'hp' if a == self.symbols['imx_hp_jack'] else 'mic'
            require(a == self.symbols['imx_' + jack + '_jack'] and b == 1 and
                    c == self.symbols['imx_' + jack + '_jack_gpio'], 'wrong GPIO association')
            self.audio_valid()
            if name == 'snd_soc_jack_add_gpios':
                result = -5 if self.hit(jack + '-gpio') else 0
                if not result:
                    self.gpios.add(jack)
            else:
                require(jack in self.gpios and not self.locked and not self.ready() and not self.node_live,
                        'GPIO work drained under mutex or after release')
                if self.pending_work:
                    self.callback = (cpu.reg_read(UC_ARM_REG_LR), jack)
                    cpu.reg_write(UC_ARM_REG_R0, a)
                    cpu.reg_write(UC_ARM_REG_LR, CALLBACK_RETURN)
                    cpu.reg_write(UC_ARM_REG_PC, self.symbols[jack + '_jack_status_check'])
                    return
                self.gpios.remove(jack)
        elif name in ('driver_create_file', 'driver_remove_file'):
            jack = 'hp' if b == self.symbols['driver_attr_headphone'] else 'mic'
            require(a == DRIVER and b == self.symbols['driver_attr_' + ('headphone' if jack == 'hp' else 'micphone')],
                    'wrong sysfs owner')
            if name == 'driver_create_file':
                result = -12 if self.hit(jack + '-attribute') else 0
                if not result:
                    self.attributes.add(jack)
            else:
                require(jack in self.attributes and not self.locked and not self.ready(), 'invalid sysfs drain')
                self.attributes.remove(jack)
        elif name == 'misc_register':
            self.audio_valid()
            require(a == self.symbols['dreem_jack_device'] and 'hp' in self.gpios and 'hp' in self.attributes and
                    not self.locked and not self.ready(), 'premature jack registration')
            require(self.string(self.field('miscdevice', a, 'name')) == 'jack' and
                    self.field('miscdevice', a, 'minor') == 255 and
                    self.field('miscdevice', a, 'mode', size=2) == 0o600, 'wrong jack node ABI')
            ops = self.field('miscdevice', a, 'fops')
            require(ops == self.symbols['fops'] and
                    self.field('file_operations', ops, 'open') == self.symbols['jack_open'] and
                    self.field('file_operations', ops, 'unlocked_ioctl') == self.symbols['jack_ioctl'],
                    'jack operations not wired to lifetime checks')
            result = -16 if self.hit('misc-register') else 0
            self.node_live = result == 0
            if self.node_live:
                self.callback = (cpu.reg_read(UC_ARM_REG_LR), 'opening')
                cpu.reg_write(UC_ARM_REG_R0, 0)
                cpu.reg_write(UC_ARM_REG_R1, FILE)
                cpu.reg_write(UC_ARM_REG_LR, CALLBACK_RETURN)
                cpu.reg_write(UC_ARM_REG_PC, self.symbols['jack_open'])
                return
        elif name == 'misc_deregister':
            require(a == self.symbols['dreem_jack_device'] and self.node_live and not self.ready() and
                    not self.locked, 'jack deregister lock order or publication error')
            self.node_live = False
            self.events.append('jack-deregister')
        elif name == 'nonseekable_open':
            require(b in (FILE, OLD_FILE) and self.ready() and self.locked, 'unsafe file open')
        elif name == 'gpio_to_desc':
            require(a in (132, 133) and self.gpios, 'GPIO read after free')
            result = a + BUFFER
        elif name == 'gpiod_get_raw_value':
            require(a - BUFFER in (132, 133), 'wrong GPIO descriptor')
            result = 1
        elif name in ('snd_soc_dapm_disable_pin', 'snd_soc_dapm_enable_pin'):
            self.audio_valid()
            require(a == DATA + self.types.members['snd_soc_card']['dapm'], 'wrong DAPM owner')
            self.audio.append([name, self.string(b)])
        elif name == 'snd_kctl_jack_report':
            self.audio_valid()
            require(a == SOUND and b == CONTROL and c in (0, 1), 'wrong control report')
            self.audio.append([name, c])
        elif name == 'snd_soc_jack_report':
            self.audio_valid()
            require(a == self.symbols['imx_hp_jack'] and b in (0, 1) and c == 1, 'wrong jack report')
            self.audio.append([name, b, c])
        elif name == 'snd_soc_update_bits':
            require(a == CODEC and (b, c, d) == (9, 64, 64), 'wrong codec late probe update')
            result = -5 if self.hit('codec-update') else 1
        elif name in ('dev_err', 'dev_warn'):
            pass
        else:
            raise ValueError('unmodeled board service: ' + name)
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def probe(self):
        return self.call('imx_wm8960_probe', PDEV)

    def clean(self):
        require(not any((self.allocated, self.card_live, self.control_live, self.clock,
                         self.node_live, self.devices, self.gpios, self.attributes, self.ready())),
                'resources retained after failed probe or removal')
        require(self.word(self.symbols['dreem_audio_owner']) == 0, 'owner retained')
        require(self.gpr_value == 0x12345678, 'GPR field was not restored')
        require(self.field('device', self.platform_device(), 'driver_data') == 0, 'dangling driver data')


def verify(image, types):
    cases = []
    m = BoardMachine(image, types)
    driver = image.symbols['imx_wm8960_driver']
    require(m.field('platform_driver', driver, 'probe') == image.symbols['imx_wm8960_probe'] and
            m.field('platform_driver', driver, 'remove') == image.symbols['imx_wm8960_remove'],
            'platform driver is not connected to the repaired lifecycle')
    cases.append('platform registration uses repaired probe/remove')
    failures = ('allocation', 'cpu-dai', 'audio-codec', 'cpu-device', 'codec-device', 'codec-unbound',
                'clock', 'gpr-phandle', 'regmap', 'gpr-read', 'gpr-write', 'asrc-device',
                'fsl,asrc-rate', 'fsl,asrc-width', 'snd_soc_of_parse_card_name',
                'snd_soc_of_parse_audio_routing', 'card-register', 'hp-det-gpios', 'mic-det-gpios',
                'control-allocation', 'control-add', 'hp-jack', 'hp-gpio', 'hp-attribute',
                'mic-jack', 'mic-gpio', 'mic-attribute', 'misc-register')
    for failure in failures:
        m = BoardMachine(image, types, fail=failure)
        require(m.probe() & 0x80000000 and m.failed, 'injected probe failure not observed: ' + failure)
        m.clean()
        require(m.call('jack_open', 0, FILE) == (-19 & 0xffffffff), 'failed probe permits open')
        m.fail = None
        m.field('device', m.codec_device(), 'driver', DRIVER)
        require(m.probe() == 0 and m.ready(), 'failed probe prevents retry: ' + failure)
        require(m.call('imx_wm8960_remove', PDEV) == 0, 'retry cannot remove')
        m.clean()
        cases.append('rollback, no open, retry and pending-work drain: ' + failure)
    for hardware, error, expected in ((0, 0, -19), (1, -517, -517), (1, -5, -5)):
        m = BoardMachine(image, types, hardware=hardware, identity_error=error)
        require(m.probe() == expected & 0xffffffff, 'identity gate changed')
        m.clean()
        cases.append('identity gate ' + str((hardware, error)))
    m = BoardMachine(image, types, width=32)
    require(m.probe() == (-22 & 0xffffffff), 'unsupported ASRC width accepted')
    m.clean()
    cases.append('unsupported ASRC width')
    for asrc in (False, True):
        for mic in ('absent', 'shared', 'separate'):
            for low in (0, 1):
                m = BoardMachine(image, types, asrc=asrc, mic=mic, low=low)
                require(m.probe() == 0 and m.ready(), 'valid board failed')
                require(m.call('jack_open', 0, FILE) == 0, 'ready jack cannot open')
                for command in (5, 6, 5):
                    m.audio.clear()
                    require(m.call('jack_ioctl', FILE, command, 0xdeadbeef) == 0, 'valid ioctl failed')
                    active = int(command == 5) != low
                    require(m.audio == [['snd_soc_dapm_disable_pin' if active else 'snd_soc_dapm_enable_pin', 'Ext Spk'],
                                        ['snd_kctl_jack_report', int(active)], ['snd_soc_jack_report', int(active), 1]],
                            'stock jack command behavior differs')
                m.audio.clear()
                require(m.call('jack_ioctl', FILE, 42, 0) == (-25 & 0xffffffff) and not m.audio,
                        'unknown ioctl not rejected')
                require(m.call('imx_wm8960_probe', OTHER) == (-16 & 0xffffffff) and m.ready(), 'duplicate probe not rejected')
                require(m.call('imx_wm8960_remove', OTHER) == (-19 & 0xffffffff) and m.ready(), 'wrong owner removed')
                require(m.call('imx_wm8960_remove', PDEV) == 0, 'remove failed')
                m.clean()
                require(m.call('jack_ioctl', FILE, 5, 0) == (-19 & 0xffffffff), 'old fd touches unmapped card')
                require(m.probe() == 0, 'rebind failed')
                m.audio.clear()
                require(m.call('jack_ioctl', FILE, 5, 0) == (-19 & 0xffffffff) and not m.audio,
                        'old fd controls newly bound card')
                require(m.call('jack_open', 0, OLD_FILE) == 0 and m.call('jack_ioctl', OLD_FILE, 5, 0) == 0,
                        'new fd cannot control new card')
                require(m.call('imx_wm8960_remove', PDEV) == 0, 'second removal failed')
                m.clean()
                cases.append('jack ABI, ownership and stale fd rebind ' + str((asrc, mic, low)))
    m = BoardMachine(image, types)
    m.set('dreem_jack_generation', 0xffffffff)
    require(m.probe() == (-75 & 0xffffffff), 'epoch wrapped and could revive an old fd')
    m.clean()
    cases.append('epoch exhaustion fails without allocation')
    for fail in (None, 'codec-update'):
        m = BoardMachine(image, types, fail=fail)
        require(m.probe() == 0, 'late-probe fixture failed')
        require(m.call('imx_wm8960_late_probe', DATA) == ((-5 if fail else 0) & 0xffffffff),
                'codec register error not propagated')
        require(m.call('imx_wm8960_remove', PDEV) == 0, 'late-probe fixture cleanup failed')
        m.clean()
        cases.append('late probe checked ' + str(fail))
    for offset, opcode, rebind in ((0x24, 0x0a000000, False), (0x34, 0x1a000000, True)):
        m = BoardMachine(image, types)
        require(m.probe() == 0 and m.call('jack_open', 0, OLD_FILE) == 0, 'mutation setup failed')
        require(m.call('imx_wm8960_remove', PDEV) == 0, 'mutation removal failed')
        if rebind:
            require(m.probe() == 0, 'mutation rebind failed')
        address = m.symbols['jack_ioctl'] + offset
        require(m.word(address) & 0xff000000 == opcode, 'review generation/readiness mutation site')
        m.put(address, 0xe1a00000)  # ARM NOP, remove only the checked conditional branch.
        detected = False
        try:
            require(m.call('jack_ioctl', OLD_FILE, 5, 0) == (-19 & 0xffffffff),
                    'injected lifetime bypass permits stale ioctl')
        except ValueError as error:
            expected = 'injected lifetime bypass permits stale ioctl' if rebind else 'audio access after card/control release'
            require(str(error) == expected, 'unrelated failure during lifetime mutation: ' + str(error))
            detected = True
        except UcError as error:
            require(not rebind and error.errno == UC_ERR_READ_UNMAPPED, 'unrelated emulator mutation error')
            detected = True
        require(detected, 'verifier missed the injected lifetime bypass')
        cases.append('negative control rejects removed ' + ('generation' if rebind else 'readiness') + ' guard')
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('research_kernel', type=Path)
    parser.add_argument('board_object', type=Path)
    args = parser.parse_args()
    obj = Module(args.board_object)
    image = load(args.research_kernel, obj)
    cases = verify(image, obj)
    files = ('verify_wm8960_lifetime.py', 'verify_adc_module.py', 'verify_busfreq.py',
             'verify_ddr_preparation.py', 'verify_wm8960_sources.py', 'verify_ddr_sources.py')
    print(json.dumps({'kernel_sha256': sha(image.binary), 'board_object_sha256': sha(obj.binary),
                      'passed_cases': len(cases), 'cases': cases,
                      'verifier_sources': {n: sha((Path(__file__).parent / n).read_bytes()) for n in files},
                      'hardware_qualified': False,
                      'limits': 'Synthetic kernel services and GPIO work; no real concurrent scheduler, ALSA internals, codec hot-unplug or physical audio proof.'}, indent=2))


if __name__ == '__main__':
    main()
