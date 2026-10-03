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
import unittest

ROOT = Path(__file__).resolve().parents[1]


def event(counter, code, value=None):
    return struct.pack('<IB', counter, code)+(b'' if value is None else struct.pack('<I', value))


def fixture(path, eeg_rows=297, transitions=((0, 1),), vectors=None):
    count = (eeg_rows+4)//5
    with (path/'eeg.data').open('wb') as f:
        f.truncate(eeg_rows*16)
    if vectors is None:
        vectors = [(0, 0, 1)]*count
    (path/'accelerometer.data').write_bytes(b''.join(struct.pack('<3f', *v) for v in vectors))
    header = bytearray(142)
    struct.pack_into('<II', header, 110, 1000, 1000+eeg_rows//250)
    struct.pack_into('<I', header, 134, eeg_rows)
    (path/'meta.data').write_bytes(header+b'\0'*253)
    (path/'algo.data').write_bytes(event(0, 16, 1000)+b''.join(event(c, 30, v) for c,v in transitions)+
                                  event(eeg_rows, 17, 1000+eeg_rows//250))


class SessionMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.commands = [[str(ROOT/'development/build/session_motion.host')]]
        if not Path(cls.commands[0][0]).is_file():
            raise unittest.SkipTest('run sh development/build.sh first')
        arm = ROOT/'development/build/session_motion.arm'
        if shutil.which('qemu-arm') and arm.is_file():
            cls.commands.append(['qemu-arm', '-cpu', 'cortex-a7', str(arm)])

    def run_feature(self, command, path, status=0):
        result = subprocess.run(command+[str(path)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, status, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()], result.stderr

    def test_health_filters_metrics_and_preserves_partial_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            vectors = [(0, 0, 100)]*60
            vectors[2:5] = [(1, 0, 1), (-1, 0, 1), (float('nan'), 0, 1)]
            vectors[8] = (0, 0, 0)
            fixture(path, transitions=((10, 1), (25, 0), (40, 1), (45, 7)), vectors=vectors)
            hashes = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in path.iterdir()}
            for command in self.commands:
                rows, _ = self.run_feature(command, path)
                self.assertEqual(len(rows), 4)
                first, last, end = rows[1:]
                self.assertEqual((first['reported_good'], first['reported_bad'], first['unknown']), (4, 3, 43))
                self.assertEqual((first['included_rows'], first['nonfinite_good_rows'], first['zero_good_rows']), (3, 1, 1))
                self.assertEqual(first['mean_axes'], [0, 0, 0.6666666667])
                self.assertAlmostEqual(first['vector_rms'], math.sqrt(4/3), places=8)
                self.assertAlmostEqual(first['dynamic_rms'], math.sqrt(8/9), places=8)
                self.assertEqual((last['motion_row_start'], last['eeg_counter_start'], last['rows']), (50, 250, 10))
                self.assertEqual(last['unknown'], 10)
                self.assertIsNone(last['vector_rms'])
                self.assertEqual((end['eeg_rows'], end['motion_rows'], end['included_rows']), (297, 60, 3))
            self.assertEqual(hashes, {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in path.iterdir()})

    def test_decimation_endpoints_and_empty_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            for count in (0, 1, 4, 5, 6, 249, 250, 251):
                fixture(path, eeg_rows=count, transitions=() if count == 0 else ((0, 1),))
                for command in self.commands:
                    rows, _ = self.run_feature(command, path)
                    self.assertEqual(rows[-1]['motion_rows'], (count+4)//5)
                    self.assertEqual(rows[-1]['included_rows'], (count+4)//5)
                    self.assertEqual(sum(r['rows'] for r in rows[1:-1]), (count+4)//5)

    def test_all_unknown_bad_or_nonfinite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            for transitions, vector, field in (((), (0, 0, 100), 'unknown'),
                                               (((0, 0),), (0, 0, 0), 'reported_bad'),
                                               (((0, 2),), (0, 0, 100), 'unknown'),
                                               (((0, 1),), (float('inf'), 0, 0), 'nonfinite_good_rows')):
                fixture(path, eeg_rows=250, transitions=transitions, vectors=[vector]*50)
                for command in self.commands:
                    rows, _ = self.run_feature(command, path)
                    self.assertEqual(rows[1][field], 50)
                    self.assertEqual(rows[1]['included_rows'], 0)
                    for key in ('mean_axes', 'vector_rms', 'dynamic_rms'):
                        self.assertIsNone(rows[1][key])

    def test_rejects_ambiguous_counters_recovery_and_missing_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            fixture(path, eeg_rows=10)
            start, stop = event(0, 16, 1000), event(10, 17, 1000)
            bad_streams = [stop, start, start+start+stop, event(1, 16, 1000)+stop,
                           start+event(5, 17, 1000), start+stop+event(10, 2),
                           start+event(1, 30, 1)+stop, start+event(10, 30, 1)+stop,
                           start+event(0, 30, 1)+event(0, 30, 0)+stop,
                           start+event(5, 30, 1)+event(0, 30, 0)+stop,
                           start+event(1, 28)+stop, start+event(11, 2)+stop,
                           event(0, 16, 999)+stop, start+event(10, 17, 999)]
            for command in self.commands:
                for stream in bad_streams:
                    (path/'algo.data').write_bytes(stream)
                    rows, _ = self.run_feature(command, path, status=1)
                    self.assertEqual(rows, [])
                for flag in (1, 2, 65535):
                    fixture(path, eeg_rows=10)
                    raw = bytearray((path/'meta.data').read_bytes())
                    struct.pack_into('<H', raw, 118, flag)
                    (path/'meta.data').write_bytes(raw)
                    rows, error = self.run_feature(command, path, status=1)
                    self.assertEqual(rows, [])
                    self.assertIn('Nonzero recovery flag', error)

    def test_rejects_inconsistent_counts_and_partial_or_unknown_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            for command in self.commands:
                for kind in ('eeg_partial', 'motion_partial', 'metadata_partial', 'algo_partial',
                             'missing_motion', 'extra_motion', 'metadata_count', 'unknown_code'):
                    fixture(path)
                    if kind == 'eeg_partial':
                        with (path/'eeg.data').open('ab') as f: f.write(b'x')
                    elif kind == 'motion_partial':
                        with (path/'accelerometer.data').open('ab') as f: f.write(b'x')
                    elif kind == 'metadata_partial': (path/'meta.data').write_bytes(b'\0'*141)
                    elif kind == 'algo_partial':
                        file=path/'algo.data'; file.write_bytes(file.read_bytes()[:-1])
                    elif kind in ('missing_motion', 'extra_motion'):
                        file=path/'accelerometer.data'
                        file.write_bytes(file.read_bytes()[:-12] if kind=='missing_motion' else file.read_bytes()+b'\0'*12)
                    elif kind == 'metadata_count':
                        file=path/'meta.data'; data=bytearray(file.read_bytes())
                        struct.pack_into('<I', data, 134, 300); file.write_bytes(data)
                    else:
                        file=path/'algo.data'; raw=file.read_bytes(); file.write_bytes(raw[:9]+event(0, 255)+raw[9:])
                    rows, _ = self.run_feature(command, path, status=1)
                    self.assertEqual(rows, [], kind)

    def test_rejects_nonregular_inputs_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); path=root/'record'; path.mkdir(); fixture(path)
            (root/'link').symlink_to(path, target_is_directory=True)
            for command in self.commands:
                self.run_feature(command, root/'link', status=1)
                self.run_feature(command, path/'eeg.data', status=1)
                for kind in ('link', 'fifo', 'directory', 'missing'):
                    file=path/'algo.data'; raw=file.read_bytes(); file.unlink()
                    try:
                        if kind=='link': file.symlink_to(path/'eeg.data')
                        elif kind=='fifo': os.mkfifo(file)
                        elif kind=='directory': file.mkdir()
                        self.run_feature(command, path, status=1)
                    finally:
                        if file.is_dir() and not file.is_symlink(): file.rmdir()
                        elif file.exists() or file.is_symlink(): file.unlink()
                        file.write_bytes(raw)

    def test_detects_input_change_before_success_record(self):
        for command in self.commands:
            for mutation in ('append', 'replace', 'remove'):
                with self.subTest(command=command, mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                    path=Path(tmp); fixture(path, eeg_rows=100000)
                    p=subprocess.Popen(command+[str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                    try:
                        with selectors.DefaultSelector() as selector:
                            selector.register(p.stdout, selectors.EVENT_READ)
                            self.assertTrue(selector.select(timeout=2), 'no alignment record')
                            self.assertEqual(json.loads(p.stdout.readline())['type'], 'alignment')
                        file=path/'eeg.data'
                        if mutation=='append':
                            with file.open('ab') as f: f.write(b'\0'*16)
                        elif mutation=='replace':
                            new=path/'replacement'; new.write_bytes(file.read_bytes()); new.replace(file)
                        else: file.unlink()
                        out, err=p.communicate(timeout=5)
                        self.assertEqual(p.returncode, 1, err)
                        self.assertNotIn('"type":"end"',out)
                        self.assertIn('Recording changed',err)
                    finally:
                        if p.poll() is None: p.kill(); p.communicate()


if __name__ == '__main__':
    unittest.main()
