/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DREEM_SDMA_EEG_H
#define DREEM_SDMA_EEG_H

#ifdef __KERNEL__
#include <linux/types.h>
#else
#include <stdint.h>
#endif

#define SDMA_EEG_CONTEXT_WORDS 32
#define SDMA_EEG_RING_BYTES 1024
#define SDMA_EEG_RING_SLOTS 64

struct sdma_eeg_context_input {
    uint32_t pc, ring_phys, counter_phys;
    uint32_t registers[8];
};

/* Construct the recovered channel-1 context; no DMA or device operation.
 * The caller supplies an allocated ring and counter, coherent DMA mappings,
 * and a program address whose ownership and RAM bounds it has verified.
 * This checks encoding width, alignment, wrap and overlap, not RAM ownership.
 * r0-r2 are replaced by ring address, 1024, and counter address; r3-r7 are
 * retained. Returns 0 or -22; invalid inputs leave output untouched. */
int sdma_eeg_prepare_context(const struct sdma_eeg_context_input *input,
                             uint32_t *output, unsigned output_words);

struct sdma_eeg_progress {
    uint32_t counter, head, initialized;
    int fault;
};

/* Publish the new producer slot before notifying a consumer. The Linux
 * callback must supply the DMA/read and publication barriers and semaphore
 * wakeup. Calls and producer state must be serialized by the provider. */
typedef void (*sdma_eeg_notify_fn)(void *context, uint32_t head);

/* Counter is one coherent snapshot for this interrupt. First interrupt only
 * marks initialization, matching stock. Progress up to one ring is processed
 * in at most 64 callbacks, with unsigned 32-bit counter wrap.
 * Larger jumps latch -75 without publishing new frames; the Linux provider
 * must stop DMA and require a coordinated reset, not silently resume. A bound
 * per interrupt does not detect all overruns across interrupts or fix ring
 * races. Returns notification count, -75, or -22 for invalid arguments/state.
 * Zero-initialize state only while the provider and consumers are quiescent. */
int sdma_eeg_advance(struct sdma_eeg_progress *state, uint32_t counter,
                      sdma_eeg_notify_fn notify, void *context);

#endif
