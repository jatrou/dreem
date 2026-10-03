/* SPDX-License-Identifier: Apache-2.0
 * Read-only event/health monitor for an explicitly selected native algo.data.
 * Counters are preserved without assuming wall-clock or file-row alignment.
 */
#define _POSIX_C_SOURCE 200809L
#define _FILE_OFFSET_BITS 64
#include "algo_events.h"
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

typedef struct {
    bool have_previous;
    uint32_t previous;
    uint64_t segment, events;
    const char *motion, *optical;
} health_state;

static void emit(health_state *s, const dreem_algo_event *e, off_t offset) {
    bool decreased = s->have_previous && e->sample_counter < s->previous;
    bool recovery = e->code == 28;
    if (decreased || recovery) {
        s->segment++;
        s->motion = s->optical = "unknown";
    }
    const char *kind = recovery ? "recovery" : "event";
    uint32_t value = 0;
    if (e->code == 30 || e->code == 31) {
        value = (uint32_t)e->payload[0] | ((uint32_t)e->payload[1] << 8) |
                ((uint32_t)e->payload[2] << 16) | ((uint32_t)e->payload[3] << 24);
        const char *status = value == 0 ? "bad" : value == 1 ? "good" : "unknown";
        if (e->code == 30) { kind = "motion_health"; s->motion = status; }
        else { kind = "optical_health"; s->optical = status; }
    }
    printf("{\"type\":\"%s\",\"byte_offset\":%" PRId64
           ",\"sample_counter\":%" PRIu32 ",\"code\":%u,\"segment\":%" PRIu64
           ",\"counter_decreased\":%s,\"motion_health\":\"%s\",\"optical_health\":\"%s\"",
           kind, (int64_t)offset, e->sample_counter, (unsigned)e->code, s->segment,
           decreased ? "true" : "false", s->motion, s->optical);
    if (e->code == 30 || e->code == 31) printf(",\"health_value\":%" PRIu32, value);
    fputs(",\"payload_hex\":\"", stdout);
    for (unsigned i = 0; i < e->payload_size; i++) printf("%02x", (unsigned)e->payload[i]);
    puts("\"}");
    s->have_previous = true;
    s->previous = e->sample_counter;
    s->events++;
}

static double monotonic_seconds(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t) != 0) return -1;
    return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}

int main(int argc, char **argv) {
    bool follow = false;
    double duration = 0;
    const char *path = NULL;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--follow-seconds") == 0 && i + 1 < argc && !follow) {
            char *end;
            errno = 0;
            duration = strtod(argv[++i], &end);
            if (errno || *end || !isfinite(duration) || duration <= 0 || duration > 86400) {
                fputs("Invalid follow duration (0 < seconds <= 86400)\n", stderr);
                return 2;
            }
            follow = true;
        } else if (path == NULL && argv[i][0] != '-') path = argv[i];
        else { path = NULL; break; }
    }
    if (!path) {
        fputs("Usage: algo_health [--follow-seconds N] /path/to/algo.data\n", stderr);
        return 2;
    }
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NONBLOCK | O_NOFOLLOW);
    if (fd < 0) { perror("open input"); return 1; }
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode)) {
        fputs("Input must be a regular, non-symlink file\n", stderr);
        close(fd);
        return 1;
    }
    double start = monotonic_seconds();
    if (start < 0) { close(fd); return 1; }
    health_state state = { .motion = "unknown", .optical = "unknown" };
    off_t offset = 0, observed_size = st.st_size;
    unsigned char buffer[4096];
    setvbuf(stdout, NULL, _IOLBF, 0);
    for (;;) {
        struct stat named;
        if (fstat(fd, &st) || lstat(path, &named) || !S_ISREG(named.st_mode) ||
            st.st_dev != named.st_dev || st.st_ino != named.st_ino ||
            st.st_size < observed_size || st.st_size < offset) {
            fputs("Input removed, replaced, or truncated; start a new event timeline\n", stderr);
            close(fd);
            return 1;
        }
        observed_size = st.st_size;
        ssize_t n = pread(fd, buffer, sizeof buffer, offset);
        if (n < 0) {
            if (errno == EINTR) continue;
            perror("pread input"); close(fd); return 1;
        }
        size_t consumed = 0;
        while (consumed < (size_t)n) {
            dreem_algo_event event;
            int size = dreem_algo_decode(buffer + consumed, (size_t)n - consumed, &event);
            if (size < 0) {
                fprintf(stderr, "Unsupported event code %u at byte %" PRId64
                        "; payload length unknown, cannot continue\n",
                        (unsigned)buffer[consumed + 4], (int64_t)(offset + (off_t)consumed));
                close(fd); return 1;
            }
            if (!size) break;
            emit(&state, &event, offset + (off_t)consumed);
            consumed += (size_t)size;
        }
        offset += (off_t)consumed;
        if (ferror(stdout)) { close(fd); return 1; }
        double now = monotonic_seconds();
        if (now < 0) { close(fd); return 1; }
        if ((!follow && consumed == 0) || (follow && now - start >= duration)) break;
        if (!consumed) {
            const struct timespec delay = { .tv_sec = 0, .tv_nsec = 100000000 };
            nanosleep(&delay, NULL);
        }
    }
    if (fstat(fd, &st) || st.st_size < offset) { close(fd); return 1; }
    /* Remaining bytes can include unprocessed complete events when follow expires. */
    printf("{\"type\":\"end\",\"events_consumed\":%" PRIu64
           ",\"bytes_consumed\":%" PRId64 ",\"bytes_remaining\":%" PRId64 "}\n",
           state.events, (int64_t)offset, (int64_t)(st.st_size - offset));
    close(fd);
    return fflush(stdout) || ferror(stdout) ? 1 : 0;
}
