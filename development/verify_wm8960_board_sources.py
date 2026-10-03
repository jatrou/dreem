#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Match all board-driver code and tables, including its known failure behavior.

This is evidence for reconstruction, not device qualification or a safety
approval. No vendor driver is executed and no firmware is installed.
"""
import argparse
import json
from pathlib import Path
import struct

from verify_wm8960_sources import Object, compare, sha
from verify_busfreq import Image, RAW_HASH, require

COMMON = {'imx_wm8960_' + n for n in ('driver_init', 'driver_exit', 'remove',
          'late_probe', 'jack_init', 'probe')} | {
    'show_headphone', 'show_micphone', 'mic_jack_status_check', 'hp_jack_status_check',
    'be_hw_params_fixup', 'imx_hifi_hw_free', 'imx_hifi_hw_params',
    'imx_hifi_shutdown', 'imx_hifi_startup'}
ADDED = {'set_jack_status', 'jack_ioctl'}
ANCHORS = COMMON | ADDED | {'imx_wm8960_dapm_widgets', 'imx_wm8960_dt_ids',
          'imx_wm8960_driver', 'card_priv', 'imx_hp_jack', 'imx_mic_jack', 'jack_inst',
          '__initcall_imx_wm8960_driver_init6'}


def board_object(binary, original=False):
    sections = {'.text': None, '.init.text': 16, '.exit.text': 12,
                '.rodata': 984 if original else 1096, '.data': 700,
                '.bss': 140 if original else 208, '.initcall6.init': 4}
    return Object(binary, functions=COMMON if original else COMMON | ADDED,
                  sections=sections, anchors=ANCHORS)


def negative_controls(obj, stock, original):
    cases = []

    def rejected(binary, label, expected, baseline=False):
        try:
            compare(board_object(bytes(binary), baseline), stock)
        except ValueError as error:
            require(expected in str(error), 'unexpected rejection for ' + label + ': ' + str(error))
        else:
            raise ValueError('mutation accepted: ' + label)
        cases.append(label)

    rejected(original.binary, 'unmodified NXP board driver', 'section layout differs', True)
    for name, offset, label in [('jack_ioctl', 0x18, 'jack command comparison changed'),
                              ('imx_wm8960_late_probe', 4, 'late-probe mask changed')]:
        s = obj.named[name]
        changed = bytearray(obj.binary)
        changed[obj.elf.get_section(s['st_shndx'])['sh_offset'] + s['st_value'] + offset] ^= 1
        rejected(changed, label, 'section bytes differ')
    index = obj.elf.get_section_index('.rodata')
    _, location = next((r, p) for r, p in obj.relocations[index]
                       if obj.table.get_symbol(r['r_info_sym']).name == 'jack_ioctl')
    target = next(i for i, s in enumerate(obj.symbols) if s.name == 'set_jack_status')
    changed = bytearray(obj.binary)
    struct.pack_into('<I', changed, location + 4, (target << 8) | 2)
    rejected(changed, 'character-device callback redirected', 'section bytes differ')
    # Keep sizes and layout intact while redirecting the OTP gate's external call.
    index = obj.elf.get_section_index('.text')
    _, location = next((r, p) for r, p in obj.relocations[index]
                       if obj.table.get_symbol(r['r_info_sym']).name == 'get_dreem_hardware_version')
    target = next(i for i, s in enumerate(obj.symbols) if s.name == 'cdev_del')
    changed = bytearray(obj.binary)
    struct.pack_into('<I', changed, location + 4, (target << 8) | 28)
    rejected(changed, 'hardware-version call redirected', 'section bytes differ')
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stock_kernel', type=Path)
    parser.add_argument('reference_object', type=Path)
    parser.add_argument('baseline_kernel', type=Path)
    parser.add_argument('baseline_object', type=Path)
    args = parser.parse_args()
    stock, baseline = Image(args.stock_kernel, True), Image(args.baseline_kernel)
    obj = board_object(args.reference_object.read_bytes())
    original = board_object(args.baseline_object.read_bytes(), True)
    result = {'stock_kernel_raw_sha256': RAW_HASH,
              'reference_object_sha256': sha(obj.binary),
              'baseline_object_sha256': sha(original.binary),
              'baseline_kernel_sha256': sha(baseline.binary),
              'reference_against_stock': compare(obj, stock),
              'unmodified_against_baseline': compare(original, baseline),
              'negative_controls': negative_controls(obj, stock, original),
              'verifier_sources': {n: sha((Path(__file__).parent / n).read_bytes())
                                   for n in ('verify_wm8960_board_sources.py', 'verify_wm8960_sources.py',
                                             'arm_relocations.py', 'verify_busfreq.py', 'verify_ddr_sources.py')},
              'preserves_observed_cleanup_defects': True,
              'hardware_qualified': False, 'executed_on_headset': False}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
