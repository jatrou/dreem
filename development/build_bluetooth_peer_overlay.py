#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build a private, non-installed Bluetooth peer overlay for one exact core.

The output still contains vendor firmware. Only this builder and the independent
filter source belong in the public repository. No vendor program is executed.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess

from elftools.elf.elffile import ELFFile

HERE = Path(__file__).resolve().parent
CORE_SHA256 = 'dfc83b247b505aa08ea62060f999192112a47b606754d5f469357b7addf0295a'
CORE_SIZE = 14_170_096
BASE, CODE = 0x01000000, 0x01001000
ORIGINAL_HELPER = 0x38318
CALL_SITES = (0x38670, 0x38b4c)
PHDR = struct.Struct('<8I')
PT_LOAD, PT_PHDR = 1, 6


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_regular(path, limit, private=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size <= limit,
                'input must be a bounded regular file')
        if private:
            require(info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                    'peer configuration must be owned by the current user and private')
        data = stream.read(limit+1)
        require(len(data) <= limit, 'input grew beyond its limit')
        return data


def write_private(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
        stream.write(data)


def normalize_peers(config):
    require(isinstance(config, dict) and set(config) == {'version', 'peers'} and
            type(config['version']) is int and config['version'] == 1,
            'expected version 1 peer configuration')
    peers = config['peers']
    require(isinstance(peers, list) and len(peers) <= 8, 'at most eight explicit peers allowed')
    require(all(isinstance(p, str) and re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}', p)
                for p in peers), 'invalid peer address')
    peers = sorted(p.upper() for p in peers)
    require(len(set(peers)) == len(peers), 'duplicate peer address')
    require(not {'00:00:00:00:00:00', 'FF:FF:FF:FF:FF:FF'}.intersection(peers),
            'reserved peer address')
    return peers


def arm_branch(source, destination, link=True):
    displacement = destination-source-8
    require(source % 4 == 0 and destination % 4 == 0 and
            -(1 << 25) <= displacement < 1 << 25, 'ARM branch is not representable')
    return struct.pack('<I', (0xeb000000 if link else 0xea000000) |
                       ((displacement >> 2) & 0xffffff))


def headers(data):
    require(len(data) >= 52 and data[:7] == b'\x7fELF\x01\x01\x01', 'expected ELF32 little endian')
    require(struct.unpack_from('<HHI', data, 16) == (2, 40, 1), 'expected ARM ET_EXEC')
    require(struct.unpack_from('<H', data, 40)[0] == 52, 'unexpected ELF header size')
    offset = struct.unpack_from('<I', data, 28)[0]
    size, count = struct.unpack_from('<HH', data, 42)
    require(size == 32 and 0 < count < 64 and 52 <= offset <= len(data)-size*count,
            'invalid program header table')
    result = [list(PHDR.unpack_from(data, offset+i*size)) for i in range(count)]
    loads = [p for p in result if p[0] == PT_LOAD]
    require(loads and loads[0][1] == 0 and loads[0][4] >= 52, 'first load must contain ELF header')
    for p in result:
        _, off, virtual, physical, filesz, memsz, flags, align = p
        require(filesz <= memsz and off <= len(data) and filesz <= len(data)-off and
                virtual+memsz < 1 << 32, 'invalid segment extent')
        if p[0] == PT_LOAD:
            require(align >= 4096 and align & (align-1) == 0 and
                    off % align == virtual % align, 'invalid load alignment')
            require(not (flags & 1 and flags & 2), 'writable executable input segment')
    require([p[2] for p in loads] == sorted(p[2] for p in loads), 'unordered load segments')
    return result


