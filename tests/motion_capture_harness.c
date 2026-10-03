/* SPDX-License-Identifier: Apache-2.0
 * Replace only OS/device boundaries; capture, lifecycle, transport and ST code
 * run unchanged. Synthetic time/registers are not a physical sensor model.
 */
#define _DEFAULT_SOURCE
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <linux/i2c.h>
#include <linux/i2c-dev.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <time.h>
#include <unistd.h>

static unsigned char regs[256];
static uint64_t now = 1000000000, next_sample;
static unsigned transactions, writes, raw_reads, generated, clock_calls, output_calls, ever_active;
static const char *scenario;
static void check(int ok) { if (!ok) abort(); }
static int mode(const char *name) { return scenario && !strcmp(scenario, name); }
static int active(void) { return !!(regs[0x20] & 0x70); }
static void defaults(void) {
    memset(regs, 0, sizeof regs);
    regs[15] = mode("identity") ? 0 : 0x41;
    regs[0x20] = 7; regs[0x23] = 4;
}
__attribute__((constructor)) static void initialize(void) {
    scenario = getenv("DREEM_CAPTURE_CASE");
    defaults();
}
__attribute__((destructor)) static void report(void) {
    printf("{\"transactions\":%u,\"writes\":%u,\"raw_reads\":%u,\"generated\":%u,"
           "\"active\":%s,\"clock_calls\":%u}\n", transactions, writes, raw_reads,
           generated, active() ? "true" : "false", clock_calls);
}
static void advance(uint64_t interval) {
    static const unsigned rates[] = {0, 10, 50, 100, 200, 400, 800};
    now += interval;
    if (!active() || mode("not_ready")) return;
    unsigned code = (regs[0x20] >> 4) & 7;
    check(code < 7 && rates[code]);
    while (next_sample <= now) {
        ++generated;
        if (regs[0x27] & 8) regs[0x27] |= 0xf0;
        else {
            int values[] = {-30000 + (int)generated, 200 - (int)generated, -(int)generated};
            for (unsigned i = 0; i < 3; ++i) {
                regs[0x28 + 2*i] = (unsigned)values[i];
                regs[0x29 + 2*i] = (unsigned)values[i] >> 8;
            }
            regs[0x27] = 0x0f;
        }
        next_sample += 1000000000u / rates[code];
    }
}
#if defined(__USE_TIME_BITS64) && __TIMESIZE == 32
#define REAL_FCNTL __real___fcntl_time64
#define REAL_FSTAT __real___fstat64_time64
#else
#define REAL_FCNTL __real_fcntl
#define REAL_FSTAT __real_fstat
#endif
int REAL_FCNTL(int, int, ...);
int __wrap_fcntl(int fd, int command, ...) {
    check(command == F_GETFL);
    if (fd == 42) return mode("fd_readonly") ? O_RDONLY : O_RDWR;
    return REAL_FCNTL(fd, command);
}
int REAL_FSTAT(int, struct stat *);
int __wrap_fstat(int fd, struct stat *st) {
    if (fd != 42) return REAL_FSTAT(fd, st);
    memset(st, 0, sizeof *st);
    st->st_mode = S_IFCHR | 0600;
    st->st_rdev = makedev(mode("fd_wrong") ? 1 : 89, 3);
    return 0;
}
int __wrap_clock_gettime(clockid_t clock, struct timespec *t) {
    check(clock == CLOCK_MONOTONIC);
    ++clock_calls;
    if (mode("clock_error") && clock_calls == 3) { errno = EIO; return -1; }
    advance(1000);
    uint64_t value = now;
    if (mode("clock_backward") && clock_calls == 4) value -= 1000000000;
    t->tv_sec = (time_t)(value / 1000000000);
    t->tv_nsec = (long)(value % 1000000000);
    return 0;
}
int __wrap_nanosleep(const struct timespec *t, struct timespec *remaining) {
    check(!remaining && t->tv_sec == 0 && t->tv_nsec > 0 && t->tv_nsec < 1000000000);
    if (mode("wait_error") && ever_active) { errno = EIO; return -1; }
    advance((uint64_t)t->tv_nsec);
    if (mode("signal_wait") && ever_active) { raise(SIGINT); errno = EINTR; return -1; }
    return 0;
}
int __wrap_ioctl(int fd, unsigned long command, ...) {
    check(fd == 42);
    va_list ap; va_start(ap, command);
    if (command == I2C_FUNCS) {
        *va_arg(ap, unsigned long *) = I2C_FUNC_I2C; va_end(ap); return 0;
    }
    if (command == I2C_SLAVE) {
        unsigned long address = va_arg(ap, unsigned long);
        check(address == 29 || address == 30); va_end(ap); return 0;
    }
    check(command == I2C_RDWR);
    struct i2c_rdwr_ioctl_data *io = va_arg(ap, struct i2c_rdwr_ioctl_data *);
    va_end(ap);
    check(io->nmsgs == 1 || io->nmsgs == 2);
    unsigned writing = io->nmsgs == 1, reg = io->msgs[0].buf[0];
    unsigned size = writing ? io->msgs[0].len - 1u : io->msgs[1].len;
    unsigned char *data = writing ? io->msgs[0].buf + 1 : io->msgs[1].buf;
    check(size && size <= 7 && reg + size <= 256);
    ++transactions;
    if (writing) {
        ++writes; check(size == 1);
        if ((mode("start_error") && reg == 0x21) ||
            ((mode("stop_error") || mode("read_and_stop")) && reg == 0x20 &&
             ever_active && !(data[0] & 0x70))) {
            errno = mode("read_and_stop") ? ENXIO : EIO; return -1;
        }
        if (mode("signal_start") && reg == 0x21) raise(SIGINT);
        if (reg == 0x24 && (data[0] & 0x40)) defaults();
        else {
            int was_active = active(); regs[reg] = data[0];
            if (!was_active && active()) {
                static const unsigned rates[] = {0,10,50,100,200,400,800};
                unsigned code = (regs[0x20] >> 4) & 7;
                check(code < 7 && code);
                ever_active = 1;
                next_sample = now + 1000000000u / rates[code];
            }
        }
    } else {
        if (reg == 0x28) {
            ++raw_reads;
            if (raw_reads == 3) {
                if (mode("read_error") || mode("read_and_stop")) {
                    memset(data, 0xaa, size); errno = EIO; return -1;
                }
                if (mode("signal")) raise(SIGTERM);
                if (mode("late_read")) advance(500000000);
            }
        }
        memcpy(data, regs + reg, size);
        if (reg == 0x28 && size == 6) regs[0x27] = 0;
    }
    advance(1000);
    return writing ? 1 : 2;
}
ssize_t __real_write(int, const void *, size_t);
ssize_t __wrap_write(int fd, const void *data, size_t length) {
    if (fd > 2) {
        ++output_calls;
        if (mode("output_header") || (mode("output_sample") && output_calls >= 2)) {
            errno = ENOSPC; return -1;
        }
        if (mode("short_write") && length > 3) length = 3;
        if (mode("slow_output") && output_calls > 1 && active()) advance(60000000);
        if (mode("signal_header") && output_calls == 1) raise(SIGTERM);
    }
    return __real_write(fd, data, length);
}
int __real_fsync(int);
int __wrap_fsync(int fd) {
    if (mode("fsync_error")) { errno = EIO; return -1; }
    return __real_fsync(fd);
}
int __real_close(int);
int __wrap_close(int fd) {
    int result = __real_close(fd);
    if (mode("close_error") && fd > 2) { errno = EIO; return -1; }
    return result;
}
int __wrap___fcntl_time64(int, int, ...) __attribute__((alias("__wrap_fcntl")));
int __wrap___fstat64_time64(int, struct stat *) __attribute__((alias("__wrap_fstat")));
int __wrap___clock_gettime64(clockid_t, struct timespec *)
    __attribute__((alias("__wrap_clock_gettime")));
int __wrap___nanosleep64(const struct timespec *, struct timespec *)
    __attribute__((alias("__wrap_nanosleep")));
int __wrap___ioctl_time64(int, unsigned long, ...) __attribute__((alias("__wrap_ioctl")));
