# SPDX-License-Identifier: Apache-2.0
"""End-to-end public BlueZ ATT exchanges over connected packet sockets.

Set DREEM_BT_CAPTURE_HOST and optionally DREEM_BT_CAPTURE_ARM/ASAN to binaries
built by development/build_bluetooth_capture.py. No radio or device is opened.
The independent peer implements a small environmental-sensing GATT database.
"""
import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid


def le16(value):
    return struct.pack('<H', value)


class Sensor:
    def __init__(self, sock, *, payload=b'\xc4\x09', services=1, properties=0x12,
                 error=None, notifications=(), indication=False, silence=False):
        self.sock = sock
        self.payload, self.services, self.properties = payload, services, properties
        self.error, self.notifications = error, notifications
        self.indication, self.silence = indication, silence
        self.packets, self.failures, self.acknowledgements = [], [], 0
        self.subscribed = threading.Event()

    def send(self, packet):
        self.sock.sendall(packet)

    def att_error(self, opcode, handle, error=0x0a):
        self.send(bytes([1, opcode])+le16(handle)+bytes([error]))

    def serve(self):
        try:
            self.sock.settimeout(5)
            while True:
                packet = self.sock.recv(1024)
                if not packet:
                    return
                self.packets.append(packet)
                if self.silence:
                    continue
                opcode = packet[0]
                if self.error == 'disconnect':
                    return
                if opcode == 2:
                    assert packet == b'\x02\x17\x00'
                    self.send(b'\x03' if self.error == 'malformed_mtu' else b'\x03\x17\x00')
                elif opcode in (0x10, 0x08):
                    assert len(packet) == 7
                    start, end, uuid = struct.unpack('<HHH', packet[1:])
                    if self.error == 'malformed_discovery':
                        self.send(bytes([opcode+1]))
                        continue
                    entries = []
                    for index in range(self.services + int(self.error == 'service_change')):
                        base = 1+4*index
                        changed = self.error == 'service_change' and index == self.services
                        service_uuid = 0x1801 if changed else 0x181a
                        characteristic_uuid = 0x2a05 if changed else 0x2a6e
                        if opcode == 0x10 and uuid == 0x2800 and start <= base <= end:
                            entries.append(le16(base)+le16(base+3)+le16(service_uuid))
                        if opcode == 0x08 and uuid == 0x2803 and start <= base+1 <= end:
                            entries.append(le16(base+1)+bytes([0x20 if changed else self.properties])+
                                           le16(base+2)+le16(characteristic_uuid))
                    if entries:
                        self.send(bytes([opcode+1, len(entries[0])])+b''.join(entries))
                    else:
                        self.att_error(opcode, start)
                elif opcode == 0x04:
                    assert len(packet) == 5
                    start, end = struct.unpack('<HH', packet[1:])
                    handles = [4+4*i for i in range(self.services + int(self.error == 'service_change'))
                               if start <= 4+4*i <= end]
                    if handles:
                        self.send(b'\x05\x01'+b''.join(le16(h)+le16(0x2902) for h in handles))
                    else:
                        self.att_error(opcode, start)
                elif opcode in (0x0a, 0x0c):
                    assert len(packet) == (3 if opcode == 0x0a else 5)
                    handle = struct.unpack('<H', packet[1:3])[0]
                    offset = 0 if opcode == 0x0a else struct.unpack('<H', packet[3:])[0]
                    assert handle == 3
                    if self.error == 'read':
                        self.att_error(opcode, handle, 0x05)
                    elif offset > len(self.payload):
                        self.att_error(opcode, handle, 0x07)
                    else:
                        self.send(bytes([opcode+1])+self.payload[offset:offset+22])
                elif opcode == 0x12:
                    if self.error == 'service_change' and packet == b'\x12\x08\x00\x02\x00':
                        self.send(b'\x13')
                        continue
                    assert packet in (b'\x12\x04\x00\x01\x00', b'\x12\x04\x00\x02\x00')
                    if self.error in ('subscribe', 'subscribe_zero'):
                        self.att_error(opcode, 4, 0x03 if self.error == 'subscribe' else 0)
                        continue
                    self.send(b'\x13')
                    self.subscribed.set()
                    if self.error == 'short_notification':
                        self.send(b'\x1b')
                        continue
                    if self.error == 'service_change':
                        self.send(b'\x1d\x07\x00\x01\x00\x04\x00')
                        continue
                    for index, value in enumerate(self.notifications):
                        self.send(bytes([0x1d if self.indication else 0x1b])+le16(3)+value)
                        if self.indication and index < len(self.notifications)-1:
                            confirmation = self.sock.recv(1024)
                            assert confirmation == b'\x1e', confirmation
                            self.acknowledgements += 1
                elif opcode == 0x1e:
                    assert packet == b'\x1e'
                    self.acknowledgements += 1
                else:
                    raise AssertionError(f'unexpected ATT opcode {opcode:#x}')
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self.failures.append(repr(error))
        finally:
            self.sock.close()


class BluetoothCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.builds = []
        cls.connect_builds = []
        cls.work = tempfile.TemporaryDirectory(prefix='dreem-bt-connect-test-')
        cls.radio_settings = (os.environ.get('DREEM_BT_RADIO_CORE'), os.environ.get('DREEM_BT_RADIO_OVERLAY'))
        if any(cls.radio_settings) and not all(cls.radio_settings):
            raise RuntimeError('set both radio core and overlay paths')
        for kind in ('HOST', 'ARM', 'ASAN'):
            binary = os.environ.get('DREEM_BT_CAPTURE_'+kind)
            if binary:
                if not Path(binary).is_file():
                    raise RuntimeError('missing '+kind+' binary')
                cls.builds.append((kind, (['qemu-arm', '-cpu', 'cortex-a7'] if kind == 'ARM' else [])+[binary]))
                report = json.loads((Path(binary).parent/'build.json').read_text())
                obj = Path(cls.work.name)/(kind+'.o')
                executable = Path(cls.work.name)/kind
                compile_args = list(report['commands'][-2])
                compile_args[compile_args.index('-c')+1] = str(Path(__file__).with_name('bluetooth_connect_harness.c'))
                compile_args[compile_args.index('-o')+1] = str(obj)
                link_args = list(report['commands'][-1])
                link_args[link_args.index('-o')+1] = str(executable)
                link_args += [str(obj), '-Wl,--wrap=socket,--wrap=bind,--wrap=connect,--wrap=setsockopt,--wrap=getsockopt']
                for command in (compile_args, link_args):
                    result = subprocess.run(command, cwd=report['command_working_directory'],
                                            capture_output=True, text=True, timeout=60)
                    if result.returncode:
                        raise RuntimeError(result.stderr)
                cls.connect_builds.append((kind, cls.builds[-1][1][:-1]+[str(executable)]))
        if not cls.builds:
            raise unittest.SkipTest('set DREEM_BT_CAPTURE_HOST/ARM/ASAN to source-built binaries')

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def capture(self, *, sensor=None, mode='read', maximum=1, duration=1000,
                extra=(), interrupt=False, existing=False, connect_case=None, lease_case=None):
        results = []
        for name, command in (self.connect_builds if connect_case else self.builds):
            with self.subTest(build=name), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)/'values.jsonl'
                if existing:
                    output.write_text('preserve me\n')
                local, remote = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                peer = Sensor(remote, **(sensor or {}))
                args = ['--att-fd', str(local.fileno()), '--service', '181a',
                        '--characteristic', '2a6e', '--mode', mode, '--max-values', str(maximum),
                        '--duration-ms', str(duration), '--output', str(output), *extra]
                env = os.environ.copy()
                trace = Path(directory)/'connect.trace'
                lease_dir = Path(directory)/'radio-lease'
                radio_model = None
                if lease_case:
                    self.assertIsNotNone(connect_case, 'lease tests use the modeled direct connection path')
                    args += ['--radio-lease', str(lease_dir)]
                    if all(self.radio_settings):
                        sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'development'))
                        try:
                            from verify_bluetooth_radio_overlay import RadioPolicy
                        finally:
                            sys.path.pop(0)
                        radio_model = RadioPolicy(*(Path(p) for p in self.radio_settings))
                        radio_model.prepare()
                        radio_model.boot_text = Path('/proc/sys/kernel/random/boot_id').read_bytes()
                if connect_case:
                    args = args[2:]+['--local', '02:00:00:00:00:01', '--peer', '02:00:00:00:00:02',
                                    '--peer-type', 'random', '--security', 'medium']
                    env.update(DREEM_BT_CONNECT_CASE=connect_case, DREEM_BT_CONNECT_TRACE=str(trace),
                               DREEM_BT_CONNECT_FD=str(local.fileno()))
                process = subprocess.Popen(command+args, pass_fds=(local.fileno(),),
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                local.close()
                thread = threading.Thread(target=peer.serve, daemon=True)
                thread.start()
                try:
                    if lease_case in ('renew', 'failure', 'crash'):
                        lease_file = lease_dir/'lease'
                        deadline = time.monotonic()+3
                        while not lease_file.exists() and time.monotonic() < deadline and process.poll() is None:
                            time.sleep(0.005)
                        self.assertTrue(lease_file.exists(), name)
                        first = lease_file.read_bytes()
                        first_time = time.monotonic_ns()
                        self.assertEqual(len(first), 52)
                        self.assertEqual(first[:8], b'DRBTL001')
                        self.assertEqual(first[8:24], uuid.UUID(Path('/proc/sys/kernel/random/boot_id').read_text().strip()).bytes)
                        self.assertEqual(first[32:], b'02:00:00:00:00:02\0\0\0')
                        self.assertEqual(stat.S_IMODE(lease_dir.stat().st_mode), 0o700)
                        self.assertEqual(stat.S_IMODE(lease_file.stat().st_mode), 0o600)
                        end, ns = struct.unpack_from('<II', first, 24)
                        self.assertLess(first_time, end*1000000000+ns)
                        self.assertLessEqual(end*1000000000+ns-first_time, 3000000000)
                        if radio_model:
                            radio_model.record, radio_model.now = first, first_time
                            radio_model.event(13)
                            self.assertEqual(radio_model.held(), 1)
                        if lease_case == 'renew':
                            deadline = time.monotonic()+2
                            while time.monotonic() < deadline:
                                renewed = lease_file.read_bytes()
                                if renewed != first: break
                                time.sleep(0.01)
                            else:
                                self.fail('capture did not renew its radio lease')
                            second, ns2 = struct.unpack_from('<II', renewed, 24)
                            self.assertGreater(second*1000000000+ns2, end*1000000000+ns)
                            if radio_model:
                                radio_model.record, radio_model.now = renewed, time.monotonic_ns()
                                self.assertEqual(radio_model.call(radio_model.symbols['dreem_radio_lease_active']), 1)
                        elif lease_case == 'failure':
                            lease_dir.chmod(0o755)
                        elif lease_case == 'crash':
                            process.kill()
                    if interrupt:
                        if connect_case:
                            deadline = time.monotonic()+3
                            while time.monotonic() < deadline:
                                if trace.exists() and 'connect' in trace.read_text().splitlines():
                                    break
                                time.sleep(0.005)
                            else:
                                self.fail('connection was not attempted')
                        else:
                            self.assertTrue(peer.subscribed.wait(3), name)
                        process.send_signal(signal.SIGTERM)
                    stdout, stderr = process.communicate(timeout=6)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()
                    thread.join(timeout=6)
                    remote.close()
                self.assertFalse(thread.is_alive(), name)
                self.assertFalse(peer.failures, (name, peer.failures))
                self.assertEqual(stdout, '', (name, stdout))
                self.assertNotIn('Sanitizer', stderr, (name, stderr))
                self.assertNotIn('runtime error:', stderr, (name, stderr))
                if lease_case:
                    if lease_case == 'crash':
                        self.assertTrue((lease_dir/'lease').exists(), 'killed owner unexpectedly cleaned its lease')
                    else:
                        self.assertFalse((lease_dir/'lease').exists(), 'capture leaked its radio lease')
                    self.assertFalse((lease_dir/'lease.tmp').exists())
                    if radio_model and lease_case in ('renew', 'failure', 'crash'):
                        radio_model.record = ((lease_dir/'lease').read_bytes() if lease_case == 'crash' else None)
                        radio_model.now = time.monotonic_ns()
                        released = radio_model.reconcile()
                        self.assertEqual(released['held'], 0)
                        self.assertEqual(released['commands'], ['/usr/bin/btmgmt -i hci0 power off'])
                if existing:
                    self.assertEqual(output.read_text(), 'preserve me\n')
                    rows = []
                else:
                    rows = [json.loads(line) for line in output.read_text().splitlines()] if output.exists() else []
                    if rows:
                        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
                        self.assertEqual(rows[0]['transport'], 'bluetooth' if connect_case else 'simulation')
                        self.assertEqual(rows[0]['continuity'], 'unknown')
                        self.assertEqual(rows[0]['calibration'], 'unknown')
                        if lease_case == 'crash':
                            self.assertEqual(process.returncode, -signal.SIGKILL)
                            self.assertNotEqual(rows[-1]['type'], 'end')
                            results.append((process.returncode, rows, peer))
                            continue
                        self.assertEqual(rows[-1]['type'], 'end',
                                         (name, process.returncode, stderr, [p.hex() for p in peer.packets]))
                        self.assertEqual(rows[-1]['status'], process.returncode)
                        values = [r for r in rows if r['type'] == 'value']
                        self.assertEqual(rows[-1]['values'], len(values))
                        self.assertEqual([r['sequence'] for r in values], list(range(len(values))))
                        self.assertEqual([r['receive_monotonic_ns'] for r in values],
                                         sorted(r['receive_monotonic_ns'] for r in values))
                results.append((process.returncode, rows, peer))
                if connect_case:
                    peer.connect_trace = trace.read_text().splitlines()
        return results

    def test_reads_preserve_raw_values_and_long_read_boundaries(self):
        for payload in (b'', b'\xc4\x09', bytes(range(44)), bytes(range(256))*2):
            for status, rows, peer in self.capture(sensor={'payload': payload}):
                self.assertEqual(status, 0)
                self.assertEqual([r['hex'] for r in rows if r['type'] == 'value'], [payload.hex()])
                self.assertEqual(rows[-1]['reason'], 'read_complete')
                self.assertFalse(any(p[0] == 0x12 for p in peer.packets))

    def test_notifications_and_indications(self):
        values = (b'\xc4\x09', b'\xc5\x09')
        for indication in (False, True):
            for status, rows, peer in self.capture(mode='notify', maximum=2, sensor={
                    'notifications': values, 'indication': indication,
                    'properties': 0x22 if indication else 0x12}):
                self.assertEqual(status, 0)
                self.assertEqual(rows[-1]['reason'], 'value_limit')
                self.assertEqual([r['hex'] for r in rows if r['type'] == 'value'], [v.hex() for v in values])
                if indication:
                    self.assertGreaterEqual(peer.acknowledgements, 1)

    def test_missing_and_ambiguous_characteristics(self):
        for count, reason in ((0, 'characteristic_missing'), (2, 'ambiguous_characteristic')):
            for status, rows, peer in self.capture(sensor={'services': count}):
                self.assertEqual(status, 1)
                self.assertEqual(rows[-1]['reason'], reason)
                self.assertFalse(any(p[0] in (0x0a, 0x0c, 0x12) for p in peer.packets))

    def test_read_and_subscription_errors(self):
        for error, mode, reason, code in (('read', 'read', 'read_error', 5),
                                         ('subscribe', 'notify', 'subscribe_error', 3),
                                         ('subscribe_zero', 'notify', 'subscribe_error', 14)):
            for status, rows, _ in self.capture(mode=mode, sensor={'error': error}):
                self.assertEqual(status, 1)
                self.assertEqual(rows[-1]['reason'], reason)
                self.assertEqual(rows[-1]['att_error'], code)

    def test_properties_are_enforced(self):
        for status, rows, peer in self.capture(sensor={'properties': 0x10}):
            self.assertEqual(status, 1)
            self.assertEqual(rows[-1]['reason'], 'unsupported_operation')
            self.assertFalse(any(p[0] in (0x0a, 0x0c, 0x12) for p in peer.packets))

    def test_oversized_value_and_changed_service_fail(self):
        for status, rows, _ in self.capture(sensor={'payload': bytes(513)}):
            self.assertEqual(status, 1)
            self.assertEqual(rows[-1]['values'], 0)
            self.assertEqual(rows[-1]['reason'], 'read_error')
            self.assertEqual(rows[-1]['att_error'], 13)
        for status, rows, _ in self.capture(mode='notify', sensor={'error': 'service_change'}):
            self.assertEqual(status, 1)
            self.assertEqual(rows[-1]['reason'], 'service_changed')
            self.assertEqual(rows[-1]['values'], 0)

    def test_short_notification_fails_without_a_value(self):
        for status, rows, _ in self.capture(mode='notify', sensor={'error': 'short_notification'}):
            self.assertEqual(status, 1)
            self.assertEqual(rows[-1]['reason'], 'malformed_notification')
            self.assertEqual(rows[-1]['values'], 0)

    def test_timeout_and_successful_duration_limit(self):
        for status, rows, _ in self.capture(sensor={'silence': True}, duration=150):
            self.assertEqual(status, 1)
            self.assertEqual(rows[-1]['reason'], 'timeout')
        for status, rows, _ in self.capture(mode='notify', maximum=5, duration=250,
                                           sensor={'notifications': (b'\x01',)}):
            self.assertEqual(status, 0)
            self.assertEqual(rows[-1]['reason'], 'duration')
            self.assertEqual(rows[-1]['values'], 1)

    def test_radio_lease_renews_then_releases_after_capture(self):
        for status, rows, _ in self.capture(mode='notify', maximum=5, duration=2500,
                sensor={'notifications': (b'\x01',)}, connect_case='immediate', lease_case='renew'):
            self.assertEqual(status, 0)
            self.assertEqual(rows[-1]['reason'], 'duration')

    def test_radio_lease_failure_stops_capture_and_cleans_up(self):
        for status, rows, _ in self.capture(mode='notify', maximum=5, duration=4000,
                sensor={'notifications': (b'\x01',)}, connect_case='immediate', lease_case='failure'):
            self.assertEqual(status, 1)
            self.assertEqual(rows[-1]['reason'], 'radio_lease_error')

    def test_radio_lease_crashed_owner_leaves_only_expiring_request(self):
        for status, rows, _ in self.capture(mode='notify', maximum=5, duration=4000,
                sensor={'notifications': (b'\x01',)}, connect_case='immediate', lease_case='crash'):
            self.assertEqual(status, -signal.SIGKILL)

    def test_radio_lease_cleanup_on_connection_error_and_signal(self):
        for status, rows, _ in self.capture(connect_case='connect', lease_case='cleanup'):
            self.assertEqual(status, 1)
            self.assertEqual(rows[-1]['reason'], 'connect_error')
        for status, rows, _ in self.capture(connect_case='stall', lease_case='cleanup', interrupt=True):
            self.assertEqual(status, 128+signal.SIGTERM)
            self.assertEqual(rows[-1]['reason'], 'interrupted')

    def test_disconnect_and_malformed_discovery(self):
        for error in ('disconnect', 'malformed_discovery'):
            for status, rows, _ in self.capture(sensor={'error': error}, duration=250):
                self.assertEqual(status, 1)
                self.assertEqual(rows[-1]['values'], 0)
                self.assertIn(rows[-1]['reason'], ('disconnected', 'discovery_error', 'timeout'))

    def test_interrupt(self):
        for status, rows, _ in self.capture(mode='notify', maximum=10, interrupt=True):
            self.assertEqual(status, 143)
            self.assertEqual(rows[-1]['reason'], 'interrupted')

    def test_existing_output_is_preserved(self):
        for status, _, peer in self.capture(existing=True):
            self.assertEqual(status, 1)
            self.assertEqual(peer.packets, [])

    def test_invalid_options_do_not_contact_peer(self):
        for extra in (('--mode', 'notify'), ('--service', 'bad-uuid'), ('unexpected',)):
            for status, rows, peer in self.capture(extra=extra):
                self.assertEqual(status, 2)
                self.assertEqual(rows, [])
                self.assertEqual(peer.packets, [])

    def test_native_connection_setup_with_simulated_socket_calls(self):
        for scenario in ('immediate', 'pending'):
            for status, rows, peer in self.capture(connect_case=scenario):
                self.assertEqual(status, 0)
                self.assertEqual(rows[-1]['reason'], 'read_complete')
                expected = ['socket', 'bind', 'security', 'connect']
                self.assertEqual(peer.connect_trace, expected+(['completion'] if scenario == 'pending' else []))
        for scenario in ('socket', 'bind', 'security', 'connect', 'soerror', 'stall'):
            for status, rows, peer in self.capture(connect_case=scenario, duration=150):
                self.assertEqual(status, 1)
                self.assertEqual(rows[-1]['reason'], 'timeout' if scenario == 'stall' else 'connect_error')
                self.assertEqual(peer.packets, [])
        for status, rows, peer in self.capture(connect_case='stall', interrupt=True):
            self.assertEqual(status, 143)
            self.assertEqual(rows[-1]['reason'], 'interrupted')
            self.assertEqual(peer.packets, [])


if __name__ == '__main__':
    unittest.main()
