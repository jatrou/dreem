import struct
import unittest

try:
    from development.recover_exports import module_versions
except ModuleNotFoundError as exc:
    if not exc.name.startswith("elftools"):
        raise
    module_versions = None


@unittest.skipIf(module_versions is None, "install development/requirements.txt")
class KernelExportTests(unittest.TestCase):
    def test_arm32_record_crc_and_name(self):
        data = struct.pack("<I60s", 0xAE63FF3F, b"module_layout")
        self.assertEqual(module_versions(data), {"module_layout": 0xAE63FF3F})

    def test_malformed_version_records_rejected(self):
        record = struct.pack("<I60s", 1, b"printk")
        for data in (record[:-1], record + record, struct.pack("<I", 1) + b"x" * 60,
                     struct.pack("<I60s", 1, b"")):
            with self.assertRaises(ValueError):
                module_versions(data)
