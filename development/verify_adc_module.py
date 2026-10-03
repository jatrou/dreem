#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Exercise the built research module's ARM interfaces in an emulator.

All kernel calls, MMIO, task memory, and samples are synthetic. No module is
loaded into Linux. Requires the debug ELF produced by build_adc_module.py.
Models kernel resource and PM calls; does not validate real scheduling,
controller power transitions, DMA concurrency, or physical hardware.
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
from verify_adc_test_signal import TestSignalTransport

ADC, FILE, RING, USER = 0x30000000, 0x30001000, 0x30005000, 0x30008004
SPI_DEVICE, SPI_MASTER, PARENT, NODE = (ADC + n for n in (0x3000, 0x4000, 0x6000, 0x7000))
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
        self.members, self.sizes = {}, {}
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
                    name = die.attributes["DW_AT_name"].value.decode()
                    self.members[name] = members
                    self.sizes[name] = die.attributes["DW_AT_byte_size"].value


class Machine:
    def __init__(self, module, pending=1, nonblock=False):
        self.module = module
        self.model = AcquisitionTransport(head=7, pending=pending)
        self.calls, self.copy_failure, self.lock_failure = [], False, False
        self.cancel_on_wait, self.signal_on_wait = False, False
        self.locked, self.spi_locked = False, False
        self.failure = None
        self.provider_error = 0
        self.provider_fault_on_wait = False
        self.dma_claimed, self.dma_paused = True, False
        self.control_failure, self.run_fault = None, False
        self.control_calls = []
        self.provider_callback = None
        self.compatible, self.resource_start = True, SPI
        self.allocated, self.registered, self.tracking = False, False, False
        self.gpios, self.mappings, self.device_refs = set(), set(), 0
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
        self.field("dreem_adc", ADC, "spi", SPI_DEVICE)
        self.field("spi_device", SPI_DEVICE, "master", SPI_MASTER)
        master_device = SPI_MASTER + module.members["spi_master"]["dev"]
        self.field("device", master_device, "parent", PARENT)
        self.field("device", PARENT, "of_node", NODE)
        self.field("spi_master", SPI_MASTER, "num_chipselect", 1, width=2)
        self.power = PARENT + module.members["device"]["power"]
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
        self.field("kref", ADC + module.members["dreem_adc"]["ref"], "refcount", 2)
        self.managed = "dreem_sdma_control" in self.symbols
        if self.managed:
            self.field("dreem_adc", ADC, "dma_claimed", 1, width=1)
        thread = STACK + 0xE000
        self.field("thread_info", thread, "addr_limit", 0x7FFFFFFF)
        self.field("thread_info", thread, "task", TASK)
        self.field("task_struct", TASK, "stack", thread)
        cpu.reg_write(UC_ARM_REG_SP, STACK + 0xFFF0)
        cpu.hook_add(UC_HOOK_CODE, self.code)
        for start, end in ((SPI, SPI + 24), (CLOCK, CLOCK + 3), (EVENT, EVENT + 3)):
            cpu.hook_add(UC_HOOK_MEM_READ, self.read_mmio, begin=start, end=end)
            cpu.hook_add(UC_HOOK_MEM_WRITE, self.write_mmio, begin=start, end=end)
        cpu.hook_add(UC_HOOK_MEM_WRITE, self.ring_write, begin=RING, end=RING + 1023)

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
        if name in ("dreem_sdma_status", "dreem_sdma_control") and self.provider_callback:
            result = self.provider_callback(name, a)
        elif name == "dreem_sdma_status":
            result = self.provider_error
        elif name == "dreem_sdma_control":
            self.control_calls.append(a)
            require(not self.spi_locked, "DMA handoff attempted while SPI bus locked")
            if self.control_failure == a:
                result = -110 if a == 1 else -95
            elif a == 0:
                result = self.provider_error or (-16 if self.dma_claimed else 0)
                if not result:
                    self.dma_claimed, self.dma_paused = True, True
            elif not self.dma_claimed:
                result = -1
            elif a in (1, 3):
                result = self.provider_error if not self.dma_paused else 0
                if not result:
                    self.dma_paused = True
                    self.model.registers[EVENT] = 0
                    if a == 3:
                        self.dma_claimed = False
            elif a == 2:
                require(self.dma_paused, "resumed an unpaused provider")
                self.dma_paused = False
                self.model.registers[EVENT] = 2
                if self.run_fault:
                    result = self.provider_error = -5
            else:
                raise ValueError("unexpected provider command")
        elif name == "of_machine_is_compatible":
            compatible = b"fsl,imx6ull-femto\0"
            require(bytes(cpu.mem_read(a, len(compatible))) == compatible, "wrong machine gate")
            result = int(self.compatible)
        elif name == "of_address_to_resource":
            require(a == NODE and b == 0, "wrong controller resource lookup")
            self.field("resource", c, "start", self.resource_start)
            result = -22 if self.failure == name else 0
        elif name == "kmem_cache_alloc":
            if self.failure == name:
                result = 0
            else:
                require(not self.allocated, "duplicate instance allocation")
                self.allocated = True
                cpu.mem_write(ADC, bytes(self.module.sizes["dreem_adc"]))
                result = ADC
        elif name == "kfree":
            require(a == ADC and self.allocated, "invalid instance free")
            require(not self.gpios and not self.mappings and not self.device_refs and not self.registered,
                    "freed instance with outstanding resources")
            require(self.field("dev_pm_info", self.power, "usage_count") == 0,
                    "freed instance with outstanding PM reference")
            self.allocated = False
        elif name in ("get_device", "put_device"):
            require(a == SPI_DEVICE + self.module.members["spi_device"]["dev"], "wrong device reference")
            self.device_refs += 1 if name == "get_device" else -1
            require(self.device_refs >= 0, "unbalanced device reference")
            result = a
        elif name == "__mutex_init":
            require(a == ADC + self.module.members["dreem_adc"]["lock"], "wrong mutex initialization")
        elif name == "gpio_request":
            require(a in (35, 90, 34) and a not in self.gpios, "invalid GPIO claim")
            if self.failure == (name, a):
                result = -16
            else:
                self.gpios.add(a)
        elif name == "gpio_free":
            require(a in self.gpios, "freeing unowned GPIO")
            self.gpios.remove(a)
        elif name == "__arm_ioremap":
            require((a, b) in ((SPI, 32), (CLOCK, 4), (EVENT, 4)), "invalid MMIO map")
            require(a not in self.mappings, "duplicate MMIO map")
            if self.failure == (name, a):
                result = 0
            else:
                self.mappings.add(a)
                result = a
        elif name == "__arm_iounmap":
            require(a in self.mappings, "unmapping unowned MMIO")
            self.mappings.remove(a)
        elif name in ("misc_register", "misc_deregister"):
            require(a == ADC + self.module.members["dreem_adc"]["misc"], "wrong misc device")
            if name == "misc_register":
                require(not self.registered, "duplicate registration")
                result = -16 if self.failure == name else 0
                self.registered = result == 0
            else:
                require(self.registered, "deregistering absent device")
                self.registered = False
        elif name in ("__pm_runtime_resume", "__pm_runtime_suspend"):
            require(a == PARENT, "wrong PM controller")
            expected_flags = 4 if name == "__pm_runtime_resume" else 13
            require(b == expected_flags, "wrong runtime PM flags")
            usage = self.field("dev_pm_info", self.power, "usage_count")
            usage += 1 if name == "__pm_runtime_resume" else -1
            require(usage >= 0, "unbalanced PM reference")
            self.field("dev_pm_info", self.power, "usage_count", usage)
            result = -5 if self.failure == name else 0
        elif name in ("mutex_lock", "mutex_lock_interruptible"):
            require(a == ADC + self.module.members["dreem_adc"]["lock"], "wrong operation mutex")
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
            require(a == SPI_MASTER, "wrong SPI controller")
            if self.spi_locked:
                raise ValueError("nested SPI bus lock")
            if self.failure == name:
                result = -16
            else:
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
            if self.provider_fault_on_wait:
                self.provider_error = -75
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
        self.check_mmio(address)
        self.put(address, self.model.io(READ, address, 0))

    def write_mmio(self, cpu, access, address, size, value, _):
        if size != 4:
            raise ValueError("invalid MMIO write width")
        self.check_mmio(address)
        self.model.io(WRITE, address, value)

    def check_mmio(self, address):
        if self.managed:
            require(self.dma_paused, "CPU touched SPI before DMA pause acknowledgement")
            require(address != EVENT, "ADC bypassed provider event ownership")
        if self.tracking:
            base = SPI if SPI <= address <= SPI + 24 else address
            require(base in self.mappings, "access to unowned MMIO")
            require(self.field("dev_pm_info", self.power, "usage_count") > 0,
                    "MMIO access without a controller PM reference")

    def ring_write(self, cpu, access, address, size, value, _):
        if self.managed:
            require(self.dma_paused, "CPU reset the ring while DMA was running")

    def prepare_probe(self, enabled=True):
        self.tracking = True
        self.dma_claimed, self.dma_paused = False, True
        self.uc.mem_write(ADC, bytes(self.module.sizes["dreem_adc"]))
        self.uc.mem_write(self.symbols["sdma_hardware_confirmed"], bytes([enabled]))

    def open(self, file=FILE):
        self.field("file", file, "private_data", ADC + self.module.members["dreem_adc"]["misc"])
        return self.call("adc_open", 0, file)

    def references(self):
        ref = ADC + self.module.members["dreem_adc"]["ref"]
        return self.field("kref", ref, "refcount")

    def resource_state(self):
        return (self.allocated, self.registered, len(self.gpios), len(self.mappings),
                self.device_refs, self.field("dev_pm_info", self.power, "usage_count"))

    def require_power_off(self):
        pins = {a: b for op, a, b, result in self.model.trace
                if op in (GPIO_OUTPUT, GPIO_SET) and not result}
        require(pins.get(35) == 0 and pins.get(90) == 1 and self.model.registers.get(EVENT) == 0,
                "shutdown left ADC power, chip select or DMA requests active")

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


