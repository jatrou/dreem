/* SPDX-License-Identifier: Apache-2.0 OR GPL-2.0-or-later */
#ifndef DREEM_RADIO_LEASE_H
#define DREEM_RADIO_LEASE_H
#include <stdint.h>

#define DREEM_LEASE_BYTES 52
#define DREEM_LEASE_SECONDS 3
#ifndef DREEM_LEASE_DIRECTORY
#define DREEM_LEASE_DIRECTORY "/run/dreem-extension-radio"
#endif

int dreem_lease_boot_id(const unsigned char text[37], unsigned char output[16]);
int dreem_lease_address(const char *input, char output[18]);
void dreem_lease_encode(unsigned char record[DREEM_LEASE_BYTES],
                        const unsigned char boot[16], uint32_t seconds,
                        uint32_t nanoseconds, const char peer[18]);
int dreem_lease_decode(const unsigned char record[DREEM_LEASE_BYTES],
                       const unsigned char boot[16], uint32_t seconds,
                       uint32_t nanoseconds, char peer[18]);
#endif