def append_rx(data, payload, *, base=BASE):
    """Append a read/execute segment with relocated PHDRs, keeping Linux 4.1 AT_PHDR.

    Used on independently compiled loader fixtures as well as the pinned core.
    Does not patch any instruction. The command-line builder pins the real core.
    """
    table = headers(data)
    loads = [p for p in table if p[0] == PT_LOAD]
    bias = loads[0][2]-loads[0][1]
    # Linux 4.1 computes AT_PHDR from first-load bias + e_phoff, not PT_PHDR.
    offset = base-bias
    require(base % 0x10000 == 0 and offset % 0x10000 == 0 and
            len(data) <= offset <= 32*1024*1024 and 0 < len(payload) <= 65536,
            'overlay extent or alignment invalid')
    require(all((p[2]+p[5]+4095) & ~4095 <= base for p in loads),
            'overlay overlaps an original load or its BSS')
    count, size = len(table)+1, 4096+len(payload)
    phdrs = [p for p in table if p[0] == PT_PHDR]
    require(len(phdrs) <= 1, 'ambiguous PHDR segment')
    if phdrs:
        phdrs[0][:] = [PT_PHDR, offset, base, base, count*32, count*32, 4, 4]
    table.append([PT_LOAD, offset, base, base, size, size, 5, 0x10000])
    result = bytearray(data)
    result.extend(bytes(offset+size-len(result)))
    struct.pack_into('<I', result, 28, offset)
    struct.pack_into('<H', result, 44, count)
    for i, p in enumerate(table):
        PHDR.pack_into(result, offset+i*32, *p)
    result[offset+4096:] = payload
    headers(result)
    return bytes(result)


def file_offset(data, virtual, length=4):
    matches = [p[1]+virtual-p[2] for p in headers(data)
               if p[0] == PT_LOAD and p[2] <= virtual and virtual+length <= p[2]+p[4]]
    require(len(matches) == 1, 'patch is not backed by exactly one load')
    return matches[0]


def overlay_payload(data):
    elf = ELFFile(io.BytesIO(data))
    require(elf['e_machine'] == 'EM_ARM' and elf['e_type'] == 'ET_EXEC' and
            elf['e_entry'] == CODE and elf['e_flags'] == 0x5000400,
            'unexpected overlay architecture, entry or ABI')
    require(not any(s['sh_type'] in ('SHT_REL', 'SHT_RELA', 'SHT_DYNAMIC') and s['sh_size']
                    for s in elf.iter_sections()), 'overlay has unresolved relocations or dynamic linkage')
    symbols = elf.get_section_by_name('.symtab')
    require(symbols is not None and not any(s.name and s['st_shndx'] == 'SHN_UNDEF'
                                          for s in symbols.iter_symbols()), 'undefined overlay symbol')
    require(symbols.get_symbol_by_name('dreem_peer_connected')[0]['st_value'] == CODE,
            'overlay entry symbol differs')
    allocated = [s for s in elf.iter_sections() if s['sh_flags'] & 2 and s['sh_size']]
    require(allocated and all(not s['sh_flags'] & 1 and s['sh_type'] == 'SHT_PROGBITS' and
                              CODE <= s['sh_addr'] < CODE+65536 for s in allocated),
            'overlay needs writable, zero-filled or out-of-range storage')
    end = max(s['sh_addr']+s['sh_size'] for s in allocated)
    require(end <= CODE+65536, 'oversized overlay')
    payload = bytearray(end-CODE)
    for section in allocated:
        start = section['sh_addr']-CODE
        payload[start:start+section['sh_size']] = section.data()
    return bytes(payload)


def patch_core(original, payload):
    require(len(original) == CORE_SIZE and digest(original) == CORE_SHA256, 'unreviewed original core')
    result = bytearray(append_rx(original, payload))
    patches = []
    for address in CALL_SITES:
        offset = file_offset(original, address)
        require(original[offset:offset+4] == arm_branch(address, ORIGINAL_HELPER),
                'original call site differs')
        result[offset:offset+4] = arm_branch(address, CODE)
        patches.append({'virtual_address': address, 'file_offset': offset,
                        'original_target': ORIGINAL_HELPER, 'new_target': CODE})
    allowed = {28, 29, 30, 31, 44, 45}
    for patch in patches:
        allowed.update(range(patch['file_offset'], patch['file_offset']+4))
    require(all(a == b or i in allowed for i, (a, b) in enumerate(zip(original, result))),
            'unexpected original-byte modification')
    return bytes(result), patches


