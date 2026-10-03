#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build a private, self-checking ARM fixture trial directory and archive.

No original firmware, recordings, identifiers or credentials are bundled.
The output directory and its .tar.gz sibling must not already exist.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile

SOURCE = Path(__file__).resolve().parent
TOOLS = ('eeg_quality', 'motion_quality', 'algo_health', 'session_motion', 'trial_exec')
CASES = (('eeg_quality', 'eeg_quality', 'normal/eeg.data', 0),
         ('motion_quality', 'motion_quality', 'normal/accelerometer.data', 0),
         ('algo_health', 'algo_health', 'normal/algo.data', 0),
         ('session_motion', 'session_motion', 'normal', 0),
         ('recovered_session', 'session_motion', 'recovered', 1))
SOURCE_NAMES = ('eeg_quality.c', 'motion_quality.c', 'algo_health.c', 'algo_events.c', 'algo_events.h',
                'session_motion.c', 'trial_exec.c', 'build.sh', 'build_feature_trial.py', 'run_feature_trial.sh')
CORE_SHA256 = 'dfc83b247b505aa08ea62060f999192112a47b606754d5f469357b7addf0295a'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def event(counter, code, value):
    return struct.pack('<IBI', counter, code, value)


def build(output):
    output = output.absolute()
    archive = output.with_name(output.name+'.tar.gz')
    if output.exists() or output.is_symlink() or archive.exists() or archive.is_symlink():
        raise ValueError('trial output or archive already exists; choose a fresh destination')
    output.mkdir(mode=0o700)
    source_hashes = {name:sha(SOURCE/name) for name in SOURCE_NAMES}
    build_env = dict(os.environ, HOST_CC='cc', ARM_CC='arm-linux-gnueabihf-gcc')
    with (output/'build.log').open('wb') as log:
        subprocess.run(['sh', str(SOURCE/'build.sh')], env=build_env,
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    fixtures = output/'fixtures'
    normal, recovered = fixtures/'normal', fixtures/'recovered'
    normal.mkdir(parents=True); recovered.mkdir()
    (normal/'eeg.data').write_bytes(struct.pack('<4f', 1, -2, 3, 0)*500)
    (normal/'accelerometer.data').write_bytes(struct.pack('<3f', 0, 0, 1)*50+struct.pack('<3f', 1, 0, 1)*50)
    (normal/'algo.data').write_bytes(b''.join(event(*e) for e in
        ((0, 16, 1000), (0, 31, 1), (0, 30, 1), (125, 30, 0), (250, 30, 1), (375, 30, 7), (500, 17, 1002))))
    header = bytearray(142)
    struct.pack_into('<II', header, 110, 1000, 1002)
    struct.pack_into('<I', header, 134, 500)
    (normal/'meta.data').write_bytes(header+b'\0'*253)
    for source in normal.iterdir():
        shutil.copyfile(source, recovered/source.name)
    struct.pack_into('<H', header, 118, 1)
    (recovered/'meta.data').write_bytes(header+b'\0'*253)
    expected = output/'expected'; expected.mkdir()
    for name, tool, path, status in CASES:
        p = subprocess.run([str(SOURCE/'build'/(tool+'.host')), str(fixtures/path)],
                           capture_output=True, timeout=20)
        if p.returncode != status:
            raise ValueError('unexpected host reference status for '+name)
        (expected/(name+'.stdout')).write_bytes(p.stdout)
        (expected/(name+'.stderr')).write_bytes(p.stderr)
    for tool in TOOLS:
        binary = SOURCE/'build'/(tool+'.arm')
        elf = binary.read_bytes()[:20]
        if elf[:6] != b'\x7fELF\x01\x01' or struct.unpack_from('<H', elf, 18)[0] != 40:
            raise ValueError('not a little-endian ARM32 executable: '+tool)
        headers = subprocess.check_output(['arm-linux-gnueabihf-readelf', '-l', str(binary)], text=True)
        if 'INTERP' in headers:
            raise ValueError('dynamic ARM binary: '+tool)
        shutil.copyfile(binary, output/binary.name)
        (output/binary.name).chmod(0o700)
    shutil.copyfile(SOURCE/'run_feature_trial.sh', output/'run_feature_trial.sh')
    (output/'run_feature_trial.sh').chmod(0o700)
    (output/'CORE_SHA256').write_text(CORE_SHA256+'\n')
    if source_hashes != {name:sha(SOURCE/name) for name in SOURCE_NAMES}:
        raise ValueError('sources changed during trial build')
    manifest = {'schema': 1, 'purpose': 'synthetic feature execution and resource trial',
                'base_git_commit': subprocess.check_output(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], text=True).strip(),
                'source_sha256': source_hashes,
                'arm_sha256': {tool:sha(output/(tool+'.arm')) for tool in TOOLS},
                'reference_host_sha256': {tool:sha(SOURCE/'build'/(tool+'.host')) for tool in TOOLS[:-1]},
                'core_sha256': CORE_SHA256,
                'host_compiler': subprocess.check_output(['cc', '--version'], text=True).splitlines()[0],
                'arm_compiler': subprocess.check_output(['arm-linux-gnueabihf-gcc', '--version'], text=True).splitlines()[0],
                'cases': [{'name':name, 'tool':tool, 'input':'fixtures/'+path, 'exit':status}
                          for name,tool,path,status in CASES],
                'contains_personal_recordings': False, 'device_trial_completed': False,
                'native_recording_fidelity_tested': False}
    (output/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    files = sorted(p for p in output.rglob('*') if p.is_file())
    (output/'SHA256SUMS').write_text(''.join(f'{sha(p)}  {p.relative_to(output).as_posix()}\n' for p in files))
    # Private reproducible archive: fixed timestamps/ownership, no links or device entries.
    with archive.open('xb') as raw:
        os.fchmod(raw.fileno(), 0o600)
        with gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as tar:
                for path in sorted(output.rglob('*')):
                    if not path.is_file():
                        continue
                    info = tarfile.TarInfo(output.name+'/'+path.relative_to(output).as_posix())
                    info.size = path.stat().st_size
                    info.mode = 0o700 if path.name.endswith('.arm') or path.name == 'run_feature_trial.sh' else 0o600
                    with path.open('rb') as data:
                        tar.addfile(info, data)
    return {'directory': str(output), 'archive': str(archive), 'archive_sha256': sha(archive),
            'manifest_sha256': sha(output/'manifest.json'), 'device_trial_completed': False}


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    print(json.dumps(build(parser.parse_args().output), indent=2))
