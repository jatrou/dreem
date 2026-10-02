import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class EegQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.commands = [[str(ROOT / "development/build/eeg_quality.host")]]
        if not Path(cls.commands[0][0]).exists():
            raise unittest.SkipTest("run sh development/build.sh first")
        arm = ROOT / "development/build/eeg_quality.arm"
        if shutil.which("qemu-arm") and arm.exists():
            cls.commands.append(["qemu-arm", "-cpu", "cortex-a7", str(arm)])

    def run_feature(self, command, path, *args):
        p = subprocess.run(command + list(args) + [str(path)], capture_output=True, text=True, timeout=5)
        self.assertEqual(p.returncode, 0, p.stderr)
        return [json.loads(line) for line in p.stdout.splitlines()]

    def test_known_sine_constant_invalid_and_partial_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "eeg.data"
            path.write_bytes(b"".join(struct.pack("<4f", math.sin(2 * math.pi * 10 * i / 250),
                                               3, float("nan"), (-1) ** i) for i in range(250)) + b"\x01")
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            results = []
            for command in self.commands:
                data = self.run_feature(command, path)
                channels = data[0]["channels"]
                self.assertAlmostEqual(channels[0]["rms"], math.sqrt(0.5), places=6)
                self.assertAlmostEqual(channels[0]["mean"], 0, places=6)
                self.assertEqual(channels[1]["rms"], 3)
                self.assertEqual(channels[1]["ac_rms"], 0)
                self.assertEqual(channels[2]["invalid"], 250)
                self.assertIsNone(channels[2]["rms"])
                self.assertEqual(channels[3]["peak_to_peak"], 2)
                self.assertEqual(data[1]["bytes_remaining"], 1)
                results.append(data)
            self.assertTrue(all(r == results[0] for r in results))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_follow_waits_for_complete_rows(self):
        for command in self.commands:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "eeg.data"
                rows = struct.pack("<4f", 1, 2, 3, 4) * 500
                path.write_bytes(rows[:3999])
                p = subprocess.Popen(command + ["--follow-seconds", "0.8", str(path)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                time.sleep(0.15)
                with path.open("ab") as stream:
                    stream.write(rows[3999:])
                out, err = p.communicate(timeout=5)
                self.assertEqual(p.returncode, 0, err)
                data = [json.loads(line) for line in out.splitlines()]
                self.assertEqual([x["sample_start"] for x in data[:-1]], [0, 250])
                self.assertEqual(data[-1]["rows_consumed"], 500)

    def test_nonregular_and_symlink_inputs_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "regular").write_bytes(b"")
            (path / "link").symlink_to(path / "regular")
            os.mkfifo(path / "fifo")
            for command in self.commands:
                for name in ("link", "fifo"):
                    p = subprocess.run(command + [str(path / name)], capture_output=True, timeout=3)
                    self.assertEqual(p.returncode, 1)


if __name__ == "__main__":
    unittest.main()
