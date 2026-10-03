/* SPDX-License-Identifier: Apache-2.0 */
#define main streamer_program_main
#include "streamer_under_test.c"
#undef main

static void check(int condition) { if (!condition) abort(); }
static frame_blob_t *frame(uint64_t sequence, size_t bytes) {
    uint8_t *data = malloc(bytes);
    check(data != NULL);
    for (size_t i = 0; i < bytes; ++i) data[i] = (uint8_t)(i * 37 + sequence);
    frame_blob_t *item = make_frame(FRAME_DATA, STREAM_EEG, 0, sequence, sequence * 125,
                                   250, 1, 4, FORMAT_FLOAT32_LE, data, (uint32_t)bytes,
                                   0, 1, 2);
    free(data);
    check(item != NULL);
    return item;
}
static void setup(streamer_t *state, size_t limit) {
    memset(state, 0, sizeof *state);
    state->client = 42; /* No real socket; eviction must leave this field alone. */
    state->queue.limit = limit;
}
static void release(streamer_t *state) {
    while (state->queue.head) {
        frame_blob_t *next = state->queue.head->next;
        free_frame(state->queue.head);
        state->queue.head = next;
    }
}
static unsigned validate(const streamer_t *state) {
    const frame_blob_t *item = state->queue.head, *last = NULL;
    size_t bytes = 0;
    unsigned count = 0;
    while (item) {
        check(++count < 1000);
        bytes += item->length;
        check(item->sent <= item->length);
        uint8_t header[HEADER_SIZE];
        memcpy(header, item->wire, HEADER_SIZE);
        uint32_t expected = (uint32_t)header[72] << 24 | (uint32_t)header[73] << 16 |
                            (uint32_t)header[74] << 8 | header[75];
        memset(header + 72, 0, 4);
        check(crc32_bytes(header, sizeof header) == expected);
        last = item; item = item->next;
    }
    check(bytes == state->queue.bytes && bytes <= state->queue.limit);
    check(last == state->queue.tail && state->client == 42);
    return count;
}
int main(void) {
    streamer_t state;
    setup(&state, 1024);
    check(!queue_drop_oldest_unsent(&state));
    check(validate(&state) == 0);

    check(queue_frame(&state, frame(1, 100)));
    check(queue_frame(&state, frame(2, 200)));
    check(queue_drop_oldest_unsent(&state));
    check(state.queue.head->sequence == 2 && state.queue.head == state.queue.tail);
    check(state.queue.dropped_frames == 1 && validate(&state) == 1);
    release(&state);

    setup(&state, 1024);
    check(queue_frame(&state, frame(1, 100)));
    state.queue.head->sent = 17;
    uint8_t saved[196];
    memcpy(saved, state.queue.head->wire, sizeof saved);
    for (uint64_t i = 2; i <= 200; ++i) {
        check(queue_frame(&state, frame(i, 120 + i % 3)));
        check(state.queue.head->sequence == 1 && state.queue.head->sent == 17);
        check(!memcmp(saved, state.queue.head->wire, sizeof saved));
        check(state.queue.tail->sequence == i);
        check(validate(&state) >= 2);
    }
    unsigned survivors = validate(&state);
    check(state.queue.dropped_frames == 200 - survivors);
    uint64_t repeated_drops = state.queue.dropped_frames;
    release(&state);

    setup(&state, 1024);
    check(queue_frame(&state, frame(1, 804)));
    state.queue.head->sent = 1;
    check(!queue_frame(&state, frame(2, 200)));
    check(state.queue.head->sequence == 1 && state.queue.head->sent == 1);
    check(state.queue.bytes == 900 && state.queue.dropped_frames == 1);
    check(validate(&state) == 1);
    release(&state);

    /* If no unsent frame can be evicted, read_stream must retain its row offset
     * so a later attempt can read those bytes again without losing a sample.
     */
    setup(&state, 4096);
    check(queue_frame(&state, frame(1, 3804)));
    state.queue.head->sent = 1;
    FILE *native = tmpfile();
    check(native != NULL);
    uint8_t rows[2000] = {0};
    check(fwrite(rows, 1, sizeof rows, native) == sizeof rows && !fflush(native));
    stream_source_t stream = {.id = STREAM_EEG, .name = "eeg", .filename = "eeg.data",
        .channels = 4, .format = FORMAT_FLOAT32_LE, .rate = 250, .row_bytes = 16,
        .batch_samples = 125, .fd = fileno(native)};
    check(!read_stream(&state, &stream, true));
    check(stream.offset == 0 && stream.sample_index == 0);
    check(state.queue.bytes == 3900 && validate(&state) == 1);
    fclose(native);
    release(&state);
    printf("{\"queue_cases\":5,\"repeated_evictions\":%" PRIu64 ","
           "\"partial_frame_immutable\":true,\"source_offset_retained\":true}\n", repeated_drops);
    return 0;
}
