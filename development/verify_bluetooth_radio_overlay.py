#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run the radio overlay with original ARM paths and synthetic kernel/services."""
import argparse
import json
from pathlib import Path
import struct

from unicorn import UC_HOOK_INTR
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R7, UC_ARM_REG_PC,
                               UC_ARM_REG_LR, UC_ARM_REG_SP)
import verify_bluetooth_policy as stock
from verify_bluetooth_peer_overlay import OverlayPolicy, CALLEE_SAVED, SENTINELS
from build_bluetooth_peer_overlay import WRITABLE, digest, require

BOOT_TEXT = b'01234567-89ab-cdef-0123-456789abcdef\n'
BOOT = bytes.fromhex('0123456789abcdef0123456789abcdef')
DIRECTORY = '/run/dreem-extension-radio'
RESTORE = [stock.ENABLE[i] for i in (3, 4, 5, 7)]


def lease(seconds=103, nanoseconds=0, peer=stock.PEERS[1], boot=BOOT):
    return b'DRBTL001'+boot+struct.pack('<II', seconds, nanoseconds)+peer.encode()+b'\0\0\0'


class RadioPolicy(OverlayPolicy):
    def __init__(self, core, overlay):
        super().__init__(core, overlay, radio=True)
        self.services.update({0x168f0: 'trywait', 0x167dc: 'usleep', 0x16e6c: 'testcancel'})
        self.cpu.hook_add(UC_HOOK_INTR, self.syscall)

    def prepare(self, **kwargs):
        super().prepare(**kwargs)
        self.cpu.mem_write(WRITABLE, bytes(4096))
        self.record = None
        self.boot_text = BOOT_TEXT
        self.now = 100_000_000_000
        self.kernel_failures, self.metadata = {}, {}
        self.fds, self.kernel_trace, self.extra_trace = {}, [], []
        self.guard_waits, self.stop_next_wait = 0, True
        self.queued_event = False
        self.trywait_results, self.system_results = [], []
        self.sleep_action = None

    def held(self):
        return self.get(self.symbols['deferred'])

    def call(self, entry, *args):
        # The file parser and bounded lease polling add instructions beyond the
        # original small-function budget. Service time is explicitly synthetic.
        self.cpu.reg_write(UC_ARM_REG_SP, stock.DATA+0x3f00)
        for register, value in zip(stock.REGISTERS, args):
            self.cpu.reg_write(register, value)
        for register, value in zip(CALLEE_SAVED, SENTINELS):
            self.cpu.reg_write(register, value)
        self.cpu.reg_write(UC_ARM_REG_LR, stock.RETURN)
        self.cpu.emu_start(entry, stock.RETURN, timeout=1000000, count=200000)
        require(self.cpu.reg_read(UC_ARM_REG_PC) == stock.RETURN, 'radio execution exceeded bound')
        if entry != 0x64c8c:
            require(tuple(self.cpu.reg_read(r) for r in CALLEE_SAVED) == SENTINELS and
                    self.cpu.reg_read(UC_ARM_REG_SP) == stock.DATA+0x3f00,
                    'radio call corrupted preserved registers or stack')
        require(not self.fds, 'lease reader leaked a descriptor')
        result = self.cpu.reg_read(UC_ARM_REG_R0)
        return result if result < 0x80000000 else result-(1 << 32)

    def syscall(self, cpu, interrupt, user):
        require(interrupt == 2, 'unexpected ARM interrupt')
        number = cpu.reg_read(UC_ARM_REG_R7)
        a, b, c, d = [cpu.reg_read(r) for r in stock.REGISTERS]
        result, operation = 0, None
        if number == 201:
            operation, result = 'euid', 1000
        elif number == 322:
            path = self.string(b)
            if path == DIRECTORY:
                require(a == 0xffffff9c and c == 0x8c800, 'unexpected directory open flags')
                operation, kind = 'open_dir', 'dir'
            elif path == 'lease':
                require(self.fds.get(a) == 'dir' and c == 0x88800, 'unexpected lease open flags')
                operation, kind = 'open_lease', 'lease'
            elif path == '/proc/sys/kernel/random/boot_id':
                require(a == 0xffffff9c and c == 0x88800, 'unexpected boot-id open flags')
                operation, kind = 'open_boot', 'boot'
            else:
                raise ValueError('unexpected syscall path')
            if operation == 'open_lease' and self.record is None:
                result = -2
            else:
                result = {'dir': 70, 'lease': 71, 'boot': 72}[kind]
                if operation not in self.kernel_failures:
                    require(result not in self.fds, 'duplicate modeled open')
                    self.fds[result] = kind
        elif number == 197:
            kind = self.fds[a]
            require(kind in ('dir', 'lease'), 'unexpected stat target')
            operation = 'stat_'+kind
            value = bytearray(104)
            mode = 0o40700 if kind == 'dir' else 0o100600
            struct.pack_into('<III', value, 16, self.metadata.get(kind+'_mode', mode),
                             self.metadata.get(kind+'_links', 1), self.metadata.get(kind+'_owner', 1000))
            struct.pack_into('<q', value, 48, self.metadata.get(kind+'_size', len(self.record or b'')))
            cpu.mem_write(b, bytes(value))
        elif number == 3:
            kind = self.fds[a]
            operation = 'read_'+kind
            value = self.record if kind == 'lease' else self.boot_text
            require(c == (53 if kind == 'lease' else 38), 'unexpected bounded read size')
            result = min(c, len(value))
            cpu.mem_write(b, value[:result])
        elif number == 6:
            operation = 'close_'+self.fds.pop(a)
        elif number == 263:
            operation = 'clock'
            require(a == 1, 'lease uses non-monotonic clock')
            cpu.mem_write(b, struct.pack('<II', self.now//1000000000, self.now % 1000000000))
        else:
            raise ValueError(f'unexpected kernel syscall {number}')
        result = self.kernel_failures.get(operation, result)
        self.kernel_trace.append([operation, result])
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)

    def code(self, cpu, address, size, user):
        name = self.services.get(address)
        a = cpu.reg_read(UC_ARM_REG_R0)
        if address == self.symbols['dreem_event_wait']:
            self.guard_waits += 1
            if self.stop_next_wait and self.guard_waits == 2:
                self.terminal = 'next_event_wait'
                cpu.reg_write(UC_ARM_REG_PC, stock.RETURN)
                cpu.emu_stop()
                return
        if name == 'trywait':
            require(a == 0xecb23c, 'unexpected event semaphore')
            error = self.trywait_results.pop(0) if self.trywait_results else (0 if self.queued_event else 11)
            if not error: self.queued_event = False
            result = -1 if error else 0
            self.word(stock.ERRNO, error)
            self.extra_trace.append(['trywait', error])
        elif name == 'testcancel':
            result = 0
            self.extra_trace.append(['testcancel'])
        elif name == 'usleep':
            require(a in (250000, 1000000), 'unexpected lease poll interval')
            self.now += a*1000
            if self.sleep_action:
                self.sleep_action(self)
            result = 0
            self.extra_trace.append(['usleep', a])
        elif name == 'system' and self.system_results and self.string(a) == stock.DISABLE[-1]:
            result = self.system_results.pop(0)
            self.events.append(['system', self.string(a), result])
        else:
            if name == 'wait':
                self.queued_event = False
            return super().code(cpu, address, size, user)
        cpu.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def event(self, event):
        self.guard_waits = 0
        self.queued_event = True
        return super().event(event)

    def reconcile(self):
        self.events, self.extra_trace, self.guard_waits = [], [], 0
        self.stop_next_wait = False
        # Stop the synthetic original blocking wait with a normal return.
        self.waits = 0
        result = self.call(self.symbols['dreem_event_wait'], 0xecb23c)
        return {'return': result, 'commands': stock.commands({'events': self.events}),
                'held': self.held(), 'now_ns': self.now, 'extra': list(self.extra_trace)}


def verify(core, empty, selected):
    original = stock.verify(core)
    original_class = stock.Policy
    try:
        stock.Policy = lambda path: RadioPolicy(path, empty)
        control = stock.verify(core)
    finally:
        stock.Policy = original_class
    require(original == control, 'empty radio overlay changed original results')
    model = RadioPolicy(core, selected)
    rows = []

    def record(kind, **values):
        rows.append({'kind': kind, **values})

    for event, state in ((13, 7), (14, 8)):
        model.prepare(state=state)
        model.record = lease()
        row = model.event(event)
        require(row['state'] == 6 and model.held() == 1 and
                stock.commands(row) == [stock.PROBE, *stock.DISABLE[:-1]], 'recording pause did not defer only power-off')
        record('defer', event=event, row=row)
        release = model.reconcile()
        require(release['held'] == 0 and release['commands'] == [stock.DISABLE[-1]] and
                release['now_ns'] == 103000000000, 'expired lease failed to release controller')
        record('expire', event=event, release=release)

    bad_records = [None, b'', lease()[:-1], lease()+b'x', lease(seconds=99), lease(seconds=100),
                   lease(seconds=104), lease(nanoseconds=1), lease(nanoseconds=1000000000),
                   lease(boot=bytes(16)), lease(peer=stock.PEERS[0])]
    for offset in (0, 7, 49, 50, 51):
        value = bytearray(lease())
        value[offset] ^= 1
        bad_records.append(bytes(value))
    for value in bad_records:
        model.prepare()
        model.record = value
        row = model.event(13)
        require(not model.held() and stock.commands(row) == [stock.PROBE, *stock.DISABLE],
                'invalid lease changed original power-off')
        record('invalid_lease', record_hex=value.hex() if value is not None else None)
    for operation in ('euid', 'open_dir', 'stat_dir', 'open_lease', 'stat_lease', 'read_lease',
                      'close_lease', 'open_boot', 'read_boot', 'clock', 'close_boot', 'close_dir'):
        model.prepare()
        model.record = lease()
        model.kernel_failures[operation] = -5
        row = model.event(13)
        require(not model.held() and stock.commands(row) == [stock.PROBE, *stock.DISABLE],
                'kernel error authorized a lease')
        record('kernel_failure', operation=operation)
    for key, value in (('dir_mode', 0o40755), ('dir_mode', 0o100700), ('dir_owner', 0),
                       ('lease_mode', 0o100644), ('lease_mode', 0o20600), ('lease_owner', 0),
                       ('lease_links', 2), ('lease_size', 51), ('lease_size', 53)):
        model.prepare()
        model.record = lease()
        model.metadata[key] = value
        row = model.event(13)
        require(not model.held() and stock.commands(row) == [stock.PROBE, *stock.DISABLE],
                'invalid file metadata authorized a lease')
        record('metadata_rejection', field=key, value=value)

    for state in (6, 7):
        model.prepare(state=state)
        model.record = lease()
        model.word(model.symbols['deferred'], 1)
        row = model.event(32)
        require(not model.held() and stock.commands(row) == [stock.PROBE, *stock.DISABLE],
                'lease blocked recovery power-off')
        require(row['events'].index(['system', stock.DISABLE[-1], 0]) <
                row['events'].index(['record_start', 1, -1, 0]), 'recovery power ordering changed')
        record('recovery', state=state, row=row)
    for entry in (0x870cc, 0x36cb8):
        model.prepare(state=6)
        model.record = lease()
        model.word(model.symbols['deferred'], 1)
        row = model.run(entry)
        require(not model.held() and stock.commands(row) == [stock.PROBE, *stock.DISABLE],
                'non-whitelisted shutdown caller was suppressed')
        record('unqualified_caller', entry=entry, row=row)

    for entry in (0x870cc, 0x36cb8, 'recovery'):
        model.prepare(state=6, probe=1)
        model.record = lease()
        model.word(model.symbols['deferred'], 1)
        row = model.event(32) if entry == 'recovery' else model.run(entry)
        require(not model.held() and stock.commands(row) == [stock.PROBE],
                'original skip gate left a revoked lease deferred')
        record('stop_probe_skip', entry=entry, row=row)

    for probe in (1, 256, -1, 32512):
        model.prepare(probe=probe)
        model.record = lease()
        row = model.event(13)
        require(not model.held() and stock.commands(row) == [stock.PROBE]+([] if probe == 1 else list(stock.DISABLE)),
                'lease deferred power-off without a successful controller-UP probe')
        record('controller_not_confirmed_up', probe=probe, row=row)

    for result in (0, 256):
        for failed in (None, *RESTORE):
            model.prepare()
            model.record = lease()
            model.event(13)
            model.probe = result
            if failed: model.failures[failed] = 256
            row = model.event(18)
            require(row['state'] == 8 and not model.held() and
                    stock.commands(row) == [stock.PROBE, *(RESTORE if result == 0 else stock.ENABLE)],
                    'resume cycled an up radio or failed to restore companion settings')
            record('resume', probe=result, failed_command=failed, row=row)

    for method in ('unlink', 'pending_event', 'renew', 'state_change', 'off_error', 'cancel_guard_error'):
        model.prepare()
        model.record = lease()
        model.event(13)
        if method == 'unlink': model.record = None
        if method == 'pending_event': model.trywait_results = [0]
        if method == 'state_change': model.word(stock.STATE, 8)
        if method == 'off_error':
            model.record = None
            model.system_results = [256, 0]
        if method == 'cancel_guard_error':
            model.record = None
            model.failures['cancelstate'] = 1
            model.sleep_action = lambda m: m.failures.clear()
        if method == 'renew':
            def renew(m):
                if m.now == 101000000000: m.record = lease(seconds=104)
            model.sleep_action = renew
        row = model.reconcile()
        if method in ('pending_event', 'state_change'):
            require(row['held'] == 1 and not row['commands'], 'queued event or companion window lost priority')
        else:
            require(not row['held'] and row['commands'] == [stock.DISABLE[-1]]*(2 if method == 'off_error' else 1),
                    'lease release handling failed')
            require(row['now_ns'] == {'unlink':100000000000, 'renew':104000000000,
                                     'off_error':101000000000, 'cancel_guard_error':101000000000}[method], 'release timing changed')
        record(method, row=row)

    return {'core_sha256': stock.CORE_SHA256, 'empty_overlay_original_cases': original['cases'],
            'empty_fixture_sha256': original['fixture_sha256'], 'radio_cases': len(rows),
            'radio_fixture_sha256': digest(json.dumps(rows, sort_keys=True).encode()), 'results': rows,
            'kernel_and_radio_services': 'synthetic', 'vendor_process_started': False,
            'controller_started_by_overlay': False, 'physical_qualification': False, 'installed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('core', type=Path)
    parser.add_argument('empty_overlay', type=Path)
    parser.add_argument('sensor_overlay', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.core, args.empty_overlay, args.sensor_overlay), indent=2))


if __name__ == '__main__':
    main()
