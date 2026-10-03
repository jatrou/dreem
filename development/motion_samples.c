/* SPDX-License-Identifier: Apache-2.0
 * Independent implementation of the observed 4.7.11 motion coordinate format.
 * No original source or extracted instructions are included.
 */
#include "motion_samples.h"
#include <math.h>

static double signed_le(const uint8_t *p) {
    unsigned value = (unsigned)p[0] | ((unsigned)p[1] << 8);
    return value < 32768 ? (double)value : (double)((int)value - 65536);
}

void dreem_motion_decode(const uint8_t input[6], float output[3]) {
    const double cosine = 0x1.e11f642522d1cp-1;
    const double sine = 0x1.5e3a8748a0bf5p-2;
    const double scale = 0x1p-14;
    double x = signed_le(input), y = signed_le(input + 2), z = signed_le(input + 4);
    /* Explicit fused operations preserve the recorder's ARM VFP rounding. */
    output[0] = (float)(-(y * scale));
    output[1] = (float)(fma(x, cosine, -(sine * -z)) * scale);
    output[2] = (float)(fma(x, sine, -z * cosine) * scale);
}
