#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Compare reconstructed ADC initialization with isolated stock kernel code.

All MMIO, GPIO and delay calls use synthetic transports. No device is opened.
The stock routines run only in a bounded Unicorn emulator. A successful trace
comparison does not validate clocks, barriers or timing on physical hardware.
"""

import argparse
import ctypes
import hashlib
import json
import io
from pathlib import Path
import struct
import subprocess
import tempfile

from elftools.elf.elffile import ELFFile
from unicorn import (Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE,
                     UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_PROT_READ,
                     UC_PROT_WRITE, UC_PROT_EXEC)
from unicorn.arm_const import (UC_CPU_ARM_CORTEX_A7, UC_ARM_REG_R0,
                               UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
                               UC_ARM_REG_LR, UC_ARM_REG_PC,
                               UC_ARM_REG_SP)

RAW_HASH = "e15f659afdffab3fde7c997883475e4c95b5d3cdc1fbcd23c76365ee4cd52dcb"
READ, WRITE, GPIO_OUTPUT, GPIO_SET, SLEEP_MS, SLEEP_US = range(6)
SPI, CLOCK, EVENT, RETURN = 0x02008000, 0x020C406C, 0x020EC20C, 0x80000000
ROUTINES = ("ads1296_sdma_open", "spi_conf_command", "spi_flush",
            "sdma_request_disable", "sdma_send_command", "sdma_write_registers")
STUBS = ("__arm_ioremap", "__arm_iounmap", "gpio_to_desc",
         "gpiod_direction_output_raw", "gpiod_set_raw_value",
         "msleep", "usleep_range", "printk")


class Transport:
    def __init__(self, ids=(0x90,), gpio_failure=0, enabled=False, stale=0, stalled=False):
        self.ids = ids
        self.gpio_failure = gpio_failure
        self.stale = stale
        self.stalled = stalled
        self.registers = {CLOCK: 0x80, SPI + 8: int(enabled)}
        self.tx = []
        self.rx_index = 0
        self.id_index = 0
        self.trace = []

    def io(self, op, a, b):
        result = 0
        if op == READ:
            if a == SPI + 24:
                result = 0 if self.stalled else 0x81 | (8 if self.stale else 0)
            elif a == SPI:
                if self.stale:
                    self.stale -= 1
                elif self.tx == [0x20, 0, 0]:
                    if self.rx_index == 2:
                        result = self.ids[min(self.id_index, len(self.ids) - 1)]
                        self.id_index += 1
                    self.rx_index += 1
            elif a in self.registers:
                result = self.registers[a]
            else:
                raise ValueError(f"unexpected modeled read {a:#x}")
        elif op == WRITE:
            if a == SPI + 4:
                self.tx.append(b)
            elif a in (CLOCK, EVENT, SPI + 8, SPI + 12):
                self.registers[a] = b
            else:
                raise ValueError(f"unexpected modeled write {a:#x}")
        elif op in (GPIO_OUTPUT, GPIO_SET):
            if a not in (35, 90) or b not in (0, 1):
                raise ValueError("unexpected GPIO operation")
            if op == GPIO_OUTPUT and a == self.gpio_failure:
                result = 0xFFFFFFFB
            if a == 90 and b == 0:
                self.tx = []
                self.rx_index = 0
        elif op not in (SLEEP_MS, SLEEP_US):
            raise ValueError("unexpected transport operation")
        self.trace.append([op, a, b, result])
        return result


def stock_inputs(path, routines=ROUTINES):
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        section = elf.get_section_by_name(".kernel")
        if section is None or hashlib.sha256(section.data()).hexdigest() != RAW_HASH:
            raise ValueError("kernel content does not match the reviewed firmware")
        raw, base = section.data(), section["sh_addr"]
        symbols = {s.name: s["st_value"] for s in elf.get_section_by_name(".symtab").iter_symbols()}
    if symbols["ads1296_sdma_open"] != 0x8041E868:
        raise ValueError("unexpected kernel symbol layout")
    addresses = sorted(set(symbols.values()))
    ranges = [(symbols[name], next(a for a in addresses if a > symbols[name])) for name in routines]
    return raw, base, symbols, ranges


def run_stock(inputs, model, entry="ads1296_sdma_open", argument=0, extension=None):
    raw, base, symbols, ranges = inputs
    uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
    uc.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_A7)
    pages = {RETURN}
    for start, end in ranges:
        pages.update(range(start & ~4095, (end + 4095) & ~4095, 4096))
    stub_names = STUBS + (tuple(extension.stubs) if extension else ())
    pages.update(symbols[name] & ~4095 for name in stub_names)
    for page in pages:
        uc.mem_map(page, 4096, UC_PROT_READ | UC_PROT_EXEC)
    for start, end in ranges:
        uc.mem_write(start, raw[start - base:end - base])
    for page in (SPI, CLOCK & ~4095, EVENT & ~4095, 0x808A4000):
        uc.mem_map(page, 4096, UC_PROT_READ | UC_PROT_WRITE)
    uc.mem_map(0x10000000, 0x10000, UC_PROT_READ | UC_PROT_WRITE)
    stubs = {symbols[name]: name for name in stub_names}

    def code_hook(cpu, address, size, _):
        name = stubs.get(address)
        if name is None:
            if not any(a <= address < b for a, b in ranges):
                raise ValueError(f"execution left selected functions: {address:#x}")
            return
        a, b = cpu.reg_read(UC_ARM_REG_R0), cpu.reg_read(UC_ARM_REG_R1)
        result = 0
        if name == "__arm_ioremap":
            if (a, b) not in ((SPI, 4096), (CLOCK, 4), (EVENT, 4096)):
                raise ValueError("unexpected ioremap request")
            result = a
        elif name == "gpio_to_desc":
            result = a
        elif name == "gpiod_direction_output_raw":
            result = model.io(GPIO_OUTPUT, a, b)
        elif name == "gpiod_set_raw_value":
            model.io(GPIO_SET, a, b)
        elif name == "msleep":
            model.io(SLEEP_MS, a, 0)
        elif name == "usleep_range":
            model.io(SLEEP_US, a, b)
        elif extension and name in extension.stubs:
            result = extension.stubs[name](cpu, a, b, cpu.reg_read(UC_ARM_REG_R2))
        cpu.reg_write(UC_ARM_REG_R0, result)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def read_hook(cpu, access, address, size, value, _):
        if size != 4:
            raise ValueError("unexpected MMIO read width")
        result = model.io(READ, address, 0)
        cpu.mem_write(address, struct.pack("<I", result))

    def write_hook(cpu, access, address, size, value, _):
        if size != 4:
            raise ValueError("unexpected MMIO write width")
        model.io(WRITE, address, value)

    uc.hook_add(UC_HOOK_CODE, code_hook)
    for start, end in ((SPI, SPI + 0x1F), (CLOCK, CLOCK + 3), (EVENT, EVENT + 3)):
        uc.hook_add(UC_HOOK_MEM_READ, read_hook, begin=start, end=end)
        uc.hook_add(UC_HOOK_MEM_WRITE, write_hook, begin=start, end=end)
    uc.reg_write(UC_ARM_REG_SP, 0x1000FFF0)
    uc.reg_write(UC_ARM_REG_LR, RETURN)
    uc.reg_write(UC_ARM_REG_R1, argument)
    if extension:
        extension.prepare(uc, symbols)
    uc.emu_start(symbols[entry], RETURN, count=20000, timeout=500000)
    if extension:
        extension.finish(uc)
    if uc.reg_read(UC_ARM_REG_PC) != RETURN:
        return None
    return ctypes.c_int32(uc.reg_read(UC_ARM_REG_R0)).value


IO_FN = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int,
                         ctypes.c_uint32, ctypes.c_uint32)


class CTransport(ctypes.Structure):
    _fields_ = [("io", IO_FN), ("context", ctypes.c_void_p), ("poll_limit", ctypes.c_uint)]


def run_reconstructed(library, model):
    errors = []

    def callback(context, op, a, b):
        try:
            return model.io(op, a, b)
        except Exception as exc:
            errors.append(exc)
            return 0

    callback_ref = IO_FN(callback)
    transport = CTransport(callback_ref, None, 32)
    result = library.ads129x_sdma_initialize(ctypes.byref(transport))
    if errors:
        raise errors[0]
    return result


def run_arm_reconstructed(binary, model, entry=None, extension=None):
    elf = ELFFile(io.BytesIO(binary))
    cpu = Uc(UC_ARCH_ARM, UC_MODE_ARM)
    cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_A7)
    mapped = set()
    for segment in elf.iter_segments():
        if segment["p_type"] != "PT_LOAD":
            continue
        start, end = segment["p_vaddr"], segment["p_vaddr"] + segment["p_memsz"]
        for page in range(start & ~4095, (end + 4095) & ~4095, 4096):
            if page not in mapped:
                cpu.mem_map(page, 4096, UC_PROT_READ | UC_PROT_EXEC)
                mapped.add(page)
        cpu.mem_write(start, segment.data())
    callback, stop, data = 0x50000000, 0x50001000, 0x60000000
    cpu.mem_map(callback, 8192, UC_PROT_READ | UC_PROT_EXEC)
    cpu.mem_map(data, 0x20000, UC_PROT_READ | UC_PROT_WRITE)
    cpu.mem_write(data, struct.pack("<3I", callback, 0, 32))

    def call_io(uc, address, size, _):
        result = model.io(uc.reg_read(UC_ARM_REG_R1),
                          uc.reg_read(UC_ARM_REG_R2), uc.reg_read(UC_ARM_REG_R3))
        uc.reg_write(UC_ARM_REG_R0, result)
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    cpu.hook_add(UC_HOOK_CODE, call_io, begin=callback, end=callback)
    cpu.reg_write(UC_ARM_REG_SP, data + 0x1FFF0)
    cpu.reg_write(UC_ARM_REG_R0, data)
    cpu.reg_write(UC_ARM_REG_LR, stop)
    if extension:
        extension.prepare_arm(cpu, data)
    start = elf["e_entry"]
    if entry:
        symbols = {s.name: s["st_value"] for s in elf.get_section_by_name(".symtab").iter_symbols()}
        start = symbols[entry]
    cpu.emu_start(start, stop, count=20000, timeout=500000)
    if cpu.reg_read(UC_ARM_REG_PC) != stop:
        raise ValueError("reconstructed ARM initializer did not return")
    if extension:
        extension.finish_arm(cpu, data)
    return ctypes.c_int32(cpu.reg_read(UC_ARM_REG_R0)).value


def verify(path):
    inputs = stock_inputs(path)
    source = Path(__file__).with_name("ads129x_init.c")
    cases = [dict(ids=(0x90,)), dict(ids=(0x91,)), dict(ids=(0x00, 0x90)),
             dict(ids=(0x92,)), dict(gpio_failure=35), dict(gpio_failure=90),
             dict(enabled=True), dict(stale=3)]
    digest = hashlib.sha256()
    counts = []
    with tempfile.TemporaryDirectory() as folder:
        shared = Path(folder) / "adc.so"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-shared", "-fPIC", str(source), "-o", str(shared)], check=True)
        arm_path = Path(folder) / "adc.arm.elf"
        subprocess.run(["arm-linux-gnueabihf-gcc", "-std=c11", "-O2", "-Wall", "-Wextra",
                        "-Werror", "-marm", "-mcpu=cortex-a7", "-ffreestanding", "-nostdlib",
                        "-static", "-no-pie", "-Wl,-Ttext=0x20000000,-e,ads129x_sdma_initialize",
                        str(source), "-o", str(arm_path)], check=True)
        arm_binary = arm_path.read_bytes()
        library = ctypes.CDLL(str(shared))
        library.ads129x_sdma_initialize.argtypes = [ctypes.POINTER(CTransport)]
        library.ads129x_sdma_initialize.restype = ctypes.c_int
        for index, options in enumerate(cases):
            original, replacement = Transport(**options), Transport(**options)
            a, b = run_stock(inputs, original), run_reconstructed(library, replacement)
            if a != b or original.trace != replacement.trace:
                mismatch = next((i for i, (x, y) in enumerate(zip(original.trace, replacement.trace)) if x != y), None)
                raise ValueError(f"case {index} differs: returns {a}/{b}, lengths {len(original.trace)}/{len(replacement.trace)}, first mismatch {mismatch}")
            arm_model = Transport(**options)
            if run_arm_reconstructed(arm_binary, arm_model) != a or arm_model.trace != original.trace:
                raise ValueError(f"reconstructed ARM trace differs in case {index}")
            counts.append(len(original.trace))
            digest.update(json.dumps([index, a, original.trace], separators=(",", ":")).encode())
        stalled = Transport(stalled=True)
        if run_stock(inputs, stalled) is not None:
            raise ValueError("stock polling did not exhibit the expected unbounded wait")
        fixed = Transport(stalled=True)
        if run_reconstructed(library, fixed) != -110:
            raise ValueError("reconstruction did not return a timeout")
        if fixed.trace[-2:] != [[GPIO_SET, 35, 0, 0], [GPIO_SET, 90, 1, 0]]:
            raise ValueError("timeout did not leave power off and chip select high")
        arm_stalled = Transport(stalled=True)
        if run_arm_reconstructed(arm_binary, arm_stalled) != -110 or arm_stalled.trace != fixed.trace:
            raise ValueError("reconstructed ARM timeout trace differs from host")
    return {"kernel_raw_sha256": RAW_HASH, "matching_cases": len(cases),
            "reconstruction_targets": ["host C", "Cortex-A7 ARM C"],
            "io_event_counts": counts, "trace_sha256": digest.hexdigest(),
            "stalled_peripheral": "stock exceeded instruction limit; reconstruction returned -110 and cleaned up"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel_elf", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.kernel_elf), indent=2))


if __name__ == "__main__":
    main()
