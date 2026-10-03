# SPDX-License-Identifier: Apache-2.0
import hashlib
import json
import math
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


class MotionQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.commands = [[str(ROOT/'development/build/motion_quality.host')]]
        if not Path(cls.commands[0][0]).is_file():
            raise unittest.SkipTest('run sh development/build.sh first')
        arm = ROOT/'development/build/motion_quality.arm'
        if shutil.which('qemu-arm') and arm.is_file():
            cls.commands.append(['qemu-arm', '-cpu', 'cortex-a7', str(arm)])

    def run_feature(self, command, path, *args):
        result = subprocess.run(command+list(args)+[str(path)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_stationary_and_alternating_motion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'accelerometer.data'
            path.write_bytes(struct.pack('<3f', 0, 0, 1)*50 + b''.join(
                struct.pack('<3f', (-1)**i, 0, 1) for i in range(50)) + b'\x01\x02')
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            results = []
            for command in self.commands:
                data = self.run_feature(command, path)
                self.assertEqual([v['sample_start'] for v in data[:-1]], [0, 50])
                stationary, moving, end = data
                self.assertEqual(stationary['mean_axes'], [0, 0, 1])
                self.assertEqual(stationary['vector_rms'], 1)
                self.assertEqual(stationary['dynamic_rms'], 0)
                self.assertEqual(stationary['step_rms'], 0)
                self.assertEqual(stationary['step_pairs'], 49)
                self.assertAlmostEqual(moving['vector_rms'], math.sqrt(2), places=8)
                self.assertAlmostEqual(moving['dynamic_rms'], 1, places=8)
                self.assertEqual(moving['step_rms'], 2)
                self.assertEqual(moving['max_step'], 2)
                self.assertEqual(end['rows_consumed'], 100)
                self.assertEqual(end['bytes_remaining'], 2)
                results.append(data)
            self.assertTrue(all(v == results[0] for v in results))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_invalid_gaps_and_zero_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'accelerometer.data'
            invalid = struct.pack('<3f', float('nan'), 0, 1)
            path.write_bytes(struct.pack('<3f', 0, 0, 1) + invalid + struct.pack('<3f', 0, 0, 3)*2 +
                             invalid*46 + invalid*50 + b'\0'*600)
            for command in self.commands:
                first, missing, zero, _ = self.run_feature(command, path)
                self.assertEqual((first['valid_rows'], first['invalid_rows'], first['step_pairs']), (3, 47, 1))
                self.assertEqual(first['step_rms'], 0)  # No artificial step across the NaN gap.
                self.assertAlmostEqual(first['mean_axes'][2], 7/3, places=8)
                self.assertAlmostEqual(first['dynamic_rms'], math.sqrt(8/9), places=8)
                for key in ('mean_axes', 'vector_rms', 'dynamic_rms', 'step_rms', 'max_step'):
                    self.assertIsNone(missing[key])
                self.assertEqual(zero['zero_rows'], 50)
                self.assertEqual(zero['valid_rows'], 50)  # Zero can be physical or a recorder placeholder.
                self.assertEqual(zero['vector_rms'], 0)

    def test_short_window_and_nonfinite_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'accelerometer.data'
            for command in self.commands:
                path.write_bytes(struct.pack('<3f', 1, 2, 3)*49+b'abc')
                data = self.run_feature(command, path)
                self.assertEqual(len(data), 1)
                self.assertEqual(data[0]['unreported_window_rows'], 49)
                self.assertEqual(data[0]['bytes_remaining'], 3)
                path.write_bytes(struct.pack('<3f', 1e38, -1e38, 1e38)*49+
                                 struct.pack('<3f', 0, float('inf'), 0))
                data = self.run_feature(command, path)
                self.assertEqual(data[0]['invalid_rows'], 1)
                self.assertTrue(math.isfinite(data[0]['vector_rms']))
                self.assertEqual(data[0]['dynamic_rms'], 0)

    def test_follow_joins_partial_rows(self):
        for command in self.commands:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'accelerometer.data'
                rows = struct.pack('<3f', 1, 2, 3)*100
                path.write_bytes(rows[:599])
                p = subprocess.Popen(command+['--follow-seconds', '0.7', str(path)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    time.sleep(0.15)
                    with path.open('ab') as f:
                        f.write(rows[599:])
                    out, err = p.communicate(timeout=5)
                    self.assertEqual(p.returncode, 0, err)
                    data = [json.loads(line) for line in out.splitlines()]
                    self.assertEqual([v['sample_start'] for v in data[:-1]], [0, 50])
                    self.assertEqual(data[-1]['rows_consumed'], 100)
                finally:
                    if p.poll() is None:
                        p.kill()
                        p.communicate()

    def test_follow_rejects_timeline_changes(self):
        for command in self.commands:
            for mutation in ('truncate', 'replace', 'remove'):
                with self.subTest(command=command, mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp)/'accelerometer.data'
                    path.write_bytes(struct.pack('<3f', 1, 2, 3)*50)
                    p = subprocess.Popen(command+['--follow-seconds', '2', str(path)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    try:
                        with selectors.DefaultSelector() as selector:
                            selector.register(p.stdout, selectors.EVENT_READ)
                            self.assertTrue(selector.select(timeout=1.5), 'no first window')
                            self.assertEqual(json.loads(p.stdout.readline())['sample_start'], 0)
                        if mutation == 'truncate':
                            path.write_bytes(b'')
                        elif mutation == 'replace':
                            replacement = Path(tmp)/'replacement'
                            replacement.write_bytes(b'\0'*600)
                            replacement.replace(path)
                        else:
                            path.unlink()
                        _, err = p.communicate(timeout=5)
                        self.assertEqual(p.returncode, 1, err)
                        self.assertIn('removed, replaced, or truncated', err)
                    finally:
                        if p.poll() is None:
                            p.kill()
                            p.communicate()

    def test_rejects_nonregular_inputs_and_invalid_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root/'regular'
            path.write_bytes(b'')
            (root/'link').symlink_to(path)
            os.mkfifo(root/'fifo')
            for command in self.commands:
                for candidate in (root/'link', root/'fifo', root, Path('/dev/null')):
                    p = subprocess.run(command+[str(candidate)], capture_output=True, timeout=3)
                    self.assertEqual(p.returncode, 1)
                for value in ('0', '-1', 'nan', 'inf', '86401', 'x'):
                    p = subprocess.run(command+['--follow-seconds', value, str(path)], capture_output=True, timeout=3)
                    self.assertEqual(p.returncode, 2)


if __name__ == '__main__':
    unittest.main()
