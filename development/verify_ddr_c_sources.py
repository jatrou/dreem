#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Match NXP DDR3 C wrappers and settings against the reviewed firmware.

Relink the actual ARM object at each kernel's symbol addresses and compare every
function byte. Local strings and data/state layouts are verified before resolving
their addresses. Only the diagnostic source-file prefix may differ. No execution,
firmware installation, recovered-source output, or ignored instruction bytes.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import struct

from elftools.elf.elffile import ELFFile
from verify_busfreq import Image, RAW_HASH, require
from verify_ddr_sources import bytes_at
from arm_relocations import relocate_mov

FUNCTIONS = {'update_ddr_freq_imx6_up': 272, 'init_mmdc_ddr3_settings_imx6_up': 852}
SOURCE_SUFFIX = b'arch/arm/mach-imx/busfreq_ddr3.c'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def immediate(word):
    return ((word >> 4) & 0xf000) | (word & 0xfff)


def string_at(image, address):
    for base, length, data in image.sections:
        offset = address - base
        if 0 <= offset < len(data):
            value, separator, _ = data[offset:offset + 4096].partition(b'\0')
            require(separator, 'unterminated linked string')
            return value
    raise ValueError('string address is not file backed')


class Object:
    def __init__(self, binary):
        self.binary = binary
        self.elf = elf = ELFFile(io.BytesIO(binary))
        require(elf.elfclass == 32 and elf.little_endian and elf['e_machine'] == 'EM_ARM' and
                elf['e_type'] == 'ET_REL', 'expected an ARM32 relocatable object')
        self.table = elf.get_section_by_name('.symtab')
        require(self.table is not None, 'object has no symbol table')
        self.symbols = list(self.table.iter_symbols())
        self.named = {s.name: s for s in self.symbols if s.name}
        self.relocations = []
        for section in elf.iter_sections():
            if section['sh_type'] != 'SHT_REL':
                continue
            require(section['sh_link'] == elf.get_section_index('.symtab'), 'wrong relocation symbol table')
            for index, relocation in enumerate(section.iter_relocations()):
                self.relocations.append((section['sh_info'], relocation,
                                         section['sh_offset'] + index * section['sh_entsize']))
        for name, size in FUNCTIONS.items():
            require(name in self.named and self.named[name]['st_size'] == size,
                    'unexpected function size: ' + name)

    def function(self, name):
        symbol = self.named[name]
        section = self.elf.get_section(symbol['st_shndx'])
        start, size = symbol['st_value'], symbol['st_size']
        code = section.data()[start:start + size]
        require(len(code) == size, 'truncated function')
        relocs = [(r, location) for index, r, location in self.relocations
                  if index == symbol['st_shndx'] and start <= r['r_offset'] < start + size]
        return code, relocs

    def source_string(self, symbol):
        section = self.elf.get_section(symbol['st_shndx'])
        value, separator, _ = section.data()[symbol['st_value']:symbol['st_value'] + 4096].partition(b'\0')
        require(separator, 'unterminated object string')
        return value


def symbol_addresses(image):
    elf = ELFFile(io.BytesIO(image.binary))
    result = {}
    for symbol in elf.get_section_by_name('.symtab').iter_symbols():
        if symbol.name:
            result.setdefault(symbol.name, set()).add(symbol['st_value'])
    return result


