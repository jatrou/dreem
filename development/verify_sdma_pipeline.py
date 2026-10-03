#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Connect the compiled ARM ADC/provider and their assembled SDMA program.

Kernel scheduling and buses are synthetic. Real ARM entry points call each
other across two Unicorn instances; the provider uploads the program actually
embedded in its object. No device, recording, firmware flash, or network access.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from verify_adc_module import (Module, Machine as ADCMachine, require, ADC, FILE,
                               SPI_DEVICE, USER, RING, EVENT)
from verify_adc_read import ORDER
from verify_sdma_provider import Machine as ProviderMachine, ENGINE


class Pipeline:
    def __init__(self, provider, adc):
        self.provider = p = ProviderMachine(provider)
        require(p.start(managed=True) == 1, "provider startup failed")
        self.adc = a = ADCMachine(adc, pending=0)
        self.notifications = 0
        a.provider_callback = self.control
        a.prepare_probe()
        self.sync()
        require(a.call("adc_probe", SPI_DEVICE) == 0 and a.open() == 0, "ADC open failed")
        require(a.dma_paused and p.ef("claimed", width=1), "ADC initialized without paused ownership")

    def sync(self):
        p, a = self.provider, self.adc
        a.uc.mem_write(RING, bytes(p.uc.mem_read(p.ef("ring"), 1024)))
        a.put(a.symbols["sdma_queue_head"], p.u32(p.symbols["sdma_queue_head"]))
        a.model.pending += len(p.notifications) - self.notifications
        self.notifications = len(p.notifications)
        a.dma_paused = bool(p.ef("paused", width=1))
        a.dma_claimed = bool(p.ef("claimed", width=1))
        a.model.registers[EVENT] = p.registers[0x20C] & 2

    def control(self, name, command):
        p, a = self.provider, self.adc
        if name == "dreem_sdma_control":
            require(not a.spi_locked, "handoff attempted while holding the SPI bus")
            a.control_calls.append(command)
            if command == 2:
                require(p.ef("paused", width=1), "ADC reset/started an unpaused ring")
                p.uc.mem_write(p.ef("ring"), bytes(a.uc.mem_read(RING, 1024)))
            ret = p.call(name, command)
        else:
            ret = p.call(name)
        self.sync()
        return ret

    def start(self):
        require(self.adc.call("adc_ioctl", FILE, 1, 0) == 0, "ADC start failed")
        require(not self.provider.ef("paused", width=1), "ADC start did not resume SDMA")

    def emit(self, frame, irq=True):
        p = self.provider
        p.program.frames.append(struct.unpack("<4I", frame))
        old_count = p.u32(p.ef("counter"))
        for _ in range(8):
            p.pump()
            if p.u32(p.ef("counter")) != old_count or not p.registers[0x1C] & 2:
                break
        else:
            raise ValueError("frame did not progress within instruction bound")
        if irq:
            require(p.call("sdma_int_handler", 42, ENGINE) == 1, "provider IRQ failed")
        self.sync()

    def read(self, raw):
        a = self.adc
        require(a.call("adc_read", FILE, USER, 16, 0) == 16, "ADC read failed")
        expected = bytes(raw[index] for index in ORDER) + bytes([1, 0, 0, 0])
        require(a.output() == expected, "delivered sample/order/depth/padding differs")


def sample(index):
    raw = bytearray((index * 31 + j * 17) & 255 for j in range(16))
    raw[:3] = bytes([index & 15, 0, 0xC0])
    return bytes(raw)


def verify(provider_path, adc_path):
    provider, adc = Module(provider_path), Module(adc_path)
    cases = []
    pipeline = Pipeline(provider, adc)
    pipeline.start()
    for i in range(70):
        raw = sample(i)
        pipeline.emit(raw)
        pipeline.read(raw)
        cases.append(f"compiled ADC/provider/SDMA sample {i}")
        if i == 14:
            require(pipeline.adc.call("adc_ioctl", FILE, 0, 0) == 0, "coordinated stop failed")
            require(pipeline.adc.call("adc_ioctl", FILE, 5, 0) == 0, "paused test setup failed")
            pipeline.start()
            cases.append("stop/test/restart preserves producer while resetting consumer")
        if i == 35:
            require(pipeline.adc.call("adc_close", 0, FILE) == 0 and pipeline.adc.open() == 0,
                    "coordinated close/reopen failed")
            pipeline.start()
            cases.append("close/reopen reacquires paused provider")
    pipeline.emit(sample(70), irq=False)
    require(pipeline.adc.call("adc_ioctl", FILE, 0, 0) == 0, "stop with undispatched IRQ failed")
    require(pipeline.provider.u32(pipeline.provider.symbols["sdma_queue_head"]) == 7,
            "undispatched frame missing from stopped producer position")
    pipeline.start()
    require(pipeline.adc.model.pending == 0, "restart retained stale notifications")
    pipeline.emit(sample(71))
    pipeline.read(sample(71))
    cases.append("pause accounts pending IRQ; restart drains old notifications")
    require(pipeline.adc.call("adc_close", 0, FILE) == 0 and
            pipeline.adc.call("adc_remove", SPI_DEVICE) == 0 and
            pipeline.adc.resource_state() == (False, False, 0, 0, 0, 0),
            "normal pipeline cleanup retained ADC resources")
    require(pipeline.provider.ef("paused", width=1) and not pipeline.provider.ef("claimed", width=1),
            "normal cleanup left DMA running or claimed")
    cases.append("normal cleanup releases ADC ownership after acknowledged pause")

    for failure in ("SDMA bus error", "stop timeout"):
        pipeline = Pipeline(provider, adc)
        pipeline.start()
        p, a = pipeline.provider, pipeline.adc
        before = len(a.model.trace)
        if failure == "SDMA bus error":
            p.program.fail_at = p.program.operations + 1
            pipeline.emit(sample(1))
            require(a.call("adc_read", FILE, USER, 16, 0) == -5, "SDMA error did not reach reader")
        else:
            p.failure = "initialization"
            require(a.call("adc_ioctl", FILE, 0, 0) == -110, "stop timeout was not propagated")
        require(len(a.model.trace) == before and a.field("dreem_adc", ADC, "poisoned", width=1),
                "uncertain SDMA handoff permitted ADC I/O")
        require(a.resource_state()[-1] == 1 and all(n == 1 for n in p.clocks.values()),
                "uncertain stop released a controller/SDMA clock")
        require(a.call("adc_remove", SPI_DEVICE) == 0 and a.call("adc_close", 0, FILE) == -5 and
                a.resource_state() == (True, False, 3, 3, 1, 1),
                "uncertain pipeline cleanup freed DMA-dependent resources")
        cases.append("compiled pipeline retains resources after " + failure)
    return {"provider_sha256": hashlib.sha256(provider.binary).hexdigest(),
            "adc_sha256": hashlib.sha256(adc.binary).hexdigest(), "passed_cases": len(cases),
            "cases": cases, "runtime_qualified": False,
            "limits": "Synthetic buses/IRQs/kernel services, no true scheduler races or physical ADC timing"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider_object", type=Path)
    parser.add_argument("adc_module", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.provider_object, args.adc_module), indent=2))


if __name__ == "__main__":
    main()
