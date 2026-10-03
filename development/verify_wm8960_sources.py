#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Relink the complete WM8960 codec object and compare with the saved kernel.

Checks all emitted functions, constant/mutable tables and init registration.
Merged strings are validated by contents, never by masking address bytes.
BSS layout is checked separately; unwind and discarded exit-call metadata are
outside this comparison. No vendor code is emitted or executed.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import struct

from elftools.elf.elffile import ELFFile
from arm_relocations import relocate_mov
from verify_busfreq import Image, RAW_HASH, require
from verify_ddr_sources import bytes_at

SECTIONS = {'.text': None, '.init.text': 16, '.exit.text': 12,
            '.rodata': 9640, '.data': 2228, '.bss': 4, '.initcall6.init': 4}
FUNCTIONS = {
    'wm8960_' + name for name in (
        'volatile', 'get_deemph', 'hw_free', 'set_bias_level', 'i2c_remove',
        'set_deemph', 'put_deemph', 'mute', 'set_dai_sysclk', 'set_dai_fmt',
        'set_dai_clkdiv', 'probe', 'i2c_probe', 'set_pll', 'set_dai_pll',
        'configure_clocking', 'hw_params', 'set_bias_level_out3',
        'set_bias_level_capless', 'i2c_driver_init', 'i2c_driver_exit')}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def imm(word):
    return ((word >> 4) & 0xf000) | (word & 0xfff)


def signed16(value):
    return (value ^ 0x8000) - 0x8000


def cstring(data, offset):
    require(0 <= offset < len(data), 'string offset outside section')
    value, separator, _ = data[offset:offset + 4096].partition(b'\0')
    require(separator, 'unterminated source string')
    return value + b'\0'


class Object:
    def __init__(self, binary, *, functions=FUNCTIONS, sections=SECTIONS, anchors=None):
        self.binary = binary
        self.function_names, self.section_sizes = functions, sections
        self.elf = e = ELFFile(io.BytesIO(binary))
        require(e.elfclass == 32 and e.little_endian and e['e_machine'] == 'EM_ARM'
                and e['e_type'] == 'ET_REL', 'expected ARM32 relocatable object')
        self.table = e.get_section_by_name('.symtab')
        require(self.table is not None, 'missing object symbols')
        self.symbols = list(self.table.iter_symbols())
        self.named = {s.name: s for s in self.symbols if s.name}
        require({s.name for s in self.symbols if s['st_info']['type'] == 'STT_FUNC'}
                == functions, 'unexpected driver functions')
        self.anchors = anchors if anchors is not None else functions | {
            s.name for s in self.symbols if s.name.startswith(('wm8960_', 'pll_div.'))
            or s.name in ('soc_codec_dev_wm8960', '__initcall_wm8960_i2c_driver_init6')}
        self.relocations = {}
        for section in e.iter_sections():
            if section['sh_type'] != 'SHT_REL':
                continue
            require(section['sh_link'] == e.get_section_index('.symtab'), 'wrong relocation symbol table')
            self.relocations[section['sh_info']] = [
                (r, section['sh_offset'] + i * section['sh_entsize'])
                for i, r in enumerate(section.iter_relocations())]
        for name, size in sections.items():
            section = e.get_section_by_name(name)
            require(section is not None and (size is None or section['sh_size'] == size),
                    'unexpected section size: ' + name)

    def string(self, symbol, addend):
        section = self.elf.get_section(symbol['st_shndx'])
        require(section['sh_flags'] & 0x20, 'target is not a string section')
        return cstring(section.data(), symbol['st_value'] + addend)


def names_in(image):
    result = {}
    e = ELFFile(io.BytesIO(image.binary))
    for symbol in e.get_section_by_name('.symtab').iter_symbols():
        result.setdefault(symbol.name, set()).add(symbol['st_value'])
    return result


def section_bases(obj, names):
    bases = {}
    for name in obj.section_sizes:
        index = obj.elf.get_section_index(name)
        anchors = [s for s in obj.symbols if s['st_shndx'] == index
                   and s['st_info']['type'] in ('STT_FUNC', 'STT_OBJECT')
                   and s.name in obj.anchors]
        require(anchors, 'no layout anchors: ' + name)
        choices = None
        for s in anchors:
            require(s.name in names, 'missing linked symbol: ' + s.name)
            candidates = {a - s['st_value'] for a in names[s.name]}
            choices = candidates if choices is None else choices & candidates
        require(len(choices) == 1, 'section layout differs or is ambiguous: ' + name)
        bases[index] = choices.pop()
    return bases


