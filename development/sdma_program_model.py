# SPDX-License-Identifier: GPL-2.0-only
# Copyright 2026 Dreem research contributors.
"""Instruction-level model for the research acquisition program, not an SoC emulator.

Models only its used SDMA instructions and functional-unit accesses, from
IMX6ULLRM Rev.1 sections 46.4.12.1/2 and 46.5.2. Memory and peripheral stores
can complete after their instruction acknowledges. Unmodeled operations fail
closed. Does not model ECSPI wire timing, bus arbitration, or SDMA context saves.
"""

from collections import deque
import struct

RING = 0x11000000
CONTROL = 0x11001000
SPI = 0x02008000
RING_BYTES = 1024
CONTROL_BYTES = 64


class BusStalled(RuntimeError):
    """A requested bus completion never arrives; quiescence is not established."""


class Machine:
    def __init__(self, program, frames=(), request=3, latency=7,
                 fail_at=None, failure="immediate", origin=0x1800,
                 peripheral_latency=None, ring_base=RING, control_base=CONTROL):
        self.words = struct.unpack(f"<{len(program) // 2}H", program)
        self.origin = origin
        self.pc = origin
        self.ring_base, self.control_base = ring_base, control_base
        self.r = [ring_base, RING_BYTES, control_base, SPI, 0, 0, 0, 0]
        self.mem = bytearray(b"\xa5" * RING_BYTES + b"\0" * CONTROL_BYTES)
        self.time = self.steps = self.operations = 0
        self.t = self.sf = self.df = False
        self.ep = True
        self.latency, self.fail_at, self.failure = latency, fail_at, failure
        self.peripheral_latency = latency if peripheral_latency is None else peripheral_latency
        self.fifo, self.pending_m = [], []
        self.cache = deque()
        self.pending_p = None
        self.msa = self.mda = self.psa = self.pda = None
        self.m_error = self.p_error = self.fifo_error = False
        self.frames = deque(tuple(frame) for frame in frames)
        if any(len(frame) != 4 for frame in self.frames):
            raise ValueError("each synthetic SPI frame must have four words")
        self.rx = deque()
        self.tx, self.irqs, self.trace = [], [], []
        self.write(self.control_base + 4, request)

    def offset(self, address, size):
        if self.ring_base <= address and address + size <= self.ring_base + RING_BYTES:
            return address - self.ring_base
        if self.control_base <= address and address + size <= self.control_base + CONTROL_BYTES:
            return RING_BYTES + address - self.control_base
        raise AssertionError(f"DMA outside allocated memory: {address:#x}+{size}")

    def read(self, address):
        return struct.unpack_from("<I", self.mem, self.offset(address, 4))[0]

    def write(self, address, value):
        struct.pack_into("<I", self.mem, self.offset(address, 4), value & 0xFFFFFFFF)

    def advance(self, ticks=1):
        self.time += ticks
        remaining = []
        for due, writes, failed in self.pending_m:
            if due is None or due > self.time:
                remaining.append((due, writes, failed))
            elif failed:
                self.m_error = True
                self.trace.append(("memory_error", self.time))
            else:
                for address, value in writes:
                    self.write(address, value)
                    self.trace.append(("memory_complete", address, value, self.time))
        self.pending_m = remaining
        if self.pending_p:
            due, value, failed = self.pending_p
            if due is not None and due <= self.time:
                self.pending_p = None
                if failed:
                    self.p_error = True
                else:
                    self.tx.append(value)
                    self.trace.append(("peripheral_complete", value, self.time))

    def wait_memory(self):
        if any(due is None for due, _, _ in self.pending_m):
            raise BusStalled("memory transfer did not complete")
        if self.pending_m:
            self.advance(max(due for due, _, _ in self.pending_m) - self.time)

    def wait_peripheral(self):
        if self.pending_p:
            due, _, _ = self.pending_p
            if due is None:
                raise BusStalled("peripheral transfer did not complete")
            self.advance(due - self.time)

    def flush(self, failure=None):
        if self.fifo:
            due = None if failure == "stall" else self.time + self.latency
            self.pending_m.append((due, self.fifo, self.fifo_error or failure == "delayed"))
            self.fifo, self.fifo_error = [], False

    def functional(self, store, reg, code):
        self.operations += 1
        failure = self.failure if self.operations == self.fail_at else None
        self.trace.append(("functional", self.operations, self.pc - self.origin, store, code))
        if failure == "immediate":
            if store:
                self.df = True
            else:
                self.sf = True
            return
        value = self.r[reg]
        error, result = False, 0
        if store and code in (0, 4):
            # Changing address registers may discard data. Catch that bug rather
            # than silently making the model flush what the program forgot.
            if self.fifo or self.pending_m:
                raise AssertionError("address replaced before memory drain")
            self.cache.clear()
            if code == 0:
                self.msa = value
            else:
                self.mda = value
            error = self.m_error
        elif store and code in (11, 40, 43):
            error = self.m_error
            if not error:
                if code != 40:
                    self.offset(self.mda, 4)
                    self.fifo.append((self.mda, value))
                    self.mda += 4
                if failure == "delayed":
                    self.fifo_error = True
                if code in (40, 43) or len(self.fifo) >= 8:
                    self.flush(failure)
                if code == 40:
                    self.wait_memory()
                    error = self.m_error
        elif not store and code == 11:
            if failure == "stall":
                raise BusStalled("memory read did not complete")
            if failure == "delayed":
                self.m_error = True
            error = self.m_error
            if not error:
                if not self.cache:
                    # A single MD word read can fetch 32 bytes. This checks the
                    # ABI's padding instead of assuming a four-byte allocation.
                    self.offset(self.msa, 32)
                    self.advance(self.latency)
                    self.cache.extend(self.read(self.msa + i) for i in range(0, 32, 4))
                result = self.cache.popleft()
                self.msa += 4
        elif not store and code == 12:
            # Wait for issued transactions. Do not invent a flush of a partial
            # FIFO: the program must explicitly request that operation.
            self.wait_memory()
            error = self.m_error
            result = 0x200 if error else 0
        elif store and code == 12:
            self.wait_memory()
            self.m_error = False
        elif store and code in (195, 211):
            self.wait_peripheral()
            error = self.p_error
            if not error:
                if value not in (SPI, SPI + 4, SPI + 24):
                    raise AssertionError("unexpected peripheral address")
                if code == 195:
                    self.psa = value
                else:
                    self.pda = value
        elif not store and code == 200:
            self.wait_peripheral()
            if failure == "stall":
                raise BusStalled("peripheral read did not complete")
            self.advance(self.peripheral_latency)
            if failure == "delayed":
                self.p_error = True
            error = self.p_error
            if not error:
                if self.psa == SPI + 24:
                    result = 16 if self.rx or self.frames else 0
                elif self.psa == SPI:
                    if not self.rx and self.frames:
                        self.rx.extend(self.frames.popleft())
                    if not self.rx:
                        raise AssertionError("read from empty SPI RX FIFO")
                    result = self.rx.popleft()
                else:
                    raise AssertionError("read from unsupported peripheral register")
        elif store and code == 200:
            self.wait_peripheral()
            error = self.p_error
            if not error:
                if self.pda != SPI + 4:
                    raise AssertionError("write outside SPI TX FIFO")
                self.pending_p = (None if failure == "stall" else self.time + self.peripheral_latency,
                                  value, failure == "delayed")
        elif not store and code == 255:
            self.wait_peripheral()
            error = self.p_error
            result = 0x200 if error else 0
        elif store and code == 204:
            self.wait_peripheral()
            self.p_error = False
        else:
            raise AssertionError(f"unmodeled functional access store={store} code={code}")
        if store:
            self.df = bool(error)
        else:
            self.sf = bool(error)
            self.r[reg] = result & 0xFFFFFFFF

    def step(self):
        if not self.ep:
            return False
        offset = self.pc - self.origin
        if not 0 <= offset < len(self.words):
            raise AssertionError("PC escaped loaded program")
        self.advance()
        self.steps += 1
        word = self.words[offset]
        reg, low = (word >> 8) & 7, word & 255
        target = self.pc + 1
        if word & 0xF800 == 0x0800:
            self.r[reg] = low
        elif word & 0xF800 == 0x1800:
            self.r[reg] = (self.r[reg] + low) & 0xFFFFFFFF
        elif word & 0xF8F8 in (0x0088, 0x0098, 0x00C8):
            other = self.r[word & 7]
            op = word & 0xF8F8
            if op == 0x0088:
                self.r[reg] = other
            elif op == 0x0098:
                self.r[reg] = (self.r[reg] + other) & 0xFFFFFFFF
            else:
                self.t = self.r[reg] == other
        elif word & 0xF8E0 == 0x0060:
            self.t = bool(self.r[reg] & (1 << (word & 31)))
        elif word == 0x0007:
            self.sf = self.df = False
        elif word & 0xF800 in (0x6000, 0x6800):
            self.functional(bool(word & 0x0800), reg, low)
        elif word & 0xFC00 == 0x7C00:
            condition = {0x7C: not self.t, 0x7D: self.t, 0x7E: self.sf, 0x7F: self.df}[word >> 8]
            if condition:
                target += low if low < 128 else low - 256
        elif word == 0x0101:
            self.irqs.append((self.read(self.control_base), self.read(self.control_base + 8),
                              self.read(self.control_base + 12), bytes(self.mem[:RING_BYTES])))
            if self.pending_m or self.pending_p or self.fifo:
                raise AssertionError("IRQ before DMA drain")
        elif word == 0x0000:
            pass  # Cooperative scheduling point, not a stop or memory barrier.
        elif word == 0x0400:
            self.ep = False
            if self.pending_m or self.pending_p or self.fifo:
                raise AssertionError("EP cleared before DMA drain")
        else:
            raise AssertionError(f"unmodeled instruction {word:#06x}")
        self.pc = target
        return True

    def run_until(self, predicate, limit=100000):
        for _ in range(limit):
            if predicate(self):
                return
            if not self.step():
                raise AssertionError("channel stopped before required condition")
        raise AssertionError("instruction bound exceeded")

    def pause(self, generation=4):
        if generation & 1 or generation == self.read(self.control_base + 8):
            raise ValueError("pause requires a fresh even generation")
        self.write(self.control_base + 4, generation)
        self.ep = True  # Host has already masked the hardware event.
        self.run_until(lambda m: not m.ep)
        if self.read(self.control_base + 12) or self.read(self.control_base + 8) != generation:
            raise AssertionError("pause was not acknowledged successfully")

    def resume(self, generation=5):
        if not generation & 1 or self.ep or self.read(self.control_base + 12):
            raise ValueError("resume requires a paused, healthy channel and odd request")
        self.write(self.control_base + 4, generation)
        self.ep = True
