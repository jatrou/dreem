/* SPDX-License-Identifier: Apache-2.0 */
#ifndef DREEM_OPTICAL_SENSOR_H
#define DREEM_OPTICAL_SENSOR_H
#include "optical_fifo.h"

enum dreem_optical_state {
    DREEM_OPTICAL_UNKNOWN = 0,
    DREEM_OPTICAL_STOPPED = 1,
    DREEM_OPTICAL_RUNNING = 2
};

struct dreem_optical_profile {
    unsigned red_current_code;      /* Explicit register code, 0..255. */
    unsigned infrared_current_code;
    unsigned samples_per_second;   /* 50, 100, 200 or 400; not a measured clock. */
};

struct dreem_optical_sensor {
    struct dreem_optical_fifo fifo;
    enum dreem_optical_state state;
    unsigned identified;
    uint8_t revision;
    int last_error;
    int cleanup_error;
};

/* Caller owns the descriptor AND exclusive sensor access across all calls.
 * This binds software state only; it does not open, identify, stop, or reset a
 * device. Initialize once per owned descriptor, never over a running instance.
 * Part ID 0x15 is not unique: physical MAX30101/MAX30102-compatible identity and
 * suitable LED currents must be established separately. No concurrency allowed.
 */
int dreem_optical_sensor_init(struct dreem_optical_sensor *sensor,
                               int fd, unsigned address);

/* Destructive explicit start/restart: reset discards old samples and framing.
 * Identify before writes, poll reset at most 20 times (1 ms between polls),
 * configure/verify while shut down, then activate and arm the FIFO reader.
 * No undocumented/proximity registers or interrupt enable bits are set.
 * A running instance returns -EBUSY. Invalid profiles cause no I/O.
 * Bus calls can block under kernel timeout policy; this is not a wall-clock
 * deadline. On failure preserve the original error and separately report any
 * failure to confirm shutdown in cleanup_error/state. The descriptor stays open.
 */
int dreem_optical_sensor_start(struct dreem_optical_sensor *sensor,
                                const struct dreem_optical_profile *profile);

/* Use the checked FIFO reader only in RUNNING state. -EAGAIN is retryable;
 * other acquisition faults quarantine samples and attempt confirmed shutdown.
 * Restart after a fault is explicit, using start (including another reset).
 */
int dreem_optical_sensor_read(struct dreem_optical_sensor *sensor,
                               struct dreem_optical_batch *output);

/* Confirm shutdown without resetting or closing the descriptor. A failure
 * leaves UNKNOWN state, never a claimed stop. Retry stop explicitly if needed.
 * An unidentified instance returns -ENODEV without writes.
 */
int dreem_optical_sensor_stop(struct dreem_optical_sensor *sensor);
#endif
