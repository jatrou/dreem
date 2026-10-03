#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Exercise the built research module's ARM read/ioctl paths in an emulator.

All kernel calls, MMIO, task memory, and samples are synthetic. No module is
loaded into Linux. Requires the debug ELF produced by build_adc_module.py.
Does not validate probe, open, removal, PM, real scheduling, or hardware.
"""

import argparse
import ctypes
import hashlib
import io
import json
from pathlib import Path
import struct

from elftools.elf.elffile import ELFFile
from unicorn import (Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_READ,
                     UC_HOOK_MEM_WRITE, UC_PROT_READ, UC_PROT_WRITE, UC_PROT_EXEC)
from unicorn.arm_const import (UC_CPU_ARM_CORTEX_A7, UC_ARM_REG_R0, UC_ARM_REG_R1,
                               UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_SP,
                               UC_ARM_REG_LR, UC_ARM_REG_PC)

from verify_adc_init import READ, WRITE, GPIO_OUTPUT, GPIO_SET, SLEEP_MS, SLEEP_US, SPI, CLOCK, EVENT
from verify_adc_control import AcquisitionTransport, QUEUE_TRYLOCK
from verify_adc_read import fixtures, ORDER

ADC, FILE, RING, USER = 0x30000000, 0x30001000, 0x30005000, 0x30008004
STACK, TASK, STOP = 0x10000000, 0x30020000, 0x210FF000
DATA_SYMBOLS = {"sdma_ads_user_buffer", "sdma_queue_head", "ads_data_sem",
                "outer_cache", "jiffies", "kmalloc_caches", "param_ops_bool"}
GUARD = bytes([0xD3]) * 4


def signed(value, bits):
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


class Module:
    def __init__(self, path):
        self.binary = path.read_bytes()
        if len(self.binary) > 4 * 1024 * 1024:
            raise ValueError("unexpectedly large module")
        self.elf = ELFFile(io.BytesIO(self.binary))
        if self.elf.elfclass != 32 or not self.elf.little_endian or self.elf["e_machine"] != "EM_ARM":
            raise ValueError("requires little-endian ARM32 module")
        if not self.elf.has_dwarf_info():
            raise ValueError("requires a module built with debug information")
        self.members = {}
        for unit in self.elf.get_dwarf_info().iter_CUs():
            for die in unit.iter_DIEs():
                if die.tag != "DW_TAG_structure_type" or "DW_AT_name" not in die.attributes:
                    continue
                members = {}
                for child in die.iter_children():
                    if child.tag == "DW_TAG_member" and "DW_AT_name" in child.attributes:
                        location = child.attributes.get("DW_AT_data_member_location")
                        if location and isinstance(location.value, int):
                            members[child.attributes["DW_AT_name"].value.decode()] = location.value
                if members:
                    self.members[die.attributes["DW_AT_name"].value.decode()] = members


class Machine:
    def __init__(self, module, pending=1, nonblock=False):
        self.module = module
        self.model = AcquisitionTransport(head=7, pending=pending)
        self.calls, self.copy_failure, self.lock_failure = [], False, False
        self.cancel_on_wait, self.signal_on_wait = False, False
        self.locked, self.spi_locked = False, False
        self.uc = cpu = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_A7)
        self.symbols, self.stubs, sections = {}, {}, {}
        cursor, stub = 0x20000000, 0x21000000
        for index, section in enumerate(module.elf.iter_sections()):
            if not section["sh_flags"] & 2 or not section["sh_size"]:
                continue
            size = (section["sh_size"] + 4095) & ~4095
            if size > 1024 * 1024:
                raise ValueError("oversized allocated section")
            permissions = UC_PROT_READ
            if section["sh_flags"] & 1:
                permissions |= UC_PROT_WRITE
            if section["sh_flags"] & 4:
                permissions |= UC_PROT_EXEC
            cpu.mem_map(cursor, size, permissions)
            if section["sh_type"] != "SHT_NOBITS":
                cpu.mem_write(cursor, section.data())
            sections[index] = cursor
            cursor += size
        cpu.mem_map(0x21000000, 0x100000, UC_PROT_READ | UC_PROT_EXEC)
        cpu.mem_map(0x22000000, 0x10000, UC_PROT_READ | UC_PROT_WRITE)
        data = 0x22000000
        table = module.elf.get_section_by_name(".symtab")
        resolved = {}
        for index, symbol in enumerate(table.iter_symbols()):
            where = symbol["st_shndx"]
            if where == "SHN_UNDEF":
                if not symbol.name:
                    resolved[index] = 0
                    continue
                if symbol.name in DATA_SYMBOLS:
                    address, data = data, data + 0x100
                else:
                    address, stub = stub, stub + 16
                    self.stubs[address] = symbol.name
            elif where == "SHN_ABS":
                address = symbol["st_value"]
            elif where in sections:
                address = sections[where] + symbol["st_value"]
            else:
                continue
            resolved[index] = address
            if symbol.name:
                self.symbols[symbol.name] = address
        for section in module.elf.iter_sections():
            if section["sh_type"] != "SHT_REL" or section["sh_info"] not in sections:
                continue
            for relocation in section.iter_relocations():
                kind = relocation["r_info_type"]
                if kind == 0:
                    continue
                place = sections[section["sh_info"]] + relocation["r_offset"]
                symbol = resolved[relocation["r_info_sym"]]
                word = self.u32(place)
                if kind == 2:  # R_ARM_ABS32
                    word = (word + symbol) & 0xFFFFFFFF
                elif kind in (28, 29):  # R_ARM_CALL, R_ARM_JUMP24
                    offset = symbol + (signed(word & 0xFFFFFF, 24) << 2) - place
                    if offset & 3 or not -(1 << 25) <= offset < (1 << 25):
                        raise ValueError("out-of-range ARM branch relocation")
                    word = (word & 0xFF000000) | ((offset >> 2) & 0xFFFFFF)
                elif kind == 42:  # R_ARM_PREL31
                    word = (word & 0x80000000) | ((symbol + signed(word & 0x7FFFFFFF, 31) - place) & 0x7FFFFFFF)
                elif kind in (43, 44):  # R_ARM_MOVW_ABS_NC, R_ARM_MOVT_ABS
                    immediate = ((word >> 4) & 0xF000) | (word & 0xFFF)
                    value = symbol + (immediate << 16 if kind == 44 else immediate)
                    immediate = (value >> 16 if kind == 44 else value) & 0xFFFF
                    word = (word & ~0xF0FFF) | ((immediate & 0xF000) << 4) | (immediate & 0xFFF)
                else:
                    raise ValueError(f"unsupported ARM relocation {kind}")
                self.put(place, word)
        for address, size in ((ADC, 0x40000), (STACK, 0x10000), (SPI, 4096),
                              (CLOCK & ~4095, 4096), (EVENT & ~4095, 4096)):
            cpu.mem_map(address, size, UC_PROT_READ | UC_PROT_WRITE)
        self.put(self.symbols["sdma_ads_user_buffer"], RING)
        self.put(self.symbols["sdma_queue_head"], 7)
        cpu.mem_write(RING, fixtures(0)["ring"])
        cpu.mem_write(USER - 4, GUARD + bytes([0xCC]) * 16 + GUARD)
        self.field("file", FILE, "private_data", ADC)
        self.field("file", FILE, "f_flags", 0x800 if nonblock else 0)
        self.field("dreem_adc", ADC, "spi", ADC + 0x3000)
        self.field("spi_device", ADC + 0x3000, "master", ADC + 0x4000)
        for name, value in (("spi_regs", SPI), ("clock_reg", CLOCK), ("event_reg", EVENT)):
            self.field("dreem_adc", ADC, name, value)
        transport = ADC + module.members["dreem_adc"]["transport"]
        for name, value in (("io", self.symbols["adc_io"]), ("context", ADC), ("poll_limit", 32)):
            self.field("ads_transport", transport, name, value)
        state = ADC + module.members["dreem_adc"]["state"]
        for name, value in (("ring", RING), ("read_offset", 0), ("errors", 42)):
            self.field("ads_sdma_state", state, name, value)
        for name in ("opened", "initialized", "running"):
            self.field("dreem_adc", ADC, name, 1, width=1)
        thread = STACK + 0xE000
        self.field("thread_info", thread, "addr_limit", 0x7FFFFFFF)
        self.field("thread_info", thread, "task", TASK)
        self.field("task_struct", TASK, "stack", thread)
        cpu.reg_write(UC_ARM_REG_SP, STACK + 0xFFF0)
        cpu.hook_add(UC_HOOK_CODE, self.code)
        for start, end in ((SPI, SPI + 24), (CLOCK, CLOCK + 3), (EVENT, EVENT + 3)):
            cpu.hook_add(UC_HOOK_MEM_READ, self.read_mmio, begin=start, end=end)
            cpu.hook_add(UC_HOOK_MEM_WRITE, self.write_mmio, begin=start, end=end)

    def put(self, address, value):
        self.uc.mem_write(address, struct.pack("<I", value & 0xFFFFFFFF))

    def u32(self, address):
        return struct.unpack("<I", self.uc.mem_read(address, 4))[0]

    def field(self, kind, address, name, value=None, width=4):
        address += self.module.members[kind][name]
        if value is None:
            return int.from_bytes(self.uc.mem_read(address, width), "little")
        self.uc.mem_write(address, value.to_bytes(width, "little"))

    def code(self, cpu, address, size, _):
        if address not in self.stubs:
            if not 0x20000000 <= address < 0x21000000:
                raise ValueError(f"execution left module code: {address:#x}")
            return
        name = self.stubs[address]
        a, b, c = (cpu.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2))
        result = 0
        self.calls.append(name)
        if name in ("mutex_lock", "mutex_lock_interruptible"):
            if self.lock_failure and name == "mutex_lock_interruptible":
                result = -4
            elif self.locked:
                raise ValueError("unexpected nested mutex acquisition")
            else:
                self.locked = True
        elif name == "mutex_unlock":
            if not self.locked:
                raise ValueError("unlock without acquisition")
            self.locked = False
        elif name == "spi_bus_lock":
            if self.spi_locked:
                raise ValueError("nested SPI bus lock")
            self.spi_locked = True
        elif name == "spi_bus_unlock":
            if not self.spi_locked:
                raise ValueError("SPI unlock without acquisition")
            self.spi_locked = False
        elif name == "__copy_to_user":
            if a != USER or c not in (4, 16):
                raise ValueError("unexpected userspace copy boundary")
            copied = c // 2 if self.copy_failure else c
            cpu.mem_write(a, bytes(cpu.mem_read(b, copied)))
            result = c - copied
        elif name in ("down_timeout", "down_trylock"):
            if a != self.symbols["ads_data_sem"]:
                raise ValueError("unexpected semaphore address")
            result = self.model.io(QUEUE_TRYLOCK, 0, 0)
            if result and name == "down_timeout":
                result = -62
                if self.cancel_on_wait:
                    self.field("dreem_adc", ADC, "cancelled", 1)
                if self.signal_on_wait:
                    self.field("thread_info", STACK + 0xE000, "flags", 1)
        elif name == "msecs_to_jiffies":
            result = a
        elif name == "gpio_to_desc":
            result = a
        elif name == "gpiod_direction_output_raw":
            result = self.model.io(GPIO_OUTPUT, a, b)
        elif name == "gpiod_set_raw_value":
            self.model.io(GPIO_SET, a, b)
        elif name == "msleep":
            self.model.io(SLEEP_MS, a, 0)
        elif name == "usleep_range":
            self.model.io(SLEEP_US, a, b)
        else:
            raise ValueError(f"unmodeled kernel call: {name}")
        cpu.reg_write(UC_ARM_REG_R0, result & 0xFFFFFFFF)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def read_mmio(self, cpu, access, address, size, value, _):
        if size != 4:
            raise ValueError("invalid MMIO read width")
        self.put(address, self.model.io(READ, address, 0))

    def write_mmio(self, cpu, access, address, size, value, _):
        if size != 4:
            raise ValueError("invalid MMIO write width")
        self.model.io(WRITE, address, value)

    def call(self, name, *args):
        for register, value in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3), args):
            self.uc.reg_write(register, value)
        self.uc.reg_write(UC_ARM_REG_LR, STOP)
        self.uc.emu_start(self.symbols[name], STOP, count=100000, timeout=1000000)
        if self.uc.reg_read(UC_ARM_REG_PC) != STOP or self.locked or self.spi_locked:
            raise ValueError("module call did not return with its mutexes released")
        return ctypes.c_int32(self.uc.reg_read(UC_ARM_REG_R0)).value

    def output(self):
        data = bytes(self.uc.mem_read(USER - 4, 24))
        if data[:4] != GUARD or data[-4:] != GUARD:
            raise ValueError("output guard changed")
        return data[4:20]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(path):
    module = Module(path)
    results = []
    for size in (0, 1, 12, 15):
        machine = Machine(module)
        require(machine.call("adc_read", FILE, USER, size, 0) == -22, "short read accepted")
        require(not machine.calls and machine.model.pending == 1, "short read consumed state")
        require(machine.output() == bytes([0xCC]) * 16, "short read changed output")
        results.append("short output " + str(size))
    machine = Machine(module)
    require(machine.call("adc_read", FILE, USER, 16, 0) == 16, "normal read failed")
    expected = bytes(fixtures(0)["ring"][i] for i in ORDER) + bytes([7, 0, 0, 0])
    require(machine.output() == expected, "normal read payload differs")
    results.append("normal read and zero padding")
    machine = Machine(module)
    machine.copy_failure = True
    require(machine.call("adc_read", FILE, USER, 16, 0) == -14, "copy failure not reported")
    require(machine.field("dreem_adc", ADC, "pending", width=1) == 1, "failed copy lost frame")
    waits = machine.calls.count("down_timeout")
    machine.copy_failure = False
    require(machine.call("adc_read", FILE, USER, 16, 0) == 16, "copy retry failed")
    require(machine.output() == expected and machine.calls.count("down_timeout") == waits,
            "copy retry consumed another frame")
    results.append("partial copy failure retains frame for retry")
    for nonblock, expected_result, expected_waits in ((True, -11, 0), (False, -110, 10)):
        machine = Machine(module, pending=0, nonblock=nonblock)
        require(machine.call("adc_read", FILE, USER, 16, 0) == expected_result, "empty queue handling differs")
        require(machine.calls.count("down_timeout") == expected_waits, "incorrect wait bound")
        results.append("nonblocking empty queue" if nonblock else "bounded blocking empty queue")
    for field, result in (("detached", -19), ("cancelled", -32)):
        machine = Machine(module)
        machine.field("dreem_adc", ADC, field, 1)
        require(machine.call("adc_read", FILE, USER, 16, 0) == result, "read did not respect lifecycle flag")
        require(machine.model.pending == 1, "rejected read consumed a notification")
        results.append(field + " read")
    for flag, expected_result in (("cancel_on_wait", -108), ("signal_on_wait", -4)):
        machine = Machine(module, pending=0)
        setattr(machine, flag, True)
        require(machine.call("adc_read", FILE, USER, 16, 0) == expected_result,
                "wait did not respect cancellation or signal")
        require(machine.calls.count("down_timeout") == 1 and machine.model.pending == 0,
                "cancelled wait continued or consumed unavailable data")
        require(machine.output() == bytes([0xCC]) * 16, "cancelled wait changed output")
        results.append(flag)
    machine = Machine(module)
    machine.lock_failure = True
    require(machine.call("adc_read", FILE, USER, 16, 0) == -512, "interrupted lock handling differs")
    results.append("interrupted read lock")
    machine = Machine(module)
    require(machine.call("adc_ioctl", FILE, 4, USER) == 0, "counter ioctl failed")
    require(machine.output()[:4] == struct.pack("<I", 42), "counter value differs")
    machine.copy_failure = True
    require(machine.call("adc_ioctl", FILE, 4, USER) == -14, "counter copy failure not reported")
    require(machine.call("adc_ioctl", FILE, 99, USER) == -25, "unknown ioctl accepted")
    results.extend(("counter ioctl", "counter copy failure", "unknown ioctl"))
    machine = Machine(module)
    require(machine.call("adc_ioctl", FILE, 1, 0) == -16, "duplicate start accepted")
    require(machine.call("adc_ioctl", FILE, 0, 0) == 0, "stop failed")
    require(machine.field("dreem_adc", ADC, "running", width=1) == 0, "stop left running state")
    require(machine.call("adc_read", FILE, USER, 16, 0) == -32, "stopped read accepted")
    require(machine.call("adc_ioctl", FILE, 1, 0) == 0, "restart failed")
    require(machine.field("dreem_adc", ADC, "running", width=1) == 1 and
            machine.field("dreem_adc", ADC, "cancelled") == 0, "restart state differs")
    require(bytes(machine.uc.mem_read(RING, 1024)) == bytes([0x42]) * 1024, "restart did not reset ring")
    results.extend(("duplicate start", "stop and stopped read", "restart after stop"))
    machine = Machine(module)
    machine.model.stalled = True
    require(machine.call("adc_ioctl", FILE, 0, 0) == -110, "stalled stop not bounded")
    require(machine.field("dreem_adc", ADC, "initialized", width=1) == 0, "stalled stop retained initialized state")
    require(machine.model.trace[-3:] == [[WRITE, EVENT, 0, 0], [GPIO_SET, 35, 0, 0], [GPIO_SET, 90, 1, 0]],
            "stalled stop did not shut down")
    results.append("stalled stop shuts down and invalidates initialization")
    return {"module_sha256": hashlib.sha256(module.binary).hexdigest(),
            "passed_cases": len(results), "cases": results,
            "runtime_qualified": False,
            "limits": "Emulated read/ioctl logic only; probe/open/removal/PM, scheduling and physical DMA remain unverified"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("module", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.module), indent=2))


if __name__ == "__main__":
    main()
