/* SPDX-License-Identifier: Apache-2.0 */
#ifndef DREEM_OPTICAL_FIFO_H
#define DREEM_OPTICAL_FIFO_H
#include <stdint.h>

#define DREEM_OPTICAL_BATCH_MAX 31u
enum dreem_optical_batch_flags {
    DREEM_OPTICAL_CONFIG_VALID = 1u,
    DREEM_OPTICAL_STATUS_VALID = 2u,
    DREEM_OPTICAL_BEFORE_VALID = 4u,
    DREEM_OPTICAL_AFTER_VALID = 8u,
    DREEM_OPTICAL_POINTERS_EQUAL = 16u,
    DREEM_OPTICAL_HIGH_BITS = 32u,
    DREEM_OPTICAL_RESYNC_REQUIRED = 64u,
    DREEM_OPTICAL_CONTINUITY_UNKNOWN = 128u
};

struct dreem_optical_fifo {
    int fd;
    unsigned address;
    unsigned needs_resync;
};

struct dreem_optical_batch {
    uint32_t samples[DREEM_OPTICAL_BATCH_MAX][2];
    unsigned count;
    unsigned flags;
    uint8_t configuration[3]; /* Registers 8..10: FIFO, mode, conversion. */
    uint8_t status;           /* Register 0: reading acknowledges interrupts. */
    uint8_t before[3];        /* Registers 4..6: write, overflow, read. */
    uint8_t after[3];
};

/* No device opens or writes. Caller exclusively owns the sensor and descriptor,
 * has established a known FIFO byte boundary, and must prevent concurrent calls,
 * reset, configuration changes, or another consumer (including nano_core).
 * Calling this function does NOT reset or synchronize hardware. Reinitializing
 * after a fault is allowed only after external, verified hardware resynchronization.
 */
int dreem_optical_fifo_init(struct dreem_optical_fifo *fifo,
                             int fd, unsigned address);

/* Read a bounded snapshot batch in red/IR mode 3 with rollover disabled.
 * Return 0 for published samples, -EAGAIN for equal pointers without affirmative
 * fullness/loss evidence, or another negative errno for a fault. Equal pointers
 * are ambiguous, not proof of an empty FIFO. A fault quarantines further I/O
 * until external resynchronization and init. Null arguments return -EINVAL.
 * Output is cleared on entry, with diagnostic bytes valid only under flags;
 * errors never publish samples. State/output must not overlap.
 * Even success does NOT establish uninterrupted acquisition: overflow can occur
 * between snapshots and be cleared by a FIFO pop. No sample timestamps or health
 * interpretation are supplied. See optical-fifo.md for the ownership contract.
 */
int dreem_optical_fifo_drain(struct dreem_optical_fifo *fifo,
                              struct dreem_optical_batch *output);
#endif