def verify_lifecycle(module):
    results = []
    empty = (False, False, 0, 0, 0, 0)
    bound = (True, True, 3, 3, 1, 0)

    def probed():
        machine = Machine(module)
        machine.prepare_probe()
        require(machine.call("adc_probe", SPI_DEVICE) == 0, "probe failed")
        require(machine.resource_state() == bound and machine.references() == 1,
                "probe ownership differs")
        transport = ADC + module.members["dreem_adc"]["transport"]
        machine.field("ads_transport", transport, "poll_limit", 32)
        return machine

    for gate in ("disabled", "machine", "chip select", "controller sharing",
                 "resource error", "controller address", "absent ring"):
        machine = Machine(module)
        machine.prepare_probe(enabled=gate != "disabled")
        if gate == "machine":
            machine.compatible = False
        elif gate == "chip select":
            machine.field("spi_device", SPI_DEVICE, "chip_select", 1, width=1)
        elif gate == "controller sharing":
            machine.field("spi_master", SPI_MASTER, "num_chipselect", 2, width=2)
        elif gate == "resource error":
            machine.failure = "of_address_to_resource"
        elif gate == "controller address":
            machine.resource_start = SPI + 4096
        elif gate == "absent ring":
            machine.put(machine.symbols["sdma_ads_user_buffer"], 0)
        expected = -517 if gate == "absent ring" else -19
        require(machine.call("adc_probe", SPI_DEVICE) == expected, "probe gate failed: " + gate)
        require(machine.resource_state() == empty and not machine.model.trace,
                "rejected probe touched hardware or retained resources")
        results.append("probe gate: " + gate)

    failures = [("kmem_cache_alloc", -12), ("misc_register", -16)]
    failures += [(("gpio_request", pin), -16) for pin in (35, 90, 34)]
    failures += [(("__arm_ioremap", base), -12) for base in (SPI, CLOCK, EVENT)]
    for failure, expected in failures:
        machine = Machine(module)
        machine.prepare_probe()
        machine.failure = failure
        require(machine.call("adc_probe", SPI_DEVICE) == expected, "probe failure code differs")
        require(machine.resource_state() == empty and not machine.model.trace,
                "failed probe leaked resources or touched hardware")
        require(machine.field("device", SPI_DEVICE, "driver_data") == 0,
                "failed probe published driver data")
        results.append("probe cleanup: " + str(failure))

    machine = probed()
    misc = ADC + module.members["dreem_adc"]["misc"]
    require(machine.field("miscdevice", misc, "mode", width=2) == 0o600,
            "device permissions differ")
    require(machine.call("adc_suspend", SPI_DEVICE) == 0, "closed suspend rejected")
    require(machine.call("adc_remove", SPI_DEVICE) == 0 and machine.resource_state() == empty,
            "closed removal leaked resources")
    require(machine.field("device", SPI_DEVICE, "driver_data") == 0,
            "removal left driver data")
    results.append("closed probe/suspend/remove releases every resource")

    for fault, expected in (("PM resume", -5), ("SPI lock", -16), ("power GPIO", -5),
                            ("CS GPIO", -5), ("wrong ADC ID", -16), ("stalled SPI", -110)):
        machine = probed()
        if fault == "PM resume":
            machine.failure = "__pm_runtime_resume"
        elif fault == "SPI lock":
            machine.failure = "spi_bus_lock"
        elif fault in ("power GPIO", "CS GPIO"):
            machine.model.gpio_failure = 35 if fault == "power GPIO" else 90
        elif fault == "wrong ADC ID":
            machine.model.ids = (0,)
        else:
            machine.model.stalled = True
        require(machine.open() == expected, "open error differs: " + fault)
        require(machine.resource_state() == bound and machine.references() == 1,
                "failed open leaked references: " + fault)
        for flag in ("opened", "initialized", "running", "runtime_held"):
            require(machine.field("dreem_adc", ADC, flag, width=1) == 0,
                    "failed open retained state: " + flag)
        require(machine.field("file", FILE, "private_data") == misc,
                "failed open published an instance")
        if fault == "PM resume":
            require(machine.calls.count("__pm_runtime_suspend") == 0,
                    "failed resume used autosuspend instead of put_noidle")
        if fault not in ("PM resume", "SPI lock"):
            machine.require_power_off()
        # Every failure must permit a subsequent ordinary open and close.
        machine.failure, machine.model.gpio_failure = None, 0
        machine.model.ids, machine.model.stalled = (0x90,), False
        require(machine.open() == 0 and machine.references() == 2,
                "retry after failed open failed: " + fault)
        require(machine.call("adc_close", 0, FILE) == 0 and machine.resource_state() == bound,
                "close after retry leaked resources")
        machine.require_power_off()
        require(machine.references() == 1, "close retained an open reference")
        require(machine.call("adc_remove", SPI_DEVICE) == 0 and machine.resource_state() == empty,
                "cleanup after retry leaked resources")
        results.append("open failure, cleanup and retry: " + fault)

    for gate, expected in (("interrupted mutex", -512), ("detached", -19), ("absent ring", -11)):
        machine = probed()
        if gate == "interrupted mutex":
            machine.lock_failure = True
        elif gate == "detached":
            machine.field("dreem_adc", ADC, "detached", 1)
        else:
            machine.put(machine.symbols["sdma_ads_user_buffer"], 0)
        require(machine.open() == expected, "open gate failed: " + gate)
        require(machine.resource_state() == bound and not machine.model.trace,
                "rejected open changed resources or hardware")
        machine.lock_failure = False
        require(machine.call("adc_remove", SPI_DEVICE) == 0 and machine.resource_state() == empty,
                "cleanup after rejected open failed")
        results.append("open gate: " + gate)

    machine = probed()
    require(machine.open() == 0 and machine.references() == 2, "ordinary open failed")
    require(machine.field("file", FILE, "private_data") == ADC, "open did not publish instance")
    require(machine.call("adc_suspend", SPI_DEVICE) == -16, "open suspend accepted")
    before = machine.resource_state(), len(machine.model.trace)
    require(machine.open(FILE + 0x100) == -16, "second open accepted")
    require((machine.resource_state(), len(machine.model.trace)) == before and machine.references() == 2,
            "rejected second open changed state")
    require(machine.call("adc_ioctl", FILE, 1, 0) == 0, "start after open failed")
    require(machine.call("adc_remove", SPI_DEVICE) == 0, "open removal failed")
    require(machine.resource_state() == (True, False, 3, 3, 1, 0) and machine.references() == 1,
            "open removal freed referenced resources or retained PM")
    machine.require_power_off()
    trace_length = len(machine.model.trace)
    for entry, args in (("adc_read", (FILE, USER, 16, 0)),
                        ("adc_ioctl", (FILE, 1, 0)), ("adc_ioctl", (FILE, 4, USER))):
        require(machine.call(entry, *args) == -19, "removed descriptor accepted I/O")
    require(len(machine.model.trace) == trace_length, "removed descriptor touched MMIO")
    require(machine.call("adc_close", 0, FILE) == 0 and machine.resource_state() == empty,
            "final close failed to release deferred resources")
    require(machine.calls.count("__pm_runtime_resume") == 1 and
            machine.calls.count("__pm_runtime_suspend") == 1, "unbalanced removal PM calls")
    results.append("open/start/suspend refusal/duplicate open/remove/final close")

    for stalled in (False, True):
        machine = probed()
        require(machine.open() == 0, "open for close test failed")
        require(machine.call("adc_ioctl", FILE, 1, 0) == 0, "start for close test failed")
        machine.model.stalled = stalled
        require(machine.call("adc_close", 0, FILE) == 0 and machine.resource_state() == bound,
                "close while running leaked resources")
        machine.require_power_off()
        require(machine.references() == 1 and machine.call("adc_suspend", SPI_DEVICE) == 0,
                "closed device retained an open reference or blocked suspend")
        machine.model.stalled = False
        require(machine.open() == 0 and machine.references() == 2, "reopen after close failed")
        require(machine.call("adc_close", 0, FILE) == 0 and machine.resource_state() == bound,
                "second close leaked resources")
        require(machine.call("adc_remove", SPI_DEVICE) == 0 and machine.resource_state() == empty,
                "cleanup after reopen leaked resources")
        results.append("running close/suspend/reopen" + (" with stalled SPI" if stalled else ""))
    return results


