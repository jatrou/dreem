#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build the research ADC adapter and check its imports against stock firmware.

Creates a new private output directory. Does not install, load, bind, unbind,
download firmware, or change the source checkout or existing baseline build.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from elftools.elf.elffile import ELFFile
from recover_exports import recover, module_versions

REVISION = "30278abfe0977b1d2f065271ce1ea23c0e2d1b6e"
RAW_HASH = "e15f659afdffab3fde7c997883475e4c95b5d3cdc1fbcd23c76365ee4cd52dcb"
SHARED = ("ads_data_sem", "sdma_ads_user_buffer", "sdma_queue_head")
VERMAGIC = "4.1.15 preempt mod_unload modversions ARMv7 p2v8 "


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(source, baseline, stock, output, compiler):
    if output.exists() or output.is_symlink():
        raise ValueError("output must be a new directory")
    source, baseline, stock, output = (p.resolve() for p in (source, baseline, stock, output))
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True)
    if revision != REVISION or dirty:
        raise ValueError("requires the unchanged pinned NXP source checkout")
    if not (baseline / "vmlinux").is_file() or not (baseline / "Module.symvers").is_file():
        raise ValueError("requires a completed baseline kernel build")
    with stock.open("rb") as stream:
        elf = ELFFile(stream)
        section = elf.get_section_by_name(".kernel")
        if section is None or hashlib.sha256(section.data()).hexdigest() != RAW_HASH:
            raise ValueError("stock kernel content does not match the reviewed image")
        stock_exports = recover(elf)
    config = (baseline / ".config").read_text()
    for line in ("CONFIG_ARM=y", "CONFIG_MODULES=y", "CONFIG_MODVERSIONS=y", "CONFIG_PREEMPT=y"):
        if line not in config.splitlines():
            raise ValueError(f"baseline config lacks {line}")
    config = config.replace('CONFIG_LOCALVERSION_AUTO=y', '# CONFIG_LOCALVERSION_AUTO is not set')
    if 'CONFIG_LOCALVERSION=""' not in config.splitlines():
        raise ValueError("baseline requires an empty CONFIG_LOCALVERSION")
    output.mkdir(mode=0o700)
    kernel, module, witness = (output / name for name in ("kernel", "module", "abi-witness"))
    for folder in (kernel, module, witness):
        folder.mkdir(mode=0o700)
    (kernel / ".config").write_text(config)
    make = ["make", "-C", str(source), "O=" + str(kernel), "ARCH=arm",
            "CROSS_COMPILE=" + compiler, "LOCALVERSION=", "HOSTCFLAGS=-O2 -fcommon",
            "KCFLAGS=-g",
            "KBUILD_BUILD_USER=builder", "KBUILD_BUILD_HOST=dreem-research"]
    subprocess.run(make + ["olddefconfig", "modules_prepare"], check=True)
    shutil.copyfile(baseline / "Module.symvers", kernel / "Module.symvers")

    # Compute the three shared-object CRCs from actual kernel headers and our
    # declarations. This witness is never a functional provider or a load target.
    (witness / "Makefile").write_text("obj-m += abi_witness.o\n")
    (witness / "abi_witness.c").write_text(
        '#include <linux/module.h>\n#include <linux/semaphore.h>\n'
        'struct semaphore ads_data_sem;\nu8 *sdma_ads_user_buffer;\nint sdma_queue_head;\n'
        'EXPORT_SYMBOL(ads_data_sem);\nEXPORT_SYMBOL(sdma_ads_user_buffer);\n'
        'EXPORT_SYMBOL(sdma_queue_head);\nMODULE_LICENSE("GPL v2");\n')
    subprocess.run(make + ["M=" + str(witness), "modules"], check=True)
    witness_exports = {}
    for line in (witness / "Module.symvers").read_text().splitlines():
        fields = line.split()
        witness_exports[fields[1]] = int(fields[0], 16)
    for name in SHARED:
        if witness_exports.get(name) != stock_exports[name][0]:
            raise ValueError(f"shared declaration CRC does not match stock: {name}")
    shutil.rmtree(witness)  # Never leave a dummy provider .ko beside the adapter.

    extra = output / "stock-eeg.symvers"
    extra.write_text("".join(f"0x{stock_exports[name][0]:08x}\t{name}\tvmlinux\t{stock_exports[name][1]}\n"
                              for name in SHARED))
    here = Path(__file__).resolve().parent
    files = {"ads129x_init.c": here / "ads129x_init.c",
             "ads129x_init.h": here / "ads129x_init.h",
             "adc_linux.c": here / "kernel" / "adc_linux.c",
             "Makefile": here / "kernel" / "Makefile"}
    for name, path in files.items():
        shutil.copyfile(path, module / name)
    subprocess.run(make + ["M=" + str(module), "KBUILD_EXTRA_SYMBOLS=" + str(extra), "modules"], check=True)
    artifact = module / "dreem_eeg_research.ko"
    with artifact.open("rb") as stream:
        elf = ELFFile(stream)
        if elf.elfclass != 32 or not elf.little_endian or elf["e_machine"] != "EM_ARM":
            raise ValueError("unexpected module architecture")
        versions = module_versions(elf.get_section_by_name("__versions").data())
        mismatches = [name for name, crc in versions.items()
                      if name not in stock_exports or stock_exports[name][0] != crc]
        if mismatches:
            raise ValueError("module imports disagree with stock: " + ", ".join(mismatches))
        if not set(SHARED) <= set(versions):
            raise ValueError("adapter is missing expected SDMA dependencies")
        metadata = elf.get_section_by_name(".modinfo").data().split(b"\0")
        vermagic = [v.removeprefix(b"vermagic=").decode() for v in metadata if v.startswith(b"vermagic=")]
        if vermagic != [VERMAGIC]:
            raise ValueError("module vermagic does not match the reviewed stock modules")
    report = {"source_revision": revision, "stock_kernel_raw_sha256": RAW_HASH,
              "baseline_config_sha256": sha(baseline / ".config"),
              "build_config_sha256": sha(kernel / ".config"),
              "source_files": {name: sha(path) for name, path in files.items()},
              "module_sha256": sha(artifact), "vermagic": vermagic[0],
              "verified_shared_declarations": {name: f"{witness_exports[name]:08x}" for name in SHARED},
              "matched_stock_imports": len(versions), "mismatched_imports": [],
              "runtime_qualified": False, "module_loaded": False,
              "limits": "CRC and release agreement do not establish full ABI, timing, DMA concurrency, power behavior, or runtime safety"}
    (output / "module-report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("nxp_source", type=Path)
    parser.add_argument("baseline_build", type=Path)
    parser.add_argument("stock_kernel_elf", type=Path)
    parser.add_argument("new_output", type=Path)
    parser.add_argument("compiler_prefix")
    args = parser.parse_args()
    os.umask(0o077)
    print(json.dumps(build(args.nxp_source, args.baseline_build, args.stock_kernel_elf,
                           args.new_output, args.compiler_prefix), indent=2))


if __name__ == "__main__":
    main()
