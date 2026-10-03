# SPDX-License-Identifier: Apache-2.0
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT/'third-party/source-snapshots/lis2hh12-pid'
START = 'S 50 2 1\n'
READY = START+'P 1 2 3\nR\n'
UNKNOWN, STOPPED, RUNNING = 0, 1, 2


def new(identity=65, reset_delay=0, address=30):
    return f'N {identity} {reset_delay} {address}\n'


def build_harnesses(output):
    builds = []
    for name, compiler, flags, runner in (
        ('host', 'cc', [], []),
        ('arm', 'arm-linux-gnueabihf-gcc', ['-marm', '-mcpu=cortex-a7', '-static'],
         ['qemu-arm', '-cpu', 'cortex-a7'])):
        if not shutil.which(compiler) or (runner and not shutil.which(runner[0])):
            if name == 'host': raise unittest.SkipTest('C compiler required')
            continue
        exe = output/name
        if name == 'host' and os.environ.get('DREEM_MOTION_SANITIZE') == '1':
            flags += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-no-pie']
        command = [compiler, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', *flags,
                   '-I', str(ROOT/'development'), '-I', str(UPSTREAM),
                   str(ROOT/'tests/motion_sensor_harness.c'),
                   str(ROOT/'development/motion_sensor.c'), str(ROOT/'development/sensor_i2c.c'),
                   str(UPSTREAM/'lis2hh12_reg.c'),
                   '-Wl,--wrap=ioctl,--wrap=__ioctl_time64,--wrap=nanosleep,--wrap=__nanosleep64',
                   '-o', str(exe)]
        p = subprocess.run(command, text=True, capture_output=True)
        if p.returncode: raise RuntimeError(p.stderr)
        builds.append((name, runner+[str(exe)]))
    return builds


class MotionSensorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='dreem-motion-sensor-')
        cls.builds = build_harnesses(Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_both(self, script):
        outputs = []
        for name, command in self.builds:
            p = subprocess.run(command, input=script, text=True, capture_output=True, timeout=25)
            self.assertEqual(p.returncode, 0, f'{name}: {p.stderr}')
            rows = [json.loads(line) for line in p.stdout.splitlines()]
            self.assertEqual(len(rows), sum(line[0] in 'STRQ' for line in script.splitlines() if line))
            outputs.append(rows)
        for other in outputs[1:]: self.assertEqual(outputs[0], other)
        return outputs[0]

    def test_upstream_snapshot_has_pinned_content_and_license(self):
        m = json.loads((UPSTREAM/'provenance.json').read_text())
        self.assertEqual(m['commit'], '09c28df1a67e2d85e4ea9f6447e6a9a983de2f0e')
        for name, digest in m['snapshot_sha256'].items():
            self.assertEqual(hashlib.sha256((UPSTREAM/name).read_bytes()).hexdigest(), digest)
        self.assertIn('BSD 3-Clause License', (UPSTREAM/'LICENSE').read_text())

    def test_lifecycle_discards_stale_and_first_fresh_sample(self):
        rows = self.run_both(new()+START+'R\nP 11 22 33\nR\nP -32768 0 32767\nR\nR\nT\nR\n'+START+'P 4 5 6\nR\n')
        start, empty, settling, sample, empty2, stop, stopped_read, restart, settling2 = rows
        self.assertEqual(start['state'], RUNNING)
        self.assertEqual(start['ctrl1'], 0xaf)
        for r in (empty, settling, empty2, settling2):
            self.assertEqual(r['result'], -errno.EAGAIN)
            self.assertTrue(r['unchanged'])
        self.assertEqual(sample['xyz'], [-32768, 0, 32767])
        self.assertEqual(sample['flags'], 1)
        self.assertEqual(stop['state'], STOPPED)
        self.assertEqual(stop['ctrl1'] & 0x70, 0)
        self.assertEqual(stopped_read['result'], -errno.EPIPE)
        self.assertEqual(stopped_read['steps'], 0)
        self.assertEqual(restart['state'], RUNNING)

    def test_all_rates_ranges_resolution_modes_and_addresses(self):
        profiles = [(rate, scale, hr, address) for rate in (10,50,100,200,400,800)
                    for scale in (2,4,8) for hr in (0,1) for address in (29,30)]
        rows = self.run_both(''.join(new(address=address)+f'S {rate} {scale} {hr}\n'
                                   for rate,scale,hr,address in profiles))
        for profile, row in zip(profiles, rows):
            rate,scale,hr,_ = profile
            self.assertEqual(row['result'], 0)
            expected = [15 | hr<<7 | ((10,50,100,200,400,800).index(rate)+1)<<4,
                        0,0,4 | {2:0,4:2,8:3}[scale]<<4,0,0,0]
            self.assertEqual(row['trace'][-1]['data'], expected)
            self.assertEqual(row['trace'][-1]['op'], 'R')
            self.assertEqual(row['trace'][-1]['reg'], 0x20)
            self.assertEqual(row['settling_rows'], 1)

    def test_invalid_calls_and_active_restart_make_no_io(self):
        profiles = [(0,2,0),(51,2,0),(1600,2,0),(50,1,0),(50,3,0),(50,16,0),(50,2,2)]
        rows = self.run_both(''.join(new()+f'S {r} {s} {h}\n' for r,s,h in profiles))
        for row in rows:
            self.assertEqual(row['result'], -errno.EINVAL)
            self.assertEqual(row['steps'], 0)
        rows = self.run_both(new()+'Q\nT\nR\n'+START+START)
        self.assertEqual(rows[0]['steps'], 0)
        self.assertEqual(rows[1]['result'], -errno.ENODEV)
        self.assertEqual(rows[2]['result'], -errno.EPIPE)
        self.assertEqual(rows[-1]['result'], -errno.EBUSY)
        self.assertEqual(rows[-1]['steps'], 0)

    def test_wrong_identity_never_writes(self):
        for row in self.run_both(''.join(new(identity=i)+START for i in (0,64,66,255))):
            self.assertEqual(row['result'], -errno.ENODEV)
            self.assertEqual(row['state'], UNKNOWN)
            self.assertEqual(row['writes'], 0)

    def test_reset_delay_timeout_and_interruption(self):
        delayed, stuck, interrupted = self.run_both(new(reset_delay=3)+START+
            new(reset_delay=0xffffffff)+START+new(reset_delay=3)+f'W {errno.EINTR}\n'+START)
        self.assertEqual(delayed['result'], 0)
        self.assertEqual(delayed['sleeps'], 2)
        self.assertEqual(stuck['result'], -errno.ETIMEDOUT)
        self.assertEqual(stuck['sleeps'], 19)
        self.assertEqual(stuck['cleanup_error'], -errno.EBUSY)
        self.assertEqual(stuck['state'], UNKNOWN)
        self.assertEqual(interrupted['result'], -errno.EINTR)

    def test_each_start_transfer_fault_stops_and_preserves_original_error(self):
        trace = self.run_both(new()+START)[0]['trace']
        cases = [(i, result, error, consumed) for i,op in enumerate(trace,1)
                 for result,error in ((-1,errno.EIO),(0,0),(2 if op['op']=='W' else 3,0))
                 for consumed in (0,1,7)]
        rows = self.run_both(''.join(new()+f'F {i} {r} {e} {n}\n'+START+'R\n'
                                   for i,r,e,n in cases))
        for (i,r,e,n), failed, read in zip(cases,rows[::2],rows[1::2]):
            with self.subTest(step=i,result=r,consumed=n):
                self.assertEqual(failed['result'], -e if r<0 else -errno.EREMOTEIO)
                self.assertEqual(failed['last_error'], failed['result'])
                self.assertNotEqual(failed['state'], RUNNING)
                self.assertEqual(read['result'], -errno.EPIPE)
                self.assertTrue(read['unchanged'])

    def test_each_read_transfer_failure_withholds_output(self):
        trace = self.run_both(new()+READY+'P 100 -200 300\nR\n')[-1]['trace']
        cases = [(i,n) for i in range(1,len(trace)+1) for n in (0,1,5,6,7)]
        rows = self.run_both(''.join(new()+READY+'P 100 -200 300\n'+f'F {i} -1 {errno.EIO} {n}\nR\nR\n'
                                   for i,n in cases))
        for i in range(0,len(rows),4):
            failed,again = rows[i+2:i+4]
            self.assertEqual(failed['result'], -errno.EIO)
            self.assertTrue(failed['unchanged'])
            self.assertEqual(failed['state'], STOPPED)
            self.assertEqual(again['result'], -errno.EPIPE)

    def test_configuration_change_quarantines_without_data_publication(self):
        mutations = [(0x20,0x2f),(0x21,4),(0x22,0x80),(0x23,0),(0x24,4),
                     (0x25,1),(0x26,1),(0x1e,1),(0x1f,1),(0x2e,0x40)]
        rows = self.run_both(''.join(new()+READY+f'V {r} {v}\nR\n' for r,v in mutations))
        for row in rows[2::3]:
            self.assertEqual(row['result'], -errno.EPROTO)
            self.assertTrue(row['unchanged'])
            self.assertEqual(row['state'], STOPPED)
            self.assertFalse(any(t['reg']==0x28 for t in row['trace']))

    def test_readback_mismatch_prevents_start_and_publication(self):
        start = self.run_both(new()+START)[0]
        i = next(i for i,t in enumerate(start['trace'],1) if t['reg']==0x20 and len(t['data'])==7)
        fail = self.run_both(new()+f'C {i} 0 0\n'+START)[0]
        self.assertEqual(fail['result'], -errno.EPROTO)
        self.assertEqual(fail['state'], STOPPED)
        rows = self.run_both(new()+READY+'P 1 2 3\nC 7 0 0\nR\n')
        self.assertEqual(rows[-1]['result'], -errno.EPROTO)
        self.assertTrue(rows[-1]['unchanged'])

    def test_overrun_is_reported_and_new_post_read_data_is_left_pending(self):
        rows = self.run_both(new()+READY+'P 10 20 30\nP 40 50 60\nR\n'+
                            'P 70 80 90\nI 6 100 110 120\nR\nR\n')
        self.assertEqual(rows[2]['xyz'], [10,20,30])
        self.assertEqual(rows[2]['flags'], 3)
        self.assertEqual(rows[3]['xyz'], [70,80,90])
        self.assertTrue(rows[3]['after']&8)
        self.assertEqual(rows[4]['xyz'], [100,110,120])

    def test_cleanup_failure_and_explicit_stop_retry(self):
        rows = self.run_both(new()+READY+'P 1 2 3\nF 5 -1 5 1\nG 6 -1 6 0\nR\nR\nT\n'+START)
        failed,again,stop,restart = rows[2:]
        self.assertEqual(failed['last_error'], -errno.EIO)
        self.assertEqual(failed['cleanup_error'], -errno.ENXIO)
        self.assertEqual(failed['state'], UNKNOWN)
        self.assertNotEqual(failed['ctrl1']&0x70, 0)
        self.assertTrue(failed['unchanged'])
        self.assertEqual(again['steps'], 0)
        self.assertEqual(stop['state'], STOPPED)
        self.assertEqual(restart['state'], RUNNING)

    def test_each_stop_failure_retains_unknown_state_until_explicit_retry(self):
        base = self.run_both(new()+READY+'T\n')[-1]['trace']
        cases = [(i,n) for i in range(1,len(base)+1) for n in (0,1)]
        rows = self.run_both(''.join(new()+READY+f'F {i} -1 5 {n}\nT\nR\nT\n'
                                   for i,n in cases))
        for i in range(0,len(rows),5):
            stop,read,retry = rows[i+2:i+5]
            self.assertEqual(stop['result'], -errno.EIO)
            self.assertEqual(stop['state'], UNKNOWN)
            self.assertEqual(read['result'], -errno.EPIPE)
            self.assertEqual(read['steps'], 0)
            self.assertEqual(retry['result'], 0)
            self.assertEqual(retry['state'], STOPPED)
        n = len(base)
        row = self.run_both(new()+READY+f'C {n} 0 175\nT\n')[-1]
        self.assertEqual(row['result'], -errno.EPROTO)
        self.assertEqual(row['state'], UNKNOWN)

    def test_shutdown_clears_activity_override_and_checks_readback(self):
        row = self.run_both(new()+READY+'V 30 1\nT\n')[-1]
        self.assertEqual(row['result'], 0)
        clear = next(i for i,t in enumerate(row['trace']) if t['op']=='W' and t['reg']==30)
        self.assertEqual(row['trace'][clear+1], {'op':'R','reg':30,'result':2,'data':[0]})
        failed = self.run_both(new()+READY+f'V 30 1\nC {clear+2} 0 1\nT\n')[-1]
        self.assertEqual(failed['result'], -errno.EPROTO)
        self.assertEqual(failed['state'], UNKNOWN)


if __name__ == '__main__':
    unittest.main()
