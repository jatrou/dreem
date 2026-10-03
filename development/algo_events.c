/* SPDX-License-Identifier: Apache-2.0
 * Independent parser for the observed 4.7.11 event framing, not vendor code.
 */
#include "algo_events.h"
#include <string.h>

int dreem_algo_decode(const uint8_t *input, size_t size, dreem_algo_event *event) {
    if (size < 5) return 0;
    unsigned payload;
    switch (input[4]) {
    case 1: payload = 16; break;
    case 2: case 3: case 15: case 23: case 28: case 32: case 33:
        payload = 0; break;
    case 18: case 19: case 24: case 34:
        payload = 1; break;
    case 13: case 14: case 16: case 17: case 20: case 21: case 22:
    case 25: case 26: case 30: case 31: case 35: case 36:
        payload = 4; break;
    case 27: case 29: payload = 8; break;
    case 37: payload = 12; break;
    default: return -1;
    }
    if (size < 5 + payload) return 0;
    memset(event, 0, sizeof *event);
    event->sample_counter = (uint32_t)input[0] | ((uint32_t)input[1] << 8) |
                            ((uint32_t)input[2] << 16) | ((uint32_t)input[3] << 24);
    event->code = input[4];
    event->payload_size = (uint8_t)payload;
    memcpy(event->payload, input + 5, payload);
    return (int)(5 + payload);
}
