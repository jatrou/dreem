#!/usr/bin/env python3
"""Recover Module.symvers from a symbolized ARM Linux 4.1 kernel.

Requires pyelftools. No firmware code is executed. A recovered CRC table is
useful for source matching; it does not prove replacement module ABI safety.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path
import struct
import tarfile

from elftools.elf.elffile import ELFFile


def module_versions(data):
    if len(data) % 64:
        raise ValueError("invalid ARM32 module version records")
    result = {}
    for offset in range(0, len(data), 64):
        crc, = struct.unpack_from("<I", data, offset)
        raw = data[offset + 4:offset + 64]
        if b"\0" not in raw:
            raise ValueError("unterminated module symbol")
        name = raw.split(b"\0", 1)[0].decode("ascii")
        if not name or name in result:
            raise ValueError("empty or duplicate module symbol")
        result[name] = crc
    return result


def recover(elf):
    if elf.elfclass != 32 or not elf.little_endian or elf['e_machine'] != 'EM_ARM':
        raise ValueError("requires little-endian ARM32 ELF")
    table = elf.get_section_by_name(".symtab")
    if table is None:
        raise ValueError("kernel needs recovered symbols; run vmlinux-to-elf first")
    symbols = {s.name: s['st_value'] for s in table.iter_symbols()}
    sections = [(s['sh_addr'], s.data()) for s in elf.iter_sections()
                if s['sh_type'] != 'SHT_NOBITS' and s['sh_flags'] & 2]

    def read_word(address):
        for start, data in sections:
            offset = address - start
            if 0 <= offset <= len(data) - 4:
                return struct.unpack_from('<I', data, offset)[0]
        raise ValueError(f"unmapped kernel address: {address:x}")

    ranges = [(symbols['__start___ksymtab' + suffix], symbols['__stop___ksymtab' + suffix], kind)
              for suffix, kind in [('', 'EXPORT_SYMBOL'), ('_gpl', 'EXPORT_SYMBOL_GPL'),
                                   ('_gpl_future', 'EXPORT_SYMBOL_GPL_FUTURE'),
                                   ('_unused', 'EXPORT_UNUSED_SYMBOL'),
                                   ('_unused_gpl', 'EXPORT_UNUSED_SYMBOL_GPL')]]
    exports = {}
    for symbol, address in symbols.items():
        if not symbol.startswith('__kcrctab_'):
            continue
        name = symbol.removeprefix('__kcrctab_')
        export_address = symbols['__ksymtab_' + name]
        kinds = [kind for start, end, kind in ranges if start <= export_address < end]
        if len(kinds) != 1:
            raise ValueError(f"ambiguous export classification: {name}")
        exports[name] = (read_word(address), kinds[0])
    if not exports:
        raise ValueError("no exported CRCs found")
    return exports


def verify_modules(rootfs, exports):
    report = {"modules": 0, "matched_kernel_references": 0,
              "unresolved_references": {}, "mismatches": []}
    module_exports = {}
    version_tables = {}
    with tarfile.open(rootfs, mode="r:gz") as archive:
        for member in archive:
            if not member.name.endswith('.ko') or not member.isfile():
                continue
            if member.size > 16 * 1024 * 1024:
                raise ValueError("oversized kernel module")
            data = archive.extractfile(member).read()
            elf = ELFFile(io.BytesIO(data))
            if elf.elfclass != 32 or elf['e_machine'] != 'EM_ARM' or not elf.little_endian:
                raise ValueError("unexpected module architecture")
            table = elf.get_section_by_name('.symtab')
            if table:
                for symbol in table.iter_symbols():
                    if symbol.name.startswith('__crc_') and symbol['st_shndx'] == 'SHN_ABS':
                        module_exports[symbol.name.removeprefix('__crc_')] = symbol['st_value']
            versions = elf.get_section_by_name('__versions')
            if versions:
                version_tables[member.name] = module_versions(versions.data())
    module_matches = 0
    for module, versions in version_tables.items():
        report['modules'] += 1
        for name, crc in versions.items():
            if name in exports:
                expected = exports[name][0]
                provider = 'kernel'
                if expected == crc:
                    report['matched_kernel_references'] += 1
                    continue
            elif name in module_exports:
                expected = module_exports[name]
                provider = 'module'
                if expected == crc:
                    module_matches += 1
                    continue
            else:
                report['unresolved_references'].setdefault(module, []).append(name)
                continue
            report['mismatches'].append({'module': module, 'symbol': name,
                                          'provider': provider,
                                          'expected': f'{expected:08x}', 'actual': f'{crc:08x}'})
    report['matched_module_references'] = module_matches
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kernel', type=Path)
    parser.add_argument('output', type=Path, help='new private output directory')
    parser.add_argument('--rootfs', type=Path, help='verify CRCs against archived stock modules')
    args = parser.parse_args()
    with args.kernel.open('rb') as stream:
        exports = recover(ELFFile(stream))
    report = {'kernel_elf_sha256': hashlib.sha256(args.kernel.read_bytes()).hexdigest(),
              'export_count': len(exports)}
    if args.rootfs:
        report['verification'] = verify_modules(args.rootfs, exports)
    args.output.mkdir(mode=0o700)
    (args.output / 'Module.symvers').write_text(''.join(
        f'0x{crc:08x}\t{name}\tvmlinux\t{kind}\n'
        for name, (crc, kind) in sorted(exports.items())))
    (args.output / 'exports-report.json').write_text(json.dumps(report, indent=2) + '\n')
    summary = {k: v for k, v in report.items() if k != 'verification'}
    if args.rootfs:
        v = report['verification']
        summary['verification'] = {k: value for k, value in v.items()
                                   if k not in ('mismatches', 'unresolved_references')}
        summary['verification']['mismatches'] = len(v['mismatches'])
        summary['verification']['unresolved_references'] = sum(map(len, v['unresolved_references'].values()))
    print(json.dumps(summary, indent=2))
    if args.rootfs and (report['verification']['mismatches'] or
                        report['verification']['unresolved_references'] or
                        not report['verification']['modules']):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
