# SPDX-License-Identifier: GPL-2.0-only
"""Execute relocated MOVW/MOVT pairs, including signed addends and carries."""
import struct
import unittest

try:
    from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM
    from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R3, UC_ARM_REG_R12
except ImportError:
    Uc = None

from development.arm_relocations import relocate_mov


@unittest.skipIf(Uc is None, 'install development/requirements.txt')
class ArmRelocationTests(unittest.TestCase):
    def test_address_materialization(self):
        # Fixed ARM instruction encodings; expected addresses are independent
        # of the relocation implementation. Nonzero MOVT addends catch the
        # old loader's incorrect shift-before-add, including carry and borrow.
        vectors = [
            (0xE3000000, 0xE3400000, 0x81234567, 0x81234567),
            (0xE3000001, 0xE3400001, 0x1234FFFF, 0x12350000),
            (0xE30F0FFF, 0xE34F0FFF, 0x12340000, 0x1233FFFF),
            (0xE3070FFF, 0xE3470FFF, 0xFFFFF001, 0x00007000),
            (0xE3080000, 0xE3480000, 0x00001000, 0xFFFF9000),
        ]
        registers = [(0, UC_ARM_REG_R0), (3, UC_ARM_REG_R3), (12, UC_ARM_REG_R12)]
        for low, high, symbol, expected in vectors:
            for number, register in registers:
                with self.subTest(symbol=hex(symbol), instruction=hex(low), register=number):
                    cpu = Uc(UC_ARCH_ARM, UC_MODE_ARM)
                    cpu.mem_map(0x1000, 0x1000)
                    for _, other in registers:
                        cpu.reg_write(other, 0xA5A55A5A)
                    code = struct.pack('<II', relocate_mov(low | number << 12, symbol, False),
                                       relocate_mov(high | number << 12, symbol, True))
                    cpu.mem_write(0x1000, code)
                    cpu.emu_start(0x1000, 0x1008, timeout=100000, count=2)
                    self.assertEqual(cpu.reg_read(register), expected)
                    for _, other in registers:
                        if other != register:
                            self.assertEqual(cpu.reg_read(other), 0xA5A55A5A)


if __name__ == '__main__':
    unittest.main()
