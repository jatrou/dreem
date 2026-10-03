#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build the experimental provider into a disposable pinned NXP source copy.

Produces an offline research kernel and ADC module. Nothing is installed or
flashed. Baseline checkout/build and the private stock firmware are read-only.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

from elftools.elf.elffile import ELFFile
from apply_sdma_overlay import apply
from apply_busfreq_overlay import apply as apply_busfreq
from apply_hardware_overlay import apply as apply_hardware
from apply_wm8960_overlay import apply as apply_wm8960
from build_adc_module import REVISION, RAW_HASH, SHARED, sha
from recover_exports import recover, module_versions


def build(source, baseline, stock, output, compiler, jobs, busfreq=False, hardware=False,
          wm8960=False):
    if wm8960 and not hardware:
        raise ValueError("WM8960 board requires --hardware-version")
    if output.exists() or output.is_symlink():
        raise ValueError("output must be a new private directory")
    source, baseline, stock, output = (p.resolve() for p in (source, baseline, stock, output))
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True)
    if revision != REVISION or dirty:
        raise ValueError("requires clean pinned NXP source")
    with stock.open("rb") as stream:
        elf = ELFFile(stream)
        if sha_section(elf) != RAW_HASH:
            raise ValueError("stock kernel content mismatch")
        stock_exports = recover(elf)
    config = (baseline / ".config").read_text()
    for option in ("CONFIG_ARM=y", "CONFIG_IMX_SDMA=y", "CONFIG_MODVERSIONS=y"):
        if option not in config.splitlines():
            raise ValueError("missing required configuration: " + option)
    if "CONFIG_OF_DYNAMIC=y" in config.splitlines():
        raise ValueError("provider requires static device tree")
    config = config.replace("CONFIG_LOCALVERSION_AUTO=y", "# CONFIG_LOCALVERSION_AUTO is not set")
    here = Path(__file__).resolve().parent
    files = [here / "ads129x_init.c", here / "ads129x_init.h",
             here / "kernel/adc_linux.c", here / "kernel/sdma_eeg_api.h", here / "kernel/Makefile"]
    inputs = [*files, here / "sdma_eeg.c", here / "sdma_eeg.h",
              here / "kernel/sdma_eeg_linux.h", here / "kernel/sdma_eeg_linux.inc",
              here / "sdma_acquire.asm", here / "sdma_assemble.py", here / "sdma_disassemble.py",
              here / "apply_sdma_overlay.py", Path(__file__).resolve()]
    if busfreq:
        inputs += [here / "apply_busfreq_overlay.py", here / "kernel/busfreq_dreem.inc",
                   here / "kernel/ddr_linux.inc", here / "kernel/ddr_prepare_dreem.inc",
                   here / "kernel/busfreq_probe_dreem.inc"]
    if hardware:
        inputs += [here / "apply_hardware_overlay.py", here / "kernel/dreem_hardware.inc",
                   here / "kernel/dreem_hardware.h"]
    if wm8960:
        inputs += [here / "apply_wm8960_overlay.py", here / "wm8960_board_reference.py",
                   here / "kernel/wm8960_jack.inc", here / "kernel/wm8960_lifetime.inc"]
    source_hashes = {str(p.relative_to(here)): sha(p) for p in inputs}
    output.mkdir(mode=0o700)
    tree, kernel, module = (output / name for name in ("source", "kernel", "module"))
    for folder in (tree, kernel, module):
        folder.mkdir(mode=0o700)
    archive = subprocess.Popen(["git", "-C", str(source), "archive", REVISION], stdout=subprocess.PIPE)
    try:
        subprocess.run(["tar", "-xf", "-", "-C", str(tree)], stdin=archive.stdout, check=True)
    finally:
        archive.stdout.close()
    if archive.wait():
        raise ValueError("source snapshot failed")
    apply(tree)
    if busfreq:
        apply_busfreq(tree)
    if hardware:
        apply_hardware(tree)
    if wm8960:
        apply_wm8960(tree)
    (kernel / ".config").write_text(config + "\nCONFIG_DREEM_EEG_SDMA=y\n" +
                                    ("CONFIG_DREEM_BUSFREQ=y\n" if busfreq else "") +
                                    ("CONFIG_DREEM_HW_VERSION=y\n" if hardware else "") +
                                    ("CONFIG_DREEM_WM8960=y\n" if wm8960 else ""))
    make = ["make", "-C", str(tree), "O=" + str(kernel), "ARCH=arm",
            "CROSS_COMPILE=" + compiler, "LOCALVERSION=", "HOSTCFLAGS=-O2 -fcommon",
            "KBUILD_BUILD_USER=builder", "KBUILD_BUILD_HOST=dreem-research"]
    with (output / "build.log").open("w") as log:
        print("Preparing isolated kernel configuration", flush=True)
        subprocess.run(make + ["olddefconfig", "modules_prepare"], stdout=log, stderr=subprocess.STDOUT, check=True)
        if "CONFIG_DREEM_EEG_SDMA=y" not in (kernel / ".config").read_text().splitlines():
            raise ValueError("research provider was not enabled")
        if busfreq and "CONFIG_DREEM_BUSFREQ=y" not in (kernel / ".config").read_text().splitlines():
            raise ValueError("research bus-frequency policy was not enabled")
        if hardware and "CONFIG_DREEM_HW_VERSION=y" not in (kernel / ".config").read_text().splitlines():
            raise ValueError("research hardware-version API was not enabled")
        if wm8960 and "CONFIG_DREEM_WM8960=y" not in (kernel / ".config").read_text().splitlines():
            raise ValueError("research WM8960 board was not enabled")
        print("Building integrated kernel", flush=True)
        subprocess.run(make + ["-j" + str(jobs), "vmlinux"], stdout=log, stderr=subprocess.STDOUT, check=True)
        # Kernel symbol CRCs come from this actual provider, without a dummy
        # declaration witness or copied stock symvers entries.
        symbols = {}
        for line in (kernel / "Module.symvers").read_text().splitlines():
            fields = line.split()
            symbols[fields[1]] = int(fields[0], 16)
        for name in SHARED:
            if symbols.get(name) != stock_exports[name][0]:
                raise ValueError("provider export does not match stock: " + name)
        for path in files:
            shutil.copyfile(path, module / path.name)
        print("Building ADC module against the real provider", flush=True)
        subprocess.run(make + ["M=" + str(module), "KCFLAGS=-g", "modules"],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    artifact = module / "dreem_eeg_research.ko"
    with artifact.open("rb") as stream:
        imports = module_versions(ELFFile(stream).get_section_by_name("__versions").data())
    if not {"dreem_sdma_status", "dreem_sdma_control"}.issubset(imports) or any(symbols.get(n) != c for n, c in imports.items()):
        raise ValueError("ADC imports do not match integrated kernel")
    if any(sha(here / name) != digest for name, digest in source_hashes.items()):
        raise ValueError("research source changed during the build; rebuild from stable inputs")
    report = {"nxp_revision": revision, "stock_kernel_raw_sha256": RAW_HASH,
              "baseline_config_sha256": sha(baseline / ".config"),
              "build_config_sha256": sha(kernel / ".config"),
              "kernel_sha256": sha(kernel / "vmlinux"),
              "provider_object_sha256": sha(kernel / "drivers/dma/imx-sdma.o"),
              "adc_module_sha256": sha(artifact),
              "stock_shared_crcs": {n: f"{symbols[n]:08x}" for n in SHARED},
              "adc_matched_imports": len(imports), "runtime_enabled_by_default": bool(wm8960),
              "sdma_runtime_enabled_by_default": False,
              "runtime_qualified": False, "installed": False,
              "source_files": source_hashes}
    if busfreq:
        report["busfreq_object_sha256"] = sha(kernel / "arch/arm/mach-imx/busfreq-imx.o")
        report["busfreq_runtime_enabled_by_default"] = False
    if hardware:
        report["hardware_version_object_sha256"] = sha(kernel / "drivers/char/fsl_otp.o")
        report["hardware_version_read_automatically"] = False
    if wm8960:
        report["wm8960_board_object_sha256"] = sha(kernel / "sound/soc/fsl/imx-wm8960.o")
        report["wm8960_board_active_when_selected"] = True
        report["wm8960_codec_integrated"] = False
        report["hardware_version_read_automatically"] = True
    (output / "provider-build.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def sha_section(elf):
    import hashlib
    section = elf.get_section_by_name(".kernel")
    return hashlib.sha256(section.data()).hexdigest() if section else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("nxp_source", type=Path)
    parser.add_argument("baseline_build", type=Path)
    parser.add_argument("stock_kernel_elf", type=Path)
    parser.add_argument("new_output", type=Path)
    parser.add_argument("compiler_prefix")
    parser.add_argument("--jobs", type=int, default=8, choices=range(1, 65))
    parser.add_argument("--busfreq-policy", action="store_true",
                        help="also compile the experimental, runtime-disabled Femto clock policy")
    parser.add_argument("--hardware-version", action="store_true",
                        help="also build the checked internal Femto hardware-version read API")
    parser.add_argument("--wm8960-board", action="store_true",
                        help="select the experimental Femto board driver; requires --hardware-version")
    args = parser.parse_args()
    os.umask(0o077)
    print(json.dumps(build(args.nxp_source, args.baseline_build, args.stock_kernel_elf,
                           args.new_output, args.compiler_prefix, args.jobs,
                           args.busfreq_policy, args.hardware_version, args.wm8960_board), indent=2))


if __name__ == "__main__":
    main()
