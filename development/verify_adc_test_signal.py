#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Compare test-signal setup with stock ioctl 5 using synthetic hardware only.

Checks native C and compiled Cortex-A7 C against the reviewed kernel's ARM
instructions. A timeout is injected at each command/register transaction.
Does not establish a physical waveform, calibration, or timing on the device.
"""

import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from verify_adc_init import (CTransport, READ, WRITE, GPIO_SET, SPI, EVENT,
                             RAW_HASH, stock_inputs, run_stock, run_arm_reconstructed)
from verify_adc_control import AcquisitionTransport, StockState, ArmState, run_host


class TestSignalTransport(AcquisitionTransport):
    def __init__(self, delay=0, failed_transaction=0):
        super().__init__()
        self.delay, self.failed_transaction = delay, failed_transaction
        self.transaction, self.status_reads = 0, 0

    def io(self, op, a, b):
        if op == GPIO_SET and a == 90 and b == 0:
            self.transaction += 1
            self.status_reads = 0
        result = super().io(op, a, b)
        if op == READ and a == SPI + 24:
            self.status_reads += 1
            if self.transaction == self.failed_transaction or self.status_reads <= self.delay:
                result &= ~1
                self.trace[-1][3] = result
        return result


def verify(path):
    inputs = stock_inputs(path, ("ads1296_sdma_ioctl", "sdma_send_command", "sdma_write_registers"))
    source = Path(__file__).with_name("ads129x_init.c")
    expected_tx = [byte for register in (2, 5, 6, 7, 8)
                   for byte in (0x11, 0x40 | register, 0, 0x15)]
    digest, event_counts = hashlib.sha256(), []
    with tempfile.TemporaryDirectory() as folder:
        shared, arm_path = Path(folder) / "adc.so", Path(folder) / "adc.arm.elf"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-shared", "-fPIC", str(source), "-o", str(shared)], check=True)
        subprocess.run(["arm-linux-gnueabihf-gcc", "-std=c11", "-O2", "-Wall", "-Wextra",
                        "-Werror", "-marm", "-mcpu=cortex-a7", "-mgeneral-regs-only",
                        "-ffreestanding", "-nostdlib", "-static", "-no-pie",
                        "-Wl,-Ttext=0x20000000,-e,ads129x_sdma_test_signal",
                        str(source), "-o", str(arm_path)], check=True)
        library, binary = ctypes.CDLL(str(shared)), arm_path.read_bytes()

        def original(model):
            return run_stock(inputs, model, entry="ads1296_sdma_ioctl", argument=5,
                             extension=StockState(model))

        def arm(model):
            return run_arm_reconstructed(binary, model, entry="ads129x_sdma_test_signal",
                                         extension=ArmState(model))

        for delay in (0, 1, 3):
            stock, host, target = (TestSignalTransport(delay=delay) for _ in range(3))
            results = [original(stock), run_host(library, "test_signal", host), arm(target)]
            if results != [0, 0, 0] or stock.trace != host.trace or stock.trace != target.trace:
                raise ValueError(f"test-signal trace mismatch with status delay {delay}: {results}")
            if stock.state() != host.state() or stock.state() != target.state():
                raise ValueError("test-signal setup changed acquisition state")
            transmitted = [b for op, a, b, _ in stock.trace if op == WRITE and a == SPI + 4]
            if transmitted != expected_tx or stock.transaction != 10:
                raise ValueError("test-signal register writes differ from the expected commands")
            if any(op == WRITE and a == EVENT for op, a, _, _ in stock.trace):
                raise ValueError("test-signal setup changed acquisition requests")
            event_counts.append(len(stock.trace))
            digest.update(json.dumps([delay, stock.trace], separators=(",", ":")).encode())

        for transaction in range(1, 11):
            stock, host, target = (TestSignalTransport(failed_transaction=transaction) for _ in range(3))
            if original(stock) is not None:
                raise ValueError("expected the stock transaction wait to exceed its instruction bound")
            if run_host(library, "test_signal", host) != -110 or arm(target) != -110:
                raise ValueError("test-signal timeout was not reported")
            if host.trace != target.trace or host.transaction != transaction:
                raise ValueError("timeout differs across targets or continued to later transactions")
            if host.trace[-3:] != [[WRITE, EVENT, 0, 0], [GPIO_SET, 35, 0, 0], [GPIO_SET, 90, 1, 0]]:
                raise ValueError("timeout did not disable requests, power off and deselect")

        function = library.ads129x_sdma_test_signal
        function.argtypes, function.restype = [ctypes.POINTER(CTransport)], ctypes.c_int
        if function(None) != -22:
            raise ValueError("null transport accepted")
        if function(ctypes.byref(CTransport())) != -22:
            raise ValueError("empty transport accepted")
    return {"kernel_raw_sha256": RAW_HASH, "matching_cases": len(event_counts),
            "reconstruction_targets": ["host C", "Cortex-A7 ARM C"],
            "io_event_counts": event_counts, "trace_sha256": digest.hexdigest(),
            "bounded_timeout_cases": 10, "invalid_transport_cases": 2,
            "runtime_qualified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel_elf", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.kernel_elf), indent=2))


if __name__ == "__main__":
    main()
