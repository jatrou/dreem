#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Replay original Bluetooth callbacks through the independently compiled filter.

All D-Bus, radio, system, clock and nested manager boundaries remain synthetic.
Neither original nor patched vendor program is started as a process.
"""
import argparse
import io
import json
from pathlib import Path
import struct

from elftools.elf.elffile import ELFFile
from unicorn.arm_const import (UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6,
                               UC_ARM_REG_R7, UC_ARM_REG_R8, UC_ARM_REG_R9,
                               UC_ARM_REG_R10, UC_ARM_REG_R11, UC_ARM_REG_SP)

import verify_bluetooth_policy as stock
from build_bluetooth_peer_overlay import (BASE, CODE, CALL_SITES, CORE_SIZE,
                                         digest, file_offset, overlay_payload,
                                         patch_core, read_regular, require)

CALLEE_SAVED = (UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
               UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11)
SENTINELS = tuple(0x23450000+i*0x100 for i in range(8))


class OverlayPolicy(stock.Policy):
    def __init__(self, path, overlay):
        super().__init__(path)
        raw = read_regular(path, CORE_SIZE)
        payload = overlay_payload((overlay/'overlay.elf').read_bytes())
        rebuilt, _ = patch_core(raw, payload)
        require((overlay/'nano_core.peer-overlay').read_bytes() == rebuilt,
                'private overlay does not reproduce from original and component')
        self.cpu.mem_map(BASE, (4096+len(payload)+4095) & ~4095, 5)
        offset = file_offset(rebuilt, BASE, 4096+len(payload))
        self.cpu.mem_write(BASE, rebuilt[offset:offset+4096+len(payload)])
        for address in CALL_SITES:
            offset = file_offset(rebuilt, address)
            self.cpu.mem_write(address, rebuilt[offset:offset+4])
        self.ranges += ((CODE, CODE+len(payload)),)
        symbols = ELFFile(io.BytesIO((overlay/'overlay.elf').read_bytes())).get_section_by_name('.symtab')
        self.classifier = symbols.get_symbol_by_name('dreem_is_extension_peer')[0]['st_value']
        count_address = symbols.get_symbol_by_name('dreem_extension_peer_count')[0]['st_value']
        table_address = symbols.get_symbol_by_name('dreem_extension_peers')[0]['st_value']
        count = struct.unpack('<I', self.cpu.mem_read(count_address, 4))[0]
        require(count <= 8, 'compiled peer count exceeds bound')
        self.extension_peers = tuple(bytes(self.cpu.mem_read(table_address+i*18, 18)) for i in range(count))

    def call(self, entry, *args):
        for register, value in zip(CALLEE_SAVED, SENTINELS):
            self.cpu.reg_write(register, value)
        result = super().call(entry, *args)
        # The event worker intentionally stops at its next modeled wait, with
        # its frame still active; ordinary callback/classifier calls must unwind.
        if entry != 0x64c8c:
            require(tuple(self.cpu.reg_read(r) for r in CALLEE_SAVED) == SENTINELS,
                    'overlay or original path corrupted a callee-saved register')
            require(self.cpu.reg_read(UC_ARM_REG_SP) == stock.DATA+0x3f00,
                    'overlay or original callback left an unbalanced stack')
        return result


def verify(core, control, selected):
    """Control is an empty table; selected must contain only synthetic PEERS[1]."""
    original = stock.verify(core)
    original_class = stock.Policy
    require(not OverlayPolicy(core, control).extension_peers, 'control peer table is not empty')
    try:
        stock.Policy = lambda path: OverlayPolicy(path, control)
        unchanged = stock.verify(core)
    finally:
        stock.Policy = original_class
    require(original == unchanged, 'empty-table overlay changed original policy results')
    model = OverlayPolicy(core, selected)
    peer, companion = stock.PEERS[1], stock.PEERS[0]
    require(model.extension_peers == (peer.encode()+b'\0',), 'selected table is not the synthetic fixture')
    model.prepare()
    rows = []

    def record(kind, row, **inputs):
        rows.append({'kind': kind, **inputs, **row})

    # Explicit positive and byte-level negative vectors; the compiler-produced
    # classifier executes rather than substituting a Python address model.
    vectors = [(peer, 1), (None, 0), ('', 0), (companion, 0), (peer+'0', 0),
               (peer+' ', 0), (' '+peer, 0)]
    vectors += [(peer[:i], 0) for i in range(1, len(peer))]
    vectors += [(peer[:i]+('X' if peer[i] != ':' else '-')+peer[i+1:], 0)
                for i in range(len(peer))]
    for address, expected in vectors:
        model.prepare()
        if address is not None:
            model.cpu.mem_write(stock.PEER, address.encode()+b'\0')
        result = model.call(model.classifier, stock.PEER if address is not None else 0)
        require(result == expected and not model.events, 'compiled peer classification differs')
        rows.append({'kind': 'classifier', 'input': address, 'result': result})

    for sensor_added in (False, True):
        for companion_added in (False, True):
            model.prepare()
            empty = model.peer_callback(peer=peer, added=sensor_added)
            require(empty['peer'] == '' and empty['connection_clock'] == 0 and
                    empty['connection_flag'] == 0 and not empty['events'],
                    'sensor claimed the companion slot or invoked a service')
            record('sensor_first', empty, added=sensor_added)
            accepted = model.peer_callback(peer=companion, added=companion_added)
            require(accepted['peer'] == companion and accepted['connection_flag'] == 1 and
                    ['event_push', 12, 0] in accepted['events'] and not stock.commands(accepted),
                    'companion failed after sensor callback')
            record('companion_after_sensor', accepted, added=companion_added)
            for _ in range(2):
                ignored = model.peer_callback(peer=peer, added=sensor_added)
                require(ignored['peer'] == companion and ignored['connection_clock'] == 100 and
                        ignored['connection_flag'] == 1 and not ignored['events'],
                        'repeated sensor callback disturbed companion ownership')
                record('sensor_while_companion', ignored, added=sensor_added)
            disconnected = model.peer_callback(peer=peer, connected=False)
            require(disconnected['peer'] == companion and not disconnected['events'],
                    'sensor disconnect changed companion state')
            record('sensor_disconnect', disconnected)
            repeated = model.peer_callback(peer=companion, added=companion_added)
            require(stock.commands(repeated) == ['echo "remove '+companion+'" | /usr/bin/bluetoothctl'],
                    'overlay unexpectedly repaired or bypassed repeated companion removal')
            record('repeated_companion_unchanged', repeated)
            disconnected = model.peer_callback(peer=companion, connected=False)
            require(disconnected['peer'] == '' and disconnected['connection_flag'] == 0 and
                    ['event_push', 13, 0] in disconnected['events'], 'companion disconnect changed')
            record('companion_disconnect', disconnected)
            off = model.event(13)
            require(off['state'] == 6 and stock.commands(off) == [stock.PROBE, *stock.DISABLE],
                    'overlay changed the original recording power policy')
            record('recording_power_still_off', off)
            sensor = model.peer_callback(peer=peer, added=sensor_added)
            require(sensor['peer'] == '' and not sensor['events'], 'late sensor claimed freed slot')
            record('late_sensor', sensor)
            new_companion = model.peer_callback(peer=companion, added=companion_added)
            require(new_companion['peer'] == companion and
                    ['event_push', 12, 0] in new_companion['events'], 'companion reconnect changed')
            record('companion_reconnect', new_companion)

    # Negative control: exact same designated address on unmodified code occupies
    # the slot, then causes removal of the following companion address.
    reference = original_class(core)
    for added in (False, True):
        reference.prepare()
        first = reference.peer_callback(peer=peer, added=added)
        second = reference.peer_callback(peer=companion, added=added)
        require(first['peer'] == peer and ['event_push', 12, 0] in first['events'] and
                stock.commands(second) == ['echo "remove '+companion+'" | /usr/bin/bluetoothctl'],
                'unmodified negative control did not exhibit original conflict')
        record('unmodified_conflict_control', second, added=added)

    for added in (False, True):
        for missing in ('Address', 'Connected'):
            model.prepare()
            reference.prepare()
            model.missing_property = reference.missing_property = missing
            expected = reference.peer_callback(peer=companion, added=added)
            actual = model.peer_callback(peer=companion, added=added)
            require(actual == expected, 'missing property handling changed')
            record('missing_property', actual, added=added, missing=missing)

    return {'core_sha256': stock.CORE_SHA256, 'empty_overlay_original_cases': original['cases'],
            'empty_overlay_fixture_sha256': original['fixture_sha256'],
            'selected_overlay_cases': len(rows),
            'selected_fixture_sha256': digest(json.dumps(rows, sort_keys=True).encode()),
            'results': rows, 'services': 'synthetic', 'callee_saved_registers_checked': True,
            'shell_commands_executed': False, 'radio_power_policy_changed': False,
            'physical_qualification': False, 'installed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('core', type=Path)
    parser.add_argument('empty_overlay', type=Path)
    parser.add_argument('synthetic_overlay', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.core, args.empty_overlay, args.synthetic_overlay), indent=2))


if __name__ == '__main__':
    main()
