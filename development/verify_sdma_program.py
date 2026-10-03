#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright 2026 Dreem research contributors.
"""Verify assembled acquisition code with synthetic memory/peripheral delays.

No device access or vendor firmware input. Optionally cross-check every byte
with the separately obtained, hash-pinned Billauer/Petri assembler.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

try:
    from .sdma_assemble import assemble
    from .sdma_program_model import Machine, BusStalled, CONTROL, RING_BYTES
except ImportError:
    from sdma_assemble import assemble
    from sdma_program_model import Machine, BusStalled, CONTROL, RING_BYTES


def verify(source):
    if not __debug__:
        raise RuntimeError("verification requires Python assertions enabled")
    program, labels = assemble(source)
    cases = []
    frame = (0x12345678, 0x90ABCDEF, 0x76543210, 0xFEDCBA98)
    raw = struct.pack("<4I", *frame)
    baseline = Machine(program, [frame])
    baseline.run_until(lambda m: len(m.irqs) == 1)
    frame_steps = baseline.steps
    access_trace = [x for x in baseline.trace if x[0] == "functional"]

    for origin in (0x1800, 0x1900, 0x2000 - len(program) // 2):
        for latency in (0, 1, 7, 1000):
            for at in range(frame_steps + 1):
                m = Machine(program, [frame], origin=origin, latency=latency)
                for _ in range(at):
                    assert m.step()
                m.pause()
                produced = m.read(CONTROL)
                assert produced in (0, 1)
                assert len(m.tx) == produced * 4 and not any(m.tx)
                if produced:
                    assert m.mem[:16] == raw
                else:
                    assert m.mem[:RING_BYTES] == b"\xa5" * RING_BYTES
                snapshot = bytes(m.mem), len(m.tx), m.operations
                for _ in range(10):
                    assert not m.step()
                assert (bytes(m.mem), len(m.tx), m.operations) == snapshot
                # Resume keeps the producer sequence and next ring slot.
                m.frames.append(frame)
                m.resume()
                m.run_until(lambda n: n.read(CONTROL) == produced + 1)
                m.pause(6)
                assert m.mem[produced * 16:(produced + 1) * 16] == raw
                cases.append(f"pause step={at} delay={latency} origin={origin:#x}, resume")

    for latency in (0, 7, 1000):
        m = Machine(program, [], latency=latency)
        for _ in range(500):
            m.step()
        m.pause()
        assert m.read(CONTROL) == 0 and not m.tx
        cases.append(f"pause when SPI never becomes ready, delay={latency}")

    frames = [tuple((i << 8) | j for j in range(4)) for i in range(130)]
    m = Machine(program, frames)
    m.run_until(lambda n: n.read(CONTROL) == 130)
    m.pause()
    for i, values in enumerate(frames[-64:], 66):
        at = i % 64 * 16
        assert m.mem[at:at + 16] == struct.pack("<4I", *values)
    assert len(m.tx) == 520
    cases.append("130 frames, two ring wraps and all four words preserved")

    m = Machine(program, [frame] * 3)
    for _ in range(3):
        m.step()
    m.r[5] = 0xFFFFFFFE
    m.write(CONTROL, m.r[5])
    m.run_until(lambda n: n.read(CONTROL) == 1)
    m.pause()
    assert [irq[0] for irq in m.irqs[:3]] == [0xFFFFFFFF, 0, 1]
    cases.append("32-bit producer counter wraps without dropping a frame")

    # Every executed functional instruction in a normal frame can signal an
    # immediate error. No such case may acknowledge the fresh pause request.
    for _, ordinal, _, store, code in access_trace:
        failures = ["immediate"]
        if (store and code in (11, 40, 43, 200)) or (not store and code in (11, 200)):
            failures.append("delayed")
        for failure in failures:
            m = Machine(program, [frame], fail_at=ordinal, failure=failure)
            m.run_until(lambda n: not n.ep)
            assert m.read(CONTROL + 12) == 1
            assert m.read(CONTROL + 8) == 0
            operations = m.operations
            m.ep = True  # Even an accidental wake must not restart faulty DMA.
            m.run_until(lambda n: not n.ep)
            assert m.operations == operations
            cases.append(f"{failure} error at functional access {ordinal}, parked")

    # A permanently stalled bus is NOT a successful cooperative stop.
    for _, ordinal, _, store, code in access_trace:
        if not ((store and code in (40, 43, 200)) or (not store and code in (11, 200))):
            continue
        m = Machine(program, [frame], fail_at=ordinal, failure="stall")
        try:
            m.run_until(lambda n: not n.ep)
        except BusStalled:
            assert m.ep and m.read(CONTROL + 8) == 0
        else:
            raise AssertionError(f"bus stall {ordinal} falsely completed")
        cases.append(f"permanent bus stall {ordinal} gives no stop proof")

    # Start already paused: no SPI transaction is permitted during CPU setup.
    initial = Machine(program, [frame], request=2)
    initial.run_until(lambda n: not n.ep)
    assert initial.read(CONTROL + 8) == 2
    assert initial.read(CONTROL) == 0 and not initial.tx
    initial_accesses = [x for x in initial.trace if x[0] == "functional"]
    assert all(x[4] < 192 for x in initial_accesses)
    cases.append("initial pause acknowledges without touching ECSPI")
    for _, ordinal, _, _, _ in initial_accesses:
        m = Machine(program, [frame], request=2, fail_at=ordinal)
        m.run_until(lambda n: not n.ep)
        assert m.read(CONTROL + 12) == 1
        cases.append(f"initial pause error {ordinal} is reported and drained")

    # Negative controls establish that delayed writes matter to this verifier.
    # Replacing a wait/status read with a register immediate must be detected.
    for name, pc, kwargs in [
        ("counter publication", labels["wait_ready"] - 3, {"latency": 1000}),
        ("pause acknowledgement", labels["fault"] - 6, {"latency": 1000, "request": 2}),
        ("last SPI write", labels["count"] - 6, {"latency": 1, "peripheral_latency": 1000}),
    ]:
        words = list(struct.unpack(f"<{len(program) // 2}H", program))
        assert words[pc] in (0x670C, 0x67FF), (name, pc, hex(words[pc]))
        words[pc] = 0x0F00  # ldi r7,0: removes the DMA completion/status read.
        changed = struct.pack(f"<{len(words)}H", *words)
        m = Machine(changed, [frame], **kwargs)
        try:
            m.run_until(lambda n: bool(n.irqs))
        except AssertionError as exc:
            assert "before DMA drain" in str(exc)
        else:
            raise AssertionError(f"missing {name} barrier was not detected")
        cases.append(f"negative control: missing {name} barrier detected")

    # A peripheral read fault after three successful words leaves a partial
    # memory FIFO. Reading MS must not be modeled as an implicit FIFO flush.
    reads = [x for x in access_trace if not x[3] and x[4] == 200]
    last_rx = reads[-1][1]
    words = list(struct.unpack(f"<{len(program) // 2}H", program))
    assert words[labels["fault"]] == 0x6F28
    words[labels["fault"]] = 0x0F00
    m = Machine(struct.pack(f"<{len(words)}H", *words), [frame], fail_at=last_rx)
    try:
        m.run_until(lambda n: not n.ep)
    except AssertionError as exc:
        assert "before memory drain" in str(exc)
    else:
        raise AssertionError("missing partial-frame flush was not detected")
    cases.append("negative control: missing partial-frame fault flush detected")

    return {"program_sha256": hashlib.sha256(program).hexdigest(),
            "program_words": len(program) // 2, "passed_cases": len(cases),
            "cases": cases, "runtime_qualified": False,
            "limits": ["instruction subset and synthetic bus model, not silicon",
                       "no ECSPI serial timing, ADC fidelity, or scheduler-context proof",
                       "Linux driver does not yet implement this new control ABI"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).with_name("sdma_acquire.asm"))
    parser.add_argument("--external-assembler", type=Path)
    parser.add_argument("--external-module", type=Path)
    args = parser.parse_args()
    source = args.source.read_text()
    report = verify(source)
    if bool(args.external_assembler) != bool(args.external_module):
        parser.error("external assembler and instruction module must both be provided")
    if args.external_assembler:
        if args.external_module.name != "mx51_sdma_set.pm":
            parser.error("external module must be named mx51_sdma_set.pm")
        pinned = [
            (args.external_assembler, "d1772f6a3a982c2105edafbab76972dfde9ddcd2bb965d3e9e52dc34716cebc3"),
            (args.external_module, "f4eeee73b889cf32926d35489efd1cd248b5f63f84f807d1491008b26f2a98b2"),
        ]
        for path, expected in pinned:
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                parser.error(f"external assembler input hash mismatch: {path.name}")
        if sys.byteorder != "little":
            parser.error("external Perl assembler packs words using native byte order")
        env = dict(os.environ, PERL5LIB=str(args.external_module.parent))
        result = subprocess.run(["perl", str(args.external_assembler.resolve())], input=source.encode(),
                                capture_output=True, env=env, check=True,
                                cwd=args.external_module.resolve().parent)
        assert result.stdout == assemble(source)[0]
        report["external_assembler_match"] = True
        report["external_module_sha256"] = hashlib.sha256(args.external_module.read_bytes()).hexdigest()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
