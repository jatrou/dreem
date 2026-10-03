#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Apply the research extension to a fresh, non-Git copy of pinned NXP source.

Replacement anchors retain small NXP driver excerpts, copyright 2010 Sascha
Hauer/Pengutronix and 2004-2016 Freescale Semiconductor, GPL-2.0-or-later.
Independent extension files retain their own GPL-2.0-only identifiers.
"""
import hashlib
from pathlib import Path
import shutil

INPUTS = {
    "imx-sdma.c": "f11b66a4c74a4ef27bb0f4c18984b04802b5ee93b7ed031d8f36ddc8164b2e43",
    "Kconfig": "717c4d0f8e4fd6e0d1cb71d07d5bb861b01f644c4a2df3d7cb5d6a799e84eeb1",
    "Makefile": "f044002f5faee0bc57ef27dd2506c02bc27527f658cc87f43106c83e8edf9ce0",
}
OPTION = "CONFIG_DREEM_EEG_SDMA"


def apply(source):
    if (source / ".git").exists():
        raise ValueError("overlay requires a disposable non-Git source copy")
    folder = source / "drivers/dma"
    for name, expected in INPUTS.items():
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != expected:
            raise ValueError("unexpected NXP input: " + name)
    code = (folder / "imx-sdma.c").read_text()

    def replace(old, new):
        nonlocal code
        if code.count(old) != 1:
            raise ValueError("overlay anchor is not unique: " + repr(old[:100]))
        code = code.replace(old, new)

    def guarded(body):
        return "#ifdef " + OPTION + "\n" + body + "#endif\n"

    replace("struct sdma_engine;\n", "struct sdma_engine;\n" + guarded('#include "sdma_eeg_linux.h"\n'))
    replace("struct sdma_engine {\n", "struct sdma_engine {\n" + guarded("\tstruct dreem_sdma eeg;\n"))
    replace("\tunsigned long timeout = 500;\n\n\tsdma_enable_channel(sdma, 0);",
            "\tunsigned long timeout = 500;\n" + guarded(
                "\tif (sdma->eeg.enabled) {\n"
                "\t\tif (READ_ONCE(sdma->eeg.command_failed))\n\t\t\treturn -EIO;\n"
                "\t\t/* A stale completion must not acknowledge a new command. */\n"
                "\t\twritel(BIT(0), sdma->regs + SDMA_H_INTR);\n\t\tdma_wmb();\n\t}\n") +
            "\n\tsdma_enable_channel(sdma, 0);")
    replace('\t\tdev_err(sdma->dev, "Timeout waiting for CH0 ready\\n");',
            '\t\tdev_err(sdma->dev, "Timeout waiting for CH0 ready\\n");\n' + guarded(
                "\t\tif (sdma->eeg.enabled)\n\t\t\tdreem_sdma_command_failed(sdma);\n"))
    replace("\tbuf_virt = gen_pool_dma_alloc(sdma->iram_pool, size, &buf_phys);",
            guarded("\tif (sdma->eeg.enabled && READ_ONCE(sdma->eeg.command_failed))\n\t\treturn -EIO;\n") +
            "\tbuf_virt = gen_pool_dma_alloc(sdma->iram_pool, size, &buf_phys);")
    replace("\tif (use_iram)\n\t\tgen_pool_free", guarded(
        "\t/* A timed-out command may still reference this allocation. */\n"
        "\tif (sdma->eeg.enabled && READ_ONCE(sdma->eeg.command_failed))\n\t\treturn ret;\n") +
        "\tif (use_iram)\n\t\tgen_pool_free")
    replace("\t\tstruct sdma_desc *desc;\n\n\t\tspin_lock(&sdmac->vc.lock);",
            "\t\tstruct sdma_desc *desc;\n" + guarded(
                "\t\tif (sdma->eeg.enabled && channel == 1) {\n"
                "\t\t\tdreem_sdma_irq(sdma);\n\t\t\t__clear_bit(channel, &stat);\n\t\t\tcontinue;\n\t\t}\n") +
            "\n\t\tspin_lock(&sdmac->vc.lock);")
    replace("static void sdma_load_firmware(const struct firmware *fw, void *context)",
            guarded('#include "sdma_eeg_linux.inc"\n') +
            "\nstatic void sdma_load_firmware(const struct firmware *fw, void *context)")
    replace("\tunsigned short *ram_code;\n\n\tif (!fw)", "\tunsigned short *ram_code;\n" + guarded(
        "\tif (sdma->eeg.enabled) {\n\t\tdreem_sdma_load_firmware(fw, sdma);\n\t\treturn;\n\t}\n") + "\n\tif (!fw)")
    replace("\tret = request_firmware_nowait(THIS_MODULE,", guarded(
        "\tif (sdma->eeg.enabled) {\n\t\tconst struct firmware *fw = NULL;\n"
        "\t\tret = fw_name ? request_firmware_direct(&fw, fw_name, sdma->dev) : -ENOENT;\n"
        "\t\tif (ret && ret != -ENOENT) {\n\t\t\tsdma->eeg.error = ret;\n"
        "\t\t\tsdma->eeg.firmware_done = true;\n\t\t\treturn ret;\n\t\t}\n"
        "\t\tsdma_load_firmware(fw, sdma);\n\t\treturn 0;\n\t}\n") +
        "\tret = request_firmware_nowait(THIS_MODULE,")
    replace("\tconst char *fw_name;\n\tint ret;", "\tconst char *fw_name = NULL;\n\tint ret;")
    replace("\tsdma->drvdata = drvdata;\n\n\tirq", "\tsdma->drvdata = drvdata;\n" + guarded(
        "\tmutex_init(&sdma->eeg.lock);\n\tspin_lock_init(&sdma->eeg.progress_lock);\n"
        "\tif (dreem_eeg_enabled) {\n\t\tif (!of_machine_is_compatible(\"fsl,imx6ull-femto\"))\n"
        "\t\t\treturn -ENODEV;\n\t\tsdma->eeg.enabled = true;\n\t}\n") + "\n\tirq")
    replace("\tiores = platform_get_resource(pdev, IORESOURCE_MEM, 0);\n\tsdma->regs",
        "\tiores = platform_get_resource(pdev, IORESOURCE_MEM, 0);\n" + guarded(
        "\tif (sdma->eeg.enabled && (!iores || iores->start != 0x020ec000))\n\t\treturn -ENODEV;\n"
        "\tsdma->eeg.irq = irq;\n") + "\tsdma->regs")
    replace("\t\tif (i)\n\t\t\tvchan_init", "\t\tif (i\n" + guarded(
        "\t\t    && !(sdma->eeg.enabled && i == 1)\n") + "\t\t   )\n\t\t\tvchan_init")
    replace("\t\tret = sdma_get_firmware(sdma, pdata->fw_name);",
            "\t\tfw_name = pdata->fw_name;\n\t\tret = sdma_get_firmware(sdma, fw_name);")
    replace("\tsdma->fw_name = fw_name;", guarded(
        "\tif (sdma->eeg.enabled && !sdma->eeg.firmware_done)\n\t\tsdma_get_firmware(sdma, NULL);\n") +
        "\tsdma->fw_name = fw_name;")
    replace('\tplatform_set_drvdata(pdev, sdma);\n\tdev_info(sdma->dev, "initialized\\n");', guarded(
        "\tret = dreem_sdma_register(sdma);\n\tif (ret) {\n\t\tif (np)\n\t\t\tof_dma_controller_free(np);\n"
        "\t\tgoto err_register;\n\t}\n") +
        '\tplatform_set_drvdata(pdev, sdma);\n\tdev_info(sdma->dev, "initialized\\n");')
    replace("\tdma_async_device_unregister(&sdma->dma_device);\n\tkfree(sdma->script_addrs);",
        guarded("\tdreem_sdma_remove(sdma);\n") +
        "\tdma_async_device_unregister(&sdma->dma_device);\n\tkfree(sdma->script_addrs);")
    replace("\t\ttasklet_kill(&sdmac->vc.task);", "\t\tif (!i)\n\t\t\tcontinue;\n" + guarded(
        "\t\tif (sdma->eeg.enabled && i == 1)\n\t\t\tcontinue;\n") + "\t\ttasklet_kill(&sdmac->vc.task);")
    replace("\tsdma->suspend_off = false;", guarded(
        "\t/* Research provider retains DMA state and cannot yet restore it. */\n"
        "\tif (sdma->eeg.enabled)\n\t\treturn -EBUSY;\n") + "\tsdma->suspend_off = false;")
    replace('\t\t.name\t= "imx-sdma",', '\t\t.name\t= "imx-sdma",\n' + guarded(
        "\t\t.suppress_bind_attrs = true,\n"))
    (folder / "imx-sdma.c").write_text(code)
    with (folder / "Kconfig").open("a") as stream:
        stream.write('\nconfig DREEM_EEG_SDMA\n\tbool "Experimental Dreem EEG SDMA provider"\n'
                     '\tdepends on IMX_SDMA=y && !OF_DYNAMIC\n\tdefault n\n\thelp\n'
                     '\t  Research-only channel-1 provider for a static Femto board.\n'
                     '\t  Runtime activation is disabled by default and prevents sleep.\n')
    with (folder / "Makefile").open("a") as stream:
        stream.write('\nifeq ($(CONFIG_DREEM_EEG_SDMA),y)\n'
                     'CFLAGS_imx-sdma.o += -std=gnu99 -Wno-declaration-after-statement -msoft-float -fno-tree-vectorize -g\nendif\n')
    here = Path(__file__).resolve().parent
    files = [here / "sdma_eeg.c", here / "sdma_eeg.h",
             here / "kernel/sdma_eeg_linux.h", here / "kernel/sdma_eeg_linux.inc"]
    for path in files:
        shutil.copyfile(path, folder / path.name)
