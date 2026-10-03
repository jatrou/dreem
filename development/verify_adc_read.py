#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Verify ring-to-record reconstruction with synthetic data and stock ARM code.

No headset, device node, private recording, or hardware transport is used.
The stock reader is emulated with bounded execution and stubbed kernel calls.
"""

import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile

from unicorn import UC_PROT_READ
from unicorn.arm_const import UC_ARM_REG_R2, UC_ARM_REG_R3

from verify_adc_init import (CTransport, IO_FN, RAW_HASH, stock_inputs,
                             run_stock, run_arm_reconstructed)
from verify_adc_control import (AcquisitionTransport, ArmState, CState,
                                StockState, RING_BYTES, POLL_LIMIT)

QUEUE_WAIT = 8
GUARD, UNTOUCHED = bytes([0xD3]) * 4, bytes([0xCC]) * 16
ORDER = (7, 6, 5, 4, 11, 10, 9, 8, 15, 14, 13, 12)


def fixtures(slot, depth=7, skipped=0, invalid=None, all_empty=False):
    ring = bytearray((i * 43 + i // 16) & 255 for i in range(RING_BYTES))
    for n in range(64):
        ring[n * 16:n * 16 + 3] = bytes([n & 15, 0, 0xC0])
    if all_empty:
        ring[:] = bytes([0x42]) * RING_BYTES
    for n in range(skipped):
        offset = ((slot + n) & 63) * 16
        ring[offset:offset + 16] = bytes([0x42]) * 16
    selected = (slot + skipped) & 63
    if invalid is not None:
        ring[selected * 16:selected * 16 + 3] = invalid
    return {"ring": bytes(ring), "offset": slot * 16,
            "head": (selected + depth) & 63, "selected": selected}


class ReadTransport(AcquisitionTransport):
    def __init__(self, fixture, interrupted=0, output_size=16, stack_fill=0xA5):
        super().__init__(head=fixture["head"], pending=1)
        self.ring, self.offset = fixture["ring"], fixture["offset"]
        self.interrupted, self.output_size, self.stack_fill = interrupted, output_size, stack_fill
        self.output = UNTOUCHED
        self.copy_sizes = []

    def io(self, op, a, b):
        if op != QUEUE_WAIT:
            return super().io(op, a, b)
        if a or b:
            raise ValueError("unexpected wait arguments")
        if self.interrupted:
            self.interrupted -= 1
            result = 0xFFFFFFFC
        elif self.pending:
            self.pending -= 1
            result = 0
        else:
            raise ValueError("synthetic queue has no notification")
        self.trace.append([op, a, b, result])
        return result


class StockRead(StockState):
    def __init__(self, model, inputs):
        super().__init__(model)
        self.inputs = inputs
        self.output_address = 0x10004004
        self.stubs = {"down_interruptible": self.wait, "__copy_to_user": self.copy}

    def wait(self, cpu, address, _b, _c):
        if address != self.symbols["ads_data_sem"]:
            raise ValueError("unexpected wait semaphore")
        return self.model.io(QUEUE_WAIT, 0, 0)

    def copy(self, cpu, destination, source, count):
        if destination != self.output_address or count != 16:
            raise ValueError("unexpected userspace copy")
        self.model.copy_sizes.append(count)
        cpu.mem_write(destination, bytes(cpu.mem_read(source, count)))
        return 0

    def prepare(self, cpu, symbols):
        super().prepare(cpu, symbols)
        raw, base, _, _ = self.inputs
        address = symbols["swap_array"]
        order = raw[address - base:address - base + 48]
        if struct.unpack("<12I", order) != ORDER:
            raise ValueError("unexpected stock byte permutation")
        cpu.mem_map(address & ~4095, 4096, UC_PROT_READ)
        cpu.mem_write(address, order)
        # Poison only synthetic stack memory; the stock reader leaves padding
        # untouched. Give the synthetic task a normal userspace address limit.
        cpu.mem_write(0x1000FE00, bytes([self.model.stack_fill]) * 0x1F0)
        cpu.mem_write(0x1000E008, struct.pack("<I", 0x7FFFFFFF))
        cpu.mem_write(self.output_address - 4, GUARD + self.model.output + GUARD)
        cpu.reg_write(UC_ARM_REG_R2, self.model.output_size)

    def finish(self, cpu):
        super().finish(cpu)
        buffer = bytes(cpu.mem_read(self.output_address - 4, 24))
        if buffer[:4] != GUARD or buffer[-4:] != GUARD:
            raise ValueError("stock copy left the allocated fixture")
        self.model.output = buffer[4:20]


class ArmRead(ArmState):
    def prepare_arm(self, cpu, data):
        super().prepare_arm(cpu, data)
        cpu.mem_write(data + 0x800, GUARD + self.model.output + GUARD)
        cpu.reg_write(UC_ARM_REG_R2, data + 0x804)
        cpu.reg_write(UC_ARM_REG_R3, self.model.output_size)

    def finish_arm(self, cpu, data):
        super().finish_arm(cpu, data)
        buffer = bytes(cpu.mem_read(data + 0x800, 24))
        if buffer[:4] != GUARD or buffer[-4:] != GUARD:
            raise ValueError("ARM output guard changed")
        self.model.output = buffer[4:20]


def run_host(library, model):
    errors = []

    def callback(context, op, a, b):
        try:
            return model.io(op, a, b)
        except Exception as exc:
            errors.append(exc)
            return 0

    callback_ref = IO_FN(callback)
    transport = CTransport(callback_ref, None, POLL_LIMIT)
    ring = (ctypes.c_uint8 * RING_BYTES).from_buffer_copy(model.ring)
    state = CState(ring, model.offset, model.errors)
    buffer = (ctypes.c_uint8 * 24).from_buffer_copy(GUARD + model.output + GUARD)
    function = library.ads129x_sdma_read_frame
    function.restype = ctypes.c_int
    function.argtypes = [ctypes.POINTER(CTransport), ctypes.POINTER(CState),
                         ctypes.c_void_p, ctypes.c_uint]
    result = function(ctypes.byref(transport), ctypes.byref(state),
                      ctypes.byref(buffer, 4), model.output_size)
    if errors:
        raise errors[0]
    if bytes(buffer)[:4] != GUARD or bytes(buffer)[-4:] != GUARD:
        raise ValueError("host output guard changed")
    model.ring, model.offset, model.errors = bytes(ring), state.read_offset, state.errors
    model.output = bytes(buffer)[4:20]
    return result


def verify(path):
    inputs = stock_inputs(path, ("ads1296_sdma_read",))
    cases = [(fixtures(slot, depth), {}) for slot in range(64) for depth in (0, 1, 31, 63)]
    cases += [(fixtures(slot, skipped=skipped), {}) for slot in (0, 63) for skipped in (1, 63)]
    cases += [(fixtures(63, invalid=header), {})
              for header in (b"\x10\x00\xc0", b"\x00\x01\xc0", b"\x00\x00\x00",
                              b"\x42\x42\x41", b"\x42\x41\x42")]
    cases += [(fixtures(63, all_empty=True), {})]
    cases += [(fixtures(3), dict(interrupted=n)) for n in (1, 4, 5, 8)]
    digest = hashlib.sha256()
    source = Path(__file__).with_name("ads129x_init.c")
    with tempfile.TemporaryDirectory() as folder:
        shared, arm_path = Path(folder) / "adc.so", Path(folder) / "adc.arm.elf"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-shared", "-fPIC", str(source), "-o", str(shared)], check=True)
        subprocess.run(["arm-linux-gnueabihf-gcc", "-std=c11", "-O2", "-Wall", "-Wextra",
                        "-Werror", "-marm", "-mcpu=cortex-a7", "-mgeneral-regs-only", "-ffreestanding", "-nostdlib",
                        "-static", "-no-pie", "-Wl,-Ttext=0x20000000,-e,ads129x_sdma_read_frame",
                        str(source), "-o", str(arm_path)], check=True)
        library, arm_binary = ctypes.CDLL(str(shared)), arm_path.read_bytes()

        def original(model):
            extension = StockRead(model, inputs)
            return run_stock(inputs, model, entry="ads1296_sdma_read",
                             argument=extension.output_address, extension=extension)

        def arm(model):
            return run_arm_reconstructed(arm_binary, model, entry="ads129x_sdma_read_frame",
                                          extension=ArmRead(model))

        for index, (fixture, options) in enumerate(cases):
            stock, host, target = (ReadTransport(fixture, **options) for _ in range(3))
            results = [original(stock), run_host(library, host), arm(target)]
            if results[0] != results[1] or results[0] != results[2] or results[0] is None:
                raise ValueError(f"case {index} return mismatch: {results}")
            if stock.trace != host.trace or stock.trace != target.trace:
                raise ValueError(f"case {index} queue trace differs")
            if stock.state() != host.state() or stock.state() != target.state():
                raise ValueError(f"case {index} state differs")
            if host.output != target.output:
                raise ValueError(f"case {index} reconstructed ARM output differs")
            if results[0] == 16:
                if stock.output[:13] != host.output[:13] or host.output[13:] != bytes(3):
                    raise ValueError(f"case {index} payload/metadata/padding mismatch")
                if stock.output[13:] != bytes([stock.stack_fill]) * 3:
                    raise ValueError("expected synthetic stack padding was not observed")
                offset = fixture["selected"] * 16
                expected = bytes(fixture["ring"][offset + i] for i in ORDER)
                if host.output[:12] != expected or host.output[12] != (fixture["head"] - fixture["selected"]) % 64:
                    raise ValueError("independent payload/queue-depth expectation failed")
            elif stock.output != UNTOUCHED or host.output != UNTOUCHED:
                raise ValueError("failed read modified output")
            digest.update(json.dumps([index, results[0], stock.trace, stock.state(), host.output.hex()],
                                      separators=(",", ":")).encode())

        # Demonstrate that the original trailing bytes depend on prior stack
        # contents, while the reconstructed output is fully initialized.
        padding = []
        for poison in (0xA5, 0x5A):
            stock = ReadTransport(fixtures(0), stack_fill=poison)
            if original(stock) != 16:
                raise ValueError("padding demonstration did not read a frame")
            padding.append(stock.output)
        if padding[0][:13] != padding[1][:13] or padding[0][13:] == padding[1][13:]:
            raise ValueError("stack-padding dependency not reproduced")

        for size in (0, 1, 12, 15):
            stock, host, target = (ReadTransport(fixtures(0), output_size=size) for _ in range(3))
            if original(stock) != 16 or stock.copy_sizes != [16]:
                raise ValueError("stock short-read behavior changed")
            if run_host(library, host) != -22 or arm(target) != -22:
                raise ValueError("short output was not rejected")
            if host.trace or target.trace or host.output != UNTOUCHED or target.output != UNTOUCHED:
                raise ValueError("rejected output size consumed data or changed output")
            if host.state() != ReadTransport(fixtures(0)).state() or host.state() != target.state():
                raise ValueError("rejected output size changed state")

        # The stock placeholder search accepts its first non-placeholder even
        # with an invalid status. The replacement validates that frame too.
        malformed = fixtures(63, skipped=1, invalid=b"\x11\x00\xc0")
        stock, host, target = (ReadTransport(malformed) for _ in range(3))
        if original(stock) != 16 or run_host(library, host) != -2 or arm(target) != -2:
            raise ValueError("post-placeholder validation difference not reproduced")
        if host.state() != target.state() or host.output != UNTOUCHED or target.output != UNTOUCHED:
            raise ValueError("invalid-frame rejection differs between targets")

        for offset in (1, 1024, 0xFFFFFFF0):
            fixture = fixtures(0)
            fixture["offset"] = offset
            host, target = ReadTransport(fixture), ReadTransport(fixture)
            before = host.state()
            if run_host(library, host) != -22 or arm(target) != -22:
                raise ValueError("invalid ring offset was not rejected")
            if host.trace or target.trace or host.state() != before or target.state() != before:
                raise ValueError("invalid ring offset consumed data or changed state")
            if host.output != UNTOUCHED or target.output != UNTOUCHED:
                raise ValueError("invalid ring offset changed output")
        fixture = fixtures(0)
        fixture["head"] = 64
        host, target = ReadTransport(fixture), ReadTransport(fixture)
        if run_host(library, host) != -22 or arm(target) != -22:
            raise ValueError("invalid producer slot was not rejected")
        if host.state() != target.state() or host.trace != target.trace:
            raise ValueError("invalid producer slot behavior differs")
        if host.output != UNTOUCHED or target.output != UNTOUCHED or host.offset != 0 or host.errors != 42:
            raise ValueError("invalid producer slot modified output or read position")
    return {"kernel_raw_sha256": RAW_HASH, "matching_payload_and_state_cases": len(cases),
            "reconstruction_targets": ["host C", "Cortex-A7 ARM C"],
            "trace_sha256": digest.hexdigest(),
            "intentional_differences": {
                "padding": "stock copies three synthetic stack bytes; reconstruction zeroes them",
                "short_reads": "stock copies 16 for requests of 0, 1, 12, 15; reconstruction rejects before consuming",
                "status_after_placeholder": "stock accepts malformed frame; reconstruction rejects with -2",
                "invalid_state": "misaligned/out-of-range reader offsets and producer slots are rejected"}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel_elf", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.kernel_elf), indent=2))


if __name__ == "__main__":
    main()
