/* SPDX-License-Identifier: GPL-2.0-only */
/* Independent reconstruction from observed firmware interfaces and TI's
 * ADS129x register definitions. See adc-findings.md for evidence and limits. */
#include "ads129x_init.h"

#define SPI_BASE 0x02008000u
#define SPI_RX (SPI_BASE + 0)
#define SPI_TX (SPI_BASE + 4)
#define SPI_CONTROL (SPI_BASE + 8)
#define SPI_CONFIG (SPI_BASE + 12)
#define SPI_DMA (SPI_BASE + 20)
#define SPI_STATUS (SPI_BASE + 24)
#define SPI_CLOCK 0x020c406cu
#define SDMA_EVENT 0x020ec20cu
#define POWER_GPIO 35
#define CS_GPIO 90

static uint32_t io(const struct ads_transport *t, enum ads_io_operation op,
                   uint32_t a, uint32_t b)
{
    return t->io(t->context, op, a, b);
}

static int valid_transport(const struct ads_transport *t)
{
    return t && t->io && t->poll_limit && t->poll_limit <= 1000000;
}

static uint32_t read32(const struct ads_transport *t, uint32_t address)
{
    return io(t, ADS_READ32, address, 0);
}

static void write32(const struct ads_transport *t, uint32_t address, uint32_t value)
{
    io(t, ADS_WRITE32, address, value);
}

static int wait_status(const struct ads_transport *t, uint32_t mask)
{
    for (unsigned i = 0; i < t->poll_limit; ++i)
        if (read32(t, SPI_STATUS) & mask)
            return 0;
    return -110;
}

static int flush(const struct ads_transport *t)
{
    for (unsigned i = 0; i < t->poll_limit; ++i) {
        if (!(read32(t, SPI_STATUS) & 8))
            return 0;
        (void)read32(t, SPI_RX);
    }
    return -110;
}

static int command_mode(const struct ads_transport *t)
{
    write32(t, SDMA_EVENT, 0);
    if (flush(t))
        return -110;
    uint32_t control = read32(t, SPI_CONTROL);
    if (!(control & 1))
        write32(t, SPI_CONTROL, control | 1);
    else if (wait_status(t, 0x80))
        return -110;
    io(t, ADS_SLEEP_MS, 10, 0);
    write32(t, SPI_CONTROL, 0x70f3f9);
    write32(t, SPI_CONFIG, 0xf);
    return flush(t);
}

static int exchange(const struct ads_transport *t, const uint8_t *bytes,
                    unsigned count, uint32_t *last)
{
    io(t, ADS_GPIO_SET, CS_GPIO, 0);
    io(t, ADS_SLEEP_US_RANGE, 100, 200);
    for (unsigned i = 0; i < count; ++i)
        write32(t, SPI_TX, bytes[i]);
    if (wait_status(t, 1)) {
        io(t, ADS_GPIO_SET, CS_GPIO, 1);
        return -110;
    }
    io(t, ADS_SLEEP_US_RANGE, 100, 200);
    io(t, ADS_GPIO_SET, CS_GPIO, 1);
    for (unsigned i = 0; i < count; ++i)
        *last = read32(t, SPI_RX);
    return 0;
}

static int command(const struct ads_transport *t, uint8_t value)
{
    uint32_t ignored;
    return exchange(t, &value, 1, &ignored);
}

static int register_access(const struct ads_transport *t, uint8_t opcode,
                           uint8_t value, uint32_t *last)
{
    const uint8_t bytes[] = {opcode, 0, value};
    if (command(t, 0x11))
        return -110;
    return exchange(t, bytes, 3, last);
}

int ads129x_sdma_initialize(const struct ads_transport *t)
{
    static const uint8_t settings[][2] = {
        {1, 6}, {2, 0xc0}, {3, 0xec}, {0x17, 0}, {4, 0},
        {0xf, 0}, {0x10, 0}, {5, 0x10}, {6, 0x10},
        {7, 0x10}, {8, 0x10}
    };
    int result = -16;
    uint32_t last = 0;
    if (!valid_transport(t))
        return -22;
    write32(t, SPI_CLOCK, read32(t, SPI_CLOCK) | 3);
    if (command_mode(t)) {
        result = -110;
        goto cleanup;
    }
    if ((int32_t)io(t, ADS_GPIO_OUTPUT, POWER_GPIO, 0) != 0 ||
        (int32_t)io(t, ADS_GPIO_OUTPUT, CS_GPIO, 1) != 0)
        goto cleanup;
    io(t, ADS_SLEEP_MS, 100, 0);
    for (unsigned i = 0; i < 3; ++i) {
        io(t, ADS_GPIO_SET, POWER_GPIO, (i & 1) ^ 1);
        io(t, ADS_SLEEP_MS, 100, 0);
    }
    if (command(t, 0x0a)) {
        result = -110;
        goto cleanup;
    }
    io(t, ADS_SLEEP_MS, 200, 0);
    if (command(t, 0x06)) {
        result = -110;
        goto cleanup;
    }
    io(t, ADS_SLEEP_MS, 10, 0);
    if (flush(t)) {
        result = -110;
        goto cleanup;
    }
    for (unsigned attempt = 0; attempt < 3; ++attempt) {
        if (register_access(t, 0x20, 0, &last)) {
            result = -110;
            goto cleanup;
        }
        if ((last & 0x1f) != 0x10 && (last & 0x1f) != 0x11)
            continue;
        for (unsigned i = 0; i < sizeof(settings) / sizeof(settings[0]); ++i)
            if (register_access(t, 0x40 | settings[i][0], settings[i][1], &last)) {
                result = -110;
                goto cleanup;
            }
        return 0;
    }
cleanup:
    (void)command(t, 0x0a);
    io(t, ADS_GPIO_SET, POWER_GPIO, 0);
    io(t, ADS_GPIO_SET, CS_GPIO, 1);
    return result;
}

