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
    (folder / "busfreq-imx.c").write_text(code)
    with (folder / "Kconfig").open("a") as stream:
        stream.write('\nconfig DREEM_BUSFREQ\n\tbool "Experimental Dreem Femto bus-frequency policy"\n'
                     '\tdepends on SOC_IMX6ULL && CPU_FREQ\n\tdefault n\n\thelp\n'
                     '\t  Reconstructs the saved Femto firmware policy. Runtime activation\n'
                     '\t  also requires busfreq_imx.dreem_busfreq=1 and a Femto device tree.\n')
    with (folder / "Makefile").open("a") as stream:
        stream.write('\nifeq ($(CONFIG_DREEM_BUSFREQ),y)\nCFLAGS_busfreq-imx.o += -g\nendif\n')
    shutil.copyfile(Path(__file__).resolve().parent / "kernel/busfreq_dreem.inc",
                    folder / "busfreq_dreem.inc")