def compare(obj, image):
    names = names_in(image)
    bases = section_bases(obj, names)
    strings, sections = {}, {}

    def validate_string(target, addend, pointer):
        expected = obj.string(target, addend)
        require(bytes_at(image, pointer, len(expected)) == expected,
                'merged string contents differ')
        key = (target['st_shndx'], target['st_value'] + addend)
        require(key not in strings or strings[key] == pointer, 'inconsistent merged string address')
        strings[key] = pointer

    for name in obj.section_sizes:
        index = obj.elf.get_section_index(name)
        section, address = obj.elf.get_section(index), bases[index]
        if name == '.bss':
            continue  # Stock raw image does not contain file-backed BSS.
        original = section.data()
        relocated = bytearray(original)
        actual = bytes_at(image, address, len(original))
        relocs = obj.relocations.get(index, [])
        string_values, lows = {}, {}
        for r, _ in relocs:
            kind, symbol_index, offset = r['r_info_type'], r['r_info_sym'], r['r_offset']
            require(offset % 4 == 0 and offset + 4 <= len(original), 'invalid relocation offset')
            target = obj.table.get_symbol(symbol_index)
            if not isinstance(target['st_shndx'], int):
                continue
            target_section = obj.elf.get_section(target['st_shndx'])
            if not target_section['sh_flags'] & 0x20:
                continue
            word, observed = (struct.unpack_from('<I', d, offset)[0] for d in (original, actual))
            if kind == 2:
                validate_string(target, word, observed)
                string_values[offset] = (observed - word) & 0xffffffff
            elif kind == 43:
                key = (symbol_index, (word >> 12) & 15)
                require(key not in lows, 'overlapping string MOVW pair')
                lows[key] = (offset, imm(observed), signed16(imm(word)))
            elif kind == 44:
                key = (symbol_index, (word >> 12) & 15)
                require(key in lows, 'string MOVT without MOVW')
                low_offset, low, addend = lows.pop(key)
                require(signed16(imm(word)) == addend, 'different string MOV pair addends')
                pointer = low | (imm(observed) << 16)
                validate_string(target, addend, pointer)
                string_values[low_offset] = string_values[offset] = (pointer - addend) & 0xffffffff
            else:
                raise ValueError('unsupported string relocation: ' + str(kind))
        require(not lows, 'unpaired string MOVW')
        seen = set()
        for r, _ in relocs:
            kind, offset = r['r_info_type'], r['r_offset']
            require(offset not in seen, 'overlapping relocations')
            seen.add(offset)
            target = obj.table.get_symbol(r['r_info_sym'])
            if offset in string_values:
                value = string_values[offset]
            elif target['st_shndx'] in bases:
                value = bases[target['st_shndx']] + target['st_value']
            else:
                require(target['st_shndx'] == 'SHN_UNDEF' and target.name in names
                        and len(names[target.name]) == 1, 'unresolved target: ' + target.name)
                value = next(iter(names[target.name]))
            word = struct.unpack_from('<I', original, offset)[0]
            if kind == 2:
                word = (value + word) & 0xffffffff
            elif kind in (28, 29):
                addend = (((word & 0xffffff) ^ 0x800000) - 0x800000) << 2
                distance = value + addend - (address + offset)
                require(distance % 4 == 0 and -(1 << 25) <= distance < (1 << 25),
                        'branch relocation overflow')
                word = (word & 0xff000000) | ((distance >> 2) & 0xffffff)
            elif kind in (43, 44):
                word = relocate_mov(word, value, kind == 44)
            else:
                raise ValueError('unsupported relocation: ' + str(kind))
            struct.pack_into('<I', relocated, offset, word)
        if bytes(relocated) != actual:
            first = next(i for i, (a, b) in enumerate(zip(relocated, actual)) if a != b)
            raise ValueError(f'section bytes differ: {name}+{first:#x}')
        sections[name] = {'bytes': len(actual), 'relocations': len(relocs), 'linked_sha256': sha(actual)}
    functions = {n: obj.named[n]['st_size'] for n in sorted(obj.function_names)}
    require(sum(functions.values()) == sum(sections.get(n, {}).get('bytes', 0)
                                          for n in ('.text', '.init.text', '.exit.text')),
            'executable coverage differs from all function sizes')
    return {'sections': sections, 'functions': functions, 'validated_strings': len(strings),
            'bss_layout_bytes': obj.elf.get_section_by_name('.bss')['sh_size'],
            'compared_bytes': sum(s['bytes'] for s in sections.values())}