static int power_off_error(const struct ads_transport *t, int result)
{
    write32(t, SDMA_EVENT, 0);
    io(t, ADS_GPIO_SET, POWER_GPIO, 0);
    io(t, ADS_GPIO_SET, CS_GPIO, 1);
    return result;
}

int ads129x_sdma_test_signal(const struct ads_transport *t)
{
    static const uint8_t registers[] = {2, 5, 6, 7, 8};
    uint32_t ignored;
    if (!valid_transport(t))
        return -22;
    for (unsigned i = 0; i < sizeof(registers); ++i)
        if (register_access(t, 0x40 | registers[i], 0x15, &ignored))
            return power_off_error(t, -110);
    return 0;
}

int ads129x_sdma_start(const struct ads_transport *t, struct ads_sdma_state *state)
{
    if (!valid_transport(t) || !state || !state->ring)
        return -22;
    state->errors = 0;
    if (command(t, 0x08) || command(t, 0x10))
        return power_off_error(t, -110);
    io(t, ADS_GPIO_SET, CS_GPIO, 0);
    for (unsigned i = 0; i < ADS_SDMA_RING_BYTES; ++i)
        state->ring[i] = 0x42;
    uint32_t head = io(t, ADS_QUEUE_HEAD, 0, 0);
    if (head >= ADS_SDMA_RING_BYTES / 16)
        return power_off_error(t, -22);
    state->read_offset = ((head + 63) & 63) * 16;
    unsigned attempts;
    for (attempts = 0; attempts < t->poll_limit; ++attempts)
        if (io(t, ADS_QUEUE_TRYLOCK, 0, 0))
            break;
    if (attempts == t->poll_limit || flush(t))
        return power_off_error(t, -110);
    uint32_t control = read32(t, SPI_CONTROL);
    if (!(control & 1))
        write32(t, SPI_CONTROL, control | 1);
    else if (wait_status(t, 0x80))
        return power_off_error(t, -110);
    write32(t, SPI_CONTROL, 0x077170f9);
    write32(t, SPI_CONFIG, 0xf);
    write32(t, SPI_DMA, 0x830000);
    if (flush(t))
        return power_off_error(t, -110);
    for (unsigned i = 0; i < 4; ++i)
        write32(t, SPI_TX, 0);
    write32(t, SDMA_EVENT, 2);
    return 0;
}

int ads129x_sdma_stop(const struct ads_transport *t)
{
    if (!valid_transport(t))
        return -22;
    if (command_mode(t))
        return power_off_error(t, -110);
    io(t, ADS_GPIO_SET, CS_GPIO, 1);
    io(t, ADS_SLEEP_MS, 10, 0);
    io(t, ADS_GPIO_SET, CS_GPIO, 0);
    io(t, ADS_SLEEP_MS, 10, 0);
    if (command(t, 0x11))
        return power_off_error(t, -110);
    io(t, ADS_SLEEP_MS, 10, 0);
    if (command(t, 0x0a))
        return power_off_error(t, -110);
    io(t, ADS_SLEEP_MS, 100, 0);
    io(t, ADS_GPIO_SET, CS_GPIO, 1);
    return 0;
}

int ads129x_sdma_release(const struct ads_transport *t)
{
    if (!valid_transport(t))
        return -22;
    write32(t, SDMA_EVENT, 0);
    if (command_mode(t) || command(t, 0x0a))
        return power_off_error(t, -110);
    io(t, ADS_GPIO_SET, POWER_GPIO, 0);
    io(t, ADS_GPIO_SET, CS_GPIO, 1);
    return 0;
}

static void next_frame(struct ads_sdma_state *state)
{
    state->read_offset = (state->read_offset + 16) & (ADS_SDMA_RING_BYTES - 1);
}

static int placeholder(const uint8_t *frame)
{
    return frame[0] == 0x42 && frame[1] == 0x42 && frame[2] == 0x42;
}

int ads129x_sdma_read_frame(const struct ads_transport *t,
                           struct ads_sdma_state *state,
                           uint8_t *output, unsigned output_size)
{
    static const uint8_t order[12] = {7, 6, 5, 4, 11, 10, 9, 8, 15, 14, 13, 12};
    if (!valid_transport(t) || !state || !state->ring || !output || output_size < 16 ||
        state->read_offset >= ADS_SDMA_RING_BYTES || (state->read_offset & 15))
        return -22;
    unsigned attempt;
    for (attempt = 0; attempt < 5; ++attempt)
        if (!io(t, ADS_QUEUE_WAIT, 0, 0))
            break;
    if (attempt == 5)
        return -3;
    const uint8_t *frame = state->ring + state->read_offset;
    if (placeholder(frame)) {
        unsigned remaining = 66;
        do {
            next_frame(state);
            if (!--remaining) {
                ++state->errors;
                return -1;
            }
            frame = state->ring + state->read_offset;
        } while (placeholder(frame));
    }
    if ((frame[0] & 0xf0) || frame[1] || frame[2] != 0xc0) {
        next_frame(state);
        ++state->errors;
        return -2;
    }
    uint8_t record[16] = {0};
    for (unsigned i = 0; i < sizeof(order); ++i)
        record[i] = frame[order[i]];
    uint32_t head = io(t, ADS_QUEUE_HEAD, 0, 0);
    if (head >= ADS_SDMA_RING_BYTES / 16)
        return -22;
    record[12] = (head + 64 - state->read_offset / 16) & 63;
    for (unsigned i = 0; i < sizeof(record); ++i)
        output[i] = record[i];
    next_frame(state);
    return 16;
}
