# SPDX-License-Identifier: Apache-2.0
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import zlib

from development.repair_streamer_poll import repair_source

ROOT = Path(__file__).resolve().parents[1]
ROW_BYTES = {1: 16, 2: 12, 3: 8}
FILES = {1: 'eeg.data', 2: 'accelerometer.data', 3: 'pulse.data'}


def decode_frames(raw, allow_partial_tail=False):
    frames = []
    while raw:
        if len(raw) < 96:
            if allow_partial_tail:
                return frames, len(raw)
            raise ValueError('partial header')
        header, raw = raw[:96], raw[96:]
        if header[:8] != b'DREEML01' or struct.unpack_from('!HH', header, 8) != (1, 96):
            raise ValueError('invalid protocol header')
        length, payload_crc, header_crc = struct.unpack_from('!III', header, 64)
        if length > 1048576:
            raise ValueError('invalid payload size')
        check = header[:72] + bytes(4) + header[76:]
        if zlib.crc32(check) != header_crc:
            raise ValueError('header CRC mismatch')
        if len(raw) < length:
            if allow_partial_tail:
                return frames, 96 + len(raw)
            raise ValueError('partial final payload')
        payload, raw = raw[:length], raw[length:]
        if zlib.crc32(payload) != payload_crc:
            raise ValueError('payload CRC mismatch')
        frame = {'type': header[12], 'stream': header[13], 'payload': payload,
                 'index': struct.unpack_from('!Q', header, 24)[0],
                 'count': struct.unpack_from('!I', header, 56)[0],
                 'dropped': struct.unpack_from('!Q', header, 76)[0]}
        if frame['type'] == 2 and len(payload) != frame['count'] * ROW_BYTES[frame['stream']]:
            raise ValueError('invalid row shape')
        frames.append(frame)
    return frames, 0


class StreamerSourcePinTests(unittest.TestCase):
    def test_unknown_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'unreviewed'):
            repair_source(b'int main(void) { return 0; }\n')


class StreamerPollTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = os.environ.get('DREEM_STREAMER_SOURCE')
        if not source:
            raise unittest.SkipTest('DREEM_STREAMER_SOURCE must identify the reviewed private source')
        raw = Path(source).read_bytes()
        patched = repair_source(raw)
        cls.tmp = tempfile.TemporaryDirectory(prefix='dreem-streamer-poll-')
        cls.directory = Path(cls.tmp.name)
        cls.builds = {}
        cls.observations = []
        for version, content in (('original', raw), ('repaired', patched)):
            path = cls.directory / (version + '.c')
            path.write_bytes(content)
            path.chmod(0o600)
            for name, cc, flags, runner in (
                ('host', 'cc', [], []),
                ('arm', 'arm-linux-gnueabihf-gcc', ['-static', '-marm', '-mcpu=cortex-a7'],
                 ['qemu-arm', '-cpu', 'cortex-a7']),
            ):
                if name == 'arm' and version == 'original':
                    continue  # One real baseline is enough to expose the busy loop.
                if not shutil.which(cc) or (runner and not shutil.which(runner[0])):
                    if name == 'host':
                        raise unittest.SkipTest('C compiler required')
                    continue
                exe = cls.directory / (version + '.' + name)
                command = [cc, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', *flags,
                           str(path), str(ROOT / 'tests/streamer_poll_harness.c'),
                           '-Wl,--wrap=poll', '-o', str(exe)]
                result = subprocess.run(command, capture_output=True, text=True)
                if result.returncode:
                    raise RuntimeError(result.stderr)
                cls.builds[version, name] = runner + [str(exe)]

    @classmethod
    def tearDownClass(cls):
        report = os.environ.get('DREEM_STREAMER_POLL_REPORT')
        if report:
            fd = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'runs': cls.observations}, stream, indent=2)
                stream.write('\n')
        cls.tmp.cleanup()

    def repaired_builds(self):
        return [name for version, name in self.builds if version == 'repaired']

    def run_streamer(self, name, *, version='repaired', mode='idle'):
        with tempfile.TemporaryDirectory(dir=self.directory) as directory:
            source = Path(directory)
            sizes = {1: 500, 2: 100, 3: 100} if mode == 'growing' else {1: 0, 2: 0, 3: 0}
            if mode == 'backpressure':
                sizes = {1: 10000, 2: 2000, 3: 2000}
            payloads = {sid: bytes((i * 37 + sid) % 256 for i in range(rows * ROW_BYTES[sid]))
                        for sid, rows in sizes.items()}
            for sid, filename in FILES.items():
                (source / filename).write_bytes(payloads[sid] if mode == 'backpressure' else b'')
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            seconds = 3 if mode == 'backpressure' else 2
            command = self.builds[version, name] + [
                '--bind', '127.0.0.1', '--port', str(port), '--source-dir', str(source),
                '--late-attach', 'from-start', '--run-seconds', str(seconds),
                '--poll-ms', '20', '--ring-bytes', '65536']
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            connections = []
            writer = None
            def connect():
                deadline = time.monotonic() + 1.5
                while True:
                    sock = socket.socket()
                    if mode == 'backpressure':
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
                    sock.settimeout(0.1)
                    try:
                        sock.connect(('127.0.0.1', port))
                        connections.append(sock)
                        return sock
                    except ConnectionRefusedError:
                        sock.close()
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.01)
            def receive(sock, deadline):
                data = bytearray()
                while time.monotonic() < deadline:
                    try:
                        block = sock.recv(65536)
                    except socket.timeout:
                        continue
                    except ConnectionResetError:
                        return bytes(data), True
                    if not block:
                        return bytes(data), True
                    data.extend(block)
                return bytes(data), False
            try:
                sock = connect()
                if mode == 'fin':
                    sock.close()
                    sock = connect()
                if mode == 'half_close':
                    sock.shutdown(socket.SHUT_WR)
                if mode == 'input':
                    sock.sendall(b'not-a-control-protocol')
                if mode == 'growing':
                    def produce():
                        for part in range(4):
                            for sid, filename in FILES.items():
                                block = len(payloads[sid]) // 4
                                with (source / filename).open('ab') as handle:
                                    handle.write(payloads[sid][part * block:(part + 1) * block])
                            time.sleep(0.04)
                        for filename in FILES.values():
                            with (source / filename).open('ab') as handle:
                                handle.write(b'\xff')  # Incomplete final row must stay untransmitted.
                    writer = threading.Thread(target=produce)
                    writer.start()
                if mode == 'backpressure':
                    time.sleep(1.3)
                wire, closed = receive(sock, time.monotonic() + seconds + 1)
                self.assertTrue(closed)
                stdout, stderr = process.communicate(timeout=2)
                self.assertEqual(process.returncode, 0, 'streamer did not exit cleanly')
                metrics = json.loads(stdout)
                frames, partial_bytes = decode_frames(wire, allow_partial_tail=mode == 'backpressure')
                self.assertTrue(any(frame['type'] == 1 for frame in frames), 'missing hello')
                self.observations.append({'build': name, 'version': version, 'case': mode,
                                          'metrics': metrics, 'validated_frames': len(frames),
                                          'partial_final_bytes': partial_bytes})
                if writer:
                    writer.join(timeout=1)
                    self.assertFalse(writer.is_alive())
                for sid, filename in FILES.items():
                    expected = payloads[sid] + (b'\xff' if mode == 'growing' else b'')
                    self.assertEqual((source / filename).read_bytes(), expected)
                return frames, metrics, payloads
            finally:
                for sock in connections:
                    sock.close()
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=3)
                if writer:
                    writer.join(timeout=1)

    def test_idle_connected_socket_no_longer_busy_polls(self):
        _, baseline, _ = self.run_streamer('host', version='original')
        self.assertGreater(baseline['poll_calls'], 1000)
        for name in self.repaired_builds():
            _, fixed, _ = self.run_streamer(name)
            self.assertLess(fixed['poll_calls'], 250)
            self.assertGreater(fixed['timeout_returns'], 25)
            self.assertGreater(baseline['poll_calls'], 20 * fixed['poll_calls'])

    def test_growing_native_files_keep_exact_rows_and_crc(self):
        for name in self.repaired_builds():
            frames, _, expected = self.run_streamer(name, mode='growing')
            for sid in FILES:
                data = sorted((f for f in frames if f['type'] == 2 and f['stream'] == sid),
                              key=lambda f: f['index'])
                self.assertTrue(data)
                index = 0
                for frame in data:
                    self.assertEqual(frame['index'], index)
                    index += frame['count']
                self.assertEqual(b''.join(f['payload'] for f in data), expected[sid])

    def test_connection_events_do_not_reintroduce_idle_spin(self):
        for name in self.repaired_builds():
            for mode in ('fin', 'half_close', 'input'):
                frames, metrics, _ = self.run_streamer(name, mode=mode)
                self.assertLess(metrics['poll_calls'], 250)
                self.assertTrue(any(frame['type'] == 3 for frame in frames))

    def test_backpressured_client_resumes_without_corrupting_frames(self):
        for name in self.repaired_builds():
            frames, metrics, expected = self.run_streamer(name, mode='backpressure')
            self.assertGreater(metrics['blocked_write_polls'], 0)
            data = [frame for frame in frames if frame['type'] == 2]
            self.assertTrue(data)
            for frame in data:
                size = ROW_BYTES[frame['stream']]
                self.assertEqual(frame['payload'], expected[frame['stream']][
                    frame['index'] * size:(frame['index'] + frame['count']) * size])


if __name__ == '__main__':
    unittest.main()