def layout(obj, image, names, section_name):
    index = obj.elf.get_section_index(section_name)
    objects = [s for s in obj.symbols if s['st_shndx'] == index and s['st_info']['type'] == 'STT_OBJECT']
    require(objects, 'no objects in ' + section_name)
    bases = None
    for symbol in objects:
        require(symbol.name in names, 'linked object missing: ' + symbol.name)
        choices = {address - symbol['st_value'] for address in names[symbol.name]}
        bases = choices if bases is None else bases & choices
    require(len(bases) == 1, 'object layout differs or is ambiguous: ' + section_name)
    base = bases.pop()
    tables = {}
    if section_name == '.data':
        for symbol in objects:
            start, size = symbol['st_value'], symbol['st_size']
            require(size > 0, 'empty data comparison')
            source = obj.elf.get_section(index).data()[start:start + size]
            actual = bytes_at(image, base + start, size)
            require(actual == source, 'data bytes differ: ' + symbol.name)
            tables[symbol.name] = {'bytes': size, 'sha256': digest(source)}
        require(len(tables) == 9 and sum(x['bytes'] for x in tables.values()) == 464,
                'unexpected DDR settings table coverage')
    else:
        require(len(objects) == 12 and all(s['st_size'] == 4 for s in objects),
                'unexpected DDR state layout')
    return index, base, tables


def verify_function(obj, image, name, names, bases):
    symbol = obj.named[name]
    code, relocs = obj.function(name)
    source = bytearray(code)
    address = image.symbols[name]
    actual = bytes_at(image, address, len(source))
    local_strings, low_words, diagnostic_paths = {}, {}, set()
    # Resolve local literals through complete MOVW/MOVT pairs, then validate the
    # pointed-to content. Addresses cannot simply be masked to claim a match.
    for relocation, _ in relocs:
        kind, index = relocation['r_info_type'], relocation['r_info_sym']
        target = obj.table.get_symbol(index)
        offset = relocation['r_offset'] - symbol['st_value']
        if kind not in (43, 44):
            continue
        original_word = struct.unpack_from('<I', code, offset)[0]
        require(immediate(original_word) == 0, 'unexpected MOVW/MOVT addend')
        word = struct.unpack_from('<I', actual, offset)[0]
        register = (original_word >> 12) & 15
        if kind == 43:
            low_words[(index, register)] = immediate(word)
        elif target.name.startswith('.LC'):
            require((index, register) in low_words, 'MOVT has no matching MOVW')
            pointer = low_words[(index, register)] | (immediate(word) << 16)
            expected_string = obj.source_string(target)
            observed_string = string_at(image, pointer)
            if expected_string != observed_string:
                require(expected_string.endswith(SOURCE_SUFFIX) and observed_string.endswith(SOURCE_SUFFIX),
                        'local string bytes differ: ' + target.name)
                diagnostic_paths.add(target.name)
            require(index not in local_strings or local_strings[index] == pointer,
                    'inconsistent address for a local string')
            local_strings[index] = pointer
    relocated_offsets = set()
    for relocation, _ in relocs:
        kind, index = relocation['r_info_type'], relocation['r_info_sym']
        target = obj.table.get_symbol(index)
        offset = relocation['r_offset'] - symbol['st_value']
        require(offset % 4 == 0 and offset + 4 <= len(source) and offset not in relocated_offsets,
                'invalid or overlapping relocation')
        relocated_offsets.add(offset)
        if index in local_strings:
            value = local_strings[index]
        elif target['st_shndx'] in bases:
            value = bases[target['st_shndx']] + target['st_value']
        else:
            require(target['st_shndx'] == 'SHN_UNDEF' and target.name in names and len(names[target.name]) == 1,
                    'unresolved or ambiguous relocation: ' + target.name)
            value = next(iter(names[target.name]))
        word = struct.unpack_from('<I', source, offset)[0]
        if kind == 2:  # R_ARM_ABS32
            word = (word + value) & 0xffffffff
        elif kind == 28:  # R_ARM_CALL: preserve opcode, resolve signed branch addend.
            addend = (((word & 0xffffff) ^ 0x800000) - 0x800000) << 2
            distance = value + addend - (address + offset)
            require(distance % 4 == 0 and -(1 << 25) <= distance < (1 << 25), 'branch relocation overflow')
            word = (word & 0xff000000) | ((distance >> 2) & 0xffffff)
        elif kind in (43, 44):  # R_ARM_MOVW_ABS_NC / R_ARM_MOVT_ABS
            word = relocate_mov(word, value, kind == 44)
        else:
            raise ValueError('unsupported relocation: ' + str(kind))
        struct.pack_into('<I', source, offset, word)
    require(bytes(source) == actual, 'function bytes differ after relocation: ' + name)
    return {'bytes': len(source), 'relocations': len(relocs), 'linked_sha256': digest(actual),
            'validated_local_strings': len(local_strings),
            'diagnostic_source_path_differences': len(diagnostic_paths)}


