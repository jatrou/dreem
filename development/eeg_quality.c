/* SPDX-License-Identifier: Apache-2.0
 * Independent example feature: bounded, read-only native EEG quality metrics.
 * Inputs are four float32 little-endian values per row at nominally 250 Hz.
 * Signal units are preserved; these metrics make no diagnostic claims.
 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

enum { CHANNELS = 4, ROW_BYTES = 16, WINDOW = 250 };

typedef struct {
    unsigned valid;
    double mean, m2, sumsq, minimum, maximum;
} metric;

static float float_le(const unsigned char *p) {
    uint32_t bits = (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
                    ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
    float value;
    memcpy(&value, &bits, sizeof value);
    return value;
}

static void add(metric *m, double value) {
    if (!isfinite(value)) return;
    if (m->valid == 0) m->minimum = m->maximum = value;
    if (value < m->minimum) m->minimum = value;
    if (value > m->maximum) m->maximum = value;
    m->valid++;
    double delta = value - m->mean;
    m->mean += delta / m->valid;
    m->m2 += delta * (value - m->mean);
    m->sumsq += value * value;
}

static void emit(metric *m, uint64_t start) {
    printf("{\"type\":\"window\",\"sample_start\":%" PRIu64
           ",\"rows\":250,\"rate_hz\":250,\"channels\":[", start);
    for (unsigned c = 0; c < CHANNELS; c++) {
        metric *v = m + c;
        if (c) putchar(',');
        printf("{\"valid\":%u,\"invalid\":%u", v->valid, WINDOW - v->valid);
        if (v->valid) {
            printf(",\"mean\":%.10g,\"rms\":%.10g,\"ac_rms\":%.10g,"
                   "\"peak_to_peak\":%.10g}", v->mean,
                   sqrt(v->sumsq / v->valid), sqrt(fmax(0, v->m2 / v->valid)),
                   v->maximum - v->minimum);
        } else {
            printf(",\"mean\":null,\"rms\":null,\"ac_rms\":null,\"peak_to_peak\":null}");
        }
    }
    puts("]}");
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
        if (strcmp(argv[i], "--follow-seconds") == 0 && i + 1 < argc) {
            char *end;
            errno = 0;
            duration = strtod(argv[++i], &end);
            if (errno || *end || !isfinite(duration) || duration <= 0 || duration > 86400) {
                fputs("Invalid follow duration (0 < seconds <= 86400)\n", stderr);
                return 2;
            }
            follow = true;
        } else if (path == NULL && argv[i][0] != '-') {
            path = argv[i];
        } else {
            path = NULL;
            break;
        }
    }
    if (!path) {
        fputs("Usage: eeg_quality [--follow-seconds N] /path/to/eeg.data\n", stderr);
        return 2;
    }
    /* O_NONBLOCK prevents a supplied FIFO from blocking before fstat rejects it. */
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NONBLOCK | O_NOFOLLOW);
    struct stat st;
    if (fd < 0) {
        perror("open input");
        return 1;
    }
    if (fstat(fd, &st) || !S_ISREG(st.st_mode)) {
        fputs("Input must be a regular, non-symlink file\n", stderr);
        close(fd);
        return 1;
    }
    double start_time = monotonic_seconds();
    if (start_time < 0) { close(fd); return 1; }
    metric metrics[CHANNELS] = {0};
    unsigned rows = 0;
    uint64_t samples = 0;
    off_t offset = 0;
    unsigned char buffer[ROW_BYTES * WINDOW];
    setvbuf(stdout, NULL, _IOLBF, 0);
    for (;;) {
        struct stat named;
        if (fstat(fd, &st) || lstat(path, &named) ||
            !S_ISREG(named.st_mode) || st.st_dev != named.st_dev || st.st_ino != named.st_ino ||
            st.st_size < offset) {
            fputs("Input removed, replaced, or truncated; start a new sample timeline\n", stderr);
            close(fd);
            return 1;
        }
        ssize_t n = pread(fd, buffer, sizeof buffer, offset);
        if (n < 0) {
            if (errno == EINTR) continue;
            perror("pread input");
            close(fd);
            return 1;
        }
        size_t complete = (size_t)n / ROW_BYTES;
        for (size_t i = 0; i < complete; i++) {
            for (unsigned c = 0; c < CHANNELS; c++)
                add(metrics + c, float_le(buffer + i * ROW_BYTES + c * 4));
            samples++;
            if (++rows == WINDOW) {
                emit(metrics, samples - WINDOW);
                memset(metrics, 0, sizeof metrics);
                rows = 0;
            }
        }
        offset += (off_t)(complete * ROW_BYTES);
        if (ferror(stdout)) { close(fd); return 1; }
        double now = monotonic_seconds();
        if (now < 0) { close(fd); return 1; }
        if ((!follow && complete == 0) || (follow && now - start_time >= duration)) break;
        if (complete == 0) {
            const struct timespec delay = { .tv_sec = 0, .tv_nsec = 100000000 };
            nanosleep(&delay, NULL);
        }
    }
    if (fstat(fd, &st)) { close(fd); return 1; }
    printf("{\"type\":\"end\",\"rows_consumed\":%" PRIu64
           ",\"unreported_window_rows\":%u,\"bytes_remaining\":%" PRId64 "}\n",
           samples, rows, (int64_t)(st.st_size - offset));
    close(fd);
    return ferror(stdout) ? 1 : 0;
}
