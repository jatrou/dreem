/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DREEM_ADS129X_INIT_H
#define DREEM_ADS129X_INIT_H

#include <stdint.h>

/* Transport must supply ordered MMIO accesses and exclusive ADC ownership.
 * No real hardware transport is supplied by this offline reconstruction. */
enum ads_io_operation {
    ADS_READ32, ADS_WRITE32, ADS_GPIO_OUTPUT, ADS_GPIO_SET,
    ADS_SLEEP_MS, ADS_SLEEP_US_RANGE,
    ADS_QUEUE_HEAD, ADS_QUEUE_TRYLOCK
};
typedef uint32_t (*ads_io_fn)(void *context, enum ads_io_operation op,
                             uint32_t a, uint32_t b);
struct ads_transport {
    ads_io_fn io;
    void *context;
    unsigned poll_limit;
};

#define ADS_SDMA_RING_BYTES 1024
struct ads_sdma_state {
    uint8_t *ring; /* Exactly ADS_SDMA_RING_BYTES accessible bytes. */
    uint32_t read_offset;
    uint32_t errors;
};

/* Reconstructs the nonzero-hardware-version SDMA open sequence.
 * Returns 0, -16 (GPIO/ID failure), -110 (bounded poll expired), or -22
 * (invalid transport). Does not start recording or install a Linux driver. */
int ads129x_sdma_initialize(const struct ads_transport *transport);

/* After initialization, with exclusive ownership and a configured SDMA
 * channel. QUEUE_HEAD returns the producer slot (0..63); QUEUE_TRYLOCK returns
 * zero for a consumed notification, nonzero when empty. Both receive a=b=0.
 * State and ring must remain valid throughout the call. No transport for
 * physical hardware is supplied. Start/stop/release use bounded polling and
 * disable requests, power off, and deselect on timeout. Release powers off on
 * success too. Call initialization again after release or an error. */
int ads129x_sdma_start(const struct ads_transport *transport,
                      struct ads_sdma_state *state);
int ads129x_sdma_stop(const struct ads_transport *transport);
int ads129x_sdma_release(const struct ads_transport *transport);

#endif
