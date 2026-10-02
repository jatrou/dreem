import math
import struct
import unittest

from development.eeg_samples import SCALE, decode_driver_record


def record(values, tail=b"\0" * 4):
    return b"".join(value.to_bytes(3, "big", signed=True) for value in values) + tail


class EegSamplesTests(unittest.TestCase):
    def test_signed_limits_and_zero_revision(self):
        result = decode_driver_record(record((-8388608, -1, 0, 8388607)), hardware_version=0)
        self.assertEqual(result[0], struct.unpack("<f", struct.pack("<f", 8388608 * SCALE))[0])
        self.assertGreater(result[1], 0)
        self.assertEqual(math.copysign(1, result[2]), -1)
        self.assertEqual(result[3], 4_000_000)

    def test_nonzero_revision_channel_order_and_polarity(self):
        result = decode_driver_record(record((8388607, -8388607, 0, 1)), hardware_version=1)
        self.assertLess(result[0], 0)
        self.assertEqual(result[1:3], (4_000_000, 4_000_000))
        self.assertEqual(math.copysign(1, result[3]), -1)
        self.assertEqual(result, decode_driver_record(record((8388607, -8388607, 0, 1)), hardware_version=2))

    def test_metadata_and_padding_do_not_enter_samples(self):
        a = decode_driver_record(record((1, 2, 3, 4)), hardware_version=1)
        b = decode_driver_record(record((1, 2, 3, 4), b"\xff\x5a\x72\xc3"), hardware_version=1)
        self.assertEqual(a, b)

    def test_requires_full_record_and_explicit_valid_revision(self):
        for data in (b"", b"\0" * 12, b"\0" * 15, b"\0" * 17):
            with self.assertRaises(ValueError):
                decode_driver_record(data, hardware_version=1)
        for revision in (-1, None, True, "1"):
            with self.assertRaises(ValueError):
                decode_driver_record(b"\0" * 16, hardware_version=revision)
