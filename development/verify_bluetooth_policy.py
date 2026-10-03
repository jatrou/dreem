#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Replay the saved core's Bluetooth ownership and selected event-policy paths.

Only the exact reviewed private executable is accepted. Shell, D-Bus, thread,
clock and nested manager operations are synthetic. No vendor process, shell
command, radio or sensor is opened. This is not a complete core simulation.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import struct

from elftools.elf.elffile import ELFFile
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_PC, UC_ARM_REG_LR,
                               UC_ARM_REG_C1_C0_2, UC_ARM_REG_FPEXC)
from verify_optical_transport import Stock, CORE_SHA256, DATA, RETURN, ERRNO, REGISTERS, require

RANGES = ((0x64c8c, 0x68578), (0x36cb8, 0x36dc8), (0x86f18, 0x871a4),
          (0x38318, 0x3877c), (0x38828, 0x38cd8))
STATE, CONTEXT, SERVER = 0xecaf7c, 0xecbf78, DATA+0x4000
PEER, PROPERTY, ITERATOR, INTERFACE = DATA+0x2400, DATA+0x2500, DATA+0x2600, DATA+0x2700
PEERS = ('02:00:00:00:00:01', '02:00:00:00:00:02')
TABLE_SHA256 = 'efe7930c2845f120edc0f79c70246beaf713ed1b1a359ae37ab57fb1107769e4'
PROBE = '/usr/bin/hciconfig hci0 | grep UP'
EXISTS = '/usr/bin/hciconfig hci0'
ENABLE = tuple('/usr/bin/btmgmt -i hci0 '+value for value in (
    'power off', 'le on', 'bredr on', 'connectable on', 'bondable on', 'discov on',
    'name DREEM-HEADBAND-V2', 'advertising on', 'power on'))
DISABLE = tuple('/usr/bin/btmgmt -i hci0 '+value for value in (
    'discov off', 'advertising off', 'connectable off', 'power off'))
BOUNDARIES = {
    0x28018: 'analytics', 0x8ef28: 'power_event', 0x36584: 'paired_manager',
    0x365f8: 'unpaired_manager', 0x636ec: 'timer_start', 0x63818: 'timer_stop',
    0x63a38: 'gesture_action', 0x649d4: 'gesture_event',
    0x70b64: 'background_stop', 0x70dc8: 'exercise_stop',
    0x7196c: 'relax_stop', 0x711f0: 'background_mode',
    0x9455c: 'alarm_update', 0x94630: 'alarm_start', 0x95690: 'alarm_stop',
    0x96040: 'led_update', 0x981f8: 'security_stop', 0x2ba98: 'record_start',
    0x64618: 'event_push', 0x6a84c: 'audio_sync', 0x39cbc: 'gatt_register',
}
SERVICES = BOUNDARIES | {
    0x863b8: 'wait', 0x16fbc: 'cancelstate', 0x15f3c: 'canceltype',
    0x15bb8: 'nice', 0x16074: 'sleep', 0x16f50: 'system', 0x160b0: 'thread_exit',
    0x16e78: 'strerror', 0x15c78: 'strlen', 0x1638c: 'memset', 0x167c4: 'memcpy',
    0x160e0: 'snprintf', 0x16944: 'clock', 0x161ac: 'strcmp', 0x17004: 'strcmp',
    0x31570: 'proxy_interface', 0x3157c: 'proxy_property', 0x16608: 'iter_basic',
}


