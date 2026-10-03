# SPDX-License-Identifier: Apache-2.0
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

from development.build_feature_trial import build

ROOT = Path(__file__).resolve().parents[1]


def running(pid):
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
        return text[text.rfind(')')+2:].split()[0] not in ('Z', 'X')
    except (FileNotFoundError, ProcessLookupError):
        return False


class TrialExecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        host=ROOT/'development/build/trial_exec.host'
        if not host.is_file(): raise unittest.SkipTest('run sh development/build.sh first')
        cls.commands=[[str(host)]]
        if shutil.which('qemu-arm'):
            cls.commands.append(['qemu-arm','-cpu','cortex-a7',str(ROOT/'development/build/trial_exec.arm')])

    def invoke(self, command, root, args, limit='2', status=0, **kwargs):
        report=root/'report.json'
        p=subprocess.run(command+[limit,str(report),'--']+args,capture_output=True,text=True,timeout=5,**kwargs)
        self.assertEqual(p.returncode,status,p.stderr)
        data=json.loads(report.read_text())
        self.assertFalse(data['monitor_error'])
        return p,data

    def test_status_streams_resources_and_lower_priority(self):
        for command in self.commands:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                code='import os,sys; a=bytearray(4*1024*1024); sum(i*i for i in range(500000)); print(os.getpriority(os.PRIO_PROCESS,0)); print("child stderr",file=sys.stderr); sys.exit(7)'
                p,data=self.invoke(command,Path(tmp),[sys.executable,'-c',code],status=7)
                self.assertGreaterEqual(int(p.stdout.strip()),10)
                self.assertEqual(p.stderr.strip(),'child stderr')
                self.assertEqual(data['exit_code'],7)
                self.assertIsNone(data['exec_errno'])
                self.assertIsNone(data['signal'])
                self.assertFalse(data['timed_out'])
                self.assertFalse(data['interrupted'])
                self.assertGreater(data['max_rss_kib'],0)
                self.assertGreater(data['user_seconds']+data['system_seconds'],0)
                self.assertGreater(data['wall_seconds'],0)

    def test_exec_failure_and_child_signal_are_distinct(self):
        for command in self.commands:
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                _,data=self.invoke(command,root,['/nonexistent-dreem-trial-command'],status=127)
                self.assertEqual(data['exec_errno'],2)
                (root/'report.json').unlink()
                _,data=self.invoke(command,root,['/bin/sh','-c','kill -TERM $$'],status=143)
                self.assertIsNone(data['exec_errno'])
                self.assertIsNone(data['exit_code'])
                self.assertEqual(data['signal'],15)

    def test_timeout_stops_own_process_group_and_preserves_sibling(self):
        for command in self.commands:
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); marker=root/'child.pid'
                sibling=subprocess.Popen(['/bin/sleep','10'])
                try:
                    _,data=self.invoke(command,root,['/bin/sh','-c','sleep 10 & echo $! > "$1"; wait','trial',str(marker)],limit='0.2',status=124)
                    self.assertTrue(data['timed_out'])
                    self.assertEqual(data['signal'],9)
                    self.assertLess(data['wall_seconds'],2)
                    self.assertIsNone(sibling.poll())
                    pid=int(marker.read_text())
                    deadline=time.monotonic()+1
                    while running(pid) and time.monotonic()<deadline: time.sleep(0.01)
                    self.assertFalse(running(pid),'trial descendant remained running')
                finally:
                    if sibling.poll() is None: sibling.kill()
                    sibling.wait()

    def test_interruption_and_parent_death(self):
        for command in self.commands:
            for sig in (signal.SIGTERM,signal.SIGKILL):
                with self.subTest(command=command,signal=sig), tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp); marker=root/'child.pid'; report=root/'report.json'
                    p=subprocess.Popen(command+['4',str(report),'--','/bin/sh','-c','echo $$ > "$1"; exec sleep 10','trial',str(marker)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                    try:
                        deadline=time.monotonic()+2
                        while (not marker.exists() or marker.stat().st_size == 0) and p.poll() is None and time.monotonic()<deadline: time.sleep(0.01)
                        self.assertTrue(marker.exists(),'child did not start')
                        pid=int(marker.read_text())
                        p.send_signal(sig)
                        p.communicate(timeout=3)
                        deadline=time.monotonic()+1
                        while running(pid) and time.monotonic()<deadline: time.sleep(0.01)
                        self.assertFalse(running(pid),'direct child survived monitor termination')
                        if sig==signal.SIGTERM:
                            self.assertEqual(p.returncode,143)
                            data=json.loads(report.read_text())
                            self.assertTrue(data['interrupted'])
                            self.assertFalse(data['timed_out'])
                    finally:
                        if p.poll() is None: p.kill(); p.communicate()

    def test_inherited_sigchld_ignore_does_not_auto_reap_child(self):
        for command in self.commands:
            with tempfile.TemporaryDirectory() as tmp:
                _,data=self.invoke(command,Path(tmp),['/bin/true'],preexec_fn=lambda:signal.signal(signal.SIGCHLD,signal.SIG_IGN))
                self.assertEqual(data['exit_code'],0)

    def test_report_is_exclusive_and_bad_durations_do_not_launch(self):
        for command in self.commands:
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); report=root/'report.json'; marker=root/'launched'
                args=['--','/bin/sh','-c','touch "$1"','trial',str(marker)]
                for duration in ('','0','-1','nan','inf','61','invalid'):
                    p=subprocess.run(command+[duration,str(report)]+args,capture_output=True,timeout=3)
                    self.assertEqual(p.returncode,2)
                    self.assertFalse(report.exists())
                    self.assertFalse(marker.exists())
                report.write_text('preserve me')
                p=subprocess.run(command+['1',str(report)]+args,capture_output=True,timeout=3)
                self.assertEqual(p.returncode,125)
                self.assertEqual(report.read_text(),'preserve me')
                self.assertFalse(marker.exists())
                report.unlink(); report.symlink_to(root/'missing')
                p=subprocess.run(command+['1',str(report)]+args,capture_output=True,timeout=3)
                self.assertEqual(p.returncode,125)
                self.assertFalse((root/'missing').exists())
                self.assertFalse(marker.exists())


class FeatureTrialBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('qemu-arm'): raise unittest.SkipTest('qemu-arm required')
        cls.tmp=tempfile.TemporaryDirectory()
        cls.base=Path(cls.tmp.name)/'trial bundle'
        cls.description=build(cls.base)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def run_bundle(self, path, *args, status=0):
        p=subprocess.run([str(path/'run_feature_trial.sh'),*args],capture_output=True,text=True,timeout=30)
        self.assertEqual(p.returncode,status,p.stderr)
        return p

    def test_emulated_bundle_checks_six_cases_and_reports_resources(self):
        p=self.run_bundle(self.base,'--emulated',shutil.which('qemu-arm'))
        result=Path(next(s.removeprefix('Results: ') for s in p.stdout.splitlines() if s.startswith('Results: ')))
        report=json.loads((result/'result.json').read_text())
        self.assertEqual(report,{'mode':'emulated','fixture_cases':6,'recorder_process_preserved':None,'native_recording_fidelity_tested':False})
        metrics=list(result.glob('*.metrics.json'))
        self.assertEqual(len(metrics),6)
        for path in metrics:
            data=json.loads(path.read_text())
            self.assertFalse(data['timed_out'])
            self.assertFalse(data['monitor_error'])
            self.assertIsNone(data['exec_errno'])
            self.assertGreater(data['wall_seconds'],0)
        subprocess.run(['sha256sum','-c','SHA256SUMS'],cwd=self.base,stdout=subprocess.DEVNULL,check=True)

    def test_archive_contains_only_manifested_regular_files(self):
        archive=Path(self.description['archive'])
        self.assertEqual(archive.stat().st_mode & 0o777,0o600)
        expected={s.split('  ',1)[1] for s in (self.base/'SHA256SUMS').read_text().splitlines()}|{'SHA256SUMS'}
        with tarfile.open(archive) as tar:
            members=tar.getmembers()
            self.assertEqual({m.name.removeprefix(self.base.name+'/') for m in members},expected)
            self.assertTrue(all(m.isfile() and not m.issym() and m.mtime==0 for m in members))
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(),self.description['archive_sha256'])

    def test_changed_fixture_fails_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'altered'; shutil.copytree(self.base,path,ignore=shutil.ignore_patterns('results.*'))
            file=path/'fixtures/normal/eeg.data'; file.write_bytes(file.read_bytes()+b'x')
            self.run_bundle(path,'--emulated',shutil.which('qemu-arm'),status=1)
            self.assertFalse(list(path.glob('results.*')))

    def test_wrong_expected_output_fails_even_with_updated_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'wrong-output'; shutil.copytree(self.base,path,ignore=shutil.ignore_patterns('results.*'))
            relative='expected/eeg_quality.stdout'
            file=path/relative; file.write_text('wrong output\n')
            manifest=path/'SHA256SUMS'; lines=manifest.read_text().splitlines()
            manifest.write_text('\n'.join(hashlib.sha256(file.read_bytes()).hexdigest()+'  '+relative if s.endswith('  '+relative) else s for s in lines)+'\n')
            self.run_bundle(path,'--emulated',shutil.which('qemu-arm'),status=1)
            self.assertTrue(list(path.glob('results.*/eeg_quality.metrics.json')))
            self.assertFalse(list(path.glob('results.*/result.json')))

    def test_native_guard_and_existing_output_protection(self):
        if os.uname().machine.startswith('armv7'): self.skipTest('native architecture is ARMv7')
        self.run_bundle(self.base,status=1)
        old=(self.base/'manifest.json').read_bytes()
        with self.assertRaisesRegex(ValueError,'already exists'): build(self.base)
        self.assertEqual((self.base/'manifest.json').read_bytes(),old)


if __name__=='__main__': unittest.main()
