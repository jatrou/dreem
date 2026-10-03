#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build the read-only process inventory without firmware or vendor objects."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def build(output, compiler, target, sanitize=False):
    if sanitize and target != 'host':
        raise ValueError('sanitizers require the host target')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    output = output.resolve()
    source = Path(__file__).with_name('startup_inventory.c')
    binary = output / 'dreem-startup-inventory'
    flags = ['-std=c11', '-Os', '-Wall', '-Wextra', '-Werror']
    if target == 'arm':
        flags += ['-marm', '-mcpu=cortex-a7', '-mfpu=neon-vfpv4', '-mfloat-abi=hard', '-static']
    if sanitize:
        flags += ['-g', '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
    command = [compiler, *flags, str(source), '-o', str(binary)]
    subprocess.run(command, check=True, capture_output=True, timeout=60)
    binary.chmod(0o700)
    report = {'target': target, 'sanitizers': sanitize, 'command': command,
              'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
              'vendor_objects_linked': False, 'fixture_support': False,
              'native_headset_qualification': False}
    path = output / 'build.json'
    path.write_text(json.dumps(report, indent=2) + '\n')
    path.chmod(0o600)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--compiler', required=True)
    parser.add_argument('--target', choices=('host', 'arm'), required=True)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    report = build(args.output, args.compiler, args.target, args.sanitize)
    print(json.dumps({key: report[key] for key in ('target', 'binary_sha256', 'fixture_support')}))


if __name__ == '__main__':
    main()
