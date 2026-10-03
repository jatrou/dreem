/* SPDX-License-Identifier: Apache-2.0 */
#ifndef DREEM_SENSOR_I2C_H
#define DREEM_SENSOR_I2C_H
#include <stddef.h>
#include <stdint.h>

#define DREEM_I2C_MAX_DATA 256u

/* Caller owns an already-open Linux i2c-dev descriptor and exclusive access to
 * the target sensor. These functions do not open devices, acquire ownership
 * against other userspace programs, or retry potentially destructive transfers.
 * Seven-bit nonreserved addresses and 1..256 data bytes are supported.
 * Return 0 on complete success, otherwise a negative errno value.
 * A failed read leaves the caller's output unchanged, even after partial I/O.
 * The hardware may nevertheless have consumed data; the caller must resync it.
 */
int dreem_i2c_read_register(int fd, unsigned address, uint8_t reg,
                            void *output, size_t length);
int dreem_i2c_write_register(int fd, unsigned address, uint8_t reg,
                             const void *input, size_t length);
#endif
