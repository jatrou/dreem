/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DREEM_ADS129X_INIT_H
#define DREEM_ADS129X_INIT_H

#ifdef __KERNEL__
#include <linux/types.h>
#else
#include <stdint.h>
#endif

/* Transport must supply ordered MMIO accesses and exclusive ADC ownership.
 * The Linux research adapter remains unqualified for physical deployment. */
enum ads_io_operation {
    ADS_READ32, ADS_WRITE32, ADS_GPIO_OUTPUT, ADS_GPIO_SET,
    ADS_SLEEP_MS, ADS_SLEEP_US_RANGE,
    ADS_QUEUE_HEAD, ADS_QUEUE_TRYLOCK, ADS_QUEUE_WAIT
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
 * State and ring must remain valid throughout the call.
 * Start/stop/release use bounded polling and
 * disable requests, power off, and deselect on timeout. Release powers off on
 * success too. Call initialization again after release or an error. */
int ads129x_sdma_start(const struct ads_transport *transport,
                      struct ads_sdma_state *state);
int ads129x_sdma_stop(const struct ads_transport *transport);
int ads129x_sdma_release(const struct ads_transport *transport);

/* Consume one notification and one frame from the configured ring. QUEUE_WAIT
 * receives a=b=0 and returns zero after acquiring a notification, nonzero on
 * interruption (up to five attempts). Its blocking/timeout policy belongs to
 * the transport. Successful reads return 16; bytes 13..15 are always zero.
 * Returns -1 for an all-placeholder ring, -2 for invalid frame status, -3 for
 * five interrupted waits, or -22 for invalid arguments/state. Unlike the
 * original driver, short outputs and malformed frames after placeholders are
 * rejected. The output remains untouched unless a full frame is returned.
 * Userspace-copy and Linux file operations belong to the separate adapter. */
int ads129x_sdma_read_frame(const struct ads_transport *transport,
                           struct ads_sdma_state *state,
                           uint8_t *output, unsigned output_size);

#endif
