/* SPDX-License-Identifier: Apache-2.0
 * Independent lifecycle for an exclusively owned red/IR optical sensor.
 */
#define _POSIX_C_SOURCE 200809L
#include "optical_sensor.h"
#include "sensor_i2c.h"
#include <errno.h>
#include <string.h>
#include <time.h>

static int read_regs(struct dreem_optical_sensor *s, uint8_t reg,
                     void *data, unsigned n) {
    return dreem_i2c_read_register(s->fifo.fd, s->fifo.address, reg, data, n);
}

static int write_regs(struct dreem_optical_sensor *s, uint8_t reg,
                      const void *data, unsigned n) {
    return dreem_i2c_write_register(s->fifo.fd, s->fifo.address, reg, data, n);
}

static int write_byte(struct dreem_optical_sensor *s, uint8_t reg, uint8_t value) {
    return write_regs(s, reg, &value, 1);
}

static int expect(struct dreem_optical_sensor *s, uint8_t reg,
                  const uint8_t *wanted, unsigned n) {
    uint8_t actual[3];
    int result = read_regs(s, reg, actual, n);
    return result ? result : memcmp(actual, wanted, n) ? -EPROTO : 0;
}

int dreem_optical_sensor_init(struct dreem_optical_sensor *s,
                               int fd, unsigned address) {
    if (!s) return -EINVAL;
    struct dreem_optical_fifo fifo;
    int result = dreem_optical_fifo_init(&fifo, fd, address);
    if (result) return result;
    fifo.needs_resync = 1;
    *s = (struct dreem_optical_sensor){.fifo = fifo, .state = DREEM_OPTICAL_UNKNOWN};
    return 0;
}

static int shutdown_sensor(struct dreem_optical_sensor *s) {
    s->state = DREEM_OPTICAL_UNKNOWN;
    s->fifo.needs_resync = 1;
    if (!s->identified) return -ENODEV;
    uint8_t mode;
    int result = read_regs(s, 9, &mode, 1);
    if (result) return result;
    if (mode & 0x40u) return -EBUSY; /* Do not write during an unfinished reset. */
    if (mode != 0x83u) {
        result = write_byte(s, 9, 0x83);
        if (result) return result;
        const uint8_t stopped = 0x83;
        result = expect(s, 9, &stopped, 1);
        if (result) return result;
    }
    s->state = DREEM_OPTICAL_STOPPED;
    return 0;
}

static int failed(struct dreem_optical_sensor *s, int original) {
    s->last_error = original;
    s->fifo.needs_resync = 1;
    s->state = DREEM_OPTICAL_UNKNOWN;
    /* A failed identity read must not become a write to an unknown target. */
    s->cleanup_error = s->identified ? shutdown_sensor(s) : 0;
    return original;
}

int dreem_optical_sensor_start(struct dreem_optical_sensor *s,
                                const struct dreem_optical_profile *profile) {
    if (!s || !profile) return -EINVAL;
    if (s->state == DREEM_OPTICAL_RUNNING) return -EBUSY;
    if (profile->red_current_code > 255 || profile->infrared_current_code > 255)
        return -EINVAL;
    unsigned rate;
    switch (profile->samples_per_second) {
    case 50: rate = 0; break;
    case 100: rate = 1; break;
    case 200: rate = 2; break;
    case 400: rate = 3; break;
    default: return -EINVAL;
    }
    s->state = DREEM_OPTICAL_UNKNOWN;
    s->fifo.needs_resync = 1;
    s->identified = 0;
    s->revision = 0;
    s->last_error = s->cleanup_error = 0;
    uint8_t identity[2], status[2];
    int result = read_regs(s, 0xfe, identity, 2);
    if (result) return failed(s, result);
    if (identity[1] != 0x15) return failed(s, -ENODEV);
    s->identified = 1;
    s->revision = identity[0];
    /* Acknowledge any old power-ready event before reset/configuration. */
    result = read_regs(s, 0, status, 2);
    if (result) return failed(s, result);
    result = write_byte(s, 9, 0x40);
    if (result) return failed(s, result);
    for (unsigned poll = 0; ; ++poll) {
        uint8_t mode;
        result = read_regs(s, 9, &mode, 1);
        if (result) return failed(s, result);
        if (!(mode & 0x40u)) break;
        if (poll == 19) return failed(s, -ETIMEDOUT);
        const struct timespec delay = {.tv_sec = 0, .tv_nsec = 1000000};
        errno = 0;
        if (nanosleep(&delay, NULL) != 0)
            return failed(s, errno ? -errno : -EIO);
    }
    result = shutdown_sensor(s);
    if (result) return failed(s, result);

    const uint8_t interrupts[2] = {0, 0};
    const uint8_t fifo_config = 6;
    const uint8_t conversion = 0x43u | (rate << 2);
    const uint8_t currents[2] = {profile->red_current_code, profile->infrared_current_code};
    const uint8_t empty[3] = {0, 0, 0};
    const uint8_t configuration[3] = {fifo_config, 0x83, conversion};
    /* Configure while shut down. Mode 3 is selected before pointer writes. */
    if ((result = write_regs(s, 2, interrupts, 2)) ||
        (result = write_byte(s, 8, fifo_config)) ||
        (result = write_byte(s, 10, conversion)) ||
        (result = write_regs(s, 12, currents, 2)) ||
        (result = write_regs(s, 4, empty, 3)) ||
        (result = expect(s, 2, interrupts, 2)) ||
        (result = expect(s, 8, configuration, 3)) ||
        (result = expect(s, 12, currents, 2)) ||
        (result = expect(s, 4, empty, 3)) ||
        (result = read_regs(s, 0, status, 2)))
        return failed(s, result);
    if (status[0] & 1u) return failed(s, -ESTALE);

    const uint8_t active = 3;
    result = write_byte(s, 9, active);
    if (result) return failed(s, result);
    result = expect(s, 9, &active, 1);
    if (result) return failed(s, result);
    /* Reset + verified empty pointers established the byte boundary. */
    result = dreem_optical_fifo_init(&s->fifo, s->fifo.fd, s->fifo.address);
    if (result) return failed(s, result);
    s->state = DREEM_OPTICAL_RUNNING;
    return 0;
}

int dreem_optical_sensor_read(struct dreem_optical_sensor *s,
                               struct dreem_optical_batch *output) {
    if (!s || !output) return -EINVAL;
    if (s->state != DREEM_OPTICAL_RUNNING) {
        memset(output, 0, sizeof *output);
        output->flags = DREEM_OPTICAL_CONTINUITY_UNKNOWN | DREEM_OPTICAL_RESYNC_REQUIRED;
        return -EPIPE;
    }
    int result = dreem_optical_fifo_drain(&s->fifo, output);
    if (result && result != -EAGAIN) return failed(s, result);
    return result;
}

int dreem_optical_sensor_stop(struct dreem_optical_sensor *s) {
    if (!s) return -EINVAL;
    s->cleanup_error = 0;
    s->last_error = shutdown_sensor(s);
    return s->last_error;
}
