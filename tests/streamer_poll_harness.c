/* SPDX-License-Identifier: Apache-2.0
 * Count real polling calls while leaving TCP, files and time unmodified.
 */
#define _POSIX_C_SOURCE 200809L
#include <inttypes.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/resource.h>

static uint64_t calls, ready, timeouts, waiting_for_write;
int __real_poll(struct pollfd *, nfds_t, int);
int __wrap_poll(struct pollfd *fds, nfds_t count, int timeout) {
    ++calls;
    int result = __real_poll(fds, count, timeout);
    if (result > 0) ++ready;
    if (result == 0) {
        ++timeouts;
        if (count == 2 && (fds[1].events & POLLOUT)) ++waiting_for_write;
    }
    return result;
}
__attribute__((destructor)) static void report(void) {
    struct rusage usage = {0};
    getrusage(RUSAGE_SELF, &usage);
    printf("{\"poll_calls\":%" PRIu64 ",\"ready_returns\":%" PRIu64 ","
           "\"timeout_returns\":%" PRIu64 ",\"blocked_write_polls\":%" PRIu64 ","
           "\"user_us\":%" PRIu64 ",\"system_us\":%" PRIu64 "}\n",
           calls, ready, timeouts, waiting_for_write,
           (uint64_t)usage.ru_utime.tv_sec * 1000000 + usage.ru_utime.tv_usec,
           (uint64_t)usage.ru_stime.tv_sec * 1000000 + usage.ru_stime.tv_usec);
}
