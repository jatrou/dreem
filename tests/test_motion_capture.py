# SPDX-License-Identifier: Apache-2.0
import errno
import itertools
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'development'
ST = ROOT / 'third-party/source-snapshots/lis2hh12-pid'


class MotionCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='dreem-motion-capture-')
        cls.directory = Path(cls.tmp.name)
        cls.builds = []
        for name, cc, flags, runner in (
            ('host', 'cc', [], []),
            ('arm', 'arm-linux-gnueabihf-gcc', ['-static', '-marm', '-mcpu=cortex-a7'],
             ['qemu-arm', '-cpu', 'cortex-a7']),
        ):
            if not shutil.which(cc) or (runner and not shutil.which(runner[0])):
                if name == 'host':
                    raise unittest.SkipTest('C compiler required')
                continue
            if name == 'host' and os.environ.get('DREEM_CAPTURE_SANITIZE') == '1':
                flags += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-no-pie']
            executable = cls.directory / name
            command = [cc, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', *flags,
                       '-I', str(SOURCE), '-I', str(ST),
                       str(SOURCE / 'motion_capture.c'), str(SOURCE / 'motion_sensor.c'),
                       str(SOURCE / 'sensor_i2c.c'), str(ST / 'lis2hh12_reg.c'),
                       str(ROOT / 'tests/motion_capture_harness.c'),
                       '-Wl,' + ','.join('--wrap=' + f for f in (
                           'ioctl', 'fcntl', 'fstat', 'clock_gettime', 'nanosleep',
                           'write', 'fsync', 'close', '__fcntl_time64', '__fstat64_time64',
                           '__clock_gettime64', '__nanosleep64', '__ioctl_time64')),
                       '-o', str(executable)]
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stderr)
            cls.builds.append((name, runner + [str(executable)]))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_capture(self, scenario='normal', *, changes=None, extra=(), prepare=None):
        results = []
        for name, command in self.builds:
            with tempfile.TemporaryDirectory(dir=self.directory) as directory:
                path = Path(directory) / 'capture.ndjson'
                if prepare:
                    prepare(path)
                options = {'--fd': '42', '--address': '0x1e', '--rate': '50',
                           '--range': '2', '--high-resolution': '1', '--duration-ms': '300',
                           '--max-samples': '4', '--output': str(path)}
                options.update(changes or {})
                args = [item for pair in options.items() for item in pair]
                env = os.environ | {'DREEM_CAPTURE_CASE': scenario}
                p = subprocess.run(command + args + list(extra), env=env, text=True,
                                   capture_output=True, timeout=15)
                self.assertNotIn('Sanitizer', p.stderr, (name, p.stderr))
                model = json.loads(p.stdout)
                rows = []
                if path.is_symlink():
                    self.assertEqual(path.read_text(), '{"preserved":true}\n')
                if path.exists() and not path.is_symlink() and path.is_file():
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    rows = [json.loads(line) for line in path.read_text().splitlines()]
                results.append((p.returncode, rows, model, p.stderr))
        for result in results[1:]:
            self.assertEqual(results[0], result)
        return results[0]

    def test_capture_runs_real_lifecycle_and_preserves_count_timing_semantics(self):
        code, rows, model, _ = self.run_capture()
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]['schema'], 'dreem.motion.capture.v1')
        self.assertFalse(rows[0]['physical_sample_timing_known'])
        previous = 0
        for i, row in enumerate(rows[1:-1]):
            self.assertEqual(row['index'], i)
            generated = i + 2  # The first fresh sensor row is discarded for settling.
            self.assertEqual(row['xyz'], [-30000 + generated, 200 - generated, -generated])
            self.assertEqual(row['flags'], 1)
            self.assertGreater(row['read_begin_ns'], previous)
            self.assertGreaterEqual(row['read_end_ns'], row['read_begin_ns'])
            previous = row['read_end_ns']
        self.assertEqual(rows[-1]['reason'], 'sample_limit')
        self.assertEqual(rows[-1]['samples'], 4)
        self.assertTrue(rows[-1]['shutdown_confirmed'])
        self.assertFalse(model['active'])

    def test_profiles_and_addresses_connect_to_capture(self):
        for rate, scale, hr, address in itertools.product(
                (10, 50, 100, 200, 400, 800), (2, 4, 8), (0, 1), (29, 30)):
            with self.subTest(rate=rate, scale=scale, hr=hr, address=address):
                code, rows, model, _ = self.run_capture(changes={
                    '--rate': str(rate), '--range': str(scale), '--high-resolution': str(hr),
                    '--address': str(address), '--max-samples': '1', '--duration-ms': '500'})
                self.assertEqual(code, 0)
                self.assertEqual(rows[0]['requested_rate_hz'], rate)
                self.assertEqual(rows[0]['full_scale_g'], scale)
                self.assertEqual(rows[-1]['samples'], 1)
                self.assertFalse(model['active'])

    def test_short_file_writes_are_completed(self):
        self.assertEqual(self.run_capture('short_write'), self.run_capture())

    def test_read_failure_never_publishes_partially_updated_sample(self):
        code, rows, model, _ = self.run_capture('read_error')
        self.assertEqual(code, 1)
        self.assertEqual([r['type'] for r in rows], ['request', 'end'])
        self.assertEqual(rows[-1]['error'], -errno.EIO)
        self.assertEqual(rows[-1]['reason'], 'read_error')
        self.assertTrue(rows[-1]['shutdown_confirmed'])
        self.assertFalse(model['active'])

    def test_failed_shutdown_preserves_unknown_state(self):
        code, rows, model, _ = self.run_capture('stop_error')
        self.assertEqual(code, 1)
        self.assertEqual(rows[-1]['stop_error'], -errno.EIO)
        self.assertEqual(rows[-1]['sensor_state'], 'UNKNOWN')
        self.assertFalse(rows[-1]['shutdown_confirmed'])
        self.assertTrue(model['active'])

    def test_read_error_and_failed_cleanup_remain_separate(self):
        code, rows, model, _ = self.run_capture('read_and_stop')
        self.assertEqual(code, 1)
        self.assertEqual(rows[-1]['error'], -errno.EIO)
        self.assertEqual(rows[-1]['stop_error'], -errno.ENXIO)
        self.assertEqual(rows[-1]['samples'], 0)
        self.assertFalse(rows[-1]['shutdown_confirmed'])
        self.assertTrue(model['active'])

    def test_slow_output_reports_overrun_without_inventing_continuity(self):
        code, rows, model, _ = self.run_capture('slow_output')
        self.assertEqual(code, 0)
        self.assertTrue(any(row['flags'] & 2 for row in rows if row['type'] == 'sample'))
        self.assertFalse(rows[-1]['continuity_known'])
        self.assertFalse(model['active'])

    def test_start_identity_clock_and_wait_failures(self):
        for scenario, reason, error in (
            ('identity', 'start_error', errno.ENODEV),
            ('start_error', 'start_error', errno.EIO),
            ('clock_error', 'clock_error', errno.EIO),
            ('clock_backward', 'clock_error', errno.ERANGE),
            ('wait_error', 'wait_error', errno.EIO),
        ):
            with self.subTest(scenario=scenario):
                code, rows, model, _ = self.run_capture(scenario)
                self.assertEqual(code, 1)
                self.assertEqual(rows[-1]['reason'], reason)
                self.assertEqual(rows[-1]['error'], -error)
                self.assertFalse(model['active'])
                if scenario == 'identity':
                    self.assertEqual(model['writes'], 0)
                    self.assertFalse(rows[-1]['shutdown_confirmed'])
                else:
                    self.assertTrue(rows[-1]['shutdown_confirmed'])

    def test_deadline_and_absent_ready_samples(self):
        for scenario in ('not_ready', 'late_read'):
            code, rows, model, _ = self.run_capture(scenario)
            self.assertEqual(code, 0)
            self.assertEqual(rows[-1]['reason'], 'duration')
            self.assertEqual(rows[-1]['samples'], 0)
            self.assertEqual(rows[-1]['late_samples_discarded'], int(scenario == 'late_read'))
            self.assertFalse(model['active'])

    def test_sigterm_during_transfer_stops_before_publishing(self):
        code, rows, model, _ = self.run_capture('signal')
        self.assertEqual(code, 143)
        self.assertEqual(rows[-1]['reason'], 'signal')
        self.assertEqual(rows[-1]['signal'], 15)
        self.assertEqual(rows[-1]['samples'], 0)
        self.assertTrue(rows[-1]['shutdown_confirmed'])
        self.assertFalse(model['active'])
        for scenario, number in (('signal_header', 15), ('signal_start', 2), ('signal_wait', 2)):
            code, rows, model, _ = self.run_capture(scenario)
            self.assertEqual(code, 128 + number)
            self.assertEqual(rows[-1]['reason'], 'signal')
            self.assertEqual(rows[-1]['signal'], number)
            self.assertEqual(rows[-1]['samples'], 0)
            self.assertEqual(rows[-1]['start_attempted'], scenario != 'signal_header')
            self.assertFalse(model['active'])
            if scenario == 'signal_header':
                self.assertEqual(model['transactions'], 0)

    def test_output_failures_stop_acquisition_and_fail_exit(self):
        for scenario in ('output_header', 'output_sample', 'fsync_error', 'close_error'):
            with self.subTest(scenario=scenario):
                code, rows, model, error = self.run_capture(scenario)
                self.assertEqual(code, 1)
                self.assertFalse(model['active'])
                self.assertIn('output_error=-', error)
                if scenario == 'output_header':
                    self.assertEqual(model['transactions'], 0)
                    self.assertEqual(rows, [])

    def test_invalid_parameters_and_descriptors_do_not_touch_sensor(self):
        for changes, extra in (({'--fd': '0'}, ()), ({'--rate': '51'}, ()),
                               ({'--range': '3'}, ()), ({'--duration-ms': '0'}, ()),
                               ({'--max-samples': '1000001'}, ()), ({'--fd': '+42'}, ()),
                               ({'--address': '0x57'}, ()), ({}, ('--rate', '50'))):
            code, rows, model, _ = self.run_capture(changes=changes, extra=extra)
            self.assertEqual(code, 2)
            self.assertEqual(rows, [])
            self.assertEqual(model['transactions'], 0)
        for scenario in ('fd_wrong', 'fd_readonly'):
            code, rows, model, _ = self.run_capture(scenario)
            self.assertEqual(code, 2)
            self.assertEqual(rows, [])
            self.assertEqual(model['transactions'], 0)

    def test_existing_files_and_symlinks_are_not_overwritten(self):
        def existing(path):
            path.write_text('{"preserved":true}\n')
            path.chmod(0o600)
        code, rows, model, _ = self.run_capture(prepare=existing)
        self.assertEqual(code, 1)
        self.assertEqual(rows, [{'preserved': True}])
        self.assertEqual(model['transactions'], 0)
        def symlink(path):
            target = path.parent / 'target'
            existing(target)
            path.symlink_to(target)
        code, rows, model, _ = self.run_capture(prepare=symlink)
        self.assertEqual(code, 1)
        self.assertEqual(model['transactions'], 0)


if __name__ == '__main__':
    unittest.main()
