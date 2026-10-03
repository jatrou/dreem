/* SPDX-License-Identifier: Apache-2.0
 * Independent bounded optical FIFO reader; no sensor lifecycle or bus probing.
 */
#include "optical_fifo.h"
#include "optical_samples.h"
#include "sensor_i2c.h"
#include <errno.h>
#include <string.h>

int dreem_optical_fifo_init(struct dreem_optical_fifo *fifo,
                             int fd, unsigned address) {
    if (!fifo) return -EINVAL;
    if (fd < 0) return -EBADF;
    if (address < 0x08 || address > 0x77) return -EINVAL;
    *fifo = (struct dreem_optical_fifo){.fd = fd, .address = address};
    return 0;
}

static int quarantine(struct dreem_optical_fifo *fifo,
                      struct dreem_optical_batch *output, int error) {
    fifo->needs_resync = 1;
    output->flags |= DREEM_OPTICAL_RESYNC_REQUIRED;
    return error;
}

static int pointers_valid(const uint8_t p[3]) {
    return !((p[0] | p[1] | p[2]) & 0xe0u);
}

int dreem_optical_fifo_drain(struct dreem_optical_fifo *fifo,
                              struct dreem_optical_batch *output) {
    if (!fifo || !output) return -EINVAL;
    memset(output, 0, sizeof *output);
    output->flags = DREEM_OPTICAL_CONTINUITY_UNKNOWN;
    if (fifo->needs_resync)
        return quarantine(fifo, output, -ESTALE);

    int result = dreem_i2c_read_register(fifo->fd, fifo->address, 8,
                                         output->configuration, 3);
    if (result) return quarantine(fifo, output, result);
    output->flags |= DREEM_OPTICAL_CONFIG_VALID;
    /* Mode must be exactly active red/IR mode; no reset, shutdown or reserved
     * bits. Rollover can replace the oldest sample during a snapshot. Reject it.
     * Keep averaging/rate/width in the diagnostics without assigning timestamps.
     */
    if (output->configuration[1] != 3 ||
        (output->configuration[0] & 0x10u) ||
        (output->configuration[2] & 0x80u))
        return quarantine(fifo, output, -EOPNOTSUPP);

    result = dreem_i2c_read_register(fifo->fd, fifo->address, 0,
                                      &output->status, 1);
    if (result) return quarantine(fifo, output, result);
    output->flags |= DREEM_OPTICAL_STATUS_VALID;
    if (output->status & 1u) /* Power-ready indicates a reset/power event. */
        return quarantine(fifo, output, -ESTALE);

    result = dreem_i2c_read_register(fifo->fd, fifo->address, 4,
                                      output->before, 3);
    if (result) return quarantine(fifo, output, result);
    output->flags |= DREEM_OPTICAL_BEFORE_VALID;
    if (!pointers_valid(output->before))
        return quarantine(fifo, output, -EPROTO);
    if (output->before[1])
        return quarantine(fifo, output, -EOVERFLOW);
    unsigned count = (output->before[0] - output->before[2]) & 31u;
    if (!count) {
        output->flags |= DREEM_OPTICAL_POINTERS_EQUAL;
        if (output->status & 0x80u)
            return quarantine(fifo, output, -EOVERFLOW);
        return -EAGAIN;
    }

    uint8_t raw[DREEM_OPTICAL_BATCH_MAX * 6u];
    result = dreem_i2c_read_register(fifo->fd, fifo->address, 7, raw, count * 6u);
    if (result) return quarantine(fifo, output, result);
    /* A failed/partial transfer can advance framing. Even a reported successful
     * transfer must advance the read pointer as expected before publishing data.
     * This check cannot prove absence of kernel retries or unobserved overflow.
     */
    result = dreem_i2c_read_register(fifo->fd, fifo->address, 4,
                                      output->after, 3);
    if (result) return quarantine(fifo, output, result);
    output->flags |= DREEM_OPTICAL_AFTER_VALID;
    if (!pointers_valid(output->after) ||
        output->after[2] != ((output->before[2] + count) & 31u))
        return quarantine(fifo, output, -EPROTO);
    if (output->after[1])
        return quarantine(fifo, output, -EOVERFLOW);
    for (unsigned i = 0; i < count; ++i) {
        dreem_optical_decode(raw + i * 6u, output->samples[i]);
        if ((output->samples[i][0] | output->samples[i][1]) & 0xfc0000u)
            output->flags |= DREEM_OPTICAL_HIGH_BITS;
    }
    output->count = count;
    return 0;
}