def negative_controls(obj, stock, baseline_obj):
    cases = []

    def rejected(binary, label, expected):
        try:
            compare(Object(bytes(binary)), stock)
        except ValueError as error:
            require(expected in str(error), 'unexpected rejection for ' + label + ': ' + str(error))
        else:
            raise ValueError('mutation accepted: ' + label)
        cases.append(label)

    rejected(baseline_obj.binary, 'unmodified upstream codec', 'section layout differs')
    for name, relative, label, expected in [
        ('wm8960_configure_clocking', 0x68, 'clock-limit instruction changed', 'section bytes differ'),
        ('bclk_divs', 4, 'bit-clock divisor changed', 'section bytes differ'),
        ('.LC0', 1, 'diagnostic string changed', 'merged string contents differ')]:
        s = obj.named[name]
        changed = bytearray(obj.binary)
        changed[obj.elf.get_section(s['st_shndx'])['sh_offset'] + s['st_value'] + relative] ^= 1
        rejected(changed, label, expected)
    text_index = obj.elf.get_section_index('.text')
    branch, location = next((r, p) for r, p in obj.relocations[text_index] if r['r_info_type'] == 28)
    replacement = next(i for i, s in enumerate(obj.symbols) if s.name == 'msleep')
    require(branch['r_info_sym'] != replacement, 'negative control selected same branch target')
    changed = bytearray(obj.binary)
    struct.pack_into('<I', changed, location + 4, (replacement << 8) | 28)
    rejected(changed, 'external call redirected', 'section bytes differ')
    section = obj.elf.get_section(text_index)
    relocated_offsets = {r['r_offset'] for r, _ in obj.relocations[text_index]}
    offset = next(i for i in range(0, section['sh_size'], 4)
                  if i not in relocated_offsets and
                  struct.unpack_from('<I', section.data(), i)[0] & 0x0f000000 == 0x0b000000)
    changed = bytearray(obj.binary)
    changed[section['sh_offset'] + offset] ^= 1
    rejected(changed, 'resolved internal call redirected', 'section bytes differ')
    data_index = obj.elf.get_section_index('.data')
    _, location = next((r, p) for r, p in obj.relocations[data_index]
                       if obj.table.get_symbol(r['r_info_sym']).name == 'wm8960_probe')
    replacement = next(i for i, s in enumerate(obj.symbols) if s.name == 'wm8960_i2c_probe')
    changed = bytearray(obj.binary)
    struct.pack_into('<I', changed, location + 4, (replacement << 8) | 2)
    rejected(changed, 'codec registration callback redirected', 'section bytes differ')
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stock_kernel', type=Path)
    parser.add_argument('reference_object', type=Path)
    parser.add_argument('baseline_kernel', type=Path)
    parser.add_argument('baseline_object', type=Path)
    args = parser.parse_args()
    stock, baseline = Image(args.stock_kernel, True), Image(args.baseline_kernel)
    obj, original = Object(args.reference_object.read_bytes()), Object(args.baseline_object.read_bytes())
    result = {'stock_kernel_raw_sha256': RAW_HASH,
              'reference_object_sha256': sha(obj.binary),
              'baseline_kernel_sha256': sha(baseline.binary),
              'baseline_object_sha256': sha(original.binary),
              'reference_against_stock': compare(obj, stock),
              'unmodified_against_baseline': compare(original, baseline),
              'negative_controls': negative_controls(obj, stock, original),
              'verifier_sources': {name: sha((Path(__file__).parent / name).read_bytes())
                                   for name in ('verify_wm8960_sources.py', 'arm_relocations.py',
                                                'verify_busfreq.py', 'verify_ddr_sources.py')},
              'hardware_qualified': False, 'executed_on_headset': False}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