def build(core, peer_file, output, compiler):
    original = read_regular(core, CORE_SIZE)
    require(len(original) == CORE_SIZE and digest(original) == CORE_SHA256, 'unreviewed original core')
    peers = normalize_peers(json.loads(read_regular(peer_file, 16384, private=True)))
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    output = output.resolve()
    source = (HERE/'bluetooth_peer_filter.c').read_bytes()
    write_private(output/'bluetooth_peer_filter.c', source)
    # Only validated hexadecimal strings enter generated C. An empty table is a
    # regression control: every peer still reaches the original helper.
    table = ('/* Private, operator-specific build input. */\n'
             'const unsigned int dreem_extension_peer_count = '+str(len(peers))+';\n'
             'const char dreem_extension_peers[][18] = {\n'+
             ',\n'.join('    "'+p+'"' for p in (peers or ['']))+'\n};\n').encode()
    write_private(output/'peers.c', table)
    linker = f'''ENTRY(dreem_peer_connected)
stock_peer_connected = {ORIGINAL_HELPER:#x};
SECTIONS {{
  . = {CODE:#x};
  .text : {{ *(.text.entry) *(.text*) }}
  .rodata : {{ *(.rodata*) }}
  .data : {{ *(.data*) *(.bss*) *(COMMON) }}
  ASSERT(SIZEOF(.data) == 0, "overlay must have no writable state")
  /DISCARD/ : {{ *(.comment) *(.note*) *(.ARM.exidx*) *(.ARM.extab*) *(.eh_frame*) }}
}}
'''.encode()
    write_private(output/'overlay.ld', linker)
    command = [compiler, '-std=c11', '-Os', '-Wall', '-Wextra', '-Werror', '-marm',
               '-mcpu=cortex-a7', '-mfpu=neon-vfpv4', '-mfloat-abi=hard',
               '-ffreestanding', '-fno-builtin', '-fno-stack-protector', '-fno-pie',
               '-fno-unwind-tables', '-fno-asynchronous-unwind-tables',
               '-nostdlib', '-static', '-no-pie', '-Wl,--build-id=none,-T,overlay.ld',
               'bluetooth_peer_filter.c', 'peers.c', '-o', 'overlay.elf']
    completed = subprocess.run(command, cwd=output, capture_output=True, text=True, timeout=60)
    require(completed.returncode == 0, 'overlay compile failed\n'+completed.stderr)
    (output/'overlay.elf').chmod(0o600)
    component = (output/'overlay.elf').read_bytes()
    payload = overlay_payload(component)
    modified, patches = patch_core(original, payload)
    write_private(output/'nano_core.peer-overlay', modified)
    report = {'original_sha256': CORE_SHA256, 'modified_sha256': digest(modified),
              'component_sha256': digest(component), 'payload_sha256': digest(payload),
              'payload_bytes': len(payload), 'peer_count': len(peers), 'call_sites': patches,
              'source_sha256': digest(source), 'private_peer_source_sha256': digest(table),
              'linker_sha256': digest(linker), 'builder_sha256': digest(Path(__file__).read_bytes()),
              'program_header_address': BASE, 'code_address': CODE,
              'command': command, 'working_directory': str(output),
              'vendor_code_in_output': True, 'installed': False,
              'radio_power_policy_changed': False, 'physical_qualification': False}
    write_private(output/'build.json', (json.dumps(report, indent=2)+'\n').encode())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('core', type=Path)
    parser.add_argument('peer_file', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--compiler', required=True)
    args = parser.parse_args()
    report = build(args.core, args.peer_file, args.output, args.compiler)
    print(json.dumps({k: report[k] for k in ('modified_sha256', 'payload_bytes', 'peer_count', 'installed')}))


if __name__ == '__main__':
    main()
