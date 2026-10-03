/* SPDX-License-Identifier: Apache-2.0 OR GPL-2.0-or-later
 * Copyright 2026 Dreem research contributors.
 * Fixed-size local lease protocol. It contains no firmware instructions.
 */
#include "radio_lease.h"

static int hex(unsigned char c)
{
    if (c >= '0' && c <= '9') return c-'0';
    if (c >= 'A' && c <= 'F') return c-'A'+10;
    if (c >= 'a' && c <= 'f') return c-'a'+10;
    return -1;
}

int dreem_lease_boot_id(const unsigned char text[37], unsigned char output[16])
{
    unsigned i, n = 0;
    int high = 0;
    if (text[36] != '\n') return 0;
    for (i = 0; i < 36; ++i) {
        int value;
        if (i == 8 || i == 13 || i == 18 || i == 23) {
            if (text[i] != '-') return 0;
            continue;
        }
        value = hex(text[i]);
        if (value < 0) return 0;
        if (!(n & 1)) high = value;
        else output[n/2] = (unsigned char)((high << 4) | value);
        ++n;
    }
    return 1;
}

int dreem_lease_address(const char *input, char output[18])
{
    const char digits[] = "0123456789ABCDEF";
    unsigned i;
    if (!input) return 0;
    for (i = 0; i < 17; ++i) {
        if (i % 3 == 2) {
            if (input[i] != ':') return 0;
            output[i] = ':';
        } else {
            int value = hex((unsigned char)input[i]);
            if (value < 0) return 0;
            output[i] = digits[value];
        }
    }
    if (input[17]) return 0;
    output[17] = 0;
    return 1;
}

static void put32(unsigned char *out, uint32_t value)
{
    unsigned i;
    for (i = 0; i < 4; ++i) out[i] = (unsigned char)(value >> (8*i));
}

static uint32_t get32(const unsigned char *in)
{
    return (uint32_t)in[0] | (uint32_t)in[1] << 8 |
           (uint32_t)in[2] << 16 | (uint32_t)in[3] << 24;
}

void dreem_lease_encode(unsigned char record[DREEM_LEASE_BYTES],
                        const unsigned char boot[16], uint32_t seconds,
                        uint32_t nanoseconds, const char peer[18])
{
    const char magic[] = "DRBTL001";
    unsigned i;
    for (i = 0; i < 8; ++i) record[i] = (unsigned char)magic[i];
    for (i = 0; i < 16; ++i) record[8+i] = boot[i];
    put32(record+24, seconds);
    put32(record+28, nanoseconds);
    for (i = 0; i < 18; ++i) record[32+i] = (unsigned char)peer[i];
    record[50] = record[51] = 0;
}

int dreem_lease_decode(const unsigned char record[DREEM_LEASE_BYTES],
                       const unsigned char boot[16], uint32_t seconds,
                       uint32_t nanoseconds, char peer[18])
{
    const char magic[] = "DRBTL001";
    uint32_t end, ns, difference;
    unsigned i;
    for (i = 0; i < 8; ++i) if (record[i] != (unsigned char)magic[i]) return 0;
    for (i = 0; i < 16; ++i) if (record[8+i] != boot[i]) return 0;
    if (record[50] || record[51] || record[49]) return 0;
    end = get32(record+24);
    ns = get32(record+28);
    if (nanoseconds >= 1000000000 || ns >= 1000000000 || end < seconds) return 0;
    difference = end-seconds;
    if ((!difference && ns <= nanoseconds) || difference > DREEM_LEASE_SECONDS ||
        (difference == DREEM_LEASE_SECONDS && ns > nanoseconds)) return 0;
    return dreem_lease_address((const char *)record+32, peer);
}
