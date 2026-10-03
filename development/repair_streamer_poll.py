#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build a bounded polling repair from one privately supplied streamer source.

The desktop checkout remains untouched. Full input/patched source and generated
executables are retained only in a new private directory outside this repository.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

SOURCE_SHA256 = '9c1cb2751d560917a18fa8867cf8dc9be00b93a9f8a8ec353f48bbdadebe46bf'
EDITS = (
    ('            descriptors[1].events = POLLOUT | POLLERR | POLLHUP;',
     '''            /* Watch writability only while bytes remain to send. An idle
             * connected socket is normally writable and would otherwise spin.
             * Error/hangup events are reported even with no requested events.
             */
            descriptors[1].events = 0;
            if (state.preface != NULL || state.queue.head != NULL) {
                descriptors[1].events |= POLLOUT;
            }'''),
)


def repair_source(raw):
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise ValueError('unreviewed streamer source')
    source = raw.decode('utf-8').replace('\r\n', '\n')
    for before, after in EDITS:
        if source.count(before) != 1:
            raise ValueError('polling repair anchor is not unique')
        source = source.replace(before, after)
    return source.encode('utf-8')


def write_private(path, data):
    with path.open('xb') as stream:
        path.chmod(0o600)
        stream.write(data)


def build(source, output):
    raw = source.read_bytes()
    patched = repair_source(raw)
    output = output.resolve()
    repository = Path(__file__).resolve().parents[1]
    if output.is_relative_to(repository):
        raise ValueError('generated source and executables must remain outside the repository')
    output.mkdir(mode=0o700)
    input_path = output / 'streamer-input.c'
    source_path = output / 'streamer-poll-repaired.c'
    write_private(input_path, raw)
    write_private(source_path, patched)
    manifest = {
        'input_sha256': SOURCE_SHA256,
        'patched_source_sha256': hashlib.sha256(patched).hexdigest(),
        'repair_tool_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'desktop_checkout_modified': False,
        'deployed': False,
        'builds': {},
    }
    for name, compiler, flags in (
        ('host', 'cc', []),
        ('arm', 'arm-linux-gnueabihf-gcc',
         ['-static', '-marm', '-mcpu=cortex-a7', '-mfpu=neon-vfpv4', '-mfloat-abi=hard']),
    ):
        executable = output / ('dreem-live-streamer.' + name)
        command = [compiler, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                   *flags, str(source_path), '-o', str(executable)]
        result = subprocess.run(command, capture_output=True)
        write_private(output / ('build-' + name + '.log'), result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(name + ' build failed; inspect the private build log')
        executable.chmod(0o700)
        version = subprocess.run([compiler, '--version'], capture_output=True,
                                 text=True, check=True).stdout.splitlines()[0]
        manifest['builds'][name] = {
            'compiler': version,
            'sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
        }
        if name == 'arm':
            contents = executable.read_bytes()
            segments = subprocess.run(['arm-linux-gnueabihf-readelf', '-l', str(executable)],
                                      capture_output=True, text=True, check=True).stdout
            if contents[:6] != b'\x7fELF\x01\x01' or 'INTERP' in segments:
                raise ValueError('ARM build is not a static little-endian ELF32 executable')
    write_private(output / 'manifest.json', (json.dumps(manifest, indent=2) + '\n').encode())
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='reviewed private dreem_live_streamer.c')
    parser.add_argument('output', type=Path, help='new private directory outside the repository')
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output), indent=2))
