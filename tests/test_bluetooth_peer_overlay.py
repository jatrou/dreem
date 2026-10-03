# SPDX-License-Identifier: Apache-2.0
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

from development.build_bluetooth_peer_overlay import (BASE, CODE, PHDR, append_rx, arm_branch,
                                                      headers, normalize_peers, patch_core,
                                                      read_regular, write_private)

ROOT = Path(__file__).resolve().parents[1]


class PeerOverlayTests(unittest.TestCase):
    def test_peer_configuration_rejects_ambiguous_or_injectable_input(self):
        valid = {'version': 1, 'peers': ['02:ab:cd:ef:01:02']}
        self.assertEqual(normalize_peers(valid), ['02:AB:CD:EF:01:02'])
        self.assertEqual(normalize_peers({'version': 1, 'peers': []}), [])
        invalid = [None, [], {}, {'version': True, 'peers': []}, {'version': 2, 'peers': []},
                   {'version': 1, 'peers': [], 'extra': None}, {'version': 1, 'peers': 'any'}]
        for peers in ([None], ['any'], ['02:ab:cd:ef:01:02\n'], ['";exit(0);//'],
                      ['00:00:00:00:00:00'], ['FF:FF:FF:FF:FF:FF'],
                      ['02:ab:cd:ef:01:02', '02:AB:CD:EF:01:02'],
                      [f'02:00:00:00:00:{i:02x}' for i in range(9)]):
            invalid.append({'version': 1, 'peers': peers})
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(ValueError):
                normalize_peers(config)

    def test_private_input_and_exclusive_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root/'peers.json'
            write_private(config, b'{}')
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(read_regular(config, 2, private=True), b'{}')
            with self.assertRaises(FileExistsError):
                write_private(config, b'overwrite')
            with self.assertRaises(ValueError):
                read_regular(config, 1)
            config.chmod(0o644)
            with self.assertRaises(ValueError):
                read_regular(config, 2, private=True)
            link = root/'link'
            link.symlink_to(config)
            with self.assertRaises(OSError):
                read_regular(link, 2)
            fifo = root/'fifo'
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                read_regular(fifo, 2)
            self.assertEqual(config.read_bytes(), b'{}')

    def test_pinned_core_rejects_unknown_inputs(self):
        for core in (b'', b'\x7fELF', bytes(14_170_096)):
            with self.assertRaisesRegex(ValueError, 'unreviewed original core'):
                patch_core(core, b'anything')

    def test_arm_branch_encoding_and_limits(self):
        for source, target in ((0x1000, 0x1000), (0x38670, CODE), (CODE, 0x38318),
                               (0x2000000, 8), (0, 0x2000004)):
            word = struct.unpack('<I', arm_branch(source, target))[0]
            displacement = word & 0xffffff
            if displacement & 0x800000:
                displacement -= 1 << 24
            self.assertEqual(source+8+displacement*4, target)
            self.assertEqual(word >> 24, 0xeb)
        for source, target in ((1, 4), (4, 1), (0, 0x2000008), (0x2000004, 0)):
            with self.assertRaises(ValueError):
                arm_branch(source, target)

    def test_compiled_filter_eight_peers_guard_pages_and_delegation(self):
        arm = os.environ.get('DREEM_OVERLAY_CC', 'arm-linux-gnueabihf-gcc')
        with tempfile.TemporaryDirectory(prefix='dreem-peer-filter-') as tmp:
            for name, compiler, flags, runner in (
                    ('host', 'cc', [], []),
                    ('asan', 'cc', ['-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                                    '-fno-pie', '-no-pie'], []),
                    ('arm', arm, ['-static', '-marm', '-mcpu=cortex-a7'],
                     ['qemu-arm', '-cpu', 'cortex-a7'])):
                with self.subTest(target=name):
                    binary = Path(tmp)/name
                    command = [compiler, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', *flags,
                               str(ROOT/'development/bluetooth_peer_filter.c'),
                               str(ROOT/'tests/bluetooth_peer_filter_harness.c'), '-o', str(binary)]
                    p = subprocess.run(command, capture_output=True, text=True, timeout=60)
                    self.assertEqual(p.returncode, 0, p.stderr)
                    p = subprocess.run(runner+[str(binary)], capture_output=True, text=True, timeout=20)
                    self.assertEqual((p.returncode, p.stdout, p.stderr), (0, 'filter-ok 166\n', ''))

    def test_real_arm_loader_static_and_dynamic(self):
        compiler = os.environ.get('DREEM_OVERLAY_CC', 'arm-linux-gnueabihf-gcc')
        prefix = os.environ.get('DREEM_OVERLAY_SYSROOT', '/usr/arm-linux-gnueabihf')
        # Newly assembled ARM mov r0,#42; bx lr. This is not extracted firmware.
        payload = struct.pack('<II', 0xe3a0002a, 0xe12fff1e)
        with tempfile.TemporaryDirectory(prefix='dreem-overlay-loader-') as tmp:
            root = Path(tmp)
            for static in (False, True):
                with self.subTest(static=static):
                    exe = root/('static' if static else 'dynamic')
                    command = [compiler, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                               '-marm', '-mcpu=cortex-a7', '-fno-pie', '-no-pie',
                               str(ROOT/'tests/bluetooth_overlay_loader.c'), '-pthread', '-o', str(exe)]
                    if static:
                        command += ['-static']
                    p = subprocess.run(command, capture_output=True, text=True, timeout=60)
                    self.assertEqual(p.returncode, 0, p.stderr)
                    original = exe.read_bytes()
                    modified = append_rx(original, payload)
                    table = headers(modified)
                    phoff = struct.unpack_from('<I', modified, 28)[0]
                    first = next(p for p in table if p[0] == 1)
                    self.assertEqual(first[2]-first[1]+phoff, BASE)
                    self.assertEqual(modified[phoff+4096:], payload)
                    allowed = {28, 29, 30, 31, 44, 45}
                    self.assertTrue(all(a == b or i in allowed for i, (a, b) in enumerate(zip(original, modified))))
                    patched = root/(exe.name+'-patched')
                    write_private(patched, modified)
                    patched.chmod(0o700)  # Only the independent test fixture is executable.
                    for binary, extra, wanted in ((exe, [], 'original-loader-ok\n'),
                                                   (patched, ['patched'], 'overlay-loader-ok\n')):
                        p = subprocess.run(['qemu-arm', '-cpu', 'cortex-a7', '-L', prefix,
                                            str(binary), *extra], capture_output=True, text=True, timeout=20)
                        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, wanted, ''))
                    radio = root/(exe.name+'-radio')
                    write_private(radio, append_rx(original, payload, writable=bytes(4)))
                    radio.chmod(0o700)
                    p = subprocess.run(['qemu-arm', '-cpu', 'cortex-a7', '-L', prefix,
                                        str(radio), 'radio'], capture_output=True, text=True, timeout=20)
                    self.assertEqual((p.returncode, p.stdout, p.stderr), (0, 'overlay-loader-ok\n', ''))
                    # Corruptions must be rejected before producing a candidate.
                    corruptions = [original[:30], b'wrong'+original[5:]]
                    for field, value in ((16, 3), (18, 62), (42, 16), (44, 0)):
                        corrupt = bytearray(original)
                        struct.pack_into('<H', corrupt, field, value)
                        corruptions.append(corrupt)
                    for corrupt in corruptions:
                        with self.assertRaises(ValueError):
                            append_rx(corrupt, payload)
                    with self.assertRaisesRegex(ValueError, 'extent or alignment'):
                        append_rx(original, payload, base=0x10000)
                    corrupt = bytearray(original)
                    phoff = struct.unpack_from('<I', corrupt, 28)[0]
                    for i, p in enumerate(headers(original)):
                        if p[0] == 1:
                            struct.pack_into('<I', corrupt, phoff+i*PHDR.size+20, BASE)
                            break
                    with self.assertRaisesRegex(ValueError, 'overlaps'):
                        append_rx(corrupt, payload)


if __name__ == '__main__':
    unittest.main()
