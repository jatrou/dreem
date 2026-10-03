/* SPDX-License-Identifier: Apache-2.0
 * Independent feature: bounded, read-only native motion summaries.
 * Inputs are three float32 little-endian values per row at nominally 50 Hz.
 * Units are nominal g; these metrics make no posture or diagnostic claims.
 * File-follow handling is shared in design with the EEG quality example.
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

enum { CHANNELS = 3, ROW_BYTES = 12, WINDOW = 50 };

typedef struct {
    unsigned valid, zeros, pairs;
    bool previous_valid;
    double mean[3], m2[3], previous[3], sumsq, step_sumsq, max_step;
} metric;

static float float_le(const unsigned char *p) {
    uint32_t bits = (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
                    ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
    float value;
    memcpy(&value, &bits, sizeof value);
    return value;
}

static void add(metric *m, const unsigned char *row) {
    double value[3], normsq = 0, stepsq = 0;
    for (unsigned c = 0; c < CHANNELS; c++) {
        value[c] = float_le(row + c * 4);
        if (!isfinite(value[c])) {
            m->previous_valid = false;
            return;
        }
    }
    m->valid++;
    for (unsigned c = 0; c < CHANNELS; c++) {
        double delta = value[c] - m->mean[c];
        m->mean[c] += delta / m->valid;
        m->m2[c] += delta * (value[c] - m->mean[c]);
        normsq += value[c] * value[c];
        if (m->previous_valid) {
            double step = value[c] - m->previous[c];
            stepsq += step * step;
        }
        m->previous[c] = value[c];
    }
    m->zeros += normsq == 0;
    m->sumsq += normsq;
    if (m->previous_valid) {
        m->pairs++;
        m->step_sumsq += stepsq;
        if (stepsq > m->max_step) m->max_step = stepsq;
    }
    m->previous_valid = true;
}

static void emit(const metric *m, uint64_t start) {
    printf("{\"type\":\"motion_window\",\"sample_start\":%" PRIu64
           ",\"rows\":50,\"nominal_rate_hz\":50,\"units\":\"g\","
           "\"valid_rows\":%u,\"invalid_rows\":%u,\"zero_rows\":%u,\"step_pairs\":%u",
           start, m->valid, WINDOW - m->valid, m->zeros, m->pairs);
    if (m->valid) {
        printf(",\"mean_axes\":[%.10g,%.10g,%.10g],\"vector_rms\":%.10g,\"dynamic_rms\":%.10g",
               m->mean[0], m->mean[1], m->mean[2], sqrt(m->sumsq / m->valid),
               sqrt(fmax(0, (m->m2[0] + m->m2[1] + m->m2[2]) / m->valid)));
    } else {
        printf(",\"mean_axes\":null,\"vector_rms\":null,\"dynamic_rms\":null");
    }
    if (m->pairs) {
        printf(",\"step_rms\":%.10g,\"max_step\":%.10g}\n",
               sqrt(m->step_sumsq / m->pairs), sqrt(m->max_step));
    } else {
        puts(",\"step_rms\":null,\"max_step\":null}");
    }
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
        fputs("Usage: motion_quality [--follow-seconds N] /path/to/accelerometer.data\n", stderr);
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
    metric metrics = {0};
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
            add(&metrics, buffer + i * ROW_BYTES);
            samples++;
            if (++rows == WINDOW) {
                emit(&metrics, samples - WINDOW);
                memset(&metrics, 0, sizeof metrics);
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