def compare(obj, image):
    names = symbol_addresses(image)
    bss_index, bss_base, _ = layout(obj, image, names, '.bss')
    data_index, data_base, tables = layout(obj, image, names, '.data')
    bases = {bss_index: bss_base, data_index: data_base}
    functions = {name: verify_function(obj, image, name, names, bases) for name in FUNCTIONS}
    return {'functions': functions, 'tables': tables, 'verified_state_words': 12}


def negative_controls(obj, stock):
    cases = []

    def rejected(binary, label, expected_message):
        try:
            compare(Object(bytes(binary)), stock)
        except ValueError as error:
            require(expected_message in str(error), 'mutation rejected for an unexpected reason: ' + label)
        else:
            raise ValueError('mutation was not detected: ' + label)
        cases.append(label)

    function = obj.named['update_ddr_freq_imx6_up']
    section = obj.elf.get_section(function['st_shndx'])
    changed = bytearray(obj.binary)
    changed[section['sh_offset'] + function['st_value'] + 0x14] ^= 1
    rejected(changed, 'changed non-relocated instruction', 'function bytes differ')

    _, relocs = obj.function('update_ddr_freq_imx6_up')
    branch, location = next((r, p) for r, p in relocs if r['r_info_type'] == 28)
    replacement = next(i for i, s in enumerate(obj.symbols) if s.name == 'save_ttbr1')
    changed = bytearray(obj.binary)
    struct.pack_into('<I', changed, location + 4, (replacement << 8) | 28)
    rejected(changed, 'changed branch relocation target', 'function bytes differ')

    table = obj.named['ddr3_dll_mx6sx']
    changed = bytearray(obj.binary)
    changed[obj.elf.get_section(table['st_shndx'])['sh_offset'] + table['st_value'] + 20] ^= 1
    rejected(changed, 'changed DDR command-table value', 'data bytes differ')

    literal = obj.named['.LC0']
    changed = bytearray(obj.binary)
    changed[obj.elf.get_section(literal['st_shndx'])['sh_offset'] + literal['st_value'] + 4] ^= 1
    rejected(changed, 'changed ordinary string contents', 'local string bytes differ')
    return cases


def verify(stock_path, rebuilt_path, object_path):
    stock, rebuilt = Image(stock_path, True), Image(rebuilt_path)
    require(stock.symbols['update_ddr_freq_imx6_up'] == 0x8002ae0c and
            stock.symbols['init_mmdc_ddr3_settings_imx6_up'] == 0x8002afd8, 'unexpected stock function layout')
    obj = Object(object_path.read_bytes())
    matches = {name: compare(obj, image) for name, image in [('stock', stock), ('rebuilt', rebuilt)]}
    controls = negative_controls(obj, stock)
    return {'stock_raw_sha256': RAW_HASH, 'rebuilt_kernel_sha256': digest(rebuilt.binary),
            'c_object_sha256': digest(obj.binary), 'matched_function_bytes': sum(FUNCTIONS.values()),
            'matched_table_bytes': 464, 'matches': matches, 'negative_controls': controls,
            'upstream_source': 'arch/arm/mach-imx/busfreq_ddr3.c', 'runtime_qualified': False,
            'limits': 'Source/binary comparison; diagnostic source paths may differ. No physical DDR or initialization-failure qualification.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stock_kernel', type=Path)
    parser.add_argument('rebuilt_kernel', type=Path)
    parser.add_argument('nxp_c_object', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.stock_kernel, args.rebuilt_kernel, args.nxp_c_object), indent=2))


if __name__ == '__main__':
    main()
