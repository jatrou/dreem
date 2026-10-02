/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DREEM_ADS129X_INIT_H
#define DREEM_ADS129X_INIT_H

#include <stdint.h>

/* Transport must supply ordered MMIO accesses and exclusive ADC ownership.
 * No real hardware transport is supplied by this offline reconstruction. */
enum ads_io_operation {
    ADS_READ32, ADS_WRITE32, ADS_GPIO_OUTPUT, ADS_GPIO_SET,
    ADS_SLEEP_MS, ADS_SLEEP_US_RANGE
};
typedef uint32_t (*ads_io_fn)(void *context, enum ads_io_operation op,
                             uint32_t a, uint32_t b);
struct ads_transport {
    ads_io_fn io;
    void *context;
    unsigned poll_limit;
};

/* Reconstructs the nonzero-hardware-version SDMA open sequence.
 * Returns 0, -16 (GPIO/ID failure), -110 (bounded poll expired), or -22
 * (invalid transport). Does not start recording or install a Linux driver. */
int ads129x_sdma_initialize(const struct ads_transport *transport);

#endif
