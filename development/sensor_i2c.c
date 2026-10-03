/* SPDX-License-Identifier: Apache-2.0
 * Independent checked Linux userspace register transport for sensor additions.
 */
#include "sensor_i2c.h"
#include <errno.h>
#include <linux/i2c.h>
#include <linux/i2c-dev.h>
#include <string.h>
#include <sys/ioctl.h>

static int syscall_error(int result) {
    return result < 0 && errno ? -errno : -EIO;
}

static int prepare(int fd, unsigned address, const void *data, size_t length) {
    if (fd < 0) return -EBADF;
    if (!data || !length || length > DREEM_I2C_MAX_DATA ||
        address < 0x08 || address > 0x77) return -EINVAL;
    unsigned long functions = 0;
    errno = 0;
    int result = ioctl(fd, I2C_FUNCS, &functions);
    if (result != 0) return syscall_error(result);
    if (!(functions & I2C_FUNC_I2C)) return -EOPNOTSUPP;
    /* Preserve the normal kernel-driver ownership check. I2C_RDWR alone does
     * not perform it. This still is not a lock against userspace clients.
     */
    errno = 0;
    result = ioctl(fd, I2C_SLAVE, (unsigned long)address);
    return result == 0 ? 0 : syscall_error(result);
}

int dreem_i2c_read_register(int fd, unsigned address, uint8_t reg,
                            void *output, size_t length) {
    int result = prepare(fd, address, output, length);
    if (result) return result;
    uint8_t received[DREEM_I2C_MAX_DATA] = {0};
    struct i2c_msg messages[2] = {
        {.addr = address, .flags = 0, .len = 1, .buf = &reg},
        {.addr = address, .flags = I2C_M_RD, .len = length, .buf = received}
    };
    struct i2c_rdwr_ioctl_data transfer = {.msgs = messages, .nmsgs = 2};
    errno = 0;
    result = ioctl(fd, I2C_RDWR, &transfer);
    if (result != 2) return result < 0 ? syscall_error(result) : -EREMOTEIO;
    memcpy(output, received, length);
    return 0;
}

int dreem_i2c_write_register(int fd, unsigned address, uint8_t reg,
                             const void *input, size_t length) {
    int result = prepare(fd, address, input, length);
    if (result) return result;
    uint8_t payload[DREEM_I2C_MAX_DATA + 1];
    payload[0] = reg;
    memcpy(payload + 1, input, length);
    struct i2c_msg message = {.addr = address, .flags = 0,
                             .len = length + 1, .buf = payload};
    struct i2c_rdwr_ioctl_data transfer = {.msgs = &message, .nmsgs = 1};
    errno = 0;
    result = ioctl(fd, I2C_RDWR, &transfer);
    return result == 1 ? 0 : result < 0 ? syscall_error(result) : -EREMOTEIO;
}
