/* SPDX-License-Identifier: Apache-2.0
 * Synthetic FIFO behind the actual checked Linux transport. No device access.
 */
#include "optical_fifo.h"
#include "sensor_i2c.h"
#include <errno.h>
#include <linux/i2c.h>
#include <linux/i2c-dev.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>

static struct dreem_optical_fifo reader;
static unsigned wp, rp, level, lost, overflow, serial, status, high_bits;
static unsigned config[3], queue[32][2], latch[2], byte_position;
static unsigned step, total_steps, data_bytes, corrupt_step, corrupt_byte, corrupt_value;
static unsigned fail_step, partial_bytes, inject_step, inject_count;
static int fail_result, fail_errno;

static void check(int ok) { if (!ok) abort(); }

static void produce(unsigned n) {
    while (n--) {
        unsigned value = serial++;
        if (level == 32) {
            lost++;
            if (overflow < 31) overflow++;
            continue;
        }
        queue[wp][0] = (value & 0x3ffffu) | (high_bits ? 0x800000u : 0);
        queue[wp][1] = (value ^ 0x15555u) & 0x3ffffu;
        wp = (wp + 1) & 31u;
        level++;
        if (level >= 32u - (config[0] & 15u)) status |= 0x80;
        status |= 0x40;
    }
}

static unsigned char pop_byte(void) {
    if (!byte_position) {
        check(level > 0);
        latch[0] = queue[rp][0];
        latch[1] = queue[rp][1];
        rp = (rp + 1) & 31u;
        level--;
        overflow = 0;
    }
    unsigned value = latch[byte_position / 3];
    unsigned shift = (2u - byte_position % 3u) * 8u;
    byte_position = (byte_position + 1) % 6u;
    data_bytes++;
    status &= ~0x40u;
    return value >> shift;
}

int __wrap_ioctl(int fd, unsigned long request, ...) {
    va_list ap;
    va_start(ap, request);
    check(fd == 42);
    if (request == I2C_FUNCS) {
        *va_arg(ap, unsigned long *) = I2C_FUNC_I2C;
        va_end(ap);
        return 0;
    }
    if (request == I2C_SLAVE) {
        check(va_arg(ap, unsigned long) == 0x57);
        va_end(ap);
        return 0;
    }
    check(request == I2C_RDWR);
    struct i2c_rdwr_ioctl_data *x = va_arg(ap, struct i2c_rdwr_ioctl_data *);
    va_end(ap);
    check(x->nmsgs == 2 && x->msgs[0].addr == 0x57 && x->msgs[1].addr == 0x57);
    check(x->msgs[0].flags == 0 && x->msgs[0].len == 1 &&
          x->msgs[1].flags == I2C_M_RD);
    unsigned reg = x->msgs[0].buf[0], n = x->msgs[1].len;
    unsigned char *p = x->msgs[1].buf;
    step++;
    total_steps++;
    if (step == inject_step) { produce(inject_count); inject_step = 0; }
    unsigned use = step == fail_step ? partial_bytes : n;
    if (use > n) use = n;
    if (reg == 7) {
        check(n > 0 && n <= 186 && n % 6 == 0);
        for (unsigned i = 0; i < use; ++i) p[i] = pop_byte();
    } else {
        unsigned values[3] = {0};
        if (reg == 8) {
            check(n == 3);
            memcpy(values, config, sizeof values);
        } else if (reg == 0) {
            check(n == 1);
            values[0] = status;
            if (use) status = 0;
        } else {
            check(reg == 4 && n == 3);
            values[0] = wp; values[1] = overflow; values[2] = rp;
        }
        for (unsigned i = 0; i < use; ++i) p[i] = values[i];
    }
    if (step == corrupt_step) {
        check(corrupt_byte < n);
        p[corrupt_byte] = corrupt_value;
        corrupt_step = 0;
    }
    if (step == fail_step) {
        fail_step = 0;
        errno = fail_errno;
        return fail_result;
    }
    return 2;
}
int __wrap___ioctl_time64(int, unsigned long, ...)
    __attribute__((alias("__wrap_ioctl")));

