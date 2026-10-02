import gzip
import io
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest

from development.inspect_firmware import (
    gunzip_member, parse_fdt, recover_kernel, selected_members, upstream_config_gaps,
)


class FirmwareInspectionTests(unittest.TestCase):
    def test_kernel_recovery_skips_false_gzip_and_trailing_bytes(self):
        config = b"CONFIG_KALLSYMS=y\nCONFIG_IKCONFIG=y\n"
        raw = b"kernel" + b"IKCFG_ST" + gzip.compress(config) + b"IKCFG_ED" + b"tail"
        prefix = b"header\x1f\x8b\x08invalid"
        recovered, actual, offset = recover_kernel(prefix + gzip.compress(raw) + b"trailer")
        self.assertEqual((recovered, actual, offset), (raw, config.decode(), len(prefix)))

    def test_truncation_and_expansion_limit(self):
        data = gzip.compress(b"a" * 1000)
        for blob, limit in [(data[:-4], 2000), (data, 100)]:
            with self.assertRaises(ValueError):
                gunzip_member(blob, limit)

    def test_symlinks_and_duplicates_rejected(self):
        for kind in ("symlink", "duplicate"):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as archive:
                member = tarfile.TarInfo("zImage")
                if kind == "symlink":
                    member.type = tarfile.SYMTYPE
                    member.linkname = "/etc/shadow"
                    archive.addfile(member)
                else:
                    archive.addfile(member)
                    archive.addfile(member)
            buf.seek(0)
            with tarfile.open(fileobj=buf) as archive, self.assertRaises(ValueError):
                selected_members(archive, {"zImage"})

    def test_archive_allowlist_does_not_read_arbitrary_paths(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as archive:
            for name in ("../../credential", "./zImage"):
                m = tarfile.TarInfo(name)
                m.size = 2
                archive.addfile(m, io.BytesIO(b"ok"))
        buf.seek(0)
        with tarfile.open(fileobj=buf) as archive:
            self.assertEqual(selected_members(archive, {"zImage"}), {"zImage": b"ok"})

    def test_fdt_bounds_and_structure(self):
        # Root, model property, child, end child/root, end tree.
        block = struct.pack(">I", 1) + b"\0" * 4
        block += struct.pack(">III", 3, 4, 0) + b"dev\0"
        block += struct.pack(">I", 1) + b"bus\0" + struct.pack(">III", 2, 2, 9)
        strings = b"model\0"
        total = 40 + len(block) + len(strings)
        header = struct.pack(">10I", 0xD00DFEED, total, 40, 40 + len(block), 0,
                             17, 16, 0, len(strings), len(block))
        data = header + block + strings
        self.assertEqual(parse_fdt(data), {"/": {"model": b"dev\0"}, "/bus": {}})
        for bad in (data[:-1], b"no", data[:8] + struct.pack(">I", total + 1) + data[12:]):
            with self.assertRaises(ValueError):
                parse_fdt(bad)

    def test_config_match_ignores_comments_and_includes_menuconfig(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "Kconfig").write_text("config COMMON\nmenuconfig SHARED\n# config CUSTOM\n")
            self.assertEqual(upstream_config_gaps(
                "CONFIG_COMMON=y\nCONFIG_SHARED=m\nCONFIG_CUSTOM=y\n# CONFIG_OFF is not set\n", p
            ), {"CUSTOM": "y"})


if __name__ == "__main__":
    unittest.main()
