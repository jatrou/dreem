# SPDX-License-Identifier: GPL-2.0-or-later
"""Build the matched board with repaired jack lifetime in a disposable tree.

Uses anchors from NXP imx-wm8960.c, copyright 2015-2016 Freescale.
The codec source remains separate; this board is not hardware-qualified.
"""
import hashlib
from pathlib import Path
import shutil

import wm8960_board_reference as reference


def reconstruct(code):
    code = reference.reconstruct(code)

    def replace(old, new):
        nonlocal code
        if code.count(old) != 1:
            raise ValueError('nonunique research board anchor: ' + repr(old))
        code = code.replace(old, new)

    replace('#include <linux/cdev.h>\n', '#include <linux/miscdevice.h>\n'
            '#include <linux/dreem_hardware.h>\n')
    replace(reference.STATE, '')
    replace('\tu32 asrc_format;\n', '\tu32 asrc_format;\n'
            '\tstruct snd_soc_dai_link links[3];\n'
            '\tstruct device_node *cpu_node, *codec_node, *asrc_node;\n'
            '\tstruct platform_device *cpu_device, *asrc_device;\n'
            '\tstruct i2c_client *codec_device;\n'
            '\tu32 gpr_register, gpr_mask, gpr_before;\n'
            '\tbool gpr_changed, card_registered, jack_registered;\n'
            '\tbool hp_gpio_added, mic_gpio_added, hp_attribute, mic_attribute;\n')
    start = code.index('static long jack_ioctl(')
    end = code.index('static int imx_wm8960_jack_init(', start)
    code = code[:start] + '#include "wm8960_jack.inc"\n\n' + code[end:]
    replace('struct snd_soc_codec *codec = codec_dai->codec;\n',
            'struct snd_soc_codec *codec = codec_dai->codec;\n\tint ret;\n')
    replace('\tsnd_soc_update_bits(codec, WM8960_IFACE2, 1<<6, 1<<6);\n\n\n\treturn 0;',
            '\tret = snd_soc_update_bits(codec, WM8960_IFACE2, 1<<6, 1<<6);\n'
            '\treturn ret < 0 ? ret : 0;')
    start = code.index('static int imx_wm8960_probe(')
    end = code.index('static const struct of_device_id imx_wm8960_dt_ids[]', start)
    return code[:start] + '#include "wm8960_lifetime.inc"\n\n' + code[end:]


def apply(source):
    if (source / '.git').exists():
        raise ValueError('requires disposable non-Git source copy')
    driver = source / reference.DRIVER
    original = driver.read_text()
    if hashlib.sha256(driver.read_bytes()).hexdigest() != reference.SOURCE_HASH:
        raise ValueError('unexpected NXP board source')
    (driver.parent / 'imx-wm8960-dreem.c').write_text(reconstruct(original))
    driver.write_text('#ifdef CONFIG_DREEM_WM8960\n#include "imx-wm8960-dreem.c"\n#else\n' +
                      original + '\n#endif\n')
    for name in ('wm8960_jack.inc', 'wm8960_lifetime.inc'):
        shutil.copyfile(Path(__file__).resolve().parent / 'kernel' / name, driver.parent / name)
    with (driver.parent / 'Kconfig').open('a') as stream:
        stream.write('\nconfig DREEM_WM8960\n\tbool "Experimental Dreem WM8960 board driver"\n'
                     '\tdepends on SND_SOC_IMX_WM8960=y && DREEM_HW_VERSION\n\tdefault n\n'
                     '\thelp\n\t  Uses the reconstructed Femto board interface and checked jack lifetime.\n'
                     '\t  Requires the Femto root compatible and nonzero hardware identity.\n'
                     '\t  Experimental: the complete audio path is not yet qualified.\n')
    with (driver.parent / 'Makefile').open('a') as stream:
        stream.write('\nifeq ($(CONFIG_DREEM_WM8960),y)\nCFLAGS_imx-wm8960.o += -g\nendif\n')
