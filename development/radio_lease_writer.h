/* SPDX-License-Identifier: Apache-2.0 OR GPL-2.0-or-later */
#ifndef DREEM_RADIO_LEASE_WRITER_H
#define DREEM_RADIO_LEASE_WRITER_H
#include "radio_lease.h"

struct dreem_lease_writer {
    int directory, lock;
    unsigned char boot[16];
    char peer[18];
};
/* Initialize a fresh writer; never call open twice on an active writer. */
int dreem_lease_writer_open(struct dreem_lease_writer *, const char *directory, const char *peer);
int dreem_lease_writer_renew(struct dreem_lease_writer *);
int dreem_lease_writer_close(struct dreem_lease_writer *);
#endif
