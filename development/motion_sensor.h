/* SPDX-License-Identifier: Apache-2.0 */
#ifndef DREEM_MOTION_SENSOR_H
#define DREEM_MOTION_SENSOR_H
#include <stdint.h>

enum dreem_motion_state { DREEM_MOTION_UNKNOWN, DREEM_MOTION_STOPPED, DREEM_MOTION_RUNNING };
enum { DREEM_MOTION_CONTINUITY_UNKNOWN = 1, DREEM_MOTION_OVERRUN_OBSERVED = 2 };
struct dreem_motion_profile {
    unsigned samples_per_second; /* 10, 50, 100, 200, 400 or 800. */
    unsigned full_scale_g;        /* 2, 4 or 8; affects count interpretation. */
    unsigned high_resolution;    /* 0 or 1. HR uses the default ODR/50 filter. */
};
struct dreem_motion_sample {
    int16_t xyz[3];               /* Sensor axes, signed counts; not recorder axes. */
    unsigned flags;
    uint8_t status_before, status_after;
};
struct dreem_motion_sensor {
    int fd;
    unsigned address, identified, settling_rows;
    enum dreem_motion_state state;
    int last_error, cleanup_error;
    uint8_t configuration[7];
    struct dreem_motion_profile profile;
};

/* Caller supplies an already-open descriptor and exclusive sensor ownership.
 * Init does no I/O and must not overwrite an active instance. No API opens or
 * closes devices, obtains ownership, or coordinates with nano_core. Start
 * resets/discards sensor state. Failed hardware cleanup retains UNKNOWN state;
 * retain ownership until an explicit recovery confirms power-down.
 */
int dreem_motion_sensor_init(struct dreem_motion_sensor *, int fd, unsigned address);
int dreem_motion_sensor_start(struct dreem_motion_sensor *, const struct dreem_motion_profile *);
/* Output unchanged on failure, including EAGAIN (not ready or first row discarded).
 * Successful samples still have unknown continuity and physical timing.
 */
int dreem_motion_sensor_read(struct dreem_motion_sensor *, struct dreem_motion_sample *);
int dreem_motion_sensor_stop(struct dreem_motion_sensor *);
#endif
