/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

const unsigned int dreem_extension_peer_count = 8;
const char dreem_extension_peers[][18] = {
    "02:AB:CD:EF:00:01", "02:AB:CD:EF:00:02", "02:AB:CD:EF:00:03", "02:AB:CD:EF:00:04",
    "02:AB:CD:EF:00:05", "02:AB:CD:EF:00:06", "02:AB:CD:EF:00:07", "02:AB:CD:EF:00:08"
};
extern int dreem_is_extension_peer(const char *address);
extern void dreem_peer_connected(const char *address);
static const char *delegated;
static unsigned calls, cases;

void stock_peer_connected(const char *address)
{
    delegated = address;
    ++calls;
}

static int check(const char *address, int expected)
{
    unsigned before = calls;
    if (dreem_is_extension_peer(address) != expected)
        return 1;
    dreem_peer_connected(address);
    if (calls != before+(unsigned)!expected || (!expected && delegated != address))
        return 2;
    ++cases;
    return 0;
}

int main(void)
{
    long page = sysconf(_SC_PAGESIZE);
    char *memory;
    unsigned i, n;
    char lowercase[18];
    if (page < 256)
        return 10;
    memory = mmap(NULL, (size_t)page*2, PROT_READ | PROT_WRITE,
                  MAP_ANONYMOUS | MAP_PRIVATE, -1, 0);
    if (memory == MAP_FAILED || mprotect(memory+page, (size_t)page, PROT_NONE))
        return 11;
    if (check(NULL, 0))
        return 12;
    for (i = 0; i < dreem_extension_peer_count; ++i) {
        memcpy(lowercase, dreem_extension_peers[i], sizeof lowercase);
        for (n = 0; n < 17; ++n)
            if (lowercase[n] >= 'A' && lowercase[n] <= 'F')
                lowercase[n] += 'a'-'A';
        if (check(dreem_extension_peers[i], 1) || check(lowercase, 1))
            return 13;
        /* Every truncated address terminates at a protected-page boundary.
         * A speculative C read beyond NUL must not reach that next page. */
        for (n = 0; n <= 17; ++n) {
            char *input = memory+page-n-1;
            memcpy(input, lowercase, n);
            input[n] = '\0';
            if (check(input, n == 17))
                return 14;
        }
    }
    if (check("02:AB:CD:EF:00:09", 0) || check("02:AB:CD:EF:00:010", 0) ||
        check("02:AB:CD:EF:00:01 ", 0) || check(" 02:AB:CD:EF:00:01", 0) ||
        check("02-ab-cd-ef-00-01", 0))
        return 15;
    if (munmap(memory, (size_t)page*2))
        return 16;
    printf("filter-ok %u\n", cases);
    return 0;
}
