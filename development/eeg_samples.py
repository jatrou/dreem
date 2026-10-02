# SPDX-License-Identifier: Apache-2.0
"""Independent decoding of saved 16-byte EEG driver records.

This is not a device reader. Native eeg.data files already contain floats and
must not pass through this decoder. Scaling reproduces stock numerical output;
physical units and electrode names are not asserted here.
"""

import struct

SCALE = 4_000_000 / 8_388_607
# Input channel -> output index, then input channel polarity.
LAYOUTS = {
    "zero": ((0, 1, 2, 3), (-1, -1, -1, 1)),
    "nonzero": ((2, 1, 3, 0), (1, -1, -1, -1)),
}


def decode_driver_record(record, *, hardware_version):
    """Return four float32-rounded samples; ignore driver metadata/padding.

    hardware_version must be explicit because the stock recorder changes
    channel order and polarity at version zero. Nonnegative integer values
    select the same two branches as the archived recorder.
    """
    if len(record) != 16:
        raise ValueError("expected exactly one 16-byte driver record")
    if type(hardware_version) is not int or hardware_version < 0:
        raise ValueError("hardware_version must be a nonnegative integer")
    order, polarity = LAYOUTS["zero" if hardware_version == 0 else "nonzero"]
    output = [0.0] * 4
    for channel in range(4):
        counts = int.from_bytes(record[channel * 3:channel * 3 + 3], "big", signed=True)
        # Negating as float preserves the stock negative-zero result. Signed
        # 24-bit integers are exactly representable in float32 before scaling.
        value = float(counts) * polarity[channel] * SCALE
        output[order[channel]] = struct.unpack("<f", struct.pack("<f", value))[0]
    return tuple(output)