def verify_test_signal(module):
    results = []
    for condition, expected in (("running", -16), ("detached", -19), ("uninitialized", -5),
                                ("interrupted mutex", -512), ("SPI lock", -16)):
        machine = Machine(module)
        if condition != "running":
            machine.field("dreem_adc", ADC, "running", 0, width=1)
        if condition == "detached":
            machine.field("dreem_adc", ADC, "detached", 1)
        elif condition == "uninitialized":
            machine.field("dreem_adc", ADC, "initialized", 0, width=1)
        elif condition == "interrupted mutex":
            machine.lock_failure = True
        elif condition == "SPI lock":
            machine.failure = "spi_bus_lock"
        require(machine.call("adc_ioctl", FILE, 5, 0) == expected, "test-signal gate failed")
        require(not machine.model.trace, "rejected test-signal setup touched hardware")
        results.append("test-signal gate: " + condition)

    machine = Machine(module)
    machine.prepare_probe()
    require(machine.call("adc_probe", SPI_DEVICE) == 0 and machine.open() == 0,
            "test-signal lifecycle setup failed")
    start = len(machine.model.trace)
    require(machine.call("adc_ioctl", FILE, 5, 0xFFFFFFFF) == 0, "idle test-signal ioctl failed")
    trace = machine.model.trace[start:]
    tx = [b for op, a, b, _ in trace if op == WRITE and a == SPI + 4]
    expected_tx = [byte for register in (2, 5, 6, 7, 8)
                   for byte in (0x11, 0x40 | register, 0, 0x15)]
    require(tx == expected_tx and machine.field("dreem_adc", ADC, "running", width=1) == 0,
            "test-signal setup wrote wrong registers or started acquisition")
    require("__copy_to_user" not in machine.calls, "test-signal ioctl interpreted its ignored argument")
    require(machine.call("adc_ioctl", FILE, 1, 0) == 0, "test-signal acquisition start failed")
    require(machine.call("adc_close", 0, FILE) == 0, "test-signal close failed")
    machine.require_power_off()
    start = len(machine.model.trace)
    require(machine.open() == 0, "reopen after test signal failed")
    transactions, current = [], []
    for op, a, b, _ in machine.model.trace[start:]:
        if op == GPIO_SET and a == 90:
            if b == 0:
                current = []
            elif current:
                transactions.append(current)
        elif op == WRITE and a == SPI + 4:
            current.append(b)
    require([0x06] in transactions and [0x42, 0, 0xC0] in transactions and
            all([0x40 | register, 0, 0x10] in transactions for register in (5, 6, 7, 8)),
            "reopen did not reset the ADC and restore normal input selection")
    require(machine.call("adc_close", 0, FILE) == 0 and machine.call("adc_remove", SPI_DEVICE) == 0,
            "test-signal lifecycle cleanup failed")
    require(machine.resource_state() == (False, False, 0, 0, 0, 0), "test-signal sequence leaked resources")
    results.append("test-signal enable/start/close/reopen restores normal input registers")

    for transaction in range(1, 11):
        machine = Machine(module)
        machine.field("dreem_adc", ADC, "running", 0, width=1)
        machine.model = TestSignalTransport(failed_transaction=transaction)
        require(machine.call("adc_ioctl", FILE, 5, 0) == -110, "test-signal timeout not reported")
        machine.require_power_off()
        require(machine.field("dreem_adc", ADC, "initialized", width=1) == 0 and
                machine.field("dreem_adc", ADC, "cancelled") == 1,
                "failed test-signal setup remained usable")
        length = len(machine.model.trace)
        require(machine.call("adc_ioctl", FILE, 1, 0) == -5 and
                machine.call("adc_ioctl", FILE, 5, 0) == -5 and len(machine.model.trace) == length,
                "failed test-signal setup accepted further control before reinitialization")
        results.append("test-signal timeout invalidates state: transaction " + str(transaction))
    return results


