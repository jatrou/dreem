#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Verify reconstructed SDMA context and progress against isolated stock ARM.

Synthetic DMA addresses, counters, memory, interrupts and wakeups only.
No kernel, module, firmware, or DMA program is installed or run on hardware.
"""

import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import random
import struct
import subprocess
import tempfile

from unicorn import UC_HOOK_MEM_READ, UC_PROT_READ, UC_PROT_WRITE
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3

from verify_adc_init import RAW_HASH, stock_inputs, run_stock, run_arm_reconstructed

DEVICE, ENGINE, CCB, CONTEXT, BD = (0x10002000, 0x10004000, 0x10007000, 0x10008000, 0x10009000)
RING, COUNTER, REGS = 0x1000A000, 0x1000B000, 0x020EC000
CONTEXT_PHYS = 0x40008000
MASK = 0xFFFFFFFF


class Progress(ctypes.Structure):
    _fields_ = [("counter", ctypes.c_uint32), ("head", ctypes.c_uint32),
                ("initialized", ctypes.c_uint32), ("fault", ctypes.c_int32)]


class ContextInput(ctypes.Structure):
    _fields_ = [("pc", ctypes.c_uint32), ("ring_phys", ctypes.c_uint32),
                ("counter_phys", ctypes.c_uint32), ("registers", ctypes.c_uint32 * 8)]


def unpack_progress(data):
    return list(struct.unpack("<3Ii", data))


def put(cpu, address, value):
    cpu.mem_write(address, struct.pack("<I", value & MASK))


def u32(cpu, address):
    return struct.unpack("<I", cpu.mem_read(address, 4))[0]


class Stock:
    def __init__(self, state=(0, 0, 0, 0), counter=0, context=None, moving=False):
        self.initial, self.counter, self.context_input = state, counter, context
        self.moving, self.notifications, self.counter_reads = moving, [], 0
        self.context_bytes, self.descriptor, self.state = None, None, None
        self.stubs = {name: self.noop for name in ("clk_enable", "clk_disable", "preempt_count_add",
                      "preempt_count_sub", "_dev_info", "dev_err", "preempt_schedule")}
        self.stubs.update({"up": self.notify, "__memzero": self.zero,
                           "arm_dma_alloc": self.allocate, "sdma_run_channel0": self.load_context,
                           "sdma_config_ownership": self.ownership})

    def io(self, *args):
        raise ValueError("unexpected ADC MMIO in SDMA-only verification")

    def noop(self, cpu, a, b, c):
        return 0

    def notify(self, cpu, a, b, c):
        if a != self.symbols["ads_data_sem"]:
            raise ValueError("unexpected notification semaphore")
        self.notifications.append([u32(cpu, self.symbols["sdma_queue_head"]),
                                   u32(cpu, self.symbols["sdma_ads_a7_counter.25055"])])
        return 0

    def zero(self, cpu, address, count, _):
        if (address, count) != (CONTEXT, 128):
            raise ValueError("unexpected context initialization")
        cpu.mem_write(address, bytes(count))
        return 0

    def allocate(self, cpu, device, size, physical):
        if device != 0 or size not in (1024, 4):
            raise ValueError("unexpected stock DMA allocation")
        put(cpu, physical, self.context_input.ring_phys if size == 1024 else self.context_input.counter_phys)
        return RING if size == 1024 else COUNTER

    def ownership(self, cpu, channel, event, host):
        if (channel, event, host, cpu.reg_read(UC_ARM_REG_R3)) != (ENGINE + 0xF4, 1, 0, 0):
            raise ValueError("unexpected channel-1 ownership")
        return 0

    def load_context(self, cpu, engine, _b, _c):
        if engine != ENGINE:
            raise ValueError("unexpected context-load engine")
        self.context_bytes = bytes(cpu.mem_read(CONTEXT, 128))
        self.descriptor = bytes(cpu.mem_read(BD, 12))
        return 0

    def prepare(self, cpu, symbols):
        self.symbols = symbols
        for page in (0x8093E000, 0x80942000, 0x808A8000):
            cpu.mem_map(page, 4096, UC_PROT_READ | UC_PROT_WRITE)
        put(cpu, ENGINE + 0x1F4C, REGS)
        put(cpu, symbols["sdma_ads_counter_user_buffer"], COUNTER)
        put(cpu, symbols["sdma_ads_a7_counter.25055"], self.initial[0])
        put(cpu, symbols["sdma_queue_head"], self.initial[1])
        put(cpu, symbols["ads_sdma_init_done.25054"], self.initial[2])
        put(cpu, COUNTER, self.counter)
        if self.context_input is not None:
            put(cpu, DEVICE + 0x4C, ENGINE)
            put(cpu, ENGINE + 0x1D0C, CCB)
            put(cpu, ENGINE + 0x1F50, CONTEXT)
            put(cpu, ENGINE + 0x1F54, CONTEXT_PHYS)
            put(cpu, ENGINE + 0x2008, BD)
            put(cpu, ENGINE + 0x2010, self.context_input.pc)
            put(cpu, ENGINE + 0x15C, ENGINE)
            put(cpu, ENGINE + 0x164, 1)
            put(cpu, symbols["arm_dma_ops"], symbols["arm_dma_alloc"])
            cpu.mem_write(symbols["user_regs"], bytes(self.context_input.registers))
            cpu.reg_write(UC_ARM_REG_R0, DEVICE)
            cpu.reg_write(UC_ARM_REG_R3, 1)
        else:
            put(cpu, REGS + 4, 2)
            cpu.reg_write(UC_ARM_REG_R1, ENGINE)

            def counter_read(uc, access, address, size, value, _):
                if size != 4:
                    raise ValueError("unexpected counter read size")
                value = self.counter + (self.counter_reads if self.moving else 0)
                put(uc, address, value)
                self.counter_reads += 1

            cpu.hook_add(UC_HOOK_MEM_READ, counter_read, begin=COUNTER, end=COUNTER)

    def finish(self, cpu):
        self.state = [u32(cpu, self.symbols["sdma_ads_a7_counter.25055"]),
                      u32(cpu, self.symbols["sdma_queue_head"]),
                      u32(cpu, self.symbols["ads_sdma_init_done.25054"]), 0]


NOTIFY = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32)


def host_progress(library, initial, counter):
    state, notifications = Progress(*initial), []
    callback = NOTIFY(lambda _, head: notifications.append([head, state.counter]))
    function = library.sdma_eeg_advance
    function.argtypes = [ctypes.POINTER(Progress), ctypes.c_uint32, NOTIFY, ctypes.c_void_p]
    function.restype = ctypes.c_int
    result = function(ctypes.byref(state), counter, callback, None)
    return result, unpack_progress(bytes(state)), notifications


class ArmProgress:
    def __init__(self, state, counter):
        self.initial, self.counter, self.notifications = state, counter, []

    def prepare_arm(self, cpu, data):
        self.address = data
        cpu.mem_write(data, struct.pack("<3Ii", *self.initial))
        for register, value in ((UC_ARM_REG_R0, data), (UC_ARM_REG_R1, self.counter),
                                (UC_ARM_REG_R2, 0x50000000), (UC_ARM_REG_R3, data)):
            cpu.reg_write(register, value)
        self.cpu = cpu

    def io(self, head, _unused_r2, _unused_r3):
        if self.cpu.reg_read(UC_ARM_REG_R0) != self.address:
            raise ValueError("unexpected notification callback arguments")
        self.notifications.append([head, u32(self.cpu, self.address)])
        return 0

    def finish_arm(self, cpu, data):
        self.state = unpack_progress(bytes(cpu.mem_read(data, 16)))


class ArmContext:
    def __init__(self, item, output_words=32):
        self.item, self.output_words = item, output_words

    def prepare_arm(self, cpu, data):
        cpu.mem_write(data, bytes(self.item))
        self.output = data + 0x100
        cpu.mem_write(self.output - 4, bytes([0xA5]) * 136)
        cpu.reg_write(UC_ARM_REG_R0, data)
        cpu.reg_write(UC_ARM_REG_R1, self.output)
        cpu.reg_write(UC_ARM_REG_R2, self.output_words)

    def io(self, *args):
        raise ValueError("unexpected I/O in context builder")

    def finish_arm(self, cpu, data):
        output = bytes(cpu.mem_read(self.output - 4, 136))
        if output[:4] != bytes([0xA5]) * 4 or output[-4:] != bytes([0xA5]) * 4:
            raise ValueError("context output exceeded its buffer")
        self.context_bytes = output[4:132]


def verify(path, arm_compiler="arm-linux-gnueabihf-gcc"):
    inputs = stock_inputs(path, ("sdma_int_handler", "trigger_user_script"))
    source = Path(__file__).with_name("sdma_eeg.c")
    digest, progress_cases, context_cases = hashlib.sha256(), 0, 0
    with tempfile.TemporaryDirectory() as folder:
        shared, arm_path = Path(folder) / "sdma.so", Path(folder) / "sdma.arm.elf"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-shared", "-fPIC", str(source), "-o", str(shared)], check=True)
        subprocess.run([arm_compiler, "-std=c11", "-O2", "-Wall", "-Wextra",
                        "-Werror", "-marm", "-mcpu=cortex-a7", "-msoft-float", "-fno-tree-vectorize",
                        "-ffreestanding", "-nostdlib", "-static", "-no-pie",
                        "-Wl,-Ttext=0x20000000,-e,sdma_eeg_advance", str(source), "-o", str(arm_path)], check=True)
        library, binary = ctypes.CDLL(str(shared)), arm_path.read_bytes()
        cases = [((100, head, 1, 0), 100 + delta) for head in range(64) for delta in (0, 1, 2, 63, 64)]
        cases += [((MASK - head, head, 1, 0), 0) for head in range(64)]
        cases += [((0, 0, 0, 0), counter) for counter in (0, 1, MASK)]
        for initial, counter in cases:
            stock = Stock(initial, counter)
            original = run_stock(inputs, stock, entry="sdma_int_handler", extension=stock)
            host = host_progress(library, initial, counter)
            target = ArmProgress(initial, counter)
            result = run_arm_reconstructed(binary, target, entry="sdma_eeg_advance", extension=target)
            if original != 1 or stock.state != host[1] or stock.state != target.state:
                raise ValueError(f"IRQ progress state mismatch: {initial}, {counter}")
            if stock.notifications != host[2] or stock.notifications != target.notifications:
                raise ValueError("notification ordering differs from stock")
            if host[0] != len(stock.notifications) or result != host[0]:
                raise ValueError("notification count differs")
            digest.update(json.dumps([initial, counter, stock.state, stock.notifications], separators=(",", ":")).encode())
            progress_cases += 1

        jump_cases = [((0, 0, 1, 0), 65), ((10, 7, 1, 0), 0), ((0, 0, 1, 0), 0x80000000)]
        for initial, counter in jump_cases:
            stock = Stock(initial, counter)
            stock_result = run_stock(inputs, stock, entry="sdma_int_handler", extension=stock)
            if counter == 65:
                if stock_result != 1 or len(stock.notifications) != 65:
                    raise ValueError("stock ring-overrun behavior differs")
            elif stock_result is not None:
                raise ValueError("stock unexpectedly bounded a reset or large counter jump")
            host = host_progress(library, initial, counter)
            target = ArmProgress(initial, counter)
            result = run_arm_reconstructed(binary, target, entry="sdma_eeg_advance", extension=target)
            expected = list(initial[:3]) + [-75]
            if host != (-75, expected, []) or result != -75 or target.state != expected or target.notifications:
                raise ValueError("oversized progress did not latch a fault without notifications")
            if host_progress(library, host[1], initial[0]) != (-75, expected, []):
                raise ValueError("counter jump fault was not latched")
            latched = ArmProgress(host[1], initial[0])
            if run_arm_reconstructed(binary, latched, entry="sdma_eeg_advance", extension=latched) != -75:
                raise ValueError("counter jump fault was not latched on ARM")
            if latched.state != expected or latched.notifications:
                raise ValueError("latched fault changed ARM state")

        invalid_progress = [(0, 64, 1, 0), (0, MASK, 1, 0), (0, 0, 2, 0), (0, 0, 1, -5)]
        for initial in invalid_progress:
            host = host_progress(library, initial, 1)
            target = ArmProgress(initial, 1)
            result = run_arm_reconstructed(binary, target, entry="sdma_eeg_advance", extension=target)
            if host != (-22, list(initial), []) or result != -22 or target.state != list(initial) or target.notifications:
                raise ValueError("invalid progress changed state or emitted notifications")
        moving = Stock((0, 0, 1, 0), 1, moving=True)
        if run_stock(inputs, moving, entry="sdma_int_handler", extension=moving) is not None:
            raise ValueError("stock handler unexpectedly caught a continuously moving counter")
        if host_progress(library, (0, 0, 1, 0), 1) != (1, [1, 1, 1, 0], [[1, 0]]):
            raise ValueError("snapshot progress was not bounded")

        rng = random.Random(1296)
        function = library.sdma_eeg_prepare_context
        function.argtypes = [ctypes.POINTER(ContextInput), ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint]
        function.restype = ctypes.c_int
        for pc in (1, 0x1800, 0x1F00, 0x3FFF):
            for ring, counter in ((0x40000000, 0x40001000), (0x80000000, 0x10000000), (0xFFFFFC00, 4)):
                item = ContextInput(pc, ring, counter, (ctypes.c_uint32 * 8)(*[rng.getrandbits(32) for _ in range(8)]))
                stock = Stock(context=item)
                original = run_stock(inputs, stock, entry="trigger_user_script", extension=stock)
                output = (ctypes.c_uint32 * 34)(*[0xA5A5A5A5] * 34)
                pointer = ctypes.cast(ctypes.byref(output, 4), ctypes.POINTER(ctypes.c_uint32))
                host_result = function(ctypes.byref(item), pointer, 32)
                target = ArmContext(item)
                arm_result = run_arm_reconstructed(binary, target, entry="sdma_eeg_prepare_context", extension=target)
                if original != 1 or host_result or arm_result:
                    raise ValueError("context construction failed")
                if bytes(output)[4:132] != stock.context_bytes or target.context_bytes != stock.context_bytes:
                    raise ValueError("context bytes differ from stock")
                if output[0] != 0xA5A5A5A5 or output[33] != 0xA5A5A5A5:
                    raise ValueError("host context guard changed")
                if stock.descriptor != struct.pack("<3I", 0x018B0020, CONTEXT_PHYS, 0x820):
                    raise ValueError("unexpected stock channel-1 context descriptor")
                digest.update(bytes(item) + stock.context_bytes + stock.descriptor)
                context_cases += 1

        invalid_contexts = [(0, 0x40000000, 0x40001000, 32), (0x4000, 0x40000000, 0x40001000, 32),
                            (1, 0, 0x40001000, 32), (1, 0x40000002, 0x40001000, 32),
                            (1, 0xFFFFFFFC, 4, 32), (1, 0x40000000, 0, 32),
                            (1, 0x40000000, 0x40000000, 32), (1, 0x40000000, 0x400003FC, 32),
                            (1, 0x40000000, 0x40001002, 32), (1, 0x40000000, 0x40001000, 31)]
        for pc, ring, counter, count in invalid_contexts:
            item = ContextInput(pc, ring, counter)
            output = (ctypes.c_uint32 * 32)(*[0xA5A5A5A5] * 32)
            target = ArmContext(item, count)
            if function(ctypes.byref(item), output, count) != -22 or bytes(output) != bytes([0xA5]) * 128:
                raise ValueError("invalid context modified host output")
            if run_arm_reconstructed(binary, target, entry="sdma_eeg_prepare_context", extension=target) != -22:
                raise ValueError("invalid context accepted on ARM")
            if target.context_bytes != bytes([0xA5]) * 128:
                raise ValueError("invalid context modified ARM output")
    return {"kernel_raw_sha256": RAW_HASH, "matching_progress_cases": progress_cases,
            "matching_context_cases": context_cases, "invalid_context_cases": len(invalid_contexts),
            "invalid_progress_cases": len(invalid_progress),
            "bounded_counter_jump_cases": len(jump_cases), "moving_counter": "stock exceeds instruction bound; reconstruction snapshots progress",
            "trace_sha256": digest.hexdigest(), "runtime_qualified": False,
            "limits": "Context and progress only; allocation, script loading, Linux callback barriers, IRQ dispatch and physical DMA remain unqualified"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel_elf", type=Path)
    parser.add_argument("--arm-compiler", default="arm-linux-gnueabihf-gcc")
    args = parser.parse_args()
    print(json.dumps(verify(args.kernel_elf, args.arm_compiler), indent=2))


if __name__ == "__main__":
    main()
