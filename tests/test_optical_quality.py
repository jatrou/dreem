# SPDX-License-Identifier: Apache-2.0
import hashlib
import json
import math
import os
from pathlib import Path
import random
import selectors
import shutil
import struct
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
ADC_MAX = 0x3ffff


def encode(rows):
    return b''.join(struct.pack('<II', *row) for row in rows)


class OpticalQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.commands = [[str(ROOT/'development/build/optical_quality.host')]]
        if not Path(cls.commands[0][0]).is_file():
            raise unittest.SkipTest('run sh development/build.sh first')
        arm = ROOT/'development/build/optical_quality.arm'
        if shutil.which('qemu-arm') and arm.is_file():
            cls.commands.append(['qemu-arm', '-cpu', 'cortex-a7', str(arm)])

    def run_feature(self, command, path, *args):
        p = subprocess.run(command+list(args)+[str(path)], capture_output=True,
                           text=True, timeout=5)
        self.assertEqual(p.returncode, 0, p.stderr)
        return [json.loads(line) for line in p.stdout.splitlines()]

    def test_constants_alternation_and_correlation(self):
        rows = [(100, 200)]*50 + [(100 + (-1)**i*10, 200 - (-1)**i*20) for i in range(50)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'pulse.data'
            path.write_bytes(encode(rows))
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            outputs = [self.run_feature(command, path) for command in self.commands]
            for data in outputs:
                self.assertEqual([r['sample_start'] for r in data[:-1]], [0, 50])
                flat, varying, end = data
                self.assertEqual(flat['channels'][0]['mean'], 100)
                self.assertEqual(flat['channels'][0]['ac_rms'], 0)
                self.assertIsNone(flat['red_ir_correlation'])
                self.assertEqual(varying['red_ir_correlation'], -1)
                self.assertEqual([c['ac_rms'] for c in varying['channels']], [10, 20])
                self.assertEqual([c['ac_to_dc'] for c in varying['channels']], [0.1, 0.1])
                self.assertEqual([c['peak_to_peak'] for c in varying['channels']], [20, 40])
                self.assertEqual(end['rows_consumed'], 100)
                self.assertEqual(end['bytes_remaining'], 0)
                self.assertEqual(varying['sensor_health'], 'unverified')
                self.assertFalse(varying['continuous_acquisition_verified'])
            self.assertTrue(all(data == outputs[0] for data in outputs))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_unsigned_range_validation_does_not_mask_or_drop_zero_rows(self):
        rows = [(0, 0), (ADC_MAX, ADC_MAX), (0x40000, 1), (1, 0x800000),
                (0xffffffff, 0xffffffff)] + [(100, 200)]*45
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'pulse.data'; path.write_bytes(encode(rows))
            for command in self.commands:
                data, end = self.run_feature(command, path)
                self.assertEqual(data['in_range_rows'], 47)
                self.assertEqual(data['out_of_range_rows'], 3)
                self.assertEqual(data['zero_rows'], 1)
                self.assertEqual([c['out_of_range_values'] for c in data['channels']], [2, 2])
                for c in data['channels']:
                    self.assertEqual(c['zero_values'], 1)
                    self.assertEqual(c['ceiling_values'], 1)
                    self.assertEqual((c['minimum'], c['maximum']), (0, ADC_MAX))
                self.assertEqual(end['rows_consumed'], 50)

    def test_empty_invalid_and_partial_windows_have_explicit_nulls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'pulse.data'
            for command in self.commands:
                path.write_bytes(b'')
                self.assertEqual(len(self.run_feature(command, path)), 1)
                path.write_bytes(encode([(0x40000, 1)]*50+[(0, 0)]*3)+b'abc')
                invalid, partial, end = self.run_feature(command, path)
                self.assertEqual(invalid['in_range_rows'], 0)
                self.assertEqual(invalid['out_of_range_rows'], 50)
                self.assertIsNone(invalid['red_ir_correlation'])
                for channel in invalid['channels']:
                    for field in ('minimum', 'maximum', 'mean', 'ac_rms', 'peak_to_peak', 'ac_to_dc'):
                        self.assertIsNone(channel[field])
                self.assertFalse(partial['complete_window'])
                self.assertEqual(partial['rows'], 3)
                self.assertEqual(partial['zero_rows'], 3)
                self.assertEqual(partial['channels'][0]['mean'], 0)
                self.assertIsNone(partial['channels'][0]['ac_to_dc'])
                self.assertEqual(end['rows_consumed'], 53)
                self.assertEqual(end['bytes_remaining'], 3)

    def test_seeded_windows_match_independent_two_pass_statistics(self):
        rng = random.Random(20261003)
        rows = [(rng.randrange(ADC_MAX+1), rng.randrange(ADC_MAX+1)) for _ in range(503)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'pulse.data'; path.write_bytes(encode(rows))
            for command in self.commands:
                output = self.run_feature(command, path)
                self.assertEqual(len(output), 12)
                for start, record in zip(range(0, len(rows), 50), output):
                    block = rows[start:start+50]
                    means = [sum(pair[c] for pair in block)/len(block) for c in range(2)]
                    variance = [sum((pair[c]-means[c])**2 for pair in block)/len(block) for c in range(2)]
                    covariance = sum((a-means[0])*(b-means[1]) for a,b in block)/len(block)
                    self.assertEqual(record['rows'], len(block))
                    for c in range(2):
                        self.assertTrue(math.isclose(record['channels'][c]['mean'], means[c], rel_tol=1e-9))
                        self.assertTrue(math.isclose(record['channels'][c]['ac_rms'], math.sqrt(variance[c]), rel_tol=1e-9))
                    self.assertTrue(math.isclose(record['red_ir_correlation'], covariance/math.sqrt(variance[0]*variance[1]), rel_tol=1e-9, abs_tol=1e-10))

    def test_follow_joins_partial_rows_and_emits_final_partial_window(self):
        for command in self.commands:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'pulse.data'; raw = encode([(100, 200)]*103)
                path.write_bytes(raw[:399])
                p = subprocess.Popen(command+['--follow-seconds', '0.6', str(path)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    time.sleep(0.15)
                    with path.open('ab') as f: f.write(raw[399:]+b'xy')
                    out, err = p.communicate(timeout=5)
                    self.assertEqual(p.returncode, 0, err)
                    data = [json.loads(line) for line in out.splitlines()]
                    self.assertEqual([v['sample_start'] for v in data[:-1]], [0, 50, 100])
                    self.assertEqual([v['rows'] for v in data[:-1]], [50, 50, 3])
                    self.assertEqual(data[-1]['rows_consumed'], 103)
                    self.assertEqual(data[-1]['bytes_remaining'], 2)
                finally:
                    if p.poll() is None: p.kill(); p.communicate()

    def test_default_mode_stops_at_initial_file_extent(self):
        for command in self.commands:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'pulse.data'; path.write_bytes(encode([(100, 200)]*5000))
                # A small output pipe holds the child before it can finish all
                # windows, making the append occur during analysis, not after it.
                p = subprocess.Popen(command+[str(path)], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, pipesize=4096)
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(p.stdout, selectors.EVENT_READ)
                        self.assertTrue(selector.select(timeout=1.5), 'no first window')
                    with path.open('ab') as f: f.write(encode([(1, 2)]*2))
                    out, err = p.communicate(timeout=5)
                    self.assertEqual(p.returncode, 0, err)
                    data = [json.loads(line) for line in out.splitlines()]
                    self.assertEqual(len(data), 101)
                    self.assertEqual(data[-1]['rows_consumed'], 5000)
                    self.assertEqual(data[-1]['initial_bytes'], 40000)
                    self.assertEqual(data[-1]['bytes_remaining'], 16)
                    self.assertFalse(data[-1]['follow'])
                finally:
                    if p.poll() is None: p.kill(); p.communicate()

    def test_follow_rejects_observed_timeline_changes(self):
        for command in self.commands:
            for mutation in ('truncate', 'replace', 'remove'):
                with self.subTest(command=command, mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp)/'pulse.data'; path.write_bytes(encode([(100, 200)]*50))
                    p = subprocess.Popen(command+['--follow-seconds', '2', str(path)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    try:
                        with selectors.DefaultSelector() as selector:
                            selector.register(p.stdout, selectors.EVENT_READ)
                            self.assertTrue(selector.select(timeout=1.5), 'no first window')
                            self.assertEqual(json.loads(p.stdout.readline())['sample_start'], 0)
                        if mutation == 'truncate': path.write_bytes(b'')
                        elif mutation == 'replace':
                            replacement = Path(tmp)/'replacement'
                            replacement.write_bytes(b'\0'*400); replacement.replace(path)
                        else: path.unlink()
                        _, err = p.communicate(timeout=5)
                        self.assertEqual(p.returncode, 1, err)
                        self.assertIn('removed, replaced, or truncated', err)
                    finally:
                        if p.poll() is None: p.kill(); p.communicate()

    def test_nonregular_inputs_cli_errors_and_output_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); path = root/'regular'; path.write_bytes(encode([(1, 2)]*50))
            (root/'link').symlink_to(path); os.mkfifo(root/'fifo')
            for command in self.commands:
                for candidate in (root/'link', root/'fifo', root, Path('/dev/null')):
                    p = subprocess.run(command+[str(candidate)], capture_output=True, timeout=3)
                    self.assertEqual(p.returncode, 1)
                for value in ('0', '-1', 'nan', 'inf', '86401', 'x', ''):
                    p = subprocess.run(command+['--follow-seconds', value, str(path)], capture_output=True, timeout=3)
                    self.assertEqual(p.returncode, 2)
                p = subprocess.run(command+['--follow-seconds', '1', '--follow-seconds', '2', str(path)], capture_output=True, timeout=3)
                self.assertEqual(p.returncode, 2)
                with open('/dev/full', 'wb') as full:
                    p = subprocess.run(command+[str(path)], stdout=full, stderr=subprocess.PIPE, timeout=3)
                self.assertEqual(p.returncode, 1)


if __name__ == '__main__':
    unittest.main()
