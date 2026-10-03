/* SPDX-License-Identifier: Apache-2.0
 * Independently implemented observed optical sample format.
 */
#include "optical_samples.h"

void dreem_optical_decode(const uint8_t input[6], uint32_t output[2]) {
    for (unsigned channel = 0; channel < 2; ++channel) {
        const uint8_t *p = input + 3 * channel;
        output[channel] = ((uint32_t)p[0] << 16) |
                          ((uint32_t)p[1] << 8) | (uint32_t)p[2];
    }
}
