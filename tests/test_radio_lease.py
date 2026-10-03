# SPDX-License-Identifier: Apache-2.0
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RadioLeaseTests(unittest.TestCase):
    def test_event_worker_cancellation_release_and_wakeup(self):
        compiler = os.environ.get('DREEM_OVERLAY_CC', 'arm-linux-gnueabihf-gcc')
        sysroot = os.environ.get('DREEM_OVERLAY_SYSROOT', '/usr/arm-linux-gnueabihf')
        with tempfile.TemporaryDirectory(prefix='dreem-radio-threads-') as tmp:
            for label, cc, flags, runner in (
                    ('host', 'cc', [], []),
                    ('arm', compiler, ['-marm', '-mcpu=cortex-a7', '-static'],
                     ['qemu-arm', '-cpu', 'cortex-a7']),
                    ('arm-dynamic', compiler, ['-marm', '-mcpu=cortex-a7', '-fno-pie', '-no-pie'],
                     ['qemu-arm', '-cpu', 'cortex-a7', '-L', sysroot])):
                with self.subTest(target=label):
                    binary = Path(tmp)/label
                    command = [cc, '-std=c11', '-Os', '-Wall', '-Wextra', '-Werror', *flags,
                               '-fno-unwind-tables', '-fno-asynchronous-unwind-tables',
                               str(ROOT/'development/bluetooth_radio_guard.c'),
                               str(ROOT/'tests/radio_guard_threads.c'), '-pthread', '-o', str(binary)]
                    p = subprocess.run(command, capture_output=True, text=True, timeout=60)
                    self.assertEqual(p.returncode, 0, p.stderr)
                    p = subprocess.run(runner+[str(binary)], capture_output=True, text=True, timeout=15)
                    self.assertEqual((p.returncode, p.stdout, p.stderr), (0, 'thread-guard-ok 3\n', ''))

    def test_protocol_and_exclusive_atomic_publication(self):
        compiler = os.environ.get('DREEM_OVERLAY_CC', 'arm-linux-gnueabihf-gcc')
        with tempfile.TemporaryDirectory(prefix='dreem-radio-lease-') as tmp:
            root = Path(tmp)
            for label, cc, flags, runner in (
                    ('host', 'cc', [], []),
                    ('asan', 'cc', ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie'], []),
                    ('arm', compiler, ['-marm', '-mcpu=cortex-a7', '-static', '-DTEST_ARM_READER'],
                     ['qemu-arm', '-cpu', 'cortex-a7'])):
                with self.subTest(target=label):
                    directory = root/(label+'-lease')
                    binary = root/label
                    sources = ['development/radio_lease.c', 'development/radio_lease_writer.c',
                               'tests/radio_lease_harness.c']
                    if label == 'arm':
                        sources += ['development/radio_lease_arm.c', 'development/bluetooth_radio_shims.S']
                    command = [cc, '-std=c11', '-Os', '-Wall', '-Wextra', '-Werror', *flags,
                               '-DDREEM_LEASE_DIRECTORY="'+str(directory)+'"', '-I'+str(ROOT/'development'),
                               *(str(ROOT/name) for name in sources), '-o', str(binary)]
                    p = subprocess.run(command, capture_output=True, text=True, timeout=60)
                    self.assertEqual(p.returncode, 0, p.stderr)
                    p = subprocess.run(runner+[str(binary), str(directory)], capture_output=True,
                                       text=True, timeout=20)
                    self.assertEqual(p.returncode, 0, p.stderr)
                    self.assertEqual(p.stderr, '')
                    self.assertRegex(p.stdout, r'^lease-ok [0-9]+\n$')
                    self.assertEqual([p.name for p in directory.iterdir()], ['owner.lock'])


if __name__ == '__main__':
    unittest.main()
