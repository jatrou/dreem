# SPDX-License-Identifier: Apache-2.0
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import struct
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SIZES = {1: 16, **dict.fromkeys((2, 3, 15, 23, 28, 32, 33), 0),
         **dict.fromkeys((18, 19, 24, 34), 1),
         **dict.fromkeys((13, 14, 16, 17, 20, 21, 22, 25, 26, 30, 31, 35, 36), 4),
         27: 8, 29: 8, 37: 12}


def frame(counter, code, value=None):
    if value is None:
        payload = bytes((code+i*19) % 256 for i in range(SIZES[code]))
    else:
        payload = struct.pack('<I', value)
    return struct.pack('<IB', counter, code)+payload


class AlgoHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.commands = [[str(ROOT/'development/build/algo_health.host')]]
        if not Path(cls.commands[0][0]).is_file():
            raise unittest.SkipTest('run sh development/build.sh first')
        arm = ROOT/'development/build/algo_health.arm'
        if shutil.which('qemu-arm') and arm.is_file():
            cls.commands.append(['qemu-arm', '-cpu', 'cortex-a7', str(arm)])

    def run_feature(self, command, path, *args, status=0):
        p = subprocess.run(command+list(args)+[str(path)], capture_output=True, text=True, timeout=4)
        self.assertEqual(p.returncode, status, p.stderr)
        return [json.loads(line) for line in p.stdout.splitlines()], p.stderr

    def test_health_recovery_and_counter_discontinuity(self):
        events = [(1, 30, 1), (1, 31, 0), (2, 18, None), (3, 30, 2), (4, 31, 1),
                  (5, 28, None), (6, 30, 0), (2, 31, 1), (2, 28, None),
                  (0xffffffff, 30, 0xffffffff), (0, 30, 1)]
        expected = [('good', 'unknown', 0), ('good', 'bad', 0), ('good', 'bad', 0),
                    ('unknown', 'bad', 0), ('unknown', 'good', 0), ('unknown', 'unknown', 1),
                    ('bad', 'unknown', 1), ('unknown', 'good', 2), ('unknown', 'unknown', 3),
                    ('unknown', 'unknown', 3), ('good', 'unknown', 4)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'algo.data'
            frames = [frame(*e) for e in events]
            path.write_bytes(b''.join(frames))
            original = hashlib.sha256(path.read_bytes()).hexdigest()
            for command in self.commands:
                rows, _ = self.run_feature(command, path)
                self.assertEqual([(r['motion_health'], r['optical_health'], r['segment'])
                                  for r in rows[:-1]], expected)
                self.assertEqual([i for i, r in enumerate(rows[:-1]) if r['counter_decreased']], [7, 10])
                offset = 0
                for row, data, event in zip(rows, frames, events):
                    self.assertEqual(row['byte_offset'], offset)
                    self.assertEqual(row['sample_counter'], event[0])
                    self.assertEqual(row['payload_hex'], data[5:].hex())
                    offset += len(data)
                self.assertEqual(rows[3]['health_value'], 2)
                self.assertEqual(rows[5]['type'], 'recovery')
                self.assertEqual(rows[-1], {'type': 'end', 'events_consumed': len(events),
                                           'bytes_consumed': offset, 'bytes_remaining': 0})
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original)

    def test_all_sizes_cross_buffer_boundaries(self):
        frames = [frame(i, code) for i, code in enumerate(sorted(SIZES)*60)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'algo.data'
            path.write_bytes(b''.join(frames))
            for command in self.commands:
                rows, _ = self.run_feature(command, path)
                self.assertEqual(len(rows), len(frames)+1)
                for i, (row, data) in enumerate(zip(rows, frames)):
                    self.assertEqual((row['sample_counter'], row['code'], row['payload_hex']),
                                     (i, data[4], data[5:].hex()))
                self.assertEqual(rows[-1]['bytes_remaining'], 0)

    def test_empty_and_every_incomplete_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'algo.data'
            for command in self.commands:
                path.write_bytes(b'')
                rows, _ = self.run_feature(command, path)
                self.assertEqual(rows, [{'type': 'end', 'events_consumed': 0,
                                         'bytes_consumed': 0, 'bytes_remaining': 0}])
                prefix = frame(17, 30, 1)
                for code in SIZES:
                    tail = frame(18, code)
                    for length in range(1, len(tail)):
                        path.write_bytes(prefix+tail[:length])
                        rows, _ = self.run_feature(command, path)
                        self.assertEqual(len(rows), 2, (command, code, length))
                        self.assertEqual(rows[-1]['bytes_consumed'], len(prefix))
                        self.assertEqual(rows[-1]['bytes_remaining'], length)

    def test_unknown_codes_fail_without_resynchronizing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'algo.data'
            for command in self.commands:
                for code in set(range(256))-SIZES.keys():
                    path.write_bytes(frame(0, 30, 1)+struct.pack('<IB', 1, code)+frame(2, 31, 1))
                    rows, error = self.run_feature(command, path, status=1)
                    self.assertEqual(len(rows), 1)
                    self.assertIn(f'Unsupported event code {code} at byte 9', error)

    def test_follow_retains_partial_header_and_payload(self):
        for command in self.commands:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'algo.data'
                first, second = frame(0, 30, 1), frame(5, 31, 0)
                path.write_bytes(first+second[:3])
                p = subprocess.Popen(command+['--follow-seconds', '0.7', str(path)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(p.stdout, selectors.EVENT_READ)
                        self.assertTrue(selector.select(timeout=2), 'no initial event')
                        self.assertEqual(json.loads(p.stdout.readline())['code'], 30)
                    with path.open('ab') as f:
                        f.write(second[3:7])
                    time.sleep(0.15)
                    with path.open('ab') as f:
                        f.write(second[7:])
                    out, err = p.communicate(timeout=4)
                    self.assertEqual(p.returncode, 0, err)
                    rows = [json.loads(line) for line in out.splitlines()]
                    self.assertEqual(rows[0]['code'], 31)
                    self.assertEqual(rows[0]['optical_health'], 'bad')
                    self.assertEqual(rows[-1]['events_consumed'], 2)
                    self.assertEqual(rows[-1]['bytes_remaining'], 0)
                finally:
                    if p.poll() is None:
                        p.kill()
                        p.communicate()

    def test_follow_stops_on_replacement_removal_and_partial_truncation(self):
        for command in self.commands:
            for mutation in ('replace', 'remove', 'truncate_partial'):
                with self.subTest(command=command, mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp)/'algo.data'
                    first = frame(0, 30, 1)
                    path.write_bytes(first+frame(5, 1)[:10])
                    p = subprocess.Popen(command+['--follow-seconds', '2', str(path)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    try:
                        with selectors.DefaultSelector() as selector:
                            selector.register(p.stdout, selectors.EVENT_READ)
                            self.assertTrue(selector.select(timeout=1.5), 'no initial event')
                            self.assertEqual(json.loads(p.stdout.readline())['code'], 30)
                        if mutation == 'remove':
                            path.unlink()
                        elif mutation == 'replace':
                            replacement = Path(tmp)/'replacement'
                            replacement.write_bytes(path.read_bytes())
                            replacement.replace(path)
                        else:
                            with path.open('r+b') as f:
                                f.truncate(len(first)+1)  # Above the consumed offset, still a shrink.
                        _, err = p.communicate(timeout=4)
                        self.assertEqual(p.returncode, 1, err)
                        self.assertIn('removed, replaced, or truncated', err)
                    finally:
                        if p.poll() is None:
                            p.kill()
                            p.communicate()

    def test_rejects_nonregular_inputs_and_invalid_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root/'regular'
            path.write_bytes(b'')
            (root/'link').symlink_to(path)
            os.mkfifo(root/'fifo')
            for command in self.commands:
                for candidate in (root/'link', root/'fifo', root, Path('/dev/null')):
                    self.run_feature(command, candidate, status=1)
                for value in ('', '0', '-1', 'nan', 'inf', '86401', 'x'):
                    self.run_feature(command, path, '--follow-seconds', value, status=2)
                self.run_feature(command, path, '--follow-seconds', '1', '--follow-seconds', '1', status=2)


if __name__ == '__main__':
    unittest.main()
