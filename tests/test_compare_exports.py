from pathlib import Path
import tempfile
import unittest

from development.compare_exports import compare, read_symvers


class ExportComparisonTests(unittest.TestCase):
    def test_different_crc_and_export_class_are_not_matches(self):
        a = {"same": {"crc": "0x00000001", "type": "EXPORT_SYMBOL"},
             "type": {"crc": "0x00000001", "type": "EXPORT_SYMBOL_GPL"},
             "crc": {"crc": "0x00000002", "type": "EXPORT_SYMBOL"},
             "extra": {"crc": "0x00000001", "type": "EXPORT_SYMBOL"}}
        b = {name: a["same"] for name in ("same", "type", "crc", "other")}
        r = compare(a, b)
        self.assertEqual(r["matching_exports"], 1)
        self.assertEqual([x["name"] for x in r["different_exports"]], ["crc", "type"])
        self.assertEqual(r["stock_only"], ["extra"])
        self.assertEqual(r["baseline_only"], ["other"])

    def test_malformed_empty_and_duplicate_tables_rejected(self):
        good = "0x00000001\tname\tvmlinux\tEXPORT_SYMBOL\n"
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "Module.symvers"
            for text in ("", "bad", good + good):
                p.write_text(text)
                with self.assertRaises(ValueError):
                    read_symvers(p)
            p.write_text(good)
            self.assertEqual(read_symvers(p)["name"]["crc"], "0x00000001")
