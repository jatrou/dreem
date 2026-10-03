#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build an independent GATT capture client from pinned public BlueZ source."""
import argparse
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import io

from match_bluetooth_sources import BLUEZ_SHA256, BLUEZ_URL, digest, pinned, require

SHARED = ('att', 'crypto', 'ecc', 'queue', 'util', 'io-mainloop',
          'timeout-mainloop', 'mainloop', 'mainloop-notify', 'gatt-client',
          'gatt-db', 'gatt-helpers')
SOURCES = tuple('src/shared/'+name+'.c' for name in SHARED) + (
    'lib/bluetooth.c', 'lib/uuid.c')
HERE = Path(__file__).resolve().parent


def build(upstream, output, compiler, target, sanitize=False):
    data = pinned(upstream, BLUEZ_SHA256, 4*1024*1024)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    output = output.resolve()
    source = output/'source'
    source.mkdir()
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:xz') as archive:
        for member in archive:
            relative = PurePosixPath(member.name).relative_to('bluez-5.52')
            require(not relative.is_absolute() and '..' not in relative.parts,
                    'unsafe source path')
            if str(relative) not in (*SOURCES, 'COPYING', 'COPYING.LIB') and relative.suffix != '.h':
                continue
            require(member.isfile() and 0 <= member.size <= 1024*1024, 'invalid source member')
            path = source/str(relative)
            require(not path.exists(), 'duplicate source member')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.extractfile(member).read())
    require(all((source/name).is_file() for name in (*SOURCES, 'COPYING', 'COPYING.LIB')),
            'missing source or license')
    patch = (HERE/'bluez-capture.patch').read_bytes()
    (source/'bluez-capture.patch').write_bytes(patch)
    patched = subprocess.run(['patch', '--batch', '--forward', '-p1', '-i', 'bluez-capture.patch'],
                             cwd=source, capture_output=True, text=True, timeout=30)
    require(patched.returncode == 0, 'BlueZ capture patch failed\n'+patched.stderr)
    app = (HERE/'bluetooth_capture.c').read_bytes()
    (source/'bluetooth_capture.c').write_bytes(app)
    lease_sources = ('radio_lease.c', 'radio_lease.h', 'radio_lease_writer.c', 'radio_lease_writer.h')
    for name in lease_sources:
        (source/name).write_bytes((HERE/name).read_bytes())
    flags = ['-Os', '-D_GNU_SOURCE', '-DVERSION="5.52"', '-I.',
             '-ffunction-sections', '-fdata-sections']
    if target == 'arm':
        flags += ['-marm', '-mcpu=cortex-a7', '-mfpu=neon-vfpv4', '-mfloat-abi=hard']
    if sanitize:
        require(target == 'host', 'sanitizers require host target')
        flags += ['-g', '-fno-omit-frame-pointer', '-fsanitize=address,undefined', '-fno-pie']
    commands, objects = [], []
    for name in (*SOURCES, 'radio_lease.c', 'radio_lease_writer.c', 'bluetooth_capture.c'):
        obj = output/(name.replace('/', '_')[:-2]+'.o')
        args = [compiler, *flags]
        if name in ('bluetooth_capture.c', 'radio_lease.c', 'radio_lease_writer.c'):
            args += ['-std=c11', '-Wall', '-Wextra', '-Werror']
        args += ['-c', name, '-o', str(obj)]
        result = subprocess.run(args, cwd=source, capture_output=True, text=True, timeout=60)
        require(result.returncode == 0, 'compile failed: '+name+'\n'+result.stderr)
        commands.append(args)
        objects.append(str(obj))
    binary = output/'dreem-bluetooth-capture'
    link = [compiler, *flags, *objects, '-Wl,--gc-sections', '-o', str(binary), '-lrt']
    if target == 'arm':
        link += ['-static']
    if sanitize:
        link += ['-no-pie']
    result = subprocess.run(link, cwd=source, capture_output=True, text=True, timeout=60)
    require(result.returncode == 0, 'link failed\n'+result.stderr)
    commands.append(link)
    report = {'upstream_url': BLUEZ_URL, 'upstream_sha256': BLUEZ_SHA256,
              'bluez_patch_sha256': digest(patch),
              'application_sha256': digest(app), 'target': target, 'sanitizers': sanitize,
              'lease_source_sha256': {n: digest((source/n).read_bytes()) for n in lease_sources},
              'binary_sha256': digest(binary.read_bytes()), 'commands': commands,
              'command_working_directory': str(source),
              'source_sha256': {n: digest((source/n).read_bytes()) for n in SOURCES},
              'vendor_objects_linked': False, 'physical_qualification': False}
    (output/'build.json').write_text(json.dumps(report, indent=2)+'\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('upstream', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--compiler', required=True)
    parser.add_argument('--target', choices=('host', 'arm'), required=True)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    report = build(args.upstream, args.output, args.compiler, args.target, args.sanitize)
    print(json.dumps({k: report[k] for k in ('target', 'binary_sha256', 'physical_qualification')}))


if __name__ == '__main__':
    main()
