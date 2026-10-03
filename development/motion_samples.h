/* SPDX-License-Identifier: Apache-2.0 */
#ifndef DREEM_MOTION_SAMPLES_H
#define DREEM_MOTION_SAMPLES_H
#include <stdint.h>

/* Six LIS2HH12 output bytes, XYZ little-endian signed counts, to the three
 * recorder axes in nominal g. Caller supplies buffers of at least 6/12 bytes.
 * This does not validate bus status or interpret an existing accelerometer.data
 * row, which already contains the converted floats. No hardware access occurs.
 */
void dreem_motion_decode(const uint8_t input[6], float output[3]);
#endif
