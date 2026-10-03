# SPDX-License-Identifier: GPL-2.0-only
"""Select the source-matched WM8960 codec for the experimental Femto profile.

Uses the public Wolfson/NXP source reconstruction already verified against the
saved kernel. Preserves the codec's observed behavior, including known defects.
"""
import hashlib

from build_wm8960_reference import DRIVER, SOURCE_HASH, reconstruct


def apply(source):
    if (source / '.git').exists():
        raise ValueError('requires disposable non-Git source copy')
    driver = source / DRIVER
    if hashlib.sha256(driver.read_bytes()).hexdigest() != SOURCE_HASH:
        raise ValueError('unexpected NXP WM8960 codec source')
    original = driver.read_text()
    (driver.parent / 'wm8960-dreem.c').write_text(reconstruct(original))
    driver.write_text('#ifdef CONFIG_DREEM_WM8960\n#include "wm8960-dreem.c"\n#else\n' +
                      original + '\n#endif\n')
    with (driver.parent / 'Makefile').open('a') as stream:
        stream.write('\nifeq ($(CONFIG_DREEM_WM8960),y)\nCFLAGS_wm8960.o += -g\nendif\n')
