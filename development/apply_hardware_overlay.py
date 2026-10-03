#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Add checked, read-only board identity access to a disposable NXP tree.

Small anchors come from fsl_otp.c, copyright 2010-2016 Freescale
Semiconductor, GPL version 2. No firmware, fuse writes or live access.
"""
import hashlib
from pathlib import Path
import shutil

INPUTS = {
    'fsl_otp.c': 'b5a26a8c5506bf890663ab674ca69a50299cac15de0d6a28a2c7cb0e9ce416c0',
    'Kconfig': 'e12d038ae070ba51522f86aa893dd5c2b4c710b8da5fae72eb22b0dc2496f071',
    'Makefile': '1617c3f8cbc0bd1d46b749aea22aa40c34c098b0eea1fc6c05a94edcb0edf1c5',
}


def apply(source):
    if (source / '.git').exists():
        raise ValueError('requires disposable non-Git source copy')
    folder = source / 'drivers/char'
    for name, digest in INPUTS.items():
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != digest:
            raise ValueError('unexpected OTP input: ' + name)
    code = (folder / 'fsl_otp.c').read_text()

    def replace(old, new):
        nonlocal code
        if code.count(old) != 1:
            raise ValueError('nonunique OTP anchor: ' + repr(old))
        code = code.replace(old, new)

    for operation in ('probe', 'remove'):
        name = 'fsl_otp_' + operation
        old = 'static int ' + name + '(struct platform_device *pdev)\n'
        replace(old, '#ifdef CONFIG_DREEM_HW_VERSION\nstatic noinline int ' + name +
                '_original(struct platform_device *pdev)\n#else\n' + old + '#endif\n')
    # DEFINE_MUTEX already initializes this lock. Reinitializing it at the end
    # of probe would race a reader checking whether the provider is ready.
    replace('\tmutex_init(&otp_mutex);\n',
            '#ifndef CONFIG_DREEM_HW_VERSION\n\tmutex_init(&otp_mutex);\n#endif\n')
    replace('static struct platform_driver fsl_otp_driver = {\n',
            '#ifdef CONFIG_DREEM_HW_VERSION\n#include "dreem_hardware.inc"\n#endif\n\n'
            'static struct platform_driver fsl_otp_driver = {\n')
    (folder / 'fsl_otp.c').write_text(code)
    with (folder / 'Kconfig').open('a') as stream:
        stream.write('\nconfig DREEM_HW_VERSION\n\tbool "Experimental Dreem board identity reader"\n'
                     '\tdepends on FSL_OTP=y && SOC_IMX6ULL\n\tdefault n\n\thelp\n'
                     '\t  Adds a checked internal read API for the Femto hardware-version\n'
                     '\t  shadow register. It never programs fuses or reloads shadows.\n')
    with (folder / 'Makefile').open('a') as stream:
        stream.write('\nifeq ($(CONFIG_DREEM_HW_VERSION),y)\nCFLAGS_fsl_otp.o += -g\nendif\n')
    here = Path(__file__).resolve().parent / 'kernel'
    shutil.copyfile(here / 'dreem_hardware.inc', folder / 'dreem_hardware.inc')
    shutil.copyfile(here / 'dreem_hardware.h', source / 'include/linux/dreem_hardware.h')
