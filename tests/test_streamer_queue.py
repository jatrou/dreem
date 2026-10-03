# SPDX-License-Identifier: Apache-2.0
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from development.repair_streamer_poll import repair_source
from tests import test_streamer_poll as polling


def queue_repair(raw):
    return repair_source(raw, preserve_partial_frame=True)


class StreamerQueueSocketTests(polling.StreamerPollTests):
    source_transform = staticmethod(queue_repair)
    resume_receive_buffer = 131072

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        raw = Path(os.environ['DREEM_STREAMER_SOURCE']).read_bytes()
        source = cls.directory / 'poll-only.c'
        source.write_bytes(repair_source(raw))
        source.chmod(0o600)
        exe = cls.directory / 'poll-only.host'
        p = subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                            str(source), str(polling.ROOT / 'tests/streamer_poll_harness.c'),
                            '-Wl,--wrap=poll', '-o', str(exe)], capture_output=True, text=True)
        if p.returncode:
            raise RuntimeError(p.stderr)
        cls.builds['poll-only', 'host'] = [str(exe)]

    def test_backpressured_client_resumes_without_corrupting_frames(self):
        _, before, _ = self.run_streamer('host', version='poll-only', mode='backpressure')
        self.assertGreater(before['congestion_disconnects'], 0)
        self.assertGreater(before['partial_final_bytes'], 0)
        for name in self.repaired_builds():
            frames, metrics, expected = self.run_streamer(name, mode='backpressure')
            self.assertGreater(metrics['blocked_write_polls'], 0)
            self.assertEqual(metrics['congestion_disconnects'], 0)
            self.assertEqual(metrics['partial_final_bytes'], 0)
            data = [frame for frame in frames if frame['type'] == 2]
            self.assertTrue(data)
            self.assertGreater(max(frame['dropped'] for frame in frames), 0)
            previous = {}
            gaps = 0
            for frame in data:
                sid = frame['stream']
                size = polling.ROW_BYTES[sid]
                self.assertEqual(frame['payload'], expected[sid][
                    frame['index'] * size:(frame['index'] + frame['count']) * size])
                if sid in previous:
                    self.assertGreaterEqual(frame['index'], previous[sid])
                    gaps += frame['index'] - previous[sid]
                previous[sid] = frame['index'] + frame['count']
            self.assertGreater(gaps, 0, 'dropped unsent rows must remain observable')


class StreamerQueueModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = os.environ.get('DREEM_STREAMER_SOURCE')
        if not source:
            raise unittest.SkipTest('DREEM_STREAMER_SOURCE required')
        cls.raw = Path(source).read_bytes()

    def test_default_poll_only_output_is_unchanged(self):
        self.assertEqual(hashlib.sha256(repair_source(self.raw)).hexdigest(),
                         'af658375798818325beb9b574a79cf9e8283a1d015f0c93e498d0a348ecca9b6')

    def test_actual_queue_code_preserves_partial_frames_and_bounds(self):
        outputs = []
        with tempfile.TemporaryDirectory(prefix='dreem-streamer-queue-') as temporary:
            root = Path(temporary)
            source = root / 'streamer_under_test.c'
            source.write_bytes(queue_repair(self.raw))
            source.chmod(0o600)
            for name, compiler, flags, runner in (
                ('host', 'cc', ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-no-pie'], []),
                ('arm', 'arm-linux-gnueabihf-gcc', ['-static', '-marm', '-mcpu=cortex-a7'],
                 ['qemu-arm', '-cpu', 'cortex-a7']),
            ):
                if not shutil.which(compiler) or (runner and not shutil.which(runner[0])):
                    if name == 'host':
                        self.skipTest('C compiler required')
                    continue
                executable = root / name
                p = subprocess.run([compiler, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                                    *flags, '-I', str(root),
                                    str(polling.ROOT / 'tests/streamer_queue_harness.c'),
                                    '-o', str(executable)], capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                p = subprocess.run(runner + [str(executable)], capture_output=True, text=True, timeout=15)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertNotIn('Sanitizer', p.stderr)
                outputs.append(json.loads(p.stdout))
            for output in outputs:
                self.assertEqual(output, {'queue_cases': 5, 'repeated_evictions': 196,
                                          'partial_frame_immutable': True, 'source_offset_retained': True})


if __name__ == '__main__':
    unittest.main()
