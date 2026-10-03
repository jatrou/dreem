#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Verify NXP DDR3 assembly against the reviewed firmware with exact relocations.

No code is executed. Only the assembler object's declared relocation is applied;
no bytes are masked or ignored. Original firmware and generated objects stay private.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import struct

from elftools.elf.elffile import ELFFile
from verify_busfreq import Image, RAW_HASH, require

FUNCTION = 'imx6_up_ddr3_freq_change'
SIZE = 1764


def bytes_at(image, address, size):
    require(size > 0, 'empty comparison is not evidence')
    for base, length, data in image.sections:
        offset = address - base
        if 0 <= offset and offset + size <= len(data):
            return data[offset:offset + size]
    raise ValueError('comparison range is not backed by file bytes')


def verify(stock_path, rebuilt_path, object_path):
    stock, rebuilt = Image(stock_path, True), Image(rebuilt_path)
    binary = object_path.read_bytes()
    elf = ELFFile(io.BytesIO(binary))
    require(elf.elfclass == 32 and elf.little_endian and elf['e_machine'] == 'EM_ARM' and
            elf['e_type'] == 'ET_REL', 'expected ARM32 relocatable assembler object')
    table = elf.get_section_by_name('.symtab')
    functions = [s for s in table.iter_symbols() if s.name == FUNCTION]
    require(len(functions) == 1, 'assembler function missing or ambiguous')
    function = functions[0]
    require(function['st_size'] == SIZE, 'unexpected DDR3 function size')
    start = function['st_value']
    code = elf.get_section(function['st_shndx']).data()[start:start + SIZE]
    require(len(code) == SIZE, 'truncated assembler code')
    relocations = []
    for section in elf.iter_sections():
        if section['sh_type'] != 'SHT_REL' or section['sh_info'] != function['st_shndx']:
            continue
        require(section['sh_link'] == elf.get_section_index('.symtab'), 'unexpected relocation table')
        for relocation in section.iter_relocations():
            offset = relocation['r_offset'] - start
            if not 0 <= offset < SIZE:
                continue
            target = table.get_symbol(relocation['r_info_sym'])
            relocations.append((offset, relocation['r_info_type'], target.name))
    require(relocations == [(0x680, 2, 'iram_tlb_phys_addr')],
            'unexpected DDR3 relocation layout')
    require(struct.unpack_from('<I', code, 0x680)[0] == 0, 'unexpected relocation addend')
    results = {}
    for name, image in [('stock', stock), ('rebuilt', rebuilt)]:
        require(image.symbols[FUNCTION + '_end'] - image.symbols[FUNCTION + '_start'] == SIZE,
                'linked DDR3 span differs')
        expected = bytearray(code)
        struct.pack_into('<I', expected, 0x680, image.symbols['iram_tlb_phys_addr'])
        actual = bytes_at(image, image.symbols[FUNCTION], SIZE)
        require(bytes(expected) == actual, name + ': DDR3 bytes differ after exact relocation')
        results[name] = hashlib.sha256(actual).hexdigest()
    helpers = {}
    for name, size in [('save_ttbr1', 8), ('restore_ttbr1', 16)]:
        a = bytes_at(stock, stock.symbols[name], size)
        b = bytes_at(rebuilt, rebuilt.symbols[name], size)
        require(a == b, name + ': exact helper bytes differ')
        helpers[name] = {'bytes': size, 'sha256': hashlib.sha256(a).hexdigest()}
    return {'stock_raw_sha256': RAW_HASH, 'rebuilt_kernel_sha256': hashlib.sha256(rebuilt.binary).hexdigest(),
            'assembler_object_sha256': hashlib.sha256(binary).hexdigest(),
            'function': FUNCTION, 'matched_bytes': SIZE, 'relocation':
            {'offset': 0x680, 'type': 'R_ARM_ABS32', 'target': 'iram_tlb_phys_addr', 'addend': 0},
            'linked_function_sha256': results, 'exact_helpers': helpers,
            'upstream_source': 'arch/arm/mach-imx/ddr3_freq_imx6sx.S',
            'runtime_qualified': False,
            'limits': 'Source/binary match only; no proof of board settings, DDR timing, physical safety or wrapper behavior'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stock_kernel', type=Path)
    parser.add_argument('rebuilt_kernel', type=Path)
    parser.add_argument('nxp_assembler_object', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.stock_kernel, args.rebuilt_kernel, args.nxp_assembler_object), indent=2))


if __name__ == '__main__':
    main()