def verify_managed_lifecycle(module):
    results = []
    def opened():
        m = Machine(module)
        m.prepare_probe()
        require(m.call("adc_probe", SPI_DEVICE) == 0 and m.open() == 0, "managed fixture open failed")
        require(m.dma_paused and m.dma_claimed and m.control_calls[0] == 0,
                "ADC setup did not follow exclusive idle claim")
        return m

    for operation in ("stop", "close", "remove", "read fault", "resume fault"):
        m = opened()
        if operation == "resume fault":
            m.run_fault = True
            ret = m.call("adc_ioctl", FILE, 1, 0)
            require(ret == -5, "partial resume error was hidden")
        else:
            require(m.call("adc_ioctl", FILE, 1, 0) == 0, "managed fixture start failed")
            length = len(m.model.trace)
            if operation == "read fault":
                m.provider_error = -75
                ret = m.call("adc_read", FILE, USER, 16, 0)
                require(ret == -75, "read fault was hidden")
            else:
                m.control_failure = 1
                if operation == "stop":
                    ret = m.call("adc_ioctl", FILE, 0, 0)
                elif operation == "close":
                    ret = m.call("adc_close", 0, FILE)
                else:
                    ret = m.call("adc_remove", SPI_DEVICE)
                require(ret == (0 if operation == "remove" else -110), "stop failure return differs")
            require(len(m.model.trace) == length, "unacknowledged stop performed ADC I/O")
        require(m.field("dreem_adc", ADC, "poisoned", width=1) and m.dma_claimed and
                m.field("dev_pm_info", m.power, "usage_count") == 1,
                "uncertain stop released the provider or controller clock")
        require(not m.field("dreem_adc", ADC, "running", width=1) and 3 not in m.control_calls,
                "uncertain stop permitted new acquisition/release")
        if operation != "remove":
            require(m.call("adc_suspend", SPI_DEVICE) == -16, "poisoned instance allowed sleep")
            require(m.call("adc_remove", SPI_DEVICE) == 0, "poisoned remove failed")
        if operation != "close":
            require(m.call("adc_close", 0, FILE) == -5, "poisoned close touched hardware or hid state")
        require(m.resource_state() == (True, False, 3, 3, 1, 1) and m.references() == 1,
                "uncertain DMA resources did not remain pinned exactly once")
        results.append("unacknowledged DMA handoff retains resources: " + operation)

    m = Machine(module)
    m.prepare_probe()
    require(m.call("adc_probe", SPI_DEVICE) == 0, "legacy gate fixture failed")
    m.control_failure = 0
    require(m.open() == -95 and not m.model.trace and not m.dma_claimed and
            m.field("dev_pm_info", m.power, "usage_count") == 0,
            "unsupported provider touched hardware")
    require(m.call("adc_remove", SPI_DEVICE) == 0, "unsupported-provider cleanup failed")
    results.append("unsupported/legacy control protocol rejected before ADC I/O")
    m = opened()
    require(m.call("adc_ioctl", FILE, 1, 0) == 0 and not m.dma_paused, "managed resume failed")
    require(m.call("adc_ioctl", FILE, 0, 0) == 0 and m.dma_paused, "managed stop failed")
    require(m.call("adc_ioctl", FILE, 5, 0) == 0 and m.dma_paused, "test waveform escaped pause")
    require(m.call("adc_ioctl", FILE, 1, 0) == 0 and not m.dma_paused, "managed restart failed")
    require(m.call("adc_close", 0, FILE) == 0 and not m.dma_claimed and m.dma_paused,
            "normal close failed to release quiescent provider")
    require(m.call("adc_remove", SPI_DEVICE) == 0 and m.resource_state() == (False, False, 0, 0, 0, 0),
            "normal coordinated lifecycle leaked resources")
    results.append("claim/init/start/pause/test/restart/close/release ordering")
    return results


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
    cleanup = [[GPIO_SET, 35, 0, 0], [GPIO_SET, 90, 1, 0]]
    if not machine.managed:
        cleanup.insert(0, [WRITE, EVENT, 0, 0])
    require(machine.model.trace[-len(cleanup):] == cleanup,
            "stalled stop did not shut down")
    results.append("stalled stop shuts down and invalidates initialization")
    results.extend(verify_lifecycle(module))
    results.extend(verify_test_signal(module))
    if any(s.name == "dreem_sdma_status" and s["st_shndx"] == "SHN_UNDEF"
           for s in module.elf.get_section_by_name(".symtab").iter_symbols()):
        for case in ("read", "pending copy", "blocking wake", "nonblocking wake", "start", "test signal", "open"):
            machine = Machine(module, nonblock=case == "nonblocking wake")
            if case in ("blocking wake", "nonblocking wake"):
                machine.provider_fault_on_wait = True
            else:
                machine.provider_error = -75
            if case == "pending copy":
                machine.field("dreem_adc", ADC, "pending", 1, width=1)
            if case in ("start", "test signal"):
                machine.field("dreem_adc", ADC, "running", 0, width=1)
                ret = machine.call("adc_ioctl", FILE, 1 if case == "start" else 5, 0)
            elif case == "open":
                machine.field("dreem_adc", ADC, "opened", 0, width=1)
                ret = machine.open()
            else:
                ret = machine.call("adc_read", FILE, USER, 16, 0)
            require(ret == -75, "provider fault was not propagated: " + case)
            require("__copy_to_user" not in machine.calls, "provider fault exposed a sample")
            if case != "open":
                require(not machine.model.trace or all(op not in (READ, WRITE, GPIO_OUTPUT, GPIO_SET)
                        for op, _, _, _ in machine.model.trace), "uncertain DMA stop touched ADC")
                require(machine.field("dreem_adc", ADC, "poisoned", width=1),
                        "unacknowledged provider fault did not retain resources")
                require(not machine.field("dreem_adc", ADC, "initialized", width=1),
                        "provider fault retained ADC initialization")
            results.append("provider fault: " + case)
        results.extend(verify_managed_lifecycle(module))
    return {"module_sha256": hashlib.sha256(module.binary).hexdigest(),
            "passed_cases": len(results), "cases": results,
            "runtime_qualified": False,
            "limits": "Kernel resource/PM calls are models; concurrent scheduling, controller power transitions and physical DMA remain unverified"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("module", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.module), indent=2))


if __name__ == "__main__":
    main()
