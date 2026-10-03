/* SPDX-License-Identifier: Apache-2.0
 * Link-time ioctl replacement: this executable never accesses an I2C device.
 * Check real host/ARM UAPI layouts and model partial kernel buffer writes.
 */
#include "sensor_i2c.h"
#include <errno.h>
#include <linux/i2c.h>
#include <linux/i2c-dev.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

static int operation, fd_value, address, reg_value, length;
static int funcs_result, funcs_errno, slave_result, slave_errno, transfer_result, transfer_errno;
static unsigned long function_bits;
static unsigned funcs_calls, slave_calls, transfer_calls;
static bool request_ok;

static unsigned char pattern(unsigned i) { return (unsigned char)(i * 37u + 11u); }

int __wrap_ioctl(int fd, unsigned long request, ...) {
    va_list args;
    va_start(args, request);
    request_ok &= fd == 42;
    int result;
    if (request == I2C_FUNCS) {
        unsigned long *functions = va_arg(args, unsigned long *);
        ++funcs_calls;
        *functions = function_bits;
        errno = funcs_errno;
        result = funcs_result;
    } else if (request == I2C_SLAVE) {
        unsigned long target = va_arg(args, unsigned long);
        ++slave_calls;
        request_ok &= target == (unsigned)address;
        errno = slave_errno;
        result = slave_result;
    } else if (request == I2C_RDWR) {
        struct i2c_rdwr_ioctl_data *transfer = va_arg(args, struct i2c_rdwr_ioctl_data *);
        ++transfer_calls;
        bool reading = !(operation & 1);
        request_ok &= transfer->nmsgs == (reading ? 2u : 1u);
        struct i2c_msg *first = transfer->msgs;
        request_ok &= first->addr == address && first->flags == 0 &&
                      first->len == (reading ? 1 : length + 1) && first->buf[0] == reg_value;
        if (reading && transfer->nmsgs == 2) {
            struct i2c_msg *second = first + 1;
            request_ok &= second->addr == address && second->flags == I2C_M_RD && second->len == length;
            /* A failing ioctl can still touch its userspace receive buffer. */
            unsigned n = transfer_result == 2 ? second->len : second->len < 3 ? second->len : 3;
            for (unsigned i = 0; i < n; ++i) second->buf[i] = pattern(i);
        } else if (!reading) {
            for (int i = 0; i < length; ++i) request_ok &= first->buf[i + 1] == pattern(i);
        }
        errno = transfer_errno;
        result = transfer_result;
    } else {
        request_ok = false;
        errno = ENOTTY;
        result = -1;
    }
    va_end(args);
    return result;
}

/* Current 32-bit glibc headers redirect ioctl through this time64 symbol. */
int __wrap___ioctl_time64(int fd, unsigned long request, ...)
    __attribute__((alias("__wrap_ioctl")));

int main(void) {
    int fields;
    while ((fields = scanf("%d %d %d %d %d %d %d %lu %d %d %d %d", &operation,
            &fd_value, &address, &reg_value, &length, &funcs_result, &funcs_errno,
            &function_bits, &slave_result, &slave_errno, &transfer_result, &transfer_errno)) == 12) {
        unsigned char output[260], input[260], before[260];
        memset(output, 0xa5, sizeof output);
        for (unsigned i = 0; i < sizeof input; ++i) input[i] = pattern(i);
        memcpy(before, input, sizeof input);
        funcs_calls = slave_calls = transfer_calls = 0;
        request_ok = true;
        int result = operation & 1 ?
            dreem_i2c_write_register(fd_value, (unsigned)address, (uint8_t)reg_value,
                                      operation >= 2 ? NULL : input, (size_t)length) :
            dreem_i2c_read_register(fd_value, (unsigned)address, (uint8_t)reg_value,
                                     operation >= 2 ? NULL : output + 2, (size_t)length);
        bool unchanged = true, output_ok = true, guards_ok = true;
        for (unsigned i = 0; i < sizeof output; ++i) {
            unchanged &= output[i] == 0xa5;
            bool inside = i >= 2 && length > 0 && i - 2 < (unsigned)length;
            if (!inside) guards_ok &= output[i] == 0xa5;
            else output_ok &= output[i] == pattern(i - 2);
        }
        printf("{\"result\":%d,\"funcs_calls\":%u,\"slave_calls\":%u,\"transfer_calls\":%u,"
               "\"request_ok\":%d,\"output_unchanged\":%d,\"output_ok\":%d,\"guards_ok\":%d,\"input_unchanged\":%d}\n",
               result, funcs_calls, slave_calls, transfer_calls, request_ok, unchanged,
               output_ok, guards_ok, !memcmp(input, before, sizeof input));
    }
    return fields == EOF && !ferror(stdin) && !fflush(stdout) ? 0 : 1;
}
