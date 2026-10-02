#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Compare reconstructed start/stop/release with isolated stock ARM routines.

Uses synthetic MMIO, GPIO, queues, and ring memory only. No hardware is opened.
Requires the exact private kernel ELF, analysis dependencies, cc, and ARM gcc.
"""

import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile

from unicorn import UC_HOOK_MEM_READ, UC_PROT_READ, UC_PROT_WRITE
from unicorn.arm_const import UC_ARM_REG_R1

from verify_adc_init import (CTransport, IO_FN, READ, WRITE, GPIO_SET, SPI,
                             EVENT, RAW_HASH, ROUTINES, Transport,
                             stock_inputs, run_stock, run_arm_reconstructed)

QUEUE_HEAD, QUEUE_TRYLOCK = 6, 7
RING_BYTES, POLL_LIMIT = 1024, 128
INITIAL_RING = bytes((i * 19 + 7) & 255 for i in range(RING_BYTES))
INITIAL_OFFSET, INITIAL_ERRORS = 0x120, 42


class AcquisitionTransport(Transport):
    def __init__(self, head=0, pending=0, endless_queue=False, busy=False,
                 stuck_fifo=False, **kwargs):
        super().__init__(**kwargs)
        self.head, self.pending = head, pending
        self.endless_queue, self.busy, self.stuck_fifo = endless_queue, busy, stuck_fifo
        self.ring = INITIAL_RING
        self.offset, self.errors = INITIAL_OFFSET, INITIAL_ERRORS

    def io(self, op, a, b):
        if op in (QUEUE_HEAD, QUEUE_TRYLOCK):
            if a or b:
                raise ValueError("unexpected queue arguments")
            if op == QUEUE_HEAD:
                result = self.head
            elif self.endless_queue or self.pending:
                result = 0
                self.pending = max(0, self.pending - 1)
            else:
                result = 1
            self.trace.append([op, a, b, result])
            return result
        if op == WRITE and a == SPI + 20:
            self.registers[a] = b
            self.trace.append([op, a, b, 0])
            return 0
        result = super().io(op, a, b)
        if op == READ and a == SPI + 24:
            if self.busy:
                result &= ~0x80
            if self.stuck_fifo:
                result |= 8
            self.trace[-1][3] = result
        return result

    def state(self):
        return [self.offset, self.errors, self.pending,
                hashlib.sha256(self.ring).hexdigest()]


class StockState:
    """Synthetic storage for the stock driver's four known globals."""

    def __init__(self, model):
        self.model = model
        self.ring_address = 0x10002000
        self.stubs = {"memset": self.memset, "down_trylock": self.trylock}

    def memset(self, cpu, address, value, count):
        if (address, value, count) != (self.ring_address, 0x42, RING_BYTES):
            raise ValueError("unexpected stock memset")
        cpu.mem_write(address, bytes([value]) * count)
        return address

    def trylock(self, cpu, address, _b, _c):
        if address != self.symbols["ads_data_sem"]:
            raise ValueError("unexpected semaphore")
        return self.model.io(QUEUE_TRYLOCK, 0, 0)

    def prepare(self, cpu, symbols):
        self.symbols = symbols
        expected = {"count": 0x80942BA0, "read_offset": 0x80942C00,
                    "sdma_ads_user_buffer": 0x8093EAC0,
                    "sdma_queue_head": 0x8093EAF0, "ads_data_sem": 0x80942BF4}
        if any(symbols[name] != address for name, address in expected.items()):
            raise ValueError("unexpected stock global layout")
        for page in sorted({address & ~4095 for address in expected.values()}):
            cpu.mem_map(page, 4096, UC_PROT_READ | UC_PROT_WRITE)
        for name, value in (("count", self.model.errors),
                            ("read_offset", self.model.offset),
                            ("sdma_ads_user_buffer", self.ring_address)):
            cpu.mem_write(symbols[name], struct.pack("<I", value))
        cpu.mem_write(self.ring_address, self.model.ring)

        def read_head(uc, access, address, size, value, _):
            if size != 4:
                raise ValueError("unexpected queue-head read width")
            uc.mem_write(address, struct.pack("<I", self.model.io(QUEUE_HEAD, 0, 0)))

        cpu.hook_add(UC_HOOK_MEM_READ, read_head,
                     begin=symbols["sdma_queue_head"], end=symbols["sdma_queue_head"])

    def finish(self, cpu):
        self.model.ring = bytes(cpu.mem_read(self.ring_address, RING_BYTES))
        self.model.offset = struct.unpack("<I", cpu.mem_read(self.symbols["read_offset"], 4))[0]
        self.model.errors = struct.unpack("<I", cpu.mem_read(self.symbols["count"], 4))[0]


class CState(ctypes.Structure):
    _fields_ = [("ring", ctypes.POINTER(ctypes.c_uint8)),
                ("read_offset", ctypes.c_uint32), ("errors", ctypes.c_uint32)]


def run_host(library, action, model):
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
    function = getattr(library, "ads129x_sdma_" + action)
    function.restype = ctypes.c_int
    function.argtypes = [ctypes.POINTER(CTransport)]
    args = [ctypes.byref(transport)]
    if action == "start":
        function.argtypes.append(ctypes.POINTER(CState))
        args.append(ctypes.byref(state))
    result = function(*args)
    if errors:
        raise errors[0]
    model.ring, model.offset, model.errors = bytes(ring), state.read_offset, state.errors
    return result