class Policy(Stock):
    def __init__(self, path):
        super().__init__(path, additional_ranges=RANGES, additional_services=SERVICES)
        self.cpu.mem_map(0xecb000, 0x3000, 3)
        self.cpu.mem_map(0xda3000, 0x1000, 3)
        self.cpu.mem_map(DATA+0x4000, 0x4000, 3)
        elf = ELFFile(io.BytesIO(path.read_bytes()))
        start, end = 0xe5000, 0x104000
        self.cpu.mem_map(start, end-start, 1)
        for segment in elf.iter_segments():
            base = segment['p_vaddr']
            if segment['p_type'] == 'PT_LOAD' and base <= start < end <= base+segment['p_filesz']:
                self.cpu.mem_write(start, segment.data()[start-base:end-base])
                break
        else:
            raise ValueError('unbacked constants')
        table = bytes(self.cpu.mem_read(0x64dc4, 48*4))
        require(hashlib.sha256(table).hexdigest() == TABLE_SHA256, 'event table differs')
        self.targets = struct.unpack('<48I', table)
        require(len(set(self.targets)) == 45, 'event target count differs')
        self.cpu.reg_write(UC_ARM_REG_C1_C0_2, 0xf00000)
        self.cpu.reg_write(UC_ARM_REG_FPEXC, 1 << 30)

    def get(self, address):
        return struct.unpack('<I', self.cpu.mem_read(address, 4))[0]

    def string(self, address):
        result = bytearray()
        for offset in range(4096):
            byte = self.cpu.mem_read(address+offset, 1)[0]
            if not byte:
                return result.decode('ascii')
            result.append(byte)
        raise ValueError('unterminated synthetic string')

    def prepare(self, *, state=7, probe=0, exists=(0,), cache=0, failures=None):
        self.reset()
        self.cpu.mem_write(DATA, bytes(0x8000))
        self.cpu.mem_write(0xeca000, bytes(0x4000))
        self.cpu.mem_write(0xda2000, bytes(0x2000))
        self.word(STATE, state)
        self.word(0xda33c8, SERVER)
        self.word(0xda2acc, cache)
        self.word(SERVER+0x101c, DATA)
        self.probe, self.exists = probe, list(exists)
        self.failures = failures or {}
        self.events, self.waits, self.iterators = [], 0, {}
        self.connected, self.peer, self.clock_seconds = True, PEERS[0], 100
        self.missing_property = None
        self.cpu.mem_write(INTERFACE, b'org.bluez.Device1\0')
        self.cpu.mem_write(PROPERTY, b'Connected\0')
        self.terminal = None

    def code(self, cpu, address, size, user):
        name = self.services.get(address)
        a, b, c, d = [cpu.reg_read(r) for r in REGISTERS]
        result = self.failures.get(name, 0)
        if name == 'system':
            command = self.string(a)
            require(command in (PROBE, EXISTS, *ENABLE, *DISABLE) or
                    command in tuple('echo "remove '+p+'" | /usr/bin/bluetoothctl' for p in PEERS),
                    'unexpected modeled command')
            if command == PROBE:
                result = self.probe
            elif command == EXISTS:
                require(self.exists, 'unexpected controller-existence retry')
                result = self.exists.pop(0)
            else:
                result = self.failures.get(command, 0)
            self.events.append(['system', command, result])
        elif name == 'sleep':
            require(a == 1, 'unexpected controller retry delay')
            self.events.append(['sleep', a])
        elif name == 'cancelstate':
            if b and not result:
                self.word(b, 0)
            self.events.append([name, a, result])
        elif name in ('canceltype', 'nice'):
            pass
        elif name == 'wait':
            require(a == 0xecb23c, 'unexpected event queue')
            self.waits += 1
            if self.waits == 2:
                self.terminal = 'next_event_wait'
                cpu.reg_write(UC_ARM_REG_PC, RETURN)
                cpu.emu_stop()
                return
        elif name == 'thread_exit':
            self.terminal = 'thread_exit'
            cpu.reg_write(UC_ARM_REG_PC, RETURN)
            cpu.emu_stop()
            return
        elif name == 'clock':
            require(a == 1, 'connection clock is not monotonic')
            if not result:
                cpu.mem_write(b, struct.pack('<II', self.clock_seconds, 123456789))
            self.events.append([name, result])
        elif name == 'memset':
            require(b == 0 and c == 4096, 'unexpected clearing')
            cpu.mem_write(a, bytes(c))
            result = a
        elif name == 'memcpy':
            require(c == len(self.peer) and self.string(b) == self.peer, 'unexpected copy')
            cpu.mem_write(a, bytes(cpu.mem_read(b, c)))
            result = a
        elif name == 'strlen':
            result = len(self.string(a))
        elif name == 'snprintf':
            require(b == 4095 and self.string(c) == 'echo "remove %s" | /usr/bin/bluetoothctl',
                    'unexpected removal format')
            peer = self.string(d)
            require(peer in PEERS, 'non-synthetic peer')
            command = 'echo "remove '+peer+'" | /usr/bin/bluetoothctl'
            cpu.mem_write(a, command.encode()+b'\0')
            result = len(command)
        elif name == 'strcmp':
            left, right = self.string(a), self.string(b)
            result = (left > right) - (left < right)
        elif name == 'proxy_interface':
            require(a == DATA+0x2000, 'unexpected proxy')
            result = INTERFACE
        elif name == 'proxy_property':
            require(a == DATA+0x2000, 'unexpected proxy')
            property_name = self.string(b)
            require(property_name in ('Connected', 'Address', 'Paired'), 'unexpected property')
            self.iterators[c] = property_name
            result = int(property_name != self.missing_property)
        elif name == 'iter_basic':
            key = 'Connected' if a == ITERATOR else self.iterators[a]
            self.word(b, PEER if key == 'Address' else int(self.connected))
        elif name in BOUNDARIES.values():
            if name == 'record_start':
                require((a, b, c) == (DATA, 1, 0xffffffff), 'unexpected recovery record request')
                self.events.append([name, 1, -1, result])
            elif name in ('event_push', 'timer_start'):
                require(a in ((12, 13) if name == 'event_push' else (60, 300)),
                        'unexpected event/timer request')
                self.events.append([name, a, result])
            else:
                self.events.append([name, result])
        elif name == 'strerror':
            result = 0xe5a8c
        else:
            return super().code(cpu, address, size, user)
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def run(self, entry, *args):
        result = self.call(entry, *args)
        return {'return': result, 'events': list(self.events), 'state': self.get(STATE),
                'terminal': self.terminal, 'peer': self.string(SERVER+0x18),
                'connection_clock': self.get(SERVER+0x105c),
                'connection_flag': self.get(CONTEXT+0x450)}

    def event(self, event):
        self.events, self.waits, self.terminal = [], 0, None
        self.word(0xecb134, event)
        self.word(0xecb234, 0)
        row = self.run(0x64c8c, DATA)
        require(self.get(0xecb234) == 1, 'event was not consumed exactly once')
        return row

    def peer_callback(self, *, connected=True, peer=PEERS[0], added=False):
        self.connected, self.peer = connected, peer
        self.cpu.mem_write(PEER, peer.encode()+b'\0')
        self.events = []
        return self.run(0x38538, DATA+0x2000) if added else self.run(
            0x38828, DATA+0x2000, PROPERTY, ITERATOR)


