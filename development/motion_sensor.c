/* SPDX-License-Identifier: Apache-2.0
 * Owned LIS2HH12 lifecycle using the attributed ST platform-independent driver.
 */
#define _POSIX_C_SOURCE 200809L
#include "motion_sensor.h"
#include "sensor_i2c.h"
#include "lis2hh12_reg.h"
#include <errno.h>
#include <string.h>
#include <time.h>

static int32_t bus_read(void *handle, uint8_t reg, uint8_t *data, uint16_t n) {
    struct dreem_motion_sensor *s = handle;
    return dreem_i2c_read_register(s->fd, s->address, reg, data, n);
}
static int32_t bus_write(void *handle, uint8_t reg, const uint8_t *data, uint16_t n) {
    struct dreem_motion_sensor *s = handle;
    return dreem_i2c_write_register(s->fd, s->address, reg, data, n);
}
static stmdev_ctx_t context(struct dreem_motion_sensor *s) {
    return (stmdev_ctx_t){.read_reg=bus_read, .write_reg=bus_write, .handle=s};
}
static int write_byte(struct dreem_motion_sensor *s, uint8_t reg, uint8_t value) {
    return bus_write(s, reg, &value, 1);
}
static int expect(struct dreem_motion_sensor *s, uint8_t reg, const uint8_t *want, unsigned n) {
    uint8_t actual[7];
    int result = bus_read(s, reg, actual, n);
    return result ? result : memcmp(actual, want, n) ? -EPROTO : 0;
}
int dreem_motion_sensor_init(struct dreem_motion_sensor *s, int fd, unsigned address) {
    if (!s || fd < 0 || (address != 0x1e && address != 0x1d)) return -EINVAL;
    *s = (struct dreem_motion_sensor){.fd=fd, .address=address};
    return 0;
}
static int power_down(struct dreem_motion_sensor *s) {
    s->state = DREEM_MOTION_UNKNOWN;
    if (!s->identified) return -ENODEV;
    stmdev_ctx_t ctx = context(s);
    uint8_t reset, control;
    int result = lis2hh12_dev_reset_get(&ctx, &reset);
    if (result) return result;
    if (reset) return -EBUSY;
    /* Activity mode can override the requested ODR. Clear and verify it before
     * using ODR=0 as the power-down gate, including after configuration drift.
     */
    uint8_t activity;
    result = bus_read(s, 0x1e, &activity, 1);
    if (result) return result;
    if (activity) {
        const uint8_t zero = 0;
        result = write_byte(s, 0x1e, zero);
        if (result) return result;
        result = expect(s, 0x1e, &zero, 1);
        if (result) return result;
    }
    result = bus_read(s, 0x20, &control, 1);
    if (result) return result;
    if (control & 0x70u) {
        result = lis2hh12_xl_data_rate_set(&ctx, LIS2HH12_XL_ODR_OFF);
        if (result) return result;
        result = bus_read(s, 0x20, &control, 1);
        if (result) return result;
        if (control & 0x70u) return -EPROTO;
    }
    s->state = DREEM_MOTION_STOPPED;
    return 0;
}
static int failed(struct dreem_motion_sensor *s, int original) {
    s->last_error = original;
    s->state = DREEM_MOTION_UNKNOWN;
    s->cleanup_error = s->identified ? power_down(s) : 0;
    return original;
}
int dreem_motion_sensor_start(struct dreem_motion_sensor *s, const struct dreem_motion_profile *p) {
    if (!s || !p || p->high_resolution > 1) return -EINVAL;
    if (s->state == DREEM_MOTION_RUNNING) return -EBUSY;
    unsigned rate, scale;
    switch (p->samples_per_second) {
    case 10: rate=1; break; case 50: rate=2; break; case 100: rate=3; break;
    case 200: rate=4; break; case 400: rate=5; break; case 800: rate=6; break;
    default: return -EINVAL;
    }
    switch (p->full_scale_g) {
    case 2: scale=0; break; case 4: scale=2; break; case 8: scale=3; break;
    default: return -EINVAL;
    }
    s->identified = 0;
    s->state = DREEM_MOTION_UNKNOWN;
    s->last_error = s->cleanup_error = 0;
    s->settling_rows = 1;
    stmdev_ctx_t ctx = context(s);
    uint8_t identity;
    int result = lis2hh12_dev_id_get(&ctx, &identity);
    if (result) return failed(s, result);
    if (identity != LIS2HH12_ID) return failed(s, -ENODEV);
    s->identified = 1;
    result = lis2hh12_dev_reset_set(&ctx, 1);
    if (result) return failed(s, result);
    for (unsigned poll=0; ; ++poll) {
        uint8_t pending;
        result = lis2hh12_dev_reset_get(&ctx, &pending);
        if (result) return failed(s, result);
        if (!pending) break;
        if (poll == 19) return failed(s, -ETIMEDOUT);
        const struct timespec delay = {.tv_sec=0, .tv_nsec=1000000};
        errno = 0;
        if (nanosleep(&delay, NULL)) return failed(s, errno ? -errno : -EIO);
    }
    result = power_down(s);
    if (result) return failed(s, result);
    /* Set explicit bypass, no interrupts, activity switching, decimation or
     * self-test. Single-byte writes do not assume current auto-increment state.
     */
    const uint8_t base[7] = {(uint8_t)(7u | p->high_resolution << 7), 0, 0, 4, 0, 0, 0};
    if ((result = write_byte(s, 0x1e, 0)) || (result = write_byte(s, 0x1f, 0)) ||
        (result = write_byte(s, 0x2e, 0))) return failed(s, result);
    for (unsigned i=0; i<7; ++i)
        if ((result = write_byte(s, 0x20+i, base[i]))) return failed(s, result);
    if ((result = lis2hh12_block_data_update_set(&ctx, 1)) ||
        (result = lis2hh12_xl_full_scale_set(&ctx, (lis2hh12_xl_fs_t)scale)))
        return failed(s, result);
    memcpy(s->configuration, base, sizeof base);
    s->configuration[0] |= 8;
    s->configuration[3] |= scale << 4;
    const uint8_t zeros[2] = {0, 0};
    if ((result = expect(s, 0x20, s->configuration, 7)) ||
        (result = expect(s, 0x1e, zeros, 2)) || (result = expect(s, 0x2e, zeros, 1)))
        return failed(s, result);
    int16_t discard[3];
    result = lis2hh12_acceleration_raw_get(&ctx, discard);
    if (result) return failed(s, result);
    result = lis2hh12_xl_data_rate_set(&ctx, (lis2hh12_xl_data_rate_t)rate);
    if (result) return failed(s, result);
    s->configuration[0] |= rate << 4;
    result = expect(s, 0x20, s->configuration, 7);
    if (result) return failed(s, result);
    s->profile = *p;
    s->state = DREEM_MOTION_RUNNING;
    return 0;
}
int dreem_motion_sensor_read(struct dreem_motion_sensor *s, struct dreem_motion_sample *out) {
    if (!s || !out) return -EINVAL;
    if (s->state != DREEM_MOTION_RUNNING) return -EPIPE;
    struct dreem_motion_sample sample = {.flags=DREEM_MOTION_CONTINUITY_UNKNOWN};
    stmdev_ctx_t ctx = context(s);
    const uint8_t zeros[2] = {0, 0};
    int result;
    if ((result = expect(s, 0x20, s->configuration, 7)) ||
        (result = expect(s, 0x1e, zeros, 2)) || (result = expect(s, 0x2e, zeros, 1)) ||
        (result = bus_read(s, 0x27, &sample.status_before, 1))) return failed(s, result);
    if (!(sample.status_before & 8u)) return -EAGAIN;
    if ((result = lis2hh12_acceleration_raw_get(&ctx, sample.xyz)) ||
        (result = bus_read(s, 0x27, &sample.status_after, 1))) return failed(s, result);
    /* Recheck configuration before publishing; concurrent access is prohibited,
     * and these point-in-time checks cannot establish exclusive ownership.
     */
    result = expect(s, 0x20, s->configuration, 7);
    if (result) return failed(s, result);
    if (s->settling_rows) { s->settling_rows--; return -EAGAIN; }
    if ((sample.status_before | sample.status_after) & 0xf0u)
        sample.flags |= DREEM_MOTION_OVERRUN_OBSERVED;
    *out = sample;
    return 0;
}
int dreem_motion_sensor_stop(struct dreem_motion_sensor *s) {
    if (!s) return -EINVAL;
    s->cleanup_error = 0;
    s->last_error = power_down(s);
    return s->last_error;
}
