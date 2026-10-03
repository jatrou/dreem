/* SPDX-License-Identifier: Apache-2.0 */
#ifndef DREEM_ALGO_EVENTS_H
#define DREEM_ALGO_EVENTS_H
#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint32_t sample_counter;
    uint8_t code, payload_size, payload[16];
} dreem_algo_event;

/* Decode one 4.7.11 algo.data event. Return its byte size, 0 for incomplete
 * input, or -1 for an unsupported code whose payload size is unknown.
 * The caller retains unconsumed input. Output is unchanged on 0/-1.
 * Input and output must be valid, nonoverlapping buffers. No I/O occurs. */
int dreem_algo_decode(const uint8_t *input, size_t size, dreem_algo_event *event);
#endif
