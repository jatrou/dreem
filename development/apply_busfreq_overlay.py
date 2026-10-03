#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Add the reconstructed Femto policy to an isolated pinned NXP source tree.

Anchors derive from NXP busfreq-imx.c, copyright 2011-2016 Freescale and
2017 NXP, GPL-2.0-or-later. The independent policy is GPL-2.0-only.
"""
import hashlib
from pathlib import Path
import shutil

INPUTS = {
    "busfreq-imx.c": "4e80c4d1537b08af085e722f460d3de5bb076c49464d5eeffbd77bcce40b42c9",
    "busfreq_ddr3.c": "355283df2a7f2922824634ac0b0b4d0b0fd1e9b2193c52e44aba486292897414",
    "Kconfig": "0f0b7608afe67c198029d9daf62fa76cdd99deadc428ace79d0cbbcadbe228aa",
    "Makefile": "002cd622c425a96acd517ef774400c1edb5da0615f279dda209a12e044a0b3a7",
}


def apply(source):
    if (source / ".git").exists():
        raise ValueError("overlay requires a disposable non-Git source copy")
    folder = source / "arch/arm/mach-imx"
    for name, digest in INPUTS.items():
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != digest:
            raise ValueError("unexpected NXP input: " + name)
    code = (folder / "busfreq-imx.c").read_text()

    def replace(old, new):
        nonlocal code
        if code.count(old) != 1:
            raise ValueError("nonunique busfreq anchor: " + repr(old))
        code = code.replace(old, new)

    def guarded(body):
        return "#ifdef CONFIG_DREEM_BUSFREQ\n" + body + "#endif\n"

    anchor = "static void exit_lpm_imx6_up(void)\n{\n"
    replace(anchor, guarded('#include "busfreq_dreem.inc"\n') + "\n" + anchor + guarded(
        "\tif (dreem_busfreq_active()) {\n\t\tdreem_exit_lpm_imx6_up();\n\t\treturn;\n\t}\n"))
    for operation in ("request", "release"):
        anchor = "void " + operation + "_bus_freq(enum bus_freq_mode mode)\n{\n"
        replace(anchor, anchor + guarded(
            "\tif (dreem_busfreq_active()) {\n\t\tdreem_busfreq_" + operation +
            "(mode);\n\t\treturn;\n\t}\n"))
    anchor = "int set_low_bus_freq(void)\n{\n"
    replace(anchor, anchor + guarded("\tif (dreem_busfreq_active())\n\t\treturn 0;\n"))

    anchor = "static int busfreq_probe(struct platform_device *pdev)\n{\n\tu32 err;\n"
    replace(anchor, guarded('#include "busfreq_probe_dreem.inc"\n') + '\n' + anchor + guarded(
        '\tif (dreem_busfreq_active() && bus_freq_scaling_initialized)\n\t\treturn -EBUSY;\n'))
    # Keep the public path unchanged unless the experimental Femto gate is on.
    # Its sysfs, notifiers, work and readiness are published only after DDR setup.
    for first, last in [
        ('\terr = sysfs_create_file(&busfreq_dev->kobj, &dev_attr_enable.attr);',
         '\t\treturn err;\n\t}\n'),
        ('\tbus_freq_scaling_is_active = 1;\n\tbus_freq_scaling_initialized = 1;',
         '\t\tmsecs_to_jiffies(10000));\n')]:
        begin = code.index(first, code.index('static int busfreq_probe('))
        end = code.index(last, begin) + len(last)
        body = code[begin:end]
        common = '\tddr_low_rate = LPAPM_CLK;\n' if 'ddr_low_rate = LPAPM_CLK;' in body else ''
        gated_body = body.replace('\tddr_low_rate = LPAPM_CLK;\n', '')
        replace(body, common + guarded('\tif (!dreem_busfreq_active())\n') + '\t{\n' + gated_body + '\t}\n')
    anchor = '\tif (err) {\n\t\tdev_err(busfreq_dev, "Busfreq init of ddr controller failed\\n");\n\t\treturn err;\n\t}\n\treturn 0;\n'
    replace(anchor, '\tif (err) {\n' + guarded(
        '\t\tif (dreem_busfreq_active())\n\t\t\tdreem_ddr_discard_settings();\n') +
        '\t\tdev_err(busfreq_dev, "Busfreq init of ddr controller failed\\n");\n\t\treturn err;\n\t}\n' +
        guarded('\tif (dreem_busfreq_active())\n\t\treturn dreem_busfreq_finish_probe();\n') + '\treturn 0;\n')
    (folder / "busfreq-imx.c").write_text(code)

    code = (folder / "busfreq_ddr3.c").read_text()
    anchor = 'int init_mmdc_ddr3_settings_imx6_up(struct platform_device *busfreq_pdev)\n'
    replace(anchor, guarded('#include "ddr_prepare_dreem.inc"\n') + '\n' + anchor)
    anchor = 'int init_mmdc_ddr3_settings_imx6_up(struct platform_device *busfreq_pdev)\n{\n\tint i;\n\tstruct device_node *node;\n\tunsigned long ddr_code_size;\n'
    replace(anchor, anchor + '\n' + guarded(
        '\tif (dreem_busfreq_active())\n\t\treturn dreem_prepare_ddr3_settings();\n'))
    (folder / "busfreq_ddr3.c").write_text(code)
    with (folder / "Kconfig").open("a") as stream:
        stream.write('\nconfig DREEM_BUSFREQ\n\tbool "Experimental Dreem Femto bus-frequency policy"\n'
                     '\tdepends on SOC_IMX6ULL && CPU_FREQ\n\tdefault n\n\thelp\n'
                     '\t  Reconstructs the saved Femto firmware policy. Runtime activation\n'
                     '\t  also requires busfreq_imx.dreem_busfreq=1 and a Femto device tree.\n')
    with (folder / "Makefile").open("a") as stream:
        stream.write('\nifeq ($(CONFIG_DREEM_BUSFREQ),y)\nCFLAGS_busfreq-imx.o += -g\n'
                     'CFLAGS_busfreq_ddr3.o += -g\nendif\n')
    for name in ("busfreq_dreem.inc", "ddr_linux.inc", "ddr_prepare_dreem.inc", "busfreq_probe_dreem.inc"):
        shutil.copyfile(Path(__file__).resolve().parent / "kernel" / name, folder / name)
