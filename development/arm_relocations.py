# SPDX-License-Identifier: GPL-2.0-only
"""ARM ELF REL address materialization used by the offline verifiers."""


def relocate_mov(word, symbol, high):
    """Apply R_ARM_MOVW_ABS_NC or R_ARM_MOVT_ABS with a signed REL addend.

    Both instructions encode a signed 16-bit addend. MOVT selects the upper
    half only after adding the symbol; its encoded addend is not shifted.
    """
    immediate = ((word >> 4) & 0xF000) | (word & 0xFFF)
    value = symbol + ((immediate ^ 0x8000) - 0x8000)
    immediate = (value >> 16 if high else value) & 0xFFFF
    return (word & ~0xF0FFF) | ((immediate & 0xF000) << 4) | (immediate & 0xFFF)
