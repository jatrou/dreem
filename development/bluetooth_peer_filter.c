/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Dreem research contributors.
 * Independently authored connection classification for the pinned core.
 * This is an ownership policy, not Bluetooth authentication or authorization.
 */

extern const unsigned int dreem_extension_peer_count;
extern const char dreem_extension_peers[][18];
extern void stock_peer_connected(const char *address);

int dreem_is_extension_peer(const char *address)
{
    unsigned int peer, byte;
    if (!address)
        return 0;
    for (peer = 0; peer < dreem_extension_peer_count; ++peer) {
        for (byte = 0; byte < 17; ++byte) {
            unsigned char value = (unsigned char)address[byte];
            if (value >= 'a' && value <= 'f')
                value -= 'a' - 'A';
            /* A shorter string stops here, before any further input read. */
            if (value != (unsigned char)dreem_extension_peers[peer][byte])
                break;
        }
        if (byte == 17 && address[17] == '\0')
            return 1;
    }
    return 0;
}

/* Both original call sites ignore the return value. No original instructions
 * are copied: unmatched peers call the unchanged original helper directly.
 * The generated table is immutable for the lifetime of this executable.
 */
__attribute__((section(".text.entry")))
void dreem_peer_connected(const char *address)
{
    if (!dreem_is_extension_peer(address))
        stock_peer_connected(address);
}