static void drain(void) {
    struct { uint32_t head; struct dreem_optical_batch batch; uint32_t tail; } b;
    memset(&b, 0xa5, sizeof b);
    step = 0;
    int result = dreem_optical_fifo_drain(&reader, &b.batch);
    check(b.head == 0xa5a5a5a5u && b.tail == 0xa5a5a5a5u);
    check(b.batch.count <= DREEM_OPTICAL_BATCH_MAX);
    for (unsigned i = b.batch.count; i < DREEM_OPTICAL_BATCH_MAX; ++i)
        check(b.batch.samples[i][0] == 0 && b.batch.samples[i][1] == 0);
    printf("{\"result\":%d,\"count\":%u,\"flags\":%u,\"quarantine\":%u,"
           "\"level\":%u,\"lost\":%u,\"overflow\":%u,\"read\":%u,\"write\":%u,"
           "\"steps\":%u,\"total_steps\":%u,\"data_bytes\":%u,\"status\":%u,"
           "\"config\":[%u,%u,%u],\"before\":[%u,%u,%u],\"after\":[%u,%u,%u],\"samples\":[",
           result, b.batch.count, b.batch.flags, reader.needs_resync,
           level, lost, overflow, rp, wp, step, total_steps, data_bytes, b.batch.status,
           b.batch.configuration[0], b.batch.configuration[1], b.batch.configuration[2],
           b.batch.before[0], b.batch.before[1], b.batch.before[2],
           b.batch.after[0], b.batch.after[1], b.batch.after[2]);
    for (unsigned i = 0; i < b.batch.count; ++i)
        printf("%s[%u,%u]", i ? "," : "", b.batch.samples[i][0], b.batch.samples[i][1]);
    puts("]}");
}

int main(void) {
    char command;
    while (scanf(" %c", &command) == 1) {
        if (command == 'N') {
            unsigned n;
            check(scanf("%u%u%u%u%u%u%u", &rp, &n, &config[0], &config[1],
                        &config[2], &status, &high_bits) == 7 && rp < 32);
            wp = rp;
            level = lost = overflow = serial = byte_position = 0;
            step = total_steps = data_bytes = fail_step = inject_step = corrupt_step = 0;
            check(dreem_optical_fifo_init(&reader, 42, 0x57) == 0);
            produce(n);
        } else if (command == 'P') {
            unsigned n; check(scanf("%u", &n) == 1); produce(n);
        } else if (command == 'D') {
            drain();
        } else if (command == 'F') {
            check(scanf("%u%d%d%u", &fail_step, &fail_result, &fail_errno,
                        &partial_bytes) == 4);
        } else if (command == 'J') {
            check(scanf("%u%u", &inject_step, &inject_count) == 2);
        } else if (command == 'R') {
            check(scanf("%u%u%u", &corrupt_step, &corrupt_byte, &corrupt_value) == 3);
        } else if (command == 'L') {
            unsigned char raw[6]; step = 0;
            check(dreem_i2c_read_register(42, 0x57, 7, raw, 6) == 0);
            printf("{\"level\":%u,\"lost\":%u,\"data_bytes\":%u}\n", level, lost, data_bytes);
        } else if (command == 'V') {
            struct dreem_optical_fifo original = reader;
            struct dreem_optical_batch batch;
            check(dreem_optical_fifo_init(NULL, 42, 0x57) == -EINVAL);
            check(dreem_optical_fifo_init(&reader, -1, 0x57) == -EBADF);
            check(dreem_optical_fifo_init(&reader, 42, 7) == -EINVAL);
            check(dreem_optical_fifo_init(&reader, 42, 0x78) == -EINVAL);
            check(memcmp(&original, &reader, sizeof reader) == 0);
            check(dreem_optical_fifo_drain(NULL, &batch) == -EINVAL);
            check(dreem_optical_fifo_drain(&reader, NULL) == -EINVAL);
            puts("{\"invalid_arguments\":6}");
        } else abort();
    }
    return ferror(stdin) || fflush(stdout) ? 1 : 0;
}
