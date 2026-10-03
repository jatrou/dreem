# SPDX-License-Identifier: Apache-2.0
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def stat_text(pid, name='nano_core', start=123456789012, flags=0):
    fields = ['S', '1', '1', '1', '0', '0', str(flags)] + ['0'] * 12 + [str(start)]
    assert len(fields) == 20
    return f'{pid} ({name}) ' + ' '.join(fields) + '\n'


class StartupInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='dreem-startup-build-')
        cls.build = Path(cls.tmp.name)
        compiler = os.environ.get('DREEM_OVERLAY_CC', 'arm-linux-gnueabihf-gcc')
        cls.commands = []
        for label, cc, flags, runner in (
                ('host', 'cc', [], []),
                ('asan', 'cc', ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie'], []),
                ('arm', compiler, ['-marm', '-mcpu=cortex-a7', '-static'], ['qemu-arm', '-cpu', 'cortex-a7'])):
            binary = cls.build / label
            subprocess.run([cc, '-std=c11', '-Os', '-Wall', '-Wextra', '-Werror', *flags,
                            '-DINVENTORY_FIXTURE', '-DINVENTORY_TEST_HOOK',
                            str(ROOT / 'development/startup_inventory.c'),
                            str(ROOT / 'tests/startup_inventory_hook.c'), '-o', str(binary)],
                           check=True, capture_output=True, timeout=60)
            cls.commands.append((label, runner + [str(binary)]))
        cls.native = cls.build / 'native'
        subprocess.run(['cc', '-std=c11', '-Os', '-Wall', '-Wextra', '-Werror',
                        str(ROOT / 'development/startup_inventory.c'), '-o', str(cls.native)],
                       check=True, capture_output=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def process(self, root, pid=101, *, name='nano_core', executable='nano_core',
                command=b'/usr/bin/nano_core\0', flags=0):
        binary = root / executable
        if not binary.exists(): binary.write_bytes(b'fixture only; never executed')
        process = root / str(pid)
        process.mkdir()
        (process / 'fd').mkdir()
        (process / 'stat').write_text(stat_text(pid, name, flags=flags))
        (process / 'cmdline').write_bytes(command)
        (process / 'exe').symlink_to(binary)
        return process

    def invoke(self, command, root, *, status=0, mutation=None):
        env = dict(os.environ)
        env.pop('INVENTORY_TEST_MUTATION', None)
        if mutation: env['INVENTORY_TEST_MUTATION'] = mutation
        p = subprocess.run(command + ['--fixture-proc', str(root)], env=env,
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, status, p.stderr)
        self.assertEqual(p.stderr, '')
        data = json.loads(p.stdout)
        self.assertTrue(data['fixture'])
        self.assertFalse(data['exclusive_access_established'])
        self.assertFalse(data['activation_ready'])
        self.assertEqual(data['inspection_complete'], status == 0)
        return data, p.stdout

    def test_roles_and_start_ticks_without_arguments_or_paths(self):
        for label, command in self.commands:
            with self.subTest(target=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                core = self.process(root, command=b'/usr/bin/nano_core\0PRIVATE_ARGUMENT\0')
                self.process(root, 102, name='sh', executable='busybox',
                             command=b'/bin/sh\0/usr/bin/mpu_watchdog.sh\0')
                self.process(root, 103, name='watchdog', executable='busybox',
                             command=b'\0'.join((b'busybox', b'watchdog', b'-t', b'5',
                                                 b'-T', b'40', b'/dev/watchdog', b'')))
                self.process(root, 104, name='observer', executable='other',
                             command=b'/bin/echo\0nano_core\0/usr/bin/mpu_watchdog.sh\0')
                data, text = self.invoke(command, root)
                self.assertEqual(data['processes_observed'], 4)
                rows = data['processes']
                self.assertEqual([r['pid'] for r in rows], [101, 102, 103])
                self.assertEqual(rows[0]['start_ticks'], 123456789012)
                self.assertEqual(rows[0]['executable']['inode'], (core / 'exe').stat().st_ino)
                self.assertTrue(rows[0]['core_executable_name'])
                self.assertTrue(rows[1]['shell_watchdog_hint'])
                self.assertTrue(rows[2]['hardware_watchdog_hint'])
                self.assertTrue(all(r['identity_unchanged_at_checks'] for r in rows))
                self.assertNotIn('PRIVATE_ARGUMENT', text)
                self.assertNotIn(str(root), text)

    def test_stat_names_with_spaces_parentheses_and_spoofed_comm(self):
        for label, command in self.commands:
            with self.subTest(target=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.process(root, name='a ) space ( )')
                self.process(root, 102, name='nano_core', executable='unrelated')
                data, _ = self.invoke(command, root)
                a, b = data['processes']
                self.assertTrue(a['core_executable_name'])
                self.assertFalse(a['core_comm_hint'])
                self.assertTrue(b['core_comm_hint'])
                self.assertFalse(b['core_executable_name'])
                self.assertEqual(a['start_ticks'], b['start_ticks'])

    def test_shell_applet_and_deleted_executable(self):
        for label, command in self.commands:
            with self.subTest(target=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.process(root, name='busybox', executable='busybox',
                             command=b'/bin/busybox\0sh\0/usr/bin/mpu_watchdog.sh\0')
                # Synthetic proc link text reproduces the kernel's deleted suffix.
                self.process(root, 102, executable='nano_core (deleted)')
                data, _ = self.invoke(command, root)
                self.assertTrue(data['processes'][0]['shell_watchdog_hint'])
                self.assertTrue(data['processes'][1]['executable_deleted'])
                self.assertTrue(data['processes'][1]['core_executable_name'])

    def test_device_descriptor_metadata_and_unverified_names(self):
        for label, command in self.commands:
            with self.subTest(target=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                process = self.process(root, name='new-sensor', executable='unrelated', command=b'other\0')
                (process / 'fd/3').symlink_to('/dev/null')
                data, _ = self.invoke(command, root)
                self.assertEqual(data['processes'][0]['descriptors']['other_device'], 1)
                targets = ('/dev/eeg_cdev', '/dev/dreem_ddr', '/dev/i2c-42', '/dev/spidev42.0',
                           '/dev/ttymxc42', '/dev/snd/missing', '/dev/watchdog')
                for n, target in enumerate(targets, 4):
                    (process / f'fd/{n}').symlink_to(target)
                data, _ = self.invoke(command, root, status=1)
                counts = data['processes'][0]['descriptors']
                self.assertTrue(all(counts[k] == 1 for k in counts))
                self.assertGreaterEqual(data['inspection_errors'], sum(not Path(p).exists() for p in targets))

    def test_missing_exe_kernel_thread_is_distinct_from_dead_owner(self):
        for label, command in self.commands:
            with self.subTest(target=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                process = self.process(root, name='kworker/0:0', command=b'', flags=0x200000)
                (process / 'exe').unlink()
                data, _ = self.invoke(command, root)
                self.assertEqual(data['processes'], [])
                (process / 'stat').write_text(stat_text(101))
                data, _ = self.invoke(command, root, status=1)
                self.assertFalse(data['processes'][0]['identity_unchanged_at_checks'])

    def test_identity_and_pid_set_changes_are_not_reported_stable(self):
        for label, command in self.commands:
            for mutation in ('start', 'command', 'executable', 'new-process', 'vanish'):
                with self.subTest(target=label, mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self.process(root)
                    (root / 'replacement').write_bytes(b'replacement executable')
                    data, text = self.invoke(command, root, status=1, mutation=mutation)
                    if mutation == 'new-process':
                        self.assertFalse(data['pid_set_unchanged_at_checks'])
                    else:
                        self.assertFalse(data['processes'][0]['identity_unchanged_at_checks'])
                    self.assertNotIn('private-argument', text)

    def test_bad_proc_inputs_remain_bounded_and_incomplete(self):
        for label, command in self.commands:
            for bad in ('fifo-stat', 'symlink-stat', 'truncated-stat', 'wrong-pid',
                        'bad-start', 'negative-start', 'long-command', 'missing-nul', 'linked-fds'):
                with self.subTest(target=label, bad=bad), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    process = self.process(root)
                    stat = process / 'stat'
                    if bad in ('fifo-stat', 'symlink-stat'):
                        stat.unlink()
                        if bad == 'fifo-stat': os.mkfifo(stat)
                        else: stat.symlink_to('/dev/zero')
                    elif bad == 'truncated-stat': stat.write_text('101 (owner) S 1\n')
                    elif bad == 'wrong-pid': stat.write_text(stat_text(202))
                    elif bad == 'bad-start': stat.write_text(stat_text(101, start=2**64))
                    elif bad == 'negative-start': stat.write_text(stat_text(101, start=-1))
                    elif bad == 'long-command': (process / 'cmdline').write_bytes(b'x' * 8192)
                    elif bad == 'missing-nul': (process / 'cmdline').write_bytes(b'missing terminator')
                    elif bad == 'linked-fds':
                        (process / 'fd').rmdir()
                        (process / 'fd').symlink_to(root, target_is_directory=True)
                    self.invoke(command, root, status=1)

    def test_empty_fixture_is_observation_not_handoff_and_native_rejects_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            for _, command in self.commands:
                data, _ = self.invoke(command, Path(tmp))
                self.assertEqual(data['processes_observed'], 0)
            p = subprocess.run([str(self.native), '--fixture-proc', tmp], capture_output=True, timeout=3)
            self.assertEqual(p.returncode, 2)
            self.assertEqual(p.stdout, b'')

    def test_process_and_descriptor_limits_report_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for pid in range(1, 4098): (root / str(pid)).mkdir()
            for label, command in self.commands:
                with self.subTest(target=label, limit='processes'):
                    data, _ = self.invoke(command, root, status=1)
                    self.assertEqual(data['processes_observed'], 4096)
                    self.assertFalse(data['pid_set_unchanged_at_checks'])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            process = self.process(root)
            for fd in range(4097): (process / f'fd/{fd}').symlink_to('/dev/null')
            for label, command in self.commands:
                with self.subTest(target=label, limit='descriptors'):
                    data, _ = self.invoke(command, root, status=1)
                    self.assertEqual(data['processes'][0]['descriptors']['other_device'], 4096)
                    self.assertEqual(data['inspection_errors'], 1)

    def test_real_proc_identity_and_readonly_syscalls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'child.c'
            source.write_text('#include <unistd.h>\nint main(void) { sleep(30); return 0; }\n')
            child = root / 'nano_core'
            subprocess.run(['cc', str(source), '-o', str(child)], check=True, capture_output=True)
            p = subprocess.Popen([str(child), 'PRIVATE_COMMAND_ARGUMENT'])
            try:
                command = [str(self.native)]
                trace = root / 'trace'
                if shutil.which('strace'):
                    command = ['strace', '-qq', '-o', str(trace), '-e',
                               'trace=open,openat,kill,tgkill,tkill,execve', *command]
                result = subprocess.run(command, capture_output=True, text=True, timeout=15)
                self.assertIn(result.returncode, (0, 1), result.stderr)
                self.assertEqual(result.stderr, '')
                report = json.loads(result.stdout)
                self.assertFalse(report['fixture'])
                self.assertFalse(report['activation_ready'])
                row = next(r for r in report['processes'] if r['pid'] == p.pid)
                self.assertTrue(row['identity_unchanged_at_checks'])
                self.assertEqual(row['executable']['inode'], child.stat().st_ino)
                self.assertNotIn('PRIVATE_COMMAND_ARGUMENT', result.stdout)
                self.assertIsNone(p.poll())
                if trace.exists():
                    lines = trace.read_text().splitlines()
                    self.assertEqual(sum('execve(' in line for line in lines), 1)
                    self.assertFalse(any('kill(' in line for line in lines))
                    self.assertFalse(any('"/dev/' in line for line in lines))
                    self.assertFalse(any('O_WRONLY' in line or 'O_RDWR' in line for line in lines))
            finally:
                p.terminate()
                p.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
