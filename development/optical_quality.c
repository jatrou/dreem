/* SPDX-License-Identifier: Apache-2.0
 * Independent read-only feature for native pulse.data (two little-endian u32s).
 * Rows and ADC counts are preserved as the observation basis, not physical time
 * or physiological measurements. No sensor bus access or recorder control.
 */
#define _POSIX_C_SOURCE 200809L
#define _FILE_OFFSET_BITS 64
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

enum { ROW_BYTES = 8, WINDOW = 50, ADC_MAX = 0x3ffff };

typedef struct {
    unsigned valid, invalid, zero_rows;
    unsigned out_of_range[2], zeros[2], ceiling[2];
    uint32_t minimum[2], maximum[2];
    double mean[2], m2[2], covariance;
} metric;

static uint32_t uint_le(const unsigned char *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void add(metric *m, const unsigned char row[ROW_BYTES]) {
    uint32_t value[2] = {uint_le(row), uint_le(row + 4)};
    bool invalid = false;
    for (unsigned c = 0; c < 2; ++c) {
        if (value[c] > ADC_MAX) { m->out_of_range[c]++; invalid = true; }
    }
    if (invalid) { m->invalid++; return; }
    m->valid++;
    m->zero_rows += value[0] == 0 && value[1] == 0;
    double delta[2];
    for (unsigned c = 0; c < 2; ++c) {
        if (m->valid == 1 || value[c] < m->minimum[c]) m->minimum[c] = value[c];
        if (m->valid == 1 || value[c] > m->maximum[c]) m->maximum[c] = value[c];
        m->zeros[c] += value[c] == 0;
        m->ceiling[c] += value[c] == ADC_MAX;
        delta[c] = (double)value[c] - m->mean[c];
        m->mean[c] += delta[c] / m->valid;
        m->m2[c] += delta[c] * ((double)value[c] - m->mean[c]);
    }
    m->covariance += delta[0] * ((double)value[1] - m->mean[1]);
}

static void emit(const metric *m, uint64_t start, unsigned rows) {
    printf("{\"type\":\"optical_window\",\"sample_start\":%" PRIu64
           ",\"rows\":%u,\"complete_window\":%s,\"units\":\"adc_counts\","
           "\"sensor_health\":\"unverified\",\"continuous_acquisition_verified\":false,"
           "\"in_range_rows\":%u,\"out_of_range_rows\":%u,\"zero_rows\":%u,\"channels\":[",
           start, rows, rows == WINDOW ? "true" : "false", m->valid, m->invalid, m->zero_rows);
    for (unsigned c = 0; c < 2; ++c) {
        printf("%s{\"name\":\"%s\",\"out_of_range_values\":%u,\"zero_values\":%u,\"ceiling_values\":%u",
               c ? "," : "", c ? "infrared" : "red", m->out_of_range[c], m->zeros[c], m->ceiling[c]);
        if (m->valid) {
            double ac = sqrt(fmax(0, m->m2[c]) / m->valid);
            printf(",\"minimum\":%" PRIu32 ",\"maximum\":%" PRIu32
                   ",\"mean\":%.10g,\"ac_rms\":%.10g,\"peak_to_peak\":%" PRIu32 ",\"ac_to_dc\":",
                   m->minimum[c], m->maximum[c], m->mean[c], ac, m->maximum[c] - m->minimum[c]);
            if (m->mean[c] > 0) printf("%.10g", ac / m->mean[c]);
            else fputs("null", stdout);
        } else {
            fputs(",\"minimum\":null,\"maximum\":null,\"mean\":null,\"ac_rms\":null,"
                  "\"peak_to_peak\":null,\"ac_to_dc\":null", stdout);
        }
        putchar('}');
    }
    fputs("],\"red_ir_correlation\":", stdout);
    if (m->valid > 1 && m->m2[0] > 0 && m->m2[1] > 0)
        printf("%.10g", fmax(-1, fmin(1, m->covariance / sqrt(m->m2[0] * m->m2[1]))));
    else fputs("null", stdout);
    puts("}");
}

static double monotonic_seconds(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t) != 0) return -1;
    return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}

