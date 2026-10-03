/* SPDX-License-Identifier: Apache-2.0 */
#ifndef DREEM_OPTICAL_SAMPLES_H
#define DREEM_OPTICAL_SAMPLES_H
#include <stdint.h>

/* Decode the recorder's six raw FIFO bytes into two unsigned values. This
 * matches the original conversion, including preservation of all 24 bits.
 * Nominal MAX30101 ADC data uses 18 bits. This function does not validate
 * sensor health, mask unexpected upper bits, or access a sensor/FIFO.
 * Caller supplies nonoverlapping buffers of at least six/eight bytes.
 */
void dreem_optical_decode(const uint8_t input[6], uint32_t output[2]);
#endif
