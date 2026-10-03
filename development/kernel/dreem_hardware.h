/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef _LINUX_DREEM_HARDWARE_H
#define _LINUX_DREEM_HARDWARE_H
#include <linux/types.h>

/* Built-in research users only. Sleeps; output is unchanged on failure.
 * Returns the raw board value, including zero for the older hardware branch.
 * No fuse programming, timing update, shadow reload or value caching. */
int dreem_get_hardware_version(u32 *version);

#endif
