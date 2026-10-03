/* SPDX-License-Identifier: GPL-2.0-only */
/* Independent reconstruction of the reviewed SDMA context and IRQ progress.
 * This component is not yet a Linux DMA provider. See sdma-findings.md. */
#include "sdma_eeg.h"

int sdma_eeg_prepare_context(const struct sdma_eeg_context_input *input,
                             uint32_t *output, unsigned output_words)
{
    uint32_t words[SDMA_EEG_CONTEXT_WORDS];
    if (!input || !output || output_words < SDMA_EEG_CONTEXT_WORDS ||
        !input->pc || (input->pc & ~0x3fffu) ||
        !input->ring_phys || !input->counter_phys ||
        ((input->ring_phys | input->counter_phys) & 3) ||
        input->ring_phys > 0xffffffffu - (SDMA_EEG_RING_BYTES - 1) ||
        input->counter_phys - input->ring_phys < SDMA_EEG_RING_BYTES)
        return -22;
    for (unsigned i = 0; i < SDMA_EEG_CONTEXT_WORDS; ++i)
        words[i] = 0;
    words[0] = input->pc;
    words[2] = input->ring_phys;
    words[3] = SDMA_EEG_RING_BYTES;
    words[4] = input->counter_phys;
    for (unsigned i = 3; i < 8; ++i)
        words[i + 2] = input->registers[i];
    for (unsigned i = 0; i < SDMA_EEG_CONTEXT_WORDS; ++i)
        output[i] = words[i];
    return 0;
}

int sdma_eeg_advance(struct sdma_eeg_progress *state, uint32_t counter,
                      sdma_eeg_notify_fn notify, void *context)
{
    if (!state || !notify || state->head >= SDMA_EEG_RING_SLOTS ||
        state->initialized > 1 || (state->fault && state->fault != -75))
        return -22;
    if (state->fault)
        return state->fault;
    if (!state->initialized) {
        state->initialized = 1;
        return 0;
    }
    uint32_t count = counter - state->counter;
    if (count > SDMA_EEG_RING_SLOTS) {
        state->fault = -75;
        return -75;
    }
    for (unsigned i = 0; i < count; ++i) {
        state->head = (state->head + 1) & (SDMA_EEG_RING_SLOTS - 1);
        notify(context, state->head);
        ++state->counter;
    }
    return (int)count;
}
