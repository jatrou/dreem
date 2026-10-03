# SPDX-License-Identifier: Apache-2.0
"""Connected sensor lifecycle, FIFO and checked transport against a chip model."""
import errno
from pathlib import Path
import tempfile
import unittest

from tests.test_optical_fifo import build_harnesses, new, run_script

START = 'S 10 60 100\n'
UNKNOWN, STOPPED, RUNNING = 0, 1, 2


class OpticalSensorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='dreem-optical-sensor-')
        cls.builds = build_harnesses(Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_both(self, script):
        results = [run_script(command, script) for _, command in self.builds]
        expected = sum(line.split()[0] in ('D', 'L', 'V', 'S', 'T', 'B', 'W')
                       for line in script.splitlines() if line.strip())
        for rows in results:
            self.assertEqual(len(rows), expected)
        for rows in results[1:]:
            self.assertEqual(results[0], rows)
        return results[0]

    def baseline(self):
        return self.run_both(new(0)+START)[0]

    def test_connected_reset_start_read_stop_and_explicit_restart(self):
        # Begin with a legacy reader that has consumed only one byte of a frame.
        rows = self.run_both(new(5)+'F 4 -1 5 1\nD\n'+START+
                             'P 3\nB\nT\nP 100\nB\n'+START+'P 2\nB\n')
        partial, started, batch, stopped, closed_read, restarted, batch2 = rows
        self.assertEqual(partial['data_bytes'], 1)
        self.assertEqual(started['state'], RUNNING)
        self.assertEqual(started['level'], 0)
        self.assertEqual(batch['samples'], [[i, i ^ 0x15555] for i in (5, 6, 7)])
        self.assertEqual(stopped['state'], STOPPED)
        self.assertEqual(stopped['mode'], 0x83)
        self.assertEqual(closed_read['result'], -errno.EPIPE)
        self.assertEqual(closed_read['steps'], 0)
        self.assertEqual(closed_read['level'], 0)  # Shutdown suppresses conversions.
        self.assertEqual(restarted['state'], RUNNING)
        self.assertEqual(batch2['samples'], [[i, i ^ 0x15555] for i in (8, 9)])
        self.assertTrue(batch2['flags'] & 128)

    def test_profiles_are_read_back_before_activation(self):
        cases = [(rate, red, ir) for rate in (50, 100, 200, 400)
                 for red in (0, 10, 255) for ir in (0, 60, 255)]
        rows = self.run_both(''.join(new(0)+f'S {red} {ir} {rate}\nP 2\nB\n'
                                      for rate, red, ir in cases))
        for i, (rate, red, ir) in enumerate(cases):
            start, batch = rows[2*i:2*i+2]
            self.assertEqual(start['result'], 0)
            self.assertEqual(start['state'], RUNNING)
            self.assertEqual(batch['count'], 2)
            trace = start['trace']
            rate_code = (50, 100, 200, 400).index(rate)
            self.assertIn({'op':'R', 'reg':8, 'result':2, 'data':[6, 0x83, 0x43 | rate_code << 2]}, trace)
            self.assertIn({'op':'R', 'reg':12, 'result':2, 'data':[red, ir]}, trace)
            self.assertEqual(trace[-2], {'op':'W', 'reg':9, 'result':1, 'data':[3]})
            self.assertFalse(any(t['reg'] == 0x30 for t in trace))

    def test_invalid_profile_null_arguments_and_running_restart(self):
        bad = [(256, 60, 100), (10, 256, 100)] + [(10, 60, n) for n in (0, 49, 51, 800, 3200)]
        rows = self.run_both(''.join(new(0)+f'S {r} {i} {s}\n' for r, i, s in bad))
        for row in rows:
            self.assertEqual(row['result'], -errno.EINVAL)
            self.assertEqual(row['steps'], 0)
            self.assertEqual(row['writes'], 0)
        rows = self.run_both(new(0)+'W\nB\nT\n'+START+START+'P 2\nB\n')
        self.assertEqual(rows[0]['invalid_sensor_arguments'], 8)
        self.assertEqual(rows[1]['result'], -errno.EPIPE)
        self.assertEqual(rows[2]['result'], -errno.ENODEV)
        self.assertEqual(rows[2]['steps'], 0)
        self.assertEqual(rows[4]['result'], -errno.EBUSY)
        self.assertEqual(rows[4]['steps'], 0)
        self.assertEqual(rows[4]['state'], RUNNING)
        self.assertEqual(rows[5]['count'], 2)

    def test_identity_failure_never_writes_to_unknown_target(self):
        rows = self.run_both(''.join(new(0)+f'I {part} 255\n'+START+'T\n'
                                      for part in (0, 0x14, 0xff)))
        for row in rows:
            self.assertEqual(row['result'], -errno.ENODEV)
            self.assertEqual(row['state'], UNKNOWN)
            self.assertEqual(row['writes'], 0)
            self.assertEqual(row['identified'], 0)
        row = self.run_both(new(0)+'I 21 255\n'+START)[0]
        self.assertEqual(row['revision'], 255)  # Retained, never claimed unique identity.

    def test_every_start_transfer_fault_preserves_error_and_prevents_reads(self):
        baseline = self.baseline()['trace']
        cases = []
        for step, op in enumerate(baseline, 1):
            for result, error in ((-1, errno.EIO), (0, 0), (2 if op['op'] == 'W' else 3, 0)):
                for consumed in (0, 1, 3):
                    cases.append((step, result, error, consumed))
        rows = self.run_both(''.join(new(0)+f'F {s} {r} {e} {c}\n'+START+'B\n'
                                      for s, r, e, c in cases))
        for i, (step, result, error, consumed) in enumerate(cases):
            with self.subTest(step=step, result=result, consumed=consumed):
                failed, read = rows[2*i:2*i+2]
                expected = -error if result < 0 else -errno.EREMOTEIO
                self.assertEqual(failed['result'], expected)
                self.assertEqual(failed['last_error'], expected)
                self.assertEqual(failed['cleanup_error'], 0)
                self.assertEqual(failed['state'], UNKNOWN if step == 1 else STOPPED)
                self.assertEqual(failed['quarantine'], 1)
                self.assertEqual(read['result'], -errno.EPIPE)
                self.assertEqual(read['steps'], 0)
                self.assertEqual(read['count'], 0)

    def test_primary_and_cleanup_failures_remain_separate(self):
        final = len(self.baseline()['trace'])
        rows = self.run_both(''.join(new(0)+f'F {final} -1 {errno.EREMOTEIO} 0\n'
                                    f'X {final+offset} -1 {errno.EIO} 0\n'+START+'B\nT\n'
                                    for offset in (1, 2, 3)))
        for i in range(3):
            failed, read, stopped = rows[3*i:3*i+3]
            self.assertEqual(failed['result'], -errno.EREMOTEIO)
            self.assertEqual(failed['cleanup_error'], -errno.EIO)
            self.assertEqual(failed['state'], UNKNOWN)
            self.assertEqual(read['steps'], 0)
            self.assertEqual(stopped['result'], 0)
            self.assertEqual(stopped['state'], STOPPED)

    def test_reset_poll_bound_and_interrupted_delay(self):
        delays = (0, 1, 2, 19, 20, 21, 50, 0xffffffff)
        rows = self.run_both(''.join(new(0)+f'H {n} 0\n'+START for n in delays))
        for row, n in zip(rows, delays):
            if n <= 20:
                self.assertEqual(row['result'], 0)
                self.assertEqual(row['state'], RUNNING)
                self.assertEqual(row['sleeps'], max(0, n-1))
            else:
                self.assertEqual(row['result'], -errno.ETIMEDOUT)
                self.assertEqual(row['sleeps'], 19)
                self.assertEqual(row['reset_reads'], 21)  # Twenty polls + cleanup check.
                self.assertEqual(row['state'], STOPPED if n == 21 else UNKNOWN)
                self.assertEqual(row['cleanup_error'], 0 if n == 21 else -errno.EBUSY)
        rows = self.run_both(''.join(new(0)+f'H {n} {errno.EINTR}\n'+START for n in (2, 3)))
        for row in rows:
            self.assertEqual(row['result'], -errno.EINTR)
            self.assertEqual(row['sleeps'], 1)
        self.assertEqual(rows[0]['state'], STOPPED)
        self.assertEqual(rows[1]['state'], UNKNOWN)

    def test_configuration_readback_and_power_event_reject_activation(self):
        trace = self.baseline()['trace']
        checks = [(i, j, byte ^ 1) for i, op in enumerate(trace, 1)
                  if op['op'] == 'R' and op['reg'] in (2, 4, 8, 12)
                  for j, byte in enumerate(op['data'])]
        # Both shutdown and final activation readbacks must be checked.
        checks += [(i, 0, op['data'][0] ^ 1) for i, op in enumerate(trace, 1)
                   if op['op'] == 'R' and op['reg'] == 9 and op['data'][0] in (3, 0x83)]
        rows = self.run_both(''.join(new(0)+f'R {i} {j} {value}\n'+START for i, j, value in checks))
        for row in rows:
            self.assertEqual(row['result'], -errno.EPROTO)
            self.assertEqual(row['state'], STOPPED)
        status_step = max(i for i, op in enumerate(trace, 1) if op['op'] == 'R' and op['reg'] == 0)
        row = self.run_both(new(0)+f'R {status_step} 0 1\n'+START)[0]
        self.assertEqual(row['result'], -errno.ESTALE)
        self.assertEqual(row['state'], STOPPED)

    def test_acquisition_fault_stops_and_requires_explicit_reset(self):
        rows = self.run_both(''.join(new(0)+START+'P 2\n'+f'F {step} -1 5 {n}\nB\nB\n'
                                    for step in range(1, 6) for n in (0, 1, 3)))
        for i in range(15):
            start, failed, read = rows[3*i:3*i+3]
            self.assertEqual(start['state'], RUNNING)
            self.assertEqual(failed['result'], -errno.EIO)
            self.assertEqual(failed['state'], STOPPED)
            self.assertEqual(failed['mode'], 0x83)
            self.assertEqual(failed['count'], 0)
            self.assertEqual(read['steps'], 0)
        rows = self.run_both(new(0)+START+'P 2\nF 4 -1 5 1\nB\n'+START+'P 2\nB\n')
        self.assertEqual(rows[-1]['samples'], [[i, i ^ 0x15555] for i in (2, 3)])

    def test_stop_faults_never_claim_confirmed_shutdown(self):
        rows = self.run_both(''.join(new(0)+START+f'F {step} -1 5 {n}\nT\nB\nT\n'
                                    for step in (1, 2, 3) for n in (0, 1)))
        for i in range(6):
            start, failed, read, stopped = rows[4*i:4*i+4]
            self.assertEqual(start['state'], RUNNING)
            self.assertEqual(failed['result'], -errno.EIO)
            self.assertEqual(failed['state'], UNKNOWN)
            self.assertEqual(read['steps'], 0)
            self.assertEqual(stopped['state'], STOPPED)
            self.assertEqual(stopped['mode'], 0x83)

    def test_connected_brownout_and_overflow_stop_acquisition(self):
        rows = self.run_both(new(0)+START+'U\nB\n'+START+'P 40\nB\n')
        self.assertEqual(rows[1]['result'], -errno.EOPNOTSUPP)
        self.assertEqual(rows[1]['state'], STOPPED)
        self.assertEqual(rows[3]['result'], -errno.EOVERFLOW)
        self.assertEqual(rows[3]['state'], STOPPED)
        self.assertEqual(rows[3]['count'], 0)

    def test_sustained_polling_uses_batch_reader_without_reinitialization(self):
        rows = self.run_both(new(0)+START+'B\n'+('P 2\nB\n' * 100)+'T\n')
        self.assertEqual(rows[1]['result'], -errno.EAGAIN)
        self.assertEqual(rows[1]['state'], RUNNING)
        self.assertEqual(sum(row['count'] for row in rows), 200)
        self.assertTrue(all(row['lost'] == 0 for row in rows))
        self.assertTrue(all(row['state'] == RUNNING for row in rows[:-1]))
        self.assertTrue(all(row['writes'] == rows[0]['writes'] for row in rows[:-1]))
        self.assertEqual(rows[-1]['state'], STOPPED)


if __name__ == '__main__':
    unittest.main()
