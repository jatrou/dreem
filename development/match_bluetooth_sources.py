#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Rebuild 20 public BlueZ objects and compare the firmware's surviving sections.

The firmware and upstream source archives are hash pinned. Vendor objects are
never linked or executed. Stripped symbols/relocations prevent proving original
external bindings; matching sections do not establish a complete source match.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile

from elftools.elf.elffile import ELFFile
from inspect_firmware import STOCK_SHA256, selected_members

BLUEZ_SHA256 = 'f7144ce2039202cfac18ccb52426efea11c98e4f6e1bb8041bcb994b8378560a'
BLUEZ_URL = 'https://www.kernel.org/pub/linux/bluetooth/bluez-5.52.tar.xz'
ARCHIVES = {
    'libshared-mainloop.a': {
        'sha256': 'f6327f4c4116ac40ffedeed299a2363b4fde2382da5aff0c73dca615015294f3',
        'prefix': 'src/shared',
        'objects': ('queue', 'mgmt', 'crypto', 'ringbuf', 'hci', 'hci-crypto',
                    'uhid', 'pcap', 'att', 'gatt-helpers', 'gatt-client',
                    'gatt-server', 'gap', 'log', 'io-mainloop',
                    'timeout-mainloop', 'mainloop', 'mainloop-notify'),
    },
    'libbluetooth-internal.a': {
        'sha256': '6cbce33b014774efc386a97e782f11fce5ec550afc100f3536e5c3e2f4524d89',
        'prefix': 'lib', 'objects': ('hci', 'uuid'),
    },
}
FLAGS = ('-Os', '-marm', '-mcpu=cortex-a7', '-mfpu=neon-vfpv4',
         '-mfloat-abi=hard', '-fPIC', '-D_GNU_SOURCE', '-DVERSION="5.52"', '-I.')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def pinned(path, expected, limit):
    require(path.stat().st_size <= limit, 'oversized input: '+path.name)
    data = path.read_bytes()
    require(digest(data) == expected, 'unreviewed input: '+path.name)
    return data


def sections(data):
    elf = ELFFile(io.BytesIO(data))
    require(elf['e_machine'] == 'EM_ARM' and elf['e_type'] == 'ET_REL',
            'not an ARM relocatable object')
    result = {}
    for section in elf.iter_sections():
        if section['sh_flags'] & 2:
            result[section.name] = {
                'type': section['sh_type'], 'bytes': section['sh_size'],
                'flags': section['sh_flags'], 'alignment': section['sh_addralign'],
                'sha256': None if section['sh_type'] == 'SHT_NOBITS' else digest(section.data()),
            }
    require('.text' in result and result['.text']['bytes'] > 0, 'missing object code')
    return result, {
        'symbol_table': elf.get_section_by_name('.symtab') is not None,
        'relocations': any(s['sh_type'] in ('SHT_REL', 'SHT_RELA') for s in elf.iter_sections()),
    }


def compare(firmware, upstream, compiler):
    firmware_data = pinned(firmware, STOCK_SHA256, 32*1024*1024)
    source_data = pinned(upstream, BLUEZ_SHA256, 4*1024*1024)
    prefix = 'usr/local/bluez5/gatt/'
    with tarfile.open(fileobj=io.BytesIO(firmware_data), mode='r:bz2') as outer:
        rootfs = selected_members(outer, {'rootfs.tar.gz'})['rootfs.tar.gz']
    with tarfile.open(fileobj=io.BytesIO(rootfs), mode='r:gz') as inner:
        originals = selected_members(inner, {prefix+n for n in ARCHIVES})
    for name, metadata in ARCHIVES.items():
        require(digest(originals[prefix+name]) == metadata['sha256'], 'unreviewed library')

    with tempfile.TemporaryDirectory(prefix='dreem-bluez-source-') as tmp:
        root = Path(tmp)
        source = root/'source'
        source.mkdir()
        wanted = {f"{a['prefix']}/{n}.c" for a in ARCHIVES.values() for n in a['objects']}
        # Read only selected public source files and headers into a new directory.
        # No source configure/build scripts, vendor code or archive links run.
        with tarfile.open(fileobj=io.BytesIO(source_data), mode='r:xz') as archive:
            for member in archive:
                relative = PurePosixPath(member.name).relative_to('bluez-5.52')
                require(not relative.is_absolute() and '..' not in relative.parts,
                        'unsafe source path')
                selected = str(relative) in wanted or relative.suffix == '.h'
                if not selected:
                    continue
                require(member.isfile() and 0 <= member.size <= 1024*1024,
                        'invalid source member')
                target = source/str(relative)
                require(not target.exists(), 'duplicate source member')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
        require(all((source/name).is_file() for name in wanted), 'missing public source')
        version = subprocess.run([compiler, '-dumpfullversion'], check=True,
                                 capture_output=True, text=True, timeout=30).stdout.strip()
        objects = []
        for name, metadata in ARCHIVES.items():
            library = root/name
            library.write_bytes(originals[prefix+name])
            for member in metadata['objects']:
                input_name = f"{metadata['prefix']}/{member}.c"
                target = root/(name+'-'+member+'.o')
                built = subprocess.run([compiler, *FLAGS, '-c', input_name, '-o', str(target)],
                                       cwd=source, capture_output=True, text=True, timeout=60)
                require(built.returncode == 0, 'compile failed for '+input_name+': '+built.stderr)
                saved = subprocess.run(['ar', 'p', str(library), member+'.o'],
                                       check=True, capture_output=True, timeout=30).stdout
                expected, stripped = sections(saved)
                actual, _ = sections(target.read_bytes())
                require(not stripped['symbol_table'] and not stripped['relocations'],
                        'original library stripping differs')
                require(expected == actual, 'allocated sections differ for '+input_name)
                objects.append({'archive': name, 'member': member+'.o', 'source': input_name,
                                'source_sha256': digest((source/input_name).read_bytes()),
                                'sections': actual})
    return {'firmware_sha256': STOCK_SHA256, 'upstream_url': BLUEZ_URL,
            'upstream_sha256': BLUEZ_SHA256, 'compiler_version': version,
            'compiler_flags': FLAGS, 'matched_objects': len(objects),
            'allocated_bytes': sum(s['bytes'] for o in objects for s in o['sections'].values()),
            'initialized_bytes': sum(s['bytes'] for o in objects for s in o['sections'].values()
                                     if s['type'] != 'SHT_NOBITS'),
            'objects': objects, 'original_external_bindings_verified': False,
            'complete_bluetooth_source_match': False, 'physical_qualification': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('firmware', type=Path)
    parser.add_argument('upstream', type=Path)
    parser.add_argument('--compiler', required=True)
    args = parser.parse_args()
    print(json.dumps(compare(args.firmware, args.upstream, args.compiler), indent=2))


if __name__ == '__main__':
    main()