class ArmState:
    def __init__(self, model):
        self.model = model

    def prepare_arm(self, cpu, data):
        cpu.mem_write(data + 8, struct.pack("<I", POLL_LIMIT))
        cpu.mem_write(data + 0x100, struct.pack("<3I", data + 0x200,
                                                self.model.offset, self.model.errors))
        cpu.mem_write(data + 0x200, self.model.ring)
        cpu.reg_write(UC_ARM_REG_R1, data + 0x100)

    def finish_arm(self, cpu, data):
        self.model.ring = bytes(cpu.mem_read(data + 0x200, RING_BYTES))
        self.model.offset, self.model.errors = struct.unpack("<2I", cpu.mem_read(data + 0x104, 8))


def verify(path):
    inputs = stock_inputs(path, ROUTINES + ("ads1296_sdma_ioctl", "ads1296_sdma_release"))
    # All 64 producer positions plus queue, controller, and stale-FIFO branches.
    cases = [("start", dict(head=head, pending=head % 5)) for head in range(64)]
    cases += [("start", dict(head=17, pending=63, enabled=True)),
              ("start", dict(head=63, stale=7))]
    cases += [(action, options) for action in ("stop", "release")
              for options in ({}, dict(enabled=True), dict(stale=3))]
    timeouts = [(action, options) for action in ("start", "stop", "release")
                for options in (dict(stalled=True), dict(enabled=True, busy=True),
                                dict(stuck_fifo=True))]
    timeouts.append(("start", dict(endless_queue=True)))
    digest = hashlib.sha256()
    counts, timeout_results = [], []
    source = Path(__file__).with_name("ads129x_init.c")
    with tempfile.TemporaryDirectory() as folder:
        shared, arm_path = Path(folder) / "adc.so", Path(folder) / "adc.arm.elf"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-shared", "-fPIC", str(source), "-o", str(shared)], check=True)
        subprocess.run(["arm-linux-gnueabihf-gcc", "-std=c11", "-O2", "-Wall", "-Wextra",
                        "-Werror", "-marm", "-mcpu=cortex-a7", "-ffreestanding", "-nostdlib",
                        "-static", "-no-pie", "-Wl,-Ttext=0x20000000,-e,ads129x_sdma_start",
                        str(source), "-o", str(arm_path)], check=True)
        library, arm_binary = ctypes.CDLL(str(shared)), arm_path.read_bytes()

        def original(action, model):
            entry = "ads1296_sdma_release" if action == "release" else "ads1296_sdma_ioctl"
            return run_stock(inputs, model, entry=entry, argument=int(action == "start"),
                             extension=StockState(model))

        def arm(action, model):
            return run_arm_reconstructed(arm_binary, model, entry="ads129x_sdma_" + action,
                                          extension=ArmState(model))

        for index, (action, options) in enumerate(cases):
            stock, host, target = (AcquisitionTransport(**options) for _ in range(3))
            results = [original(action, stock), run_host(library, action, host), arm(action, target)]
            if results != [0, 0, 0] or stock.trace != host.trace or stock.trace != target.trace:
                mismatch = next((i for i, (x, y) in enumerate(zip(stock.trace, host.trace)) if x != y), None)
                raise ValueError(f"case {index} {action} differs: returns {results}, "
                                 f"lengths {len(stock.trace)}/{len(host.trace)}/{len(target.trace)}, "
                                 f"first host mismatch {mismatch}")
            if stock.state() != host.state() or stock.state() != target.state():
                raise ValueError(f"case {index} {action} state differs")
            # Explicit interface invariants independent of the stock comparison.
            if action == "start" and (stock.offset != ((options.get("head", 0) + 63) % 64) * 16
                                       or stock.errors or stock.pending
                                       or stock.ring != bytes([0x42]) * RING_BYTES
                                       or stock.trace[-1] != [WRITE, EVENT, 2, 0]):
                raise ValueError("start did not reset the ring/queue before enabling requests")
            counts.append(len(stock.trace))
            digest.update(json.dumps([index, action, options, stock.trace, stock.state()],
                                      separators=(",", ":")).encode())
        for action, options in timeouts:
            stock, host, target = (AcquisitionTransport(**options) for _ in range(3))
            if original(action, stock) is not None:
                raise ValueError(f"expected unbounded stock polling: {action} {options}")
            if run_host(library, action, host) != -110 or arm(action, target) != -110:
                raise ValueError("reconstruction did not return a timeout")
            if host.trace != target.trace or host.state() != target.state():
                raise ValueError("timeout host/ARM behavior differs")
            if host.trace[-3:] != [[WRITE, EVENT, 0, 0], [GPIO_SET, 35, 0, 0], [GPIO_SET, 90, 1, 0]]:
                raise ValueError("timeout did not disable requests, power off, and deselect")
            if [WRITE, EVENT, 2, 0] in host.trace:
                raise ValueError("failed start enabled requests")
            timeout_results.append({"action": action, "conditions": options, "return": -110})
        invalid, invalid_arm = AcquisitionTransport(head=64), AcquisitionTransport(head=64)
        if run_host(library, "start", invalid) != -22 or arm("start", invalid_arm) != -22:
            raise ValueError("invalid queue head was not rejected")
        if invalid.trace != invalid_arm.trace or invalid.state() != invalid_arm.state():
            raise ValueError("invalid-head host/ARM behavior differs")
        if invalid.trace[-3:] != [[WRITE, EVENT, 0, 0], [GPIO_SET, 35, 0, 0], [GPIO_SET, 90, 1, 0]]:
            raise ValueError("invalid-head cleanup differs")
    return {"kernel_raw_sha256": RAW_HASH, "matching_cases": len(cases),
            "reconstruction_targets": ["host C", "Cortex-A7 ARM C"],
            "compared": ["return values", "ordered MMIO/GPIO/delay/queue traces", "ring/state bytes"],
            "io_event_counts": counts, "trace_sha256": digest.hexdigest(),
            "bounded_timeout_cases": timeout_results, "invalid_queue_head": "rejected with -22"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel_elf", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.kernel_elf), indent=2))


if __name__ == "__main__":
    main()