static bool same_input(int fd, const char *path, struct stat *st,
                       off_t *observed_size, off_t offset) {
    struct stat named;
    if (fstat(fd, st) || lstat(path, &named) || !S_ISREG(named.st_mode) ||
        st->st_dev != named.st_dev || st->st_ino != named.st_ino ||
        st->st_size < *observed_size || st->st_size < offset) {
        fputs("Input removed, replaced, or truncated; start a new sample timeline\n", stderr);
        return false;
    }
    *observed_size = st->st_size;
    return true;
}

int main(int argc, char **argv) {
    bool follow = false;
    double duration = 0;
    const char *path = NULL;
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--follow-seconds") && !follow && i + 1 < argc) {
            char *end;
            errno = 0;
            duration = strtod(argv[++i], &end);
            if (errno || *end || !isfinite(duration) || duration <= 0 || duration > 86400) {
                fputs("Invalid follow duration (0 < seconds <= 86400)\n", stderr);
                return 2;
            }
            follow = true;
        } else if (!path && argv[i][0] != '-') path = argv[i];
        else { path = NULL; break; }
    }
    if (!path) {
        fputs("Usage: optical_quality [--follow-seconds N] /path/to/pulse.data\n", stderr);
        return 2;
    }
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NONBLOCK | O_NOFOLLOW);
    if (fd < 0) { perror("open input"); return 1; }
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode)) {
        fputs("Input must be a regular, non-symlink file\n", stderr);
        close(fd); return 1;
    }
    const off_t initial_size = st.st_size;
    off_t observed_size = st.st_size, offset = 0;
    double started = monotonic_seconds();
    if (started < 0) { close(fd); return 1; }
    metric metrics = {0};
    unsigned rows = 0;
    uint64_t samples = 0;
    unsigned char buffer[ROW_BYTES * WINDOW];
    setvbuf(stdout, NULL, _IOLBF, 0);
    for (;;) {
        if (!same_input(fd, path, &st, &observed_size, offset)) { close(fd); return 1; }
        double now = monotonic_seconds();
        if (now < 0) { close(fd); return 1; }
        if (follow && now - started >= duration) break;
        size_t request = sizeof buffer;
        if (!follow && initial_size - offset < (off_t)request)
            request = (size_t)(initial_size - offset);
        ssize_t n = pread(fd, buffer, request, offset);
        if (n < 0) {
            if (errno == EINTR) continue;
            perror("pread input"); close(fd); return 1;
        }
        size_t complete = (size_t)n / ROW_BYTES;
        for (size_t i = 0; i < complete; ++i) {
            add(&metrics, buffer + i * ROW_BYTES);
            samples++;
            if (++rows == WINDOW) {
                emit(&metrics, samples - WINDOW, rows);
                memset(&metrics, 0, sizeof metrics);
                rows = 0;
            }
        }
        offset += (off_t)(complete * ROW_BYTES);
        if (ferror(stdout)) { close(fd); return 1; }
        if (!follow && complete == 0) break;
        if (follow && complete == 0) {
            const struct timespec delay = {.tv_sec = 0, .tv_nsec = 100000000};
            nanosleep(&delay, NULL);
        }
    }
    if (!same_input(fd, path, &st, &observed_size, offset)) { close(fd); return 1; }
    if (rows) emit(&metrics, samples - rows, rows);
    printf("{\"type\":\"end\",\"rows_consumed\":%" PRIu64
           ",\"bytes_remaining\":%" PRId64 ",\"initial_bytes\":%" PRId64
           ",\"follow\":%s}\n", samples, (int64_t)(st.st_size - offset),
           (int64_t)initial_size, follow ? "true" : "false");
    close(fd);
    return fflush(stdout) || ferror(stdout) ? 1 : 0;
}
