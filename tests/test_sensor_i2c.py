# SPDX-License-Identifier: Apache-2.0
import errno
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def scenario(op=0, fd=42, address=0x57, reg=7, length=6, funcs_result=0,
             funcs_errno=0, functions=1, slave_result=0, slave_errno=0,
             transfer_result=None, transfer_errno=0):
    if transfer_result is None:
        transfer_result = 1 if op & 1 else 2
    return [op, fd, address, reg, length, funcs_result, funcs_errno, functions,
            slave_result, slave_errno, transfer_result, transfer_errno]


def build_harnesses(output):
    builds = []
    for name, compiler, flags, runner in (
        ('host', 'cc', [], []),
        ('arm', 'arm-linux-gnueabihf-gcc', ['-marm', '-mcpu=cortex-a7', '-static'],
         ['qemu-arm', '-cpu', 'cortex-a7'])):
        if not shutil.which(compiler) or (runner and not shutil.which(runner[0])):
            if name == 'host':
                raise unittest.SkipTest('C compiler required')
            continue
        exe = output/name
        subprocess.run([compiler, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                        *flags, '-I', str(ROOT/'development'),
                        str(ROOT/'tests/sensor_i2c_harness.c'),
                        str(ROOT/'development/sensor_i2c.c'),
                        '-Wl,--wrap=ioctl,--wrap=__ioctl_time64',
                        '-o', str(exe)], check=True, capture_output=True)
        builds.append((name, runner + [str(exe)]))
    return builds


def run_cases(command, cases):
    p = subprocess.run(command, input=''.join(' '.join(map(str, row))+'\n' for row in cases),
                       capture_output=True, text=True, check=True, timeout=10)
    return [json.loads(line) for line in p.stdout.splitlines()]


class SensorI2CTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.builds = build_harnesses(Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def check_cases(self, cases, expected, counts):
        for name, command in self.builds:
            with self.subTest(build=name):
                outputs = run_cases(command, cases)
                self.assertEqual(len(outputs), len(cases))
                for case, data in zip(cases, outputs):
                    self.assertEqual(data['result'], expected, (case, data))
                    self.assertEqual(tuple(data[key] for key in
                                     ('funcs_calls', 'slave_calls', 'transfer_calls')), counts, case)
                    self.assertTrue(data['request_ok'], case)
                    self.assertTrue(data['guards_ok'], case)
                    self.assertTrue(data['input_unchanged'], case)
                    if expected or case[0] & 1:
                        self.assertTrue(data['output_unchanged'], case)
                    else:
                        self.assertTrue(data['output_ok'], case)

    def test_complete_combined_reads_and_writes(self):
        self.check_cases([scenario(op=op, address=addr, reg=reg, length=length)
                          for op in (0, 1) for addr in (8, 0x57, 0x77)
                          for reg in (0, 7, 255) for length in (1, 6, 192, 256)],
                         0, (1, 1, 1))

    def test_invalid_arguments_do_not_access_kernel(self):
        cases = [scenario(op=op, **kw) for op in (0, 1) for kw in
                 ([{'address': v} for v in (-1, 0, 7, 0x78, 0x80, 0x3ff)] +
                  [{'length': v} for v in (-1, 0, 257)])]
        cases += [scenario(op=2), scenario(op=3)]
        self.check_cases(cases, -errno.EINVAL, (0, 0, 0))
        self.check_cases([scenario(op=op, fd=-1) for op in (0, 1)], -errno.EBADF, (0, 0, 0))

    def test_capability_failure_stops_before_address_selection(self):
        self.check_cases([scenario(op=op, functions=0) for op in (0, 1)],
                         -errno.EOPNOTSUPP, (1, 0, 0))
        for error in (errno.ENOTTY, errno.EINTR, errno.EIO, 0):
            self.check_cases([scenario(op=op, funcs_result=-1, funcs_errno=error) for op in (0, 1)],
                             -(error or errno.EIO), (1, 0, 0))
        self.check_cases([scenario(op=op, funcs_result=1) for op in (0, 1)], -errno.EIO, (1, 0, 0))

    def test_busy_or_failed_selection_never_transfers(self):
        for error in (errno.EBUSY, errno.EACCES, errno.EINTR, 0):
            self.check_cases([scenario(op=op, slave_result=-1, slave_errno=error,
                                       transfer_result=0) for op in (0, 1)],
                             -(error or errno.EIO), (1, 1, 0))
        self.check_cases([scenario(op=op, slave_result=1) for op in (0, 1)], -errno.EIO, (1, 1, 0))

    def test_partial_or_failed_transfers_do_not_publish_or_retry(self):
        for error in (errno.ENXIO, errno.EREMOTEIO, errno.EINTR, errno.ETIMEDOUT, 0):
            self.check_cases([scenario(op=op, transfer_result=-1, transfer_errno=error)
                              for op in (0, 1)], -(error or errno.EIO), (1, 1, 1))
        self.check_cases([scenario(op=0, transfer_result=n, length=length)
                          for n in (0, 1, 3) for length in (1, 6, 256)] +
                         [scenario(op=1, transfer_result=n) for n in (0, 2)],
                         -errno.EREMOTEIO, (1, 1, 1))


if __name__ == '__main__':
    unittest.main()
