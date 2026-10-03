# SPDX-License-Identifier: GPL-2.0-only
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from development.sdma_assemble import assemble
from development.verify_sdma_program import verify

ROOT = Path(__file__).resolve().parents[1]


class SdmaProgramTests(unittest.TestCase):
    def test_assembled_program_execution(self):
        report = verify((ROOT / "development/sdma_acquire.asm").read_text())
        self.assertGreater(report["passed_cases"], 0)
        self.assertFalse(report["runtime_qualified"])

    def test_labels_and_manual_encodings(self):
        program, labels = assemble("start: ldi r7,255\n bt end\n bt start\nend: done 4")
        self.assertEqual(program, struct.pack("<4H", 0x0FFF, 0x7D01, 0x7DFD, 0x0400))
        self.assertEqual(labels, {"start": 0, "end": 3})

    def test_branch_range_boundaries(self):
        program, _ = assemble("bt end\n" + "done 0\n" * 127 + "end: done 4")
        self.assertEqual(program[:2], b"\x7f\x7d")
        program, _ = assemble("start: done 0\n" + "done 0\n" * 126 + "bt start")
        self.assertEqual(program[-2:], b"\x80\x7d")
        for source in ("bt end\n" + "done 0\n" * 128 + "end: done 4",
                       "start: done 0\n" + "done 0\n" * 127 + "bt start"):
            with self.assertRaises(ValueError):
                assemble(source)

    def test_malformed_or_unrelocatable_program_rejected(self):
        for source in ("", "ldi r8,0", "ldi r0,256", "ldi r0,-1", "bt missing",
                       "bt 0", "jmp 0x1800", "loop 3,0", "done 5", "notify 0",
                       "x: done 4\nx: done 4", "9: done 4", "ldi r0,0,1",
                       "btsti r0,32", "clrf 4", "ldi r0,__import__('os')",
                       "done 0\n" * 513):
            with self.subTest(source=source[:40]), self.assertRaises(ValueError):
                assemble(source)

    def test_cli_does_not_overwrite_and_writes_private_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "program.bin"
            cmd = [sys.executable, str(ROOT / "development/sdma_assemble.py"),
                   str(ROOT / "development/sdma_acquire.asm"), str(output)]
            subprocess.run(cmd, capture_output=True, check=True)
            data = output.read_bytes()
            if os.name == "posix":
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            result = subprocess.run(cmd, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_bytes(), data)

    def test_optimized_interpreter_cannot_report_unchecked_success(self):
        result = subprocess.run([sys.executable, "-O", "-m", "development.verify_sdma_program"],
                                cwd=ROOT, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b'"passed_cases"', result.stdout)
