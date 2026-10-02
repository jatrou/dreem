# SPDX-License-Identifier: GPL-2.0-or-later
import struct
import unittest

from development.sdma_disassemble import decode, disassemble, listing


class SdmaDisassemblyTests(unittest.TestCase):
    def test_published_assembler_example(self):
        # Eli Billauer's loop example, also used to validate the external assembler.
        words = [0x0804, 0x7803, 0x5C05, 0x1D01, 0x1C10,
                 0x0300, 0x1C40, 0x0B00, 0x4B00, 0x7DF6]
        code = disassemble(struct.pack("<10H", *words))
        self.assertEqual(code[0].assembly(), "ldi      r0, 4")
        self.assertEqual(code[2].assembly(), "st       r4, (r5, 0)")
        self.assertEqual(code[1].relative_target, 5)
        self.assertEqual(code[-1].relative_target, 0)

    def test_signed_branches_and_operand_boundaries(self):
        self.assertEqual(decode(0x7D80, 200).relative_target, 73)
        self.assertEqual(decode(0x7C7F, 10).relative_target, 138)
        self.assertEqual(decode(0x5FFF).operands, "r7, (r7, 31)")
        self.assertEqual(decode(0xFFFF).assembly(), "jsr      16383")
        self.assertEqual(decode(0x0707).name, "illegal")
        self.assertEqual(decode(0x0700).operands, "7")
        self.assertEqual(decode(0x06E2).name, "cpshreg")

    def test_reserved_and_incomplete_input_rejected(self):
        for data in (b"", b"\x00", b"\x00" * 32770):
            with self.assertRaises(ValueError):
                disassemble(data)
        for word in (0x7800, 0x7000, -1, 0x10000):
            with self.assertRaises(ValueError):
                decode(word)

    def test_listing_has_hash_and_word_address_units(self):
        result = listing(b"\x04\x08\x03\x78")
        self.assertIn("Input SHA-256:", result)
        self.assertIn("0001: 7803; target word 0x5", result)