def commands(row):
    return [e[1] for e in row['events'] if e[0] == 'system']


def verify(path):
    model = Policy(path)
    rows = []

    def record(kind, row, **inputs):
        rows.append({'kind': kind, **inputs, **row})

    for probe in (0, 1, 256, -1, 32512):
        for helper, entry in (('enable', 0x86f18), ('disable', 0x870cc)):
            model.prepare(probe=probe)
            row = model.run(entry)
            expected = [PROBE]+(list(ENABLE) if helper == 'enable' and probe else
                                list(DISABLE) if helper == 'disable' and probe != 1 else [])
            require(row['return'] == 0 and commands(row) == expected, 'controller gate changed')
            record(helper, row, probe=probe)
    for helper, entry, sequence in (('enable', 0x86f18, ENABLE), ('disable', 0x870cc, DISABLE)):
        for command in sequence:
            for status in (-1, 256):
                model.prepare(probe=256, failures={command: status})
                row = model.run(entry)
                require(row['return'] == 0 and commands(row) == [PROBE, *sequence],
                        'controller helper stopped ignoring a command failure')
                record(helper+'_failure', row, failed_command=command, failure=status)
    for attempts in (1, 3, 10):
        model.prepare(probe=0, cache=1, exists=(256,)*(attempts-1)+(0,))
        row = model.run(0x86f18)
        require(row['return'] == 0 and commands(row) == [EXISTS]*attempts+[PROBE] and
                row['events'].count(['sleep', 1]) == attempts, 'existence retry changed')
        record('existence_retry', row, attempts=attempts)
    for entry, returned in ((0x86f18, 1), (0x36d70, 0)):
        model.prepare(cache=1, exists=(256,)*10)
        row = model.run(entry)
        require(row['return'] == returned and commands(row) == [EXISTS]*10 and
                row['events'].count(['sleep', 1]) == 10, 'enable/resume failure handling changed')
        record('existence_failure', row, entry=hex(entry))
    for failed in ('background_stop', 'exercise_stop', 'cancelstate', DISABLE[-1]):
        model.prepare(failures={failed: 1})
        row = model.run(0x36cb8)
        require(row['return'] == 0 and commands(row) == [PROBE, *DISABLE], 'pause error path changed')
        record('pause_failure', row, failed_boundary=failed)

    # Selected, independently enumerated recording/user-interaction transitions.
    # Nested manager calls succeed without executing their bodies or side effects.
    transitions = ((12, 5, 4, 'none', None), (12, 8, 7, 'none', None),
                   (13, 4, 5, 'none', 300), (13, 6, 6, 'none', None),
                   (13, 7, 6, 'off', None), (13, 8, 8, 'none', None),
                   (14, 4, 4, 'none', None), (14, 7, 7, 'none', None),
                   (14, 8, 6, 'off', None), (18, 6, 8, 'on', 60),
                   (18, 7, 7, 'none', None), (18, 8, 8, 'none', 60),
                   (31, 4, 7, 'none', None), (31, 6, 7, 'none', None),
                   (32, 6, 6, 'off', None), (32, 7, 6, 'off', None))
    for event, state, final, radio, timer in transitions:
        model.prepare(state=state, probe=256 if radio == 'on' else 0)
        row = model.event(event)
        expected = [] if radio == 'none' else [PROBE, *(ENABLE if radio == 'on' else DISABLE)]
        require(row['state'] == final and row['terminal'] == 'next_event_wait' and
                commands(row) == expected, 'recording radio transition changed')
        require([e[1] for e in row['events'] if e[0] == 'timer_start'] ==
                ([] if timer is None else [timer]), 'timer request changed')
        if event == 32:
            require(row['events'].index(['system', DISABLE[-1], 0]) <
                    row['events'].index(['record_start', 1, -1, 0]),
                    'recovery no longer powers off before restarting recording')
        record('event', row, event=event, initial_state=state)

    for added in (False, True):
        model.prepare()
        first = model.peer_callback(added=added)
        require(first['peer'] == PEERS[0] and first['connection_clock'] == 100 and
                first['connection_flag'] == 1 and ['event_push', 12, 0] in first['events'],
                'first peer no longer claims the single connection slot')
        record('first_peer', first, added=added)
        for peer in PEERS:
            row = model.peer_callback(peer=peer, added=added)
            require(commands(row) == ['echo "remove '+peer+'" | /usr/bin/bluetoothctl'] and
                    row['peer'] == PEERS[0] and not any(e[0] == 'event_push' for e in row['events']),
                    'second or repeated peer is no longer removed')
            record('occupied_peer', row, added=added, incoming=peer)
        unrelated = model.peer_callback(connected=False, peer=PEERS[1])
        require(unrelated['peer'] == PEERS[0] and unrelated['connection_flag'] == 1 and
                not any(e[0] == 'event_push' for e in unrelated['events']), 'unrelated disconnect changed')
        record('unrelated_disconnect', unrelated, added=added)
        row = model.peer_callback(connected=False)
        require(row['peer'] == '' and row['connection_clock'] == 0 and
                row['connection_flag'] == 0 and ['event_push', 13, 0] in row['events'],
                'tracked disconnect no longer clears the peer slot')
        record('tracked_disconnect', row, added=added)
    for failure, clock in ((-1, 100), (0, 0)):
        model.prepare(failures={'clock': failure})
        model.clock_seconds = clock
        first = model.peer_callback()
        second = model.peer_callback(peer=PEERS[1])
        require(first['connection_clock'] == 0 and second['peer'] == PEERS[1] and
                commands(second) == [] and ['event_push', 12, 0] in second['events'],
                'zero connection timestamp no longer bypasses the occupied-slot check')
        record('zero_connection_clock', second, clock_result=failure, clock_seconds=clock)
    for property_name in ('Address', 'Connected'):
        model.prepare()
        model.missing_property = property_name
        row = model.peer_callback(added=True)
        require(row['peer'] == '' and not any(e[0] == 'event_push' for e in row['events']),
                'missing property unexpectedly accepted a peer')
        record('missing_property', row, property=property_name)
    for command_status in (-1, 256):
        model.prepare()
        model.peer_callback()
        removal = 'echo "remove '+PEERS[1]+'" | /usr/bin/bluetoothctl'
        model.failures[removal] = command_status
        row = model.peer_callback(peer=PEERS[1])
        require(row['peer'] == PEERS[0] and row['connection_flag'] == 1 and
                ['system', removal, command_status] in row['events'],
                'failed peer removal changed tracked ownership')
        record('removal_failure', row, command_result=command_status)

    # Connect the callback's queued event to the original dispatcher, then the
    # button/timeout path. Boundary timers do not elapse and radio state is supplied.
    model.prepare(state=7)
    model.peer_callback()
    disconnected = model.peer_callback(connected=False)
    require(['event_push', 13, 0] in disconnected['events'], 'disconnect event missing')
    off = model.event(13)
    model.probe = 256
    on = model.event(18)
    model.probe = 0
    off_again = model.event(14)
    require((off['state'], on['state'], off_again['state']) == (6, 8, 6) and
            commands(off) == [PROBE, *DISABLE] and commands(on) == [PROBE, *ENABLE] and
            commands(off_again) == [PROBE, *DISABLE] and
            ['timer_start', 60, 0] in on['events'], 'connected owner/radio sequence changed')
    rows.append({'kind': 'disconnect_button_timeout_sequence',
                 'steps': [disconnected, off, on, off_again]})
    fixture = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    return {'core_sha256': CORE_SHA256, 'cases': len(rows), 'fixture_sha256': fixture,
            'event_table_sha256': TABLE_SHA256, 'event_table_entries': 48,
            'distinct_event_targets': len(set(model.targets)), 'results': rows,
            'nested_manager_boundaries': {hex(a): n for a, n in BOUNDARIES.items()},
            'services': 'synthetic', 'shell_commands_executed': False,
            'physical_qualification': False, 'complete_state_machine_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('core', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.core), indent=2))


if __name__ == '__main__':
    main()
