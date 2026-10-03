/* SPDX-License-Identifier: Apache-2.0 */
.syntax unified
.arm
.text
.macro disable_callers
    ldr r1, [sp, #12]
    movw r3, #0x6cf8
    movt r3, #0x0003
    mov r2, #0
    cmp r1, r3
    ldreq r2, [sp, #20]
.endm
.global dreem_power_off_shim
.type dreem_power_off_shim, %function
dreem_power_off_shim:
    /* At stock disable's power-off BL, its saved caller is sp+12. Only
     * pause's known frame permits reading its caller at sp+20. No prologue
     * is copied or replaced; r0 is the original fixed command pointer. */
    disable_callers
    ldr r3, [sp]
    b dreem_power_off
.size dreem_power_off_shim, .-dreem_power_off_shim

.global dreem_disable_probe_shim
.type dreem_disable_probe_shim, %function
dreem_disable_probe_shim:
    disable_callers
    /* Stock disable allocates 12 bytes: sp+4 is its cancellation-state word;
     * sp+0 is unused throughout this exact function. Keep this invocation's
     * probe result there, rather than sharing it across caller threads. */
    mov r3, sp
    b dreem_disable_probe
.size dreem_disable_probe_shim, .-dreem_disable_probe_shim

.global dreem_syscall4
.type dreem_syscall4, %function
dreem_syscall4:
    push {r4, r7}
    mov r7, r0
    mov r0, r1
    mov r1, r2
    mov r2, r3
    ldr r3, [sp, #8]
    svc #0
    pop {r4, r7}
    bx lr
.size dreem_syscall4, .-dreem_syscall4
.section .note.GNU-stack,"",%progbits
