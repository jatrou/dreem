# SPDX-License-Identifier: Apache-2.0
"""Host/ARM tests of source FIFO reader + real transport against a chip model."""
import errno
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
UNKNOWN, RESYNC = 128, 64


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
                        str(ROOT/'tests/optical_fifo_harness.c'),
                        *[str(ROOT/'development'/f) for f in
                          ('sensor_i2c.c', 'optical_samples.c', 'optical_fifo.c')],
                        '-Wl,--wrap=ioctl,--wrap=__ioctl_time64', '-o', str(exe)],
                       check=True, capture_output=True)
        builds.append((name, runner + [str(exe)]))
    return builds


def run_script(command, script):
    p = subprocess.run(command, input=script, text=True, capture_output=True,
                       check=True, timeout=20)
    return [json.loads(line) for line in p.stdout.splitlines()]


def new(count=2, read=0, cfg=6, mode=3, conversion=0x47, status=0, high=0):
    return f'N {read} {count} {cfg} {mode} {conversion} {status} {high}\n'


class OpticalFIFOTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='dreem-fifo-')
        cls.builds = build_harnesses(Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_both(self, script):
        results = [run_script(command, script) for _, command in self.builds]
        expected_rows = sum(line in ('D', 'L', 'V') for line in script.splitlines())
        for rows in results:
            self.assertEqual(len(rows), expected_rows)
        for other in results[1:]:
            self.assertEqual(results[0], other)
        return results[0]

    def test_all_pointer_positions_and_batch_sizes(self):
        cases = [(read, count) for read in range(32) for count in range(1, 32)]
        rows = self.run_both(''.join(new(n, r)+'D\n' for r, n in cases))
        for row, (read, count) in zip(rows, cases):
            with self.subTest(read=read, count=count):
                self.assertEqual(row['result'], 0)
                self.assertEqual(row['count'], count)
                self.assertEqual(row['flags'], UNKNOWN | 15)
                self.assertEqual(row['samples'], [[i, i ^ 0x15555] for i in range(count)])
                self.assertEqual(row['read'], (read + count) % 32)
                self.assertEqual(row['write'], row['read'])
                self.assertEqual(row['level'], 0)
                self.assertEqual(row['steps'], 5)
                self.assertEqual(row['data_bytes'], count * 6)

    def test_equal_pointers_fullness_overflow_and_reset(self):
        cases = [new(0), new(32), new(33), new(100), new(2, status=1)]
        rows = self.run_both(''.join(case+'D\nD\n' for case in cases))
        errors = [-errno.EAGAIN, -errno.EOVERFLOW, -errno.EOVERFLOW,
                  -errno.EOVERFLOW, -errno.ESTALE]
        for i, error in enumerate(errors):
            first, second = rows[2*i:2*i+2]
            self.assertEqual(first['result'], error)
            self.assertEqual(first['count'], 0)
            self.assertEqual(first['data_bytes'], 0)
            self.assertTrue(first['flags'] & UNKNOWN)
            if i:
                self.assertEqual(second['result'], -errno.ESTALE)
                self.assertEqual(second['steps'], 0)
                self.assertEqual(second['flags'], UNKNOWN | RESYNC)
            else:
                self.assertEqual(second['result'], -errno.EAGAIN)
        self.assertEqual(rows[6]['before'][1], 31)  # Saturating count, not exact loss.
        self.assertEqual(rows[6]['lost'], 68)

    def test_configuration_and_raw_health_diagnostics(self):
        bad = [new(mode=m) for m in (0, 2, 7, 0x43, 0x83, 0x23)]
        bad += [new(cfg=0x16), new(conversion=0xc7)]
        rows = self.run_both(''.join(case+'D\n' for case in bad))
        for row in rows:
            self.assertEqual(row['result'], -errno.EOPNOTSUPP)
            self.assertEqual(row['steps'], 1)
            self.assertEqual(row['data_bytes'], 0)
        row = self.run_both(new(1, high=1, status=0x20)+'D\n')[0]
        self.assertEqual(row['result'], 0)
        self.assertTrue(row['flags'] & 32)
        self.assertEqual(row['samples'][0], [0x800000, 0x15555])
        self.assertTrue(row['status'] & 0x20)  # Ambient-light saturation retained.
        row = self.run_both(new(2, cfg=0x66, conversion=0x23)+'D\n')[0]
        self.assertEqual(row['config'], [0x66, 3, 0x23])
        self.assertEqual(row['count'], 2)  # No invented rate or averaging semantics.

    def test_transfer_failures_quarantine_even_after_partial_consumption(self):
        cases = [(step, result, error, consumed)
                 for step in range(1, 6)
                 for result, error in ((-1, errno.EINTR), (-1, errno.EIO),
                                       (0, 0), (1, 0), (3, 0))
                 for consumed in (0, 1, 3, 6)]
        script = ''.join(new()+f'F {s} {r} {e} {c}\nD\nD\n' for s, r, e, c in cases)
        rows = self.run_both(script)
        for i, (step, result, error, consumed) in enumerate(cases):
            with self.subTest(step=step, result=result, consumed=consumed):
                first, second = rows[2*i:2*i+2]
                self.assertEqual(first['result'], -error if result < 0 else -errno.EREMOTEIO)
                self.assertEqual(first['count'], 0)
                self.assertEqual(first['steps'], step)
                self.assertEqual(first['quarantine'], 1)
                self.assertEqual(second['steps'], 0)
                self.assertEqual(second['result'], -errno.ESTALE)
                self.assertEqual(second['data_bytes'], first['data_bytes'])

    def test_inconsistent_snapshot_rejected(self):
        cases = [(3, i, 0x80, errno.EPROTO) for i in range(3)]
        cases += [(5, i, 0x80, errno.EPROTO) for i in range(3)]
        cases += [(5, 2, 1, errno.EPROTO), (5, 1, 1, errno.EOVERFLOW)]
        rows = self.run_both(''.join(new()+f'R {s} {i} {v}\nD\n' for s, i, v, _ in cases))
        for row, (_, _, _, error) in zip(rows, cases):
            self.assertEqual(row['result'], -error)
            self.assertEqual(row['count'], 0)
            self.assertEqual(row['quarantine'], 1)

    def test_arrivals_and_hidden_loss_do_not_become_continuity_claims(self):
        # Two old samples drain; three arrivals after the first snapshot remain.
        rows = self.run_both(new()+'J 4 3\nD\nD\n')
        self.assertEqual([row['count'] for row in rows], [2, 3])
        self.assertEqual(rows[1]['samples'], [[i, i ^ 0x15555] for i in range(2, 5)])
        # Thirty-one pending samples plus two late arrivals: one loss, then the
        # pop clears overflow. Both snapshots can show zero despite actual loss.
        row = self.run_both(new(31)+'J 4 2\nD\n')[0]
        self.assertEqual(row['lost'], 1)
        self.assertEqual(row['before'][1], 0)
        self.assertEqual(row['after'][1], 0)
        self.assertEqual(row['count'], 31)
        self.assertTrue(row['flags'] & UNKNOWN)
        # Filling between status and pointer reads makes equal pointers
        # ambiguous without a captured A_FULL flag. The next poll detects it.
        rows = self.run_both(new(0)+'J 3 32\nD\nD\n')
        self.assertEqual(rows[0]['result'], -errno.EAGAIN)
        self.assertEqual(rows[1]['result'], -errno.EOVERFLOW)
        self.assertEqual(rows[0]['data_bytes'], 0)

    def test_100hz_producer_with_50hz_poll_model(self):
        original = self.run_both(new(0)+('P 2\nL\n' * 100))
        drained = self.run_both(new(0)+('P 2\nD\n' * 100))
        self.assertEqual(original[-1]['lost'], 69)
        self.assertEqual(original[-1]['level'], 31)
        self.assertEqual(original[-1]['data_bytes'], 600)
        self.assertEqual(next(i + 1 for i, row in enumerate(original) if row['lost']), 32)
        self.assertEqual(sum(row['count'] for row in drained), 200)
        self.assertEqual(drained[-1]['lost'], 0)
        self.assertEqual(drained[-1]['level'], 0)
        self.assertTrue(all(row['result'] == 0 and row['flags'] & UNKNOWN for row in drained))

    def test_invalid_arguments_and_external_resynchronization(self):
        rows = self.run_both(new()+'V\nF 4 -1 5 1\nD\nD\n'+new(3, 29)+'D\n')
        self.assertEqual(rows[0]['invalid_arguments'], 6)
        self.assertEqual(rows[1]['data_bytes'], 1)
        self.assertEqual(rows[2]['steps'], 0)
        self.assertEqual(rows[3]['count'], 3)
        self.assertEqual(rows[3]['result'], 0)


if __name__ == '__main__':
    unittest.main()
