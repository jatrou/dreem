# SPDX-License-Identifier: GPL-2.0-only
# Copyright 2026 Dreem research contributors.
# Research acquisition protocol v1, written from the i.MX6ULL functional-unit
# specification and the observed four-word ECSPI interface. NOT a drop-in
# replacement for ads_sdma.bin: see sdma-program.md before integration.
#
# Inputs: r0 = coherent 1024-byte ring, r1 = 1024, r2 = coherent 64-byte control
# allocation, r3 = ECSPI1 base (0x02008000). Ring is word-aligned; control is
# 64-byte-aligned so that any burst read stays within its padded allocation.
# Control: +0 produced, +4 host request, +8 paused acknowledgement, +12 fault.
# Request bit 0: 1 run, 0 pause. Bits 31:1 identify a fresh host generation.
# No absolute branches: this program can be placed anywhere in program RAM.
# Functional-unit numbers below follow IMX6ULLRM Rev.1, tables 46-20/23/32/35.

        ldi r4, 0               # Next ring byte offset; first frame uses slot 0.
        ldi r5, 0               # Number of complete, published frames.
        clrf 0
command:
        mov r7, r2
        addi r7, 4
        stf r7, 0               # MSA, no speculative prefetch flag.
        bdf fault
        ldf r6, 11              # MD, one 32-bit request (bus may fetch a burst).
        bsf fault
        btsti r6, 0
        bf pause

        mov r7, r3
        addi r7, 24             # ECSPI STATREG.
        stf r7, 195             # PSA, frozen address, 32-bit, no prefetch.
        bdf fault
        ldf r7, 200             # PD, no prefetch.
        bsf fault
        btsti r7, 4             # Receive DMA threshold reached.
        bf wait_ready

        mov r7, r0
        add r7, r4
        stf r7, 4               # MDA, incrementing.
        bdf fault
        stf r3, 195             # PSA = ECSPI RXDATA, frozen, 32-bit.
        bdf fault
        ldf r7, 200
        bsf fault
        stf r7, 11              # MD, 32-bit; accumulate a four-word frame.
        bdf fault
        ldf r7, 200
        bsf fault
        stf r7, 11
        bdf fault
        ldf r7, 200
        bsf fault
        stf r7, 11
        bdf fault
        ldf r7, 200
        bsf fault
        stf r7, 11
        bdf fault
        stf r7, 40              # MD | FL | SZ0: flush without appending data.
        bdf fault
        ldf r7, 12              # MS: wait for completion and check bus errors.
        bsf fault

        mov r7, r3
        addi r7, 4
        stf r7, 211             # PDA = ECSPI TXDATA, frozen, 32-bit.
        bdf fault
        ldi r6, 0
        stf r6, 200             # Refill the four SPI transmit words.
        bdf fault
        stf r6, 200
        bdf fault
        stf r6, 200
        bdf fault
        stf r6, 200
        bdf fault
        ldf r7, 255             # PS: last peripheral write must have completed.
        bsf fault

        addi r4, 16
        cmpeq r4, r1
        bf count
        ldi r4, 0
count:
        addi r5, 1
        stf r2, 4               # MDA = produced counter.
        bdf fault
        stf r5, 43              # MD | FL | SZ32 (acknowledged before completion).
        bdf fault
        ldf r7, 12              # Complete publication before interrupting CPU.
        bsf fault
        notify 1
wait_ready:
        done 0                 # Higher-priority DMA may run; check host next.
        cmpeq r0, r0
        bt command

pause:
        ldf r7, 12              # Complete the request read before publishing ACK.
        bsf fault
        mov r7, r2
        addi r7, 8
        stf r7, 4
        bdf fault
        stf r6, 43              # ACK is the exact fresh even request value.
        bdf fault
        ldf r7, 12              # ACK must be globally visible before EP clears.
        bsf fault
        notify 1
        done 4                 # Host must mask the hardware event before pause.
        cmpeq r0, r0            # A subsequent explicit wake resumes command reads.
        bt command

fault:
        stf r7, 40              # Flush any partial, unpublished frame explicitly.
        ldf r7, 12              # Drain both units, even on the error path.
        ldf r7, 255             # A bus that never answers may stall here.
        stf r7, 12              # Clear MS error only, preserving other fields.
        stf r7, 204             # Clear PS error only.
        clrf 0
        mov r7, r2
        addi r7, 12
        stf r7, 4
        bdf fault_park
        ldi r6, 1              # Generic DMA fault; never acknowledge a new pause.
        stf r6, 43
        ldf r7, 12              # Attempt once; do not loop on an inaccessible bus.
fault_park:
        notify 1
        done 4
        cmpeq r0, r0
        bt fault_park           # A fault requires a new context, not another wake.
