/* SPDX-License-Identifier: Apache-2.0
 * Synthetic FIFO behind the actual checked Linux transport. No device access.
 */
#include "optical_fifo.h"
#include "optical_sensor.h"
#include "sensor_i2c.h"
#include <errno.h>
#include <linux/i2c.h>
#include <linux/i2c-dev.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>

static struct dreem_optical_fifo reader;
static struct dreem_optical_sensor sensor;
static unsigned wp, rp, level, lost, overflow, serial, status, high_bits;
static unsigned config[3], queue[32][2], latch[2], byte_position;
static unsigned step, total_steps, data_bytes, corrupt_step, corrupt_byte, corrupt_value;
static unsigned fail_step, partial_bytes, inject_step, inject_count;
static int fail_result, fail_errno;
static unsigned second_fail_step, second_partial;
static int second_result, second_errno;
static unsigned char registers[256];
static unsigned reset_delay, reset_pending, reset_reads, sleeps, wait_errno, writes;
static struct operation {
    unsigned reg, length, write;
    int result;
    unsigned char data[186];
} trace[128];

static void check(int ok) { if (!ok) abort(); }

static void produce(unsigned n) {
    if (config[1] != 3) return;
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

static void finish_reset(void) {
    memset(registers, 0, 0xfe);
    memset(config, 0, sizeof config);
    wp = rp = level = overflow = status = byte_position = reset_pending = 0;
}

static unsigned read_value(unsigned reg) {
    if (reg == 9 && (config[1] & 0x40u)) {
        reset_reads++;
        if (reset_pending != 0xffffffffu && reset_pending && --reset_pending == 0)
            finish_reset();
    }
    if (reg == 0) { unsigned v = status; status = 0; return v; }
    if (reg == 4) return wp;
    if (reg == 5) return overflow;
    if (reg == 6) return rp;
    if (reg >= 8 && reg <= 10) return config[reg-8];
    return registers[reg];
}

static void write_value(unsigned reg, unsigned value) {
    check(reg == 2 || reg == 3 || reg == 4 || reg == 5 || reg == 6 ||
          reg == 8 || reg == 9 || reg == 10 || reg == 12 || reg == 13);
    if (reg == 9 && (value & 0x40u)) {
        reset_pending = reset_delay;
        if (!reset_pending) finish_reset();
        else config[1] = 0x40;
        return;
    }
    if (reg >= 4 && reg <= 6) {
        check(config[1] == 0x83 && value < 32);
        if (reg == 4) wp = value;
        if (reg == 5) overflow = value;
        if (reg == 6) { rp = value; byte_position = 0; }
        level = (wp - rp) & 31u;
    } else if (reg >= 8 && reg <= 10) {
        config[reg-8] = value;
        if (reg == 9 && (value & 0x80u)) status = 0;
    } else registers[reg] = value;
}

int __wrap_nanosleep(const struct timespec *requested, struct timespec *remaining) {
    check(requested->tv_sec == 0 && requested->tv_nsec == 1000000 && !remaining);
    sleeps++;
    if (wait_errno) {
        errno = wait_errno; wait_errno = 0;
        return -1;
    }
    return 0;
}
int __wrap___nanosleep64(const struct timespec *, struct timespec *)
    __attribute__((alias("__wrap_nanosleep")));

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
    check((x->nmsgs == 1 || x->nmsgs == 2) && x->msgs[0].addr == 0x57 &&
          x->msgs[0].flags == 0);
    unsigned writing = x->nmsgs == 1;
    if (!writing) check(x->msgs[0].len == 1 && x->msgs[1].addr == 0x57 &&
                        x->msgs[1].flags == I2C_M_RD);
    unsigned reg = x->msgs[0].buf[0];
    unsigned n = writing ? x->msgs[0].len - 1u : x->msgs[1].len;
    unsigned char *p = writing ? x->msgs[0].buf + 1 : x->msgs[1].buf;
    check(n > 0 && n <= sizeof trace[0].data && step < 128);
    step++;
    total_steps++;
    if (step == inject_step) { produce(inject_count); inject_step = 0; }
    int failed = step == fail_step || step == second_fail_step;
    int result = writing ? 1 : 2, error = 0;
    unsigned use = n;
    if (step == fail_step) {
        use = partial_bytes; result = fail_result; error = fail_errno; fail_step = 0;
    } else if (step == second_fail_step) {
        use = second_partial; result = second_result; error = second_errno; second_fail_step = 0;
    }
    if (use > n) use = n;
    if (writing) {
        writes++;
        for (unsigned i = 0; i < use; ++i) write_value(reg+i, p[i]);
    } else if (reg == 7) {
        check(n > 0 && n <= 186 && n % 6 == 0);
        for (unsigned i = 0; i < use; ++i) p[i] = pop_byte();
    } else {
        check(reg + n <= 256);
        for (unsigned i = 0; i < use; ++i) p[i] = read_value(reg+i);
    }
    if (step == corrupt_step) {
        check(corrupt_byte < n);
        p[corrupt_byte] = corrupt_value;
        corrupt_step = 0;
    }
    trace[step-1] = (struct operation){.reg = reg, .length = n, .write = writing,
                                      .result = result};
    memcpy(trace[step-1].data, p, n);
    if (failed) errno = error;
    return result;
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

static void sensor_result(int result, const struct dreem_optical_batch *batch) {
    printf("{\"result\":%d,\"state\":%u,\"identified\":%u,\"revision\":%u,"
           "\"last_error\":%d,\"cleanup_error\":%d,\"quarantine\":%u,\"mode\":%u,"
           "\"level\":%u,\"lost\":%u,\"steps\":%u,\"writes\":%u,\"reset_reads\":%u,"
           "\"sleeps\":%u,\"data_bytes\":%u,\"count\":%u,\"flags\":%u,\"samples\":[",
           result, sensor.state, sensor.identified, sensor.revision, sensor.last_error,
           sensor.cleanup_error, sensor.fifo.needs_resync, config[1], level, lost,
           step, writes, reset_reads, sleeps, data_bytes, batch ? batch->count : 0,
           batch ? batch->flags : 0);
    if (batch) {
        check(batch->count <= DREEM_OPTICAL_BATCH_MAX);
        for (unsigned i = 0; i < batch->count; ++i)
            printf("%s[%u,%u]", i ? "," : "", batch->samples[i][0], batch->samples[i][1]);
        for (unsigned i = batch->count; i < DREEM_OPTICAL_BATCH_MAX; ++i)
            check(batch->samples[i][0] == 0 && batch->samples[i][1] == 0);
    }
    printf("],\"trace\":[");
    for (unsigned i = 0; i < step; ++i) {
        struct operation *t = &trace[i];
        printf("%s{\"op\":\"%c\",\"reg\":%u,\"result\":%d,\"data\":[",
               i ? "," : "", t->write ? 'W' : 'R', t->reg, t->result);
        for (unsigned j = 0; j < t->length; ++j) printf("%s%u", j ? "," : "", t->data[j]);
        printf("]}");
    }
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
            second_fail_step = reset_delay = reset_pending = reset_reads = sleeps = wait_errno = writes = 0;
            memset(registers, 0, sizeof registers);
            registers[0xfe] = 6; registers[0xff] = 0x15;
            check(dreem_optical_fifo_init(&reader, 42, 0x57) == 0);
            check(dreem_optical_sensor_init(&sensor, 42, 0x57) == 0);
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
        } else if (command == 'X') {
            check(scanf("%u%d%d%u", &second_fail_step, &second_result, &second_errno,
                        &second_partial) == 4);
        } else if (command == 'H') {
            check(scanf("%u%u", &reset_delay, &wait_errno) == 2);
        } else if (command == 'I') {
            unsigned part, revision;
            check(scanf("%u%u", &part, &revision) == 2 && part < 256 && revision < 256);
            registers[0xff] = part; registers[0xfe] = revision;
        } else if (command == 'S') {
            struct dreem_optical_profile profile;
            check(scanf("%u%u%u", &profile.red_current_code, &profile.infrared_current_code,
                        &profile.samples_per_second) == 3);
            step = 0;
            sensor_result(dreem_optical_sensor_start(&sensor, &profile), NULL);
        } else if (command == 'T') {
            step = 0;
            sensor_result(dreem_optical_sensor_stop(&sensor), NULL);
        } else if (command == 'B') {
            struct { uint32_t head; struct dreem_optical_batch batch; uint32_t tail; } b;
            memset(&b, 0xa5, sizeof b); step = 0;
            int result = dreem_optical_sensor_read(&sensor, &b.batch);
            check(b.head == 0xa5a5a5a5u && b.tail == 0xa5a5a5a5u);
            sensor_result(result, &b.batch);
        } else if (command == 'U') {
            finish_reset(); status = 1;
        } else if (command == 'W') {
            struct dreem_optical_sensor original = sensor;
            struct dreem_optical_profile profile = {10, 60, 100};
            struct dreem_optical_batch batch;
            check(dreem_optical_sensor_init(NULL, 42, 0x57) == -EINVAL);
            check(dreem_optical_sensor_init(&sensor, -1, 0x57) == -EBADF);
            check(dreem_optical_sensor_init(&sensor, 42, 0x78) == -EINVAL);
            check(memcmp(&original, &sensor, sizeof sensor) == 0);
            check(dreem_optical_sensor_start(NULL, &profile) == -EINVAL);
            check(dreem_optical_sensor_start(&sensor, NULL) == -EINVAL);
            check(dreem_optical_sensor_read(NULL, &batch) == -EINVAL);
            check(dreem_optical_sensor_read(&sensor, NULL) == -EINVAL);
            check(dreem_optical_sensor_stop(NULL) == -EINVAL);
            puts("{\"invalid_sensor_arguments\":8}");
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
