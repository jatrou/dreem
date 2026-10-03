#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Exercise the integrated imx-sdma ARM object with synthetic kernel services.

No firmware is installed or executed on hardware. MMIO and DMA completion are
models; a passing result does not prove peripheral timing or concurrency.
"""
import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import struct

from unicorn import (Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_READ,
                     UC_HOOK_MEM_WRITE, UC_PROT_READ, UC_PROT_WRITE, UC_PROT_EXEC)
from unicorn.arm_const import (UC_CPU_ARM_CORTEX_A7, UC_ARM_REG_R0, UC_ARM_REG_R1,
                               UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_SP,
                               UC_ARM_REG_LR, UC_ARM_REG_PC)
from verify_adc_module import Module, signed, require
from arm_relocations import relocate_mov
from sdma_disassemble import decode
from sdma_program_model import Machine as ProgramMachine
from sdma_assemble import assemble

ENGINE, DEVICE, CCB, CONTEXT, BD, DRVDATA = (0x30000000, 0x30004000, 0x30005000,
                                           0x30006000, 0x30006800, 0x30006A00)
FW, FW_DATA, USER = 0x30006C00, 0x30008000, 0x3000C000
IPG, AHB, STACK, STOP, REGS = 0x30020000, 0x30020100, 0x10000000, 0x210FF000, 0x020EC000
DATA_SYMBOLS = {"arm_dma_ops", "arm_delay_ops", "outer_cache", "param_ops_bool", "kmalloc_caches"}


class Machine:
    def __init__(self, module):
        self.module = module
        self.uc = cpu = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_A7)
        self.symbols, self.stubs, sections = {}, {}, {}
        cursor, stub = 0x20000000, 0x21000000
        for index, section in enumerate(module.elf.iter_sections()):
            if not section["sh_flags"] & 2 or not section["sh_size"]:
                continue
            size = (section["sh_size"] + 4095) & ~4095
            require(size <= 1024 * 1024, "oversized section")
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
        data, resolved = 0x22000000, {}
        for index, symbol in enumerate(module.elf.get_section_by_name(".symtab").iter_symbols()):
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
                if kind == 2:
                    word = (word + symbol) & 0xFFFFFFFF
                elif kind in (28, 29):
                    offset = symbol + (signed(word & 0xFFFFFF, 24) << 2) - place
                    require(not offset & 3 and -(1 << 25) <= offset < (1 << 25), "branch relocation overflow")
                    word = (word & 0xFF000000) | ((offset >> 2) & 0xFFFFFF)
                elif kind == 42:
                    word = (word & 0x80000000) | ((symbol + signed(word & 0x7FFFFFFF, 31) - place) & 0x7FFFFFFF)
                elif kind in (43, 44):
                    word = relocate_mov(word, symbol, kind == 44)
                else:
                    raise ValueError("unsupported relocation " + str(kind))
                self.put(place, word)
        for address, size in ((ENGINE, 0x40000), (STACK, 0x10000), (REGS, 4096)):
            cpu.mem_map(address, size, UC_PROT_READ | UC_PROT_WRITE)
        self.calls, self.writes, self.descriptors, self.notifications = [], [], [], []
        self.allocations, self.clocks = {}, {IPG: 0, AHB: 0}
        self.next_allocation, self.device_refs, self.locked, self.preempt = 0x30010000, 0, False, 0
        self.failure, self.sleeps, self.command_count = None, 0, 0
        self.program_code, self.program = None, None
        self.registers = {4: 0, 0x1C: 0, 0x38: 0, 0x20C: 0x20, 0x108: 5}
        self.eeg = ENGINE + module.members["sdma_engine"]["eeg"]
        self.field("device", DEVICE, "driver_data", ENGINE)
        for name, value in (("dev", DEVICE), ("regs", REGS), ("channel_control", CCB),
                            ("context", CONTEXT), ("context_phys", 0x40006000),
                            ("bd0", BD), ("drvdata", DRVDATA), ("clk_ipg", IPG), ("clk_ahb", AHB),
                            ("script_addrs", 0x30007000)):
            self.field("sdma_engine", ENGINE, name, value)
        self.field("sdma_driver_data", DRVDATA, "chnenbl0", 0x200)
        self.field("sdma_driver_data", DRVDATA, "num_events", 48)
        for index in range(32):
            channel = ENGINE + module.members["sdma_engine"]["channel"] + index * module.sizes["sdma_channel"]
            self.field("sdma_channel", channel, "sdma", ENGINE)
            self.field("sdma_channel", channel, "channel", index)
        self.ef("enabled", 1, 1)
        self.ef("irq", 42)
        self.ef("pc", 0x1800)
        self.ef("firmware_done", 1, 1)
        self.put(self.symbols["dreem_sdma_owner"], ENGINE)
        cpu.mem_write(USER, b"1\0" + bytes(2046))
        for name in ("dma_allocate", "dma_free", "delay"):
            self.stubs[stub] = name
            self.symbols[name] = stub
            stub += 16
        self.put(self.symbols["arm_dma_ops"], self.symbols["dma_allocate"])
        self.put(self.symbols["arm_dma_ops"] + 4, self.symbols["dma_free"])
        for offset in (0, 4, 8):
            self.put(self.symbols["arm_delay_ops"] + offset, self.symbols["delay"])
        cpu.reg_write(UC_ARM_REG_SP, STACK + 0xFFF0)
        cpu.hook_add(UC_HOOK_CODE, self.code)
        cpu.hook_add(UC_HOOK_MEM_READ, self.read_mmio, begin=REGS, end=REGS + 4095)
        cpu.hook_add(UC_HOOK_MEM_WRITE, self.write_mmio, begin=REGS, end=REGS + 4095)

    def put(self, address, value):
        self.uc.mem_write(address, struct.pack("<I", value & 0xFFFFFFFF))

    def u32(self, address):
        return struct.unpack("<I", self.uc.mem_read(address, 4))[0]

    def field(self, kind, address, name, value=None, width=4):
        address += self.module.members[kind][name]
        if value is None:
            return int.from_bytes(self.uc.mem_read(address, width), "little")
        self.uc.mem_write(address, (value & ((1 << (8 * width)) - 1)).to_bytes(width, "little"))

    def ef(self, name, value=None, width=4):
        return self.field("dreem_sdma", self.eeg, name, value, width)

    def code(self, cpu, address, size, _):
        if address not in self.stubs:
            require(0x20000000 <= address < 0x21000000, "execution left object")
            return
        name = self.stubs[address]
        a, b, c, d = (cpu.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3))
        result = 0
        self.calls.append(name)
        if name == "dma_allocate":
            require(a in (0, DEVICE) and 0 < b <= 4096, "unexpected DMA allocation")
            if self.failure == ("allocation", b):
                result = 0
            else:
                result = self.next_allocation
                self.next_allocation += 4096
                self.allocations[result] = (a, b, result + 0x10000000)
                self.put(c, result + 0x10000000)
                cpu.mem_write(result, bytes([0xCC]) * b)
        elif name == "dma_free":
            require(self.allocations.get(c) == (a, b, d), "freeing wrong DMA allocation")
            del self.allocations[c]
        elif name == "gen_pool_dma_alloc":
            result = 0
        elif name == "__memzero":
            cpu.mem_write(a, bytes(b))
        elif name == "memcpy":
            cpu.mem_write(a, bytes(cpu.mem_read(b, c)))
            result = a
        elif name == "clk_enable":
            require(a in self.clocks, "wrong clock")
            result = -5 if self.failure == ("clock", a) else 0
            if not result:
                self.clocks[a] += 1
        elif name == "clk_disable":
            require(a in self.clocks and self.clocks[a] > 0, "unbalanced clock release")
            self.clocks[a] -= 1
        elif name == "get_device":
            require(a == DEVICE, "wrong device reference")
            self.device_refs += 1
            result = a
        elif name == "mutex_lock":
            require(not self.locked and a == self.eeg + self.module.members["dreem_sdma"]["lock"], "wrong/nested mutex")
            self.locked = True
        elif name == "mutex_unlock":
            require(self.locked, "unbalanced unlock")
            self.locked = False
        elif name in ("preempt_count_add", "preempt_count_sub"):
            self.preempt += a if name.endswith("add") else -a
            require(self.preempt >= 0, "preemption accounting underflow")
        elif name in ("preempt_schedule", "delay", "dev_err", "dev_warn", "_dev_info"):
            pass
        elif name == "usleep_range":
            require((a, b) == (100, 200), "unexpected readiness wait")
            self.sleeps += 1
            if self.ef("managed", width=1):
                self.pump()
            elif self.failure != "initialization" and self.sleeps >= 2:
                self.registers[0x1C] &= ~2  # done 4: clear EP, no interrupt.
                if self.failure == "startup_fault":
                    self.ef("error", -75)
                    self.put(self.symbols["dreem_sdma_error"], -75)
        elif name == "up":
            require(a == self.symbols["ads_data_sem"], "wrong semaphore")
            self.notifications.append(self.u32(self.symbols["sdma_queue_head"]))
        elif name == "synchronize_irq":
            require(a == 42, "wrong IRQ synchronization")
        elif name in ("_kstrtoul", "kstrtouint"):
            data = bytes(cpu.mem_read(a, 32)).split(b"\0", 1)[0].strip()
            try:
                value = int(data, 0)
                require(0 <= value <= 0xFFFFFFFF, "integer range")
                self.put(c, value)
            except (ValueError, UnicodeError):
                result = -22
        elif name == "release_firmware":
            require(a == FW, "wrong firmware release")
        elif name == "request_firmware_direct":
            result = -2 if self.failure != "firmware_io" else -5
        elif name in ("sysfs_create_group", "sysfs_remove_group"):
            result = -12 if self.failure == "sysfs" else 0
        else:
            raise ValueError("unmodeled kernel call: " + name)
        cpu.reg_write(UC_ARM_REG_R0, result & 0xFFFFFFFF)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def read_mmio(self, cpu, access, address, size, value, _):
        require(size == 4 and all(n > 0 for n in self.clocks.values()), "MMIO read without clocks")
        if address == REGS + 0x1C and self.ef("managed", width=1):
            self.pump()
        self.put(address, self.registers.get(address - REGS, 0))

    def write_mmio(self, cpu, access, address, size, value, _):
        require(size == 4 and all(n > 0 for n in self.clocks.values()), "MMIO write without clocks")
        offset = address - REGS
        self.writes.append((offset, value))
        if offset == 4:
            self.registers[4] &= ~value
        elif offset == 0x1C:
            self.registers[0x1C] |= value
        elif offset == 0xC:
            if value == 1:
                descriptor = bytes(cpu.mem_read(BD, 12))
                self.descriptors.append(descriptor)
                self.command_count += 1
                mode, source, destination = struct.unpack("<3I", descriptor)
                if mode >> 24 == 4:
                    self.program_code = bytes(cpu.mem_read(source - 0x10000000, (mode & 0xFFFF) * 2))
                if self.failure != "channel0":
                    self.registers[4] |= 1
            else:
                require(value == 2, "unexpected channel start")
                require(self.ef("armed", width=1), "started before IRQ state was armed")
                if self.ef("managed", width=1):
                    if self.program is None:
                        context = struct.unpack("<32I", cpu.mem_read(CONTEXT, 128))
                        require(self.program_code == assemble(Path(__file__).with_name("sdma_acquire.asm").read_text())[0],
                                "managed loader uploaded different program bytes")
                        self.program = ProgramMachine(self.program_code, origin=context[0],
                                                      ring_base=context[2], control_base=context[4])
                        self.program.r = list(context[2:10])
                else:
                    require(self.u32(self.symbols["sdma_ads_user_buffer"]) == 0, "published before initialization")
        elif offset != 8:
            self.registers[offset] = value

    def pump(self, steps=128):
        if self.program is None or self.failure == "initialization":
            return
        if self.failure in ("stale_ack", "ack_with_ep", "ep_without_ack"):
            if self.failure == "stale_ack":
                self.put(self.ef("counter") + 8, self.ef("request") - 2)
                self.registers[0x1C] &= ~2
            elif self.failure == "ack_with_ep":
                self.put(self.ef("counter") + 8, self.ef("request"))
            else:
                self.registers[0x1C] &= ~2
            return
        ring, control = self.ef("ring"), self.ef("counter")
        self.program.mem[:] = bytes(self.uc.mem_read(ring, 1024)) + bytes(self.uc.mem_read(control, 64))
        self.program.ep = bool(self.registers[0x1C] & 2)
        notifications = len(self.program.irqs)
        for _ in range(steps):
            if not self.program.step():
                break
        self.uc.mem_write(ring, bytes(self.program.mem[:1024]))
        self.uc.mem_write(control, bytes(self.program.mem[1024:]))
        self.registers[0x1C] = (self.registers[0x1C] & ~2) | (2 if self.program.ep else 0)
        if len(self.program.irqs) != notifications:
            self.registers[4] |= 2

    def call(self, name, *args):
        for register, value in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3), args):
            self.uc.reg_write(register, value)
        self.uc.reg_write(UC_ARM_REG_LR, STOP)
        self.uc.emu_start(self.symbols[name], STOP, count=500000, timeout=2000000)
        require(self.uc.reg_read(UC_ARM_REG_PC) == STOP and not self.locked and not self.preempt,
                "call did not return with locks released")
        return ctypes.c_int32(self.uc.reg_read(UC_ARM_REG_R0)).value

    def script(self, count=106):
        return self.call("dreem_user_script_store", DEVICE, 0, USER, count)

    def start(self, managed=False):
        if managed:
            self.uc.mem_write(USER, b"2\0")
        return self.call("dreem_trigger_store", DEVICE, 0, USER, 1)

    def interrupt(self, count, mask=2):
        self.put(self.ef("counter"), count)
        self.registers[4] = mask
        return self.call("sdma_int_handler", 42, ENGINE)

    def firmware(self, **changes):
        header = dict(magic=0x414D4453, major=4, minor=1, table=28, scripts=42,
                      code=200, size=128)
        header.update(changes)
        data = bytearray(512)
        struct.pack_into("<7I", data, 0, *[header[n] for n in
                         ("magic", "major", "minor", "table", "scripts", "code", "size")])
        struct.pack_into("<i", data, 28 + 136, changes.get("pc", 0x1800))
        self.uc.mem_write(FW_DATA, bytes(data))
        self.field("firmware", FW, "data", FW_DATA)
        self.field("firmware", FW, "size", changes.get("file_size", len(data)))
        return self.call("sdma_load_firmware", FW, ENGINE)


def verify_managed(module):
    results = []
    def ready():
        m = Machine(module)
        require(m.start(managed=True) == 1, "managed startup failed")
        require(m.ef("paused", width=1) and m.ef("request") == 2 and
                m.u32(m.ef("counter") + 8) == 2 and m.registers[0x104] == 0 and
                not m.notifications and not m.program.tx, "startup did not establish idle ownership")
        require(sorted(size for _, size, _ in m.allocations.values()) == [64, 1024],
                "managed buffers have wrong sizes")
        return m

    m = ready()
    require(m.call("dreem_sdma_control", 2) == -1, "unclaimed start permitted")
    require(m.call("dreem_sdma_control", 0) == 0 and m.call("dreem_sdma_control", 0) == -16,
            "consumer claim is not exclusive")
    require(m.call("dreem_sdma_control", 99) == -22, "invalid command accepted")
    require(m.call("dreem_sdma_control", 2) == 0 and m.call("dreem_sdma_control", 2) == -16,
            "duplicate resume permitted")
    require(m.registers[0x20C] == 0x22, "resume changed another event consumer")
    m.program.frames.append((1, 2, 3, 4))
    m.pump()
    require(m.call("sdma_int_handler", 42, ENGINE) == 1 and m.notifications == [1],
            "first managed frame was suppressed")
    require(m.call("dreem_sdma_control", 1) == 0 and m.ef("request") == 4 and
            m.registers[0x20C] == 0x20, "pause failed or changed another event consumer")
    sleeps = m.sleeps
    require(m.call("dreem_sdma_control", 1) == 0 and m.sleeps == sleeps,
            "already paused consumer unexpectedly reawakened DMA")
    require(m.call("dreem_sdma_control", 3) == 0 and not m.ef("claimed", width=1), "release failed")
    require(m.call("dreem_sdma_control", 0) == 0 and m.call("dreem_sdma_control", 2) == 0,
            "reacquire/resume failed")
    m.program.frames.append((5, 6, 7, 8))
    m.pump()
    # Do not dispatch the pending IRQ: PAUSE must account for it itself.
    require(m.call("dreem_sdma_control", 1) == 0 and m.notifications == [1, 2] and
            m.u32(m.ef("counter")) == 2, "pause lost a pending frame or reset the producer")
    require(bytes(m.uc.mem_read(m.ef("ring"), 32)) == struct.pack("<8I", *range(1, 9)),
            "resume overwrote the previous ring slot")
    results.extend(("managed source bytes and padded control context", "idle startup without SPI access",
                    "exclusive consumer claim", "unclaimed/duplicate/unknown control rejection",
                    "first managed frame delivered", "event updates preserve other channels",
                    "idempotent pause", "release and reacquire", "pending IRQ accounted at pause",
                    "resume preserves producer and ring offset"))

    for failure in (("allocation", 212), ("allocation", 64), ("allocation", 1024),
                    ("clock", IPG), ("clock", AHB), "channel0", "initialization"):
        m = Machine(module)
        m.failure = failure
        require(m.start(managed=True) < 0 and not m.u32(m.symbols["sdma_ads_user_buffer"]),
                "failed managed startup published memory")
        if failure == "initialization":
            require(m.sleeps == 1000 and len(m.allocations) == 2 and m.device_refs == 1,
                    "managed initialization timeout reclaimed state or exceeded bound")
        elif failure != "channel0":
            require(not m.allocations and not any(m.clocks.values()), "pre-DMA startup failure leaked")
        results.append("managed startup failure " + str(failure))

    for failure in ("initialization", "stale_ack", "ack_with_ep", "ep_without_ack"):
        m = ready()
        require(m.call("dreem_sdma_control", 0) == 0 and m.call("dreem_sdma_control", 2) == 0,
                "timeout fixture failed")
        old = dict(m.allocations)
        m.failure = failure
        sleeps = m.sleeps
        require(m.call("dreem_sdma_control", 1) == -110 and m.sleeps - sleeps == 1000,
                "pause accepted incomplete/stale proof: " + failure)
        require(m.allocations == old and all(n == 1 for n in m.clocks.values()) and
                not m.ef("paused", width=1) and m.ef("claimed", width=1),
                "uncertain stop released DMA resources")
        require(m.call("dreem_sdma_control", 3) == -110 and m.call("dreem_sdma_control", 2) == -110,
                "timeout allowed release/resume")
        results.append("pause requires fresh ACK and EP clear: " + failure)

    for fault_word in (0, 1):
        m = ready()
        m.call("dreem_sdma_control", 0)
        m.call("dreem_sdma_control", 2)
        m.put(m.ef("counter") + 12, fault_word)
        m.registers[0x1C] &= ~2
        m.registers[4] = 2
        require(m.call("sdma_int_handler", 42, ENGINE) == 1 and m.call("dreem_sdma_status") == -5,
                "unexpected managed halt did not latch an error")
        require(m.call("dreem_sdma_control", 1) == -5, "faulted DMA falsely acknowledged pause")
        results.append("fault notification with readable fault word=" + str(fault_word))

    m = ready()
    m.call("dreem_sdma_control", 0)
    m.ef("request", 0xFFFFFFFE)
    require(m.call("dreem_sdma_control", 2) == -75 and m.ef("paused", width=1),
            "generation exhaustion wrapped or activated DMA")
    results.append("generation exhaustion stays paused")
    m = Machine(module)
    require(m.script() == 106 and m.start() == 1 and m.call("dreem_sdma_control", 0) == -95,
            "managed consumer accepted the legacy stop protocol")
    results.append("legacy script rejected by managed consumer API")
    return results


def verify(path, script_path):
    script = script_path.read_bytes()
    require(hashlib.sha256(script).hexdigest() ==
            "8718f1d8aef068043ddcf82b2237674ccdb769eaec363ae38c8f1305ec5bfc3f", "wrong DMA script")
    # Evaluate only the arithmetic prefix of the private input. No vendor
    # instruction bytes are retained here. DONE 4 clears EP, whereas DONE 3
    # raises HI (NXP RM 46.5.2.21).
    registers = [0x40010000, 1024, 0x40011000, 0, 0, 0, 0, 0]
    for pc in range(32):
        instruction = decode(struct.unpack_from("<H", script, pc * 2)[0], pc)
        if instruction.name == "done":
            require(instruction.operands == "4" and registers[3:7] == [0x02008000, 1008, 0, 0],
                    "initialization protocol changed")
            break
        destination, source = instruction.operands.split(", ")
        index = int(destination[1:])
        if instruction.name == "ldi":
            registers[index] = int(source)
        elif instruction.name == "bseti":
            registers[index] |= 1 << int(source)
        elif instruction.name == "mov":
            registers[index] = registers[int(source[1:])]
        elif instruction.name == "subi":
            registers[index] = (registers[index] - int(source)) & 0xFFFFFFFF
        else:
            raise ValueError("unexpected pre-initialization instruction")
    else:
        raise ValueError("initialization does not yield within the bound")
    module, results = Module(path), ["stock script initialization clears EP without an IRQ"]
    for count in (0, 1, 3, 1025, 1026, 4096):
        m = Machine(module)
        require(m.script(count) == -22 and not m.writes and not m.allocations, "invalid script touched DMA")
        results.append("invalid script size " + str(count))
    for name, value, width, expected in (("firmware_done", 0, 1, -11), ("pc", 0x17FF, 4, -28),
                                        ("pc", 0x2000, 4, -28), ("pc", 0x1FD0, 4, -28),
                                        ("armed", 1, 1, -16), ("removed", 1, 1, -16),
                                        ("error", -5, 4, -5)):
        m = Machine(module)
        m.ef(name, value, width)
        require(m.script() == expected and not m.writes, "script gate failed: " + name)
        results.append("script gate " + name + "=" + str(value))
    for failure in (("allocation", 106), ("clock", IPG), ("clock", AHB), "channel0"):
        m = Machine(module)
        m.failure = failure
        if failure == "channel0":
            m.registers[4] = 1  # Stale completion cannot acknowledge the new upload.
        require(m.script() < 0 and not m.ef("script_loaded", width=1), "failed upload was accepted")
        if failure == "channel0":
            require(len(m.allocations) == 1 and all(v == 1 for v in m.clocks.values()), "in-flight script storage reclaimed")
            calls = len(m.calls)
            require(m.script() == -110 and m.calls[calls:] == ["mutex_lock", "mutex_unlock"], "poisoned loader retried hardware")
        else:
            require(not m.allocations and not any(m.clocks.values()), "upload failure leaked resources")
        results.append("upload failure " + str(failure))
    for failure in (("allocation", 1024), ("allocation", 4), ("clock", IPG), ("clock", AHB),
                    "channel0", "initialization", "startup_fault"):
        m = Machine(module)
        require(m.script() == 106, "fixture upload failed")
        m.failure = failure
        require(m.start() < 0 and m.u32(m.symbols["sdma_ads_user_buffer"]) == 0, "failed start published a buffer")
        if failure in ("channel0", "initialization", "startup_fault"):
            require(len(m.allocations) == 2 and m.device_refs == 1 and
                    all(v == 1 for v in m.clocks.values()), "possibly active DMA allocation reclaimed")
            allocations = dict(m.allocations)
            require(m.start() == (-75 if failure == "startup_fault" else -110) and
                    m.allocations == allocations, "poisoned start retried")
            if failure == "initialization":
                require(m.sleeps == 1000, "readiness timeout not bounded")
        else:
            require(not m.allocations and not any(m.clocks.values()) and m.device_refs == 0, "pre-DMA failure leaked resources")
            m.failure = None
            require(m.start() == 1, "allocation/clock failure was not retryable")
        results.append("start failure " + str(failure))
    for pc, count in ((0x1800, 2), (0x1FFF, 2), (0x1FCB, 106), (0x1E00, 1024)):
        m = Machine(module)
        m.ef("pc", pc)
        require(m.script(count) == count, "valid boundary script rejected")
        control, _, destination = struct.unpack("<3I", m.descriptors[0])
        require(control == 0x048B0000 + count // 2 and destination == pc,
                "script command has wrong size/address")
        results.append("valid script boundary " + str((pc, count)))
    m = Machine(module)
    require(m.start() == -61 and not m.allocations, "start without script accepted")
    results.append("start requires a successful script upload")
    m.uc.mem_write(USER, b"0\0")
    require(m.start() == -22 and not m.allocations, "invalid trigger accepted")
    results.append("trigger requires value one")
    m = Machine(module)
    for index in range(8):
        m.put(m.eeg + module.members["dreem_sdma"]["registers"] + index * 4, 0xA000 + index)
    require(m.script() == 106 and not m.allocations and not any(m.clocks.values()), "successful upload leaked resources")
    require(m.start() == 1 and m.sleeps == 2, "startup incorrectly requires an IRQ")
    expected = [0x1800, 0, m.ef("ring_phys"), 1024, m.ef("counter_phys"),
                *range(0xA003, 0xA008), *([0] * 22)]
    require(list(struct.unpack("<32I", m.uc.mem_read(CONTEXT, 128))) == expected, "context differs")
    require(struct.unpack("<3I", m.descriptors[-1]) == (0x018B0020, 0x40006000, 0x820), "context descriptor differs")
    require(m.call("dreem_sdma_status") == 0 and not m.notifications, "wrong successful publication")
    require(m.start() == -16 and m.script() == -16, "running state accepted reconfiguration")
    results.extend(("context and descriptor match recovered format", "EP initialization without IRQ",
                    "success-only buffer publication", "running reconfiguration rejected"))
    m.interrupt(1)
    require(not m.notifications, "first interrupt published a frame")
    m.interrupt(2, 7)
    require(m.notifications == [1, 2] and m.registers[4] == 1, "mixed IRQ dispatch or channel-0 preservation differs")
    m.interrupt(2)
    require(m.notifications == [1, 2], "duplicate counter produced frames")
    m.interrupt(67)
    require(m.call("dreem_sdma_status") == -75 and len(m.notifications) == 3 and
            m.registers[0x104] == 0 and m.registers[0x20C] == 0x20 and m.registers[0x108] == 5,
            "overflow did not stop only the EEG channel")
    m.interrupt(68)
    require(len(m.notifications) == 3, "fault did not latch")
    results.extend(("first IRQ preserves stock suppression", "mixed EEG and ordinary DMA IRQ",
                    "duplicate counter", "overflow stops EEG and wakes reader", "latched overflow"))
    require(m.call("sdma_suspend", DEVICE) == -16, "research provider permitted unsupported suspend")
    results.append("enabled research provider refuses system sleep")
    m = Machine(module)
    require(m.script() == 106 and m.start() == 1, "wrap fixture failed")
    progress = m.eeg + module.members["dreem_sdma"]["progress"]
    m.field("sdma_eeg_progress", progress, "initialized", 1)
    m.field("sdma_eeg_progress", progress, "counter", 0xFFFFFFFF)
    m.field("sdma_eeg_progress", progress, "head", 63)
    m.interrupt(0)
    require(m.notifications == [0] and m.call("dreem_sdma_status") == 0, "normal counter wrap failed")
    results.append("32-bit counter and ring index wrap")
    m = Machine(module)
    require(m.call("sdma_get_firmware", ENGINE, 0) == 0 and m.ef("pc") == 0x1800,
            "ROM-only placement failed")
    require(not m.allocations and not m.writes, "ROM fallback unexpectedly loaded DMA")
    results.append("missing firmware selects program RAM after contexts")
    for failure, expected in ((None, 0), ("firmware_io", -5)):
        m = Machine(module)
        m.failure = failure
        require(m.call("sdma_get_firmware", ENGINE, USER) == expected and
                "request_firmware_nowait" not in m.calls and not m.writes,
                "firmware lookup ignored an I/O failure or used asynchronous loading")
        results.append("synchronous firmware lookup " + str(failure))
    for changes in ({"file_size": 12}, {"magic": 0}, {"major": 9}, {"table": 0xFFFFFFFC},
                    {"table": 29}, {"scripts": 1}, {"code": 0xFFFFFFFE}, {"code": 194},
                    {"size": 0}, {"size": 127}, {"size": 0xFFFFFFFE}, {"pc": 0x17FF},
                    {"pc": 0x2000}, {"pc": 0x1FFF}):
        m = Machine(module)
        m.firmware(**changes)
        require(ctypes.c_int32(m.ef("error")).value == -22 and not m.writes and not m.allocations,
                "malformed external firmware touched DMA: " + str(changes))
        require(m.calls.count("release_firmware") == 1, "firmware not released exactly once")
        results.append("firmware rejection " + str(changes))
    for confirmed in (False, True):
        m = Machine(module)
        m.uc.mem_write(m.symbols["dreem_ram_tail_confirmed"], bytes([confirmed]))
        m.firmware()
        require(ctypes.c_int32(m.ef("error")).value == (0 if confirmed else -95) and
                m.ef("pc") == 0x1840 and not m.allocations and not any(m.clocks.values()),
                "external RAM ownership gate failed")
        results.append("external firmware tail ownership " + str(confirmed))
    for failure in (("clock", IPG), ("allocation", 128), "channel0"):
        m = Machine(module)
        m.failure = failure
        m.uc.mem_write(m.symbols["dreem_ram_tail_confirmed"], b"\1")
        m.firmware()
        require(ctypes.c_int32(m.ef("error")).value < 0 and
                m.field("sdma_engine", ENGINE, "script_number") == 0,
                "failed base firmware published its address table")
        require(m.calls.count("release_firmware") == 1, "failed base firmware not released")
        results.append("base firmware upload failure " + str(failure))
    results.extend(verify_managed(module))
    return {"provider_object_sha256": hashlib.sha256(module.binary).hexdigest(),
            "passed_cases": len(results), "cases": results, "runtime_qualified": False,
            "limits": "MMIO, coherent DMA, IRQ dispatch and kernel calls are models; no scheduler races or physical SDMA execution are verified"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider_object", type=Path)
    parser.add_argument("private_ads_script", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.provider_object, args.private_ads_script), indent=2))


if __name__ == "__main__":
    main()
