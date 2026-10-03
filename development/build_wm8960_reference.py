#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build a source-match candidate for the saved Dreem WM8960 codec.

The edits use small anchors from wm8960.c, copyright 2007-11 Wolfson
Microelectronics, GPL version 2. This is an isolated, unqualified reference
object, not an overlay enabled in the research kernel or an installable image.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

REVISION = '30278abfe0977b1d2f065271ce1ea23c0e2d1b6e'
DRIVER = 'sound/soc/codecs/wm8960.c'
SOURCE_HASH = '0afaddaf3632e87452438fd4a1e5f6dd1463de99e61d1f8b253f58a536d846ef'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reconstruct(code):
    """Preserve the observed constant and delay removal without modernizing it."""
    edits = [
        ('\t\tsysclk = freq_out / sysclk_divs[i];\n',
         '\t\tsysclk = freq_out / sysclk_divs[i];\n'
         '\t\t/* Observed signed limit in the reviewed Femto codec. */\n'
         '\t\tif (sysclk > 16000015)\n\t\t\tcontinue;\n'),
        ('\tmsleep(250);\n',
         '\t/* The saved Femto codec omits the upstream PLL settling delay. */\n'),
    ]
    for old, new in edits:
        if code.count(old) != 1:
            raise ValueError('nonunique WM8960 source anchor')
        code = code.replace(old, new)
    for channel, register, replacement in [('Right', 1, 'Left'), ('Left', 2, 'Right')]:
        for number, shift in [(3, 4), (2, 1)]:
            old = (f'SOC_SINGLE_TLV("{channel} Input Boost Mixer {channel[0]}INPUT{number} Volume",\n'
                   f'\t       WM8960_INBMIX{register}, {shift}, 7, 0, lineinboost_tlv),')
            new = old.replace(f'{channel} Input Boost Mixer {channel[0]}INPUT',
                              f'{replacement} Input Boost Mixer {replacement[0]}INPUT')
            if code.count(old) != 1:
                raise ValueError('nonunique input-volume label anchor')
            code = code.replace(old, new)
    for side, pin in [('Left', 'LINPUT1'), ('Right', 'RINPUT1')]:
        old = f'{{ "{side} Input Mixer", NULL, "{pin}", }}'
        new = f'{{ "{side} Input Mixer", "Boost Switch", "{pin}", }}'
        if code.count(old) != 1:
            raise ValueError('nonunique input-route anchor')
        code = code.replace(old, new)
    return code


def build(source, config, output, compiler, jobs):
    if output.exists() or output.is_symlink():
        raise ValueError('output must be a new private directory')
    source, config, output = (p.resolve() for p in (source, config, output))
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain'], text=True)
    if revision != REVISION or dirty or sha(source / DRIVER) != SOURCE_HASH:
        raise ValueError('requires clean pinned NXP source')
    original_config = config.read_bytes()
    for option in (b'CONFIG_ARM=y', b'CONFIG_SND_SOC_WM8960=y'):
        if option not in original_config.splitlines():
            raise ValueError('missing required option: ' + option.decode())
    output.mkdir(mode=0o700)
    tree, kernel = output / 'source', output / 'kernel'
    tree.mkdir(mode=0o700)
    kernel.mkdir(mode=0o700)
    archive = subprocess.Popen(['git', '-C', str(source), 'archive', REVISION], stdout=subprocess.PIPE)
    try:
        subprocess.run(['tar', '-xf', '-', '-C', str(tree)], stdin=archive.stdout, check=True)
    finally:
        archive.stdout.close()
    if archive.wait():
        raise ValueError('NXP snapshot failed')
    (tree / DRIVER).write_text(reconstruct((tree / DRIVER).read_text()))
    patched_hash = sha(tree / DRIVER)
    (kernel / '.config').write_bytes(original_config)
    make = ['make', '-C', str(tree), 'O=' + str(kernel), 'ARCH=arm',
            'CROSS_COMPILE=' + compiler, 'HOSTCFLAGS=-O2 -fcommon',
            'KBUILD_BUILD_USER=builder', 'KBUILD_BUILD_HOST=dreem-research']
    with (output / 'build.log').open('w') as log:
        subprocess.run([compiler + 'gcc', '--version'], stdout=log, check=True)
        subprocess.run(make + ['olddefconfig', 'modules_prepare'], stdout=log,
                       stderr=subprocess.STDOUT, check=True)
        subprocess.run(make + ['-j' + str(jobs), 'sound/soc/codecs/wm8960.o'],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    if sha(tree / DRIVER) != patched_hash:
        raise ValueError('reference source changed during build')
    result = {'nxp_revision': revision, 'original_source_sha256': SOURCE_HASH,
              'reference_source_sha256': patched_hash,
              'input_config_sha256': hashlib.sha256(original_config).hexdigest(),
              'build_config_sha256': sha(kernel / '.config'),
              'object_sha256': sha(kernel / 'sound/soc/codecs/wm8960.o'),
              'builder_sha256': sha(Path(__file__)),
              'signed_sysclk_limit': 16000015, 'upstream_pll_delay_removed_ms': 250,
              'input_volume_labels_corrected': 4,
              'input_routes_with_explicit_boost_control': 2,
              'integrated_into_research_kernel': False, 'installed': False,
              'hardware_qualified': False}
    (output / 'reference-build.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('nxp_source', type=Path)
    parser.add_argument('kernel_config', type=Path)
    parser.add_argument('new_output', type=Path)
    parser.add_argument('compiler_prefix')
    parser.add_argument('--jobs', type=int, choices=range(1, 65), default=8)
    args = parser.parse_args()
    os.umask(0o077)
    print(json.dumps(build(args.nxp_source, args.kernel_config, args.new_output,
                           args.compiler_prefix, args.jobs), indent=2))


if __name__ == '__main__':
    main()
