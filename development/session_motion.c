/* SPDX-License-Identifier: Apache-2.0
 * Health-aware motion summaries for a coherent, completed native recording.
 * The normal 4.7.11 path starts at counter zero; recovered sessions require
 * additional alignment evidence and are rejected explicitly.
 */
#define _POSIX_C_SOURCE 200809L
#define _FILE_OFFSET_BITS 64
#include "algo_events.h"
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

enum { EEG, MOTION, ALGO, META, FILES, WINDOW = 50 };
static const char *const names[FILES] = {"eeg.data", "accelerometer.data", "algo.data", "meta.data"};
typedef struct { int fd; struct stat initial; } input_file;
typedef struct {
    unsigned rows, good, bad, unknown, finite, zero;
    double mean[3], m2[3], sumsq;
} window;

static uint32_t u32(const unsigned char *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static int read_exact(int fd, unsigned char *p, size_t size, off_t offset) {
    size_t done = 0;
    while (done < size) {
        ssize_t n = pread(fd, p+done, size-done, offset+(off_t)done);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        done += (size_t)n;
    }
    return 0;
}

/* 1 event, 0 clean end, -1 malformed input or read error. */
static int next_event(const input_file *f, off_t *offset, dreem_algo_event *event) {
    off_t remaining = f->initial.st_size - *offset;
    if (!remaining) return 0;
    unsigned char bytes[21];
    size_t size = remaining > (off_t)sizeof bytes ? sizeof bytes : (size_t)remaining;
    if (read_exact(f->fd, bytes, size, *offset)) return -1;
    int consumed = dreem_algo_decode(bytes, size, event);
    if (consumed <= 0) {
        fprintf(stderr, "%s algo event at byte %" PRId64 "\n",
                consumed ? "Unsupported" : "Incomplete", (int64_t)*offset);
        return -1;
    }
    *offset += consumed;
    return 1;
}

static bool same_stat(const struct stat *a, const struct stat *b) {
    return S_ISREG(b->st_mode) && a->st_dev == b->st_dev && a->st_ino == b->st_ino &&
           a->st_size == b->st_size && a->st_mtim.tv_sec == b->st_mtim.tv_sec &&
           a->st_mtim.tv_nsec == b->st_mtim.tv_nsec && a->st_ctim.tv_sec == b->st_ctim.tv_sec &&
           a->st_ctim.tv_nsec == b->st_ctim.tv_nsec;
}

static int unchanged(int directory, input_file *files) {
    for (unsigned i = 0; i < FILES; i++) {
        struct stat opened, named;
        if (fstat(files[i].fd, &opened) ||
            fstatat(directory, names[i], &named, AT_SYMLINK_NOFOLLOW) ||
            !same_stat(&files[i].initial, &opened) || !same_stat(&files[i].initial, &named)) {
            fputs("Recording changed during inspection; use a stable completed copy\n", stderr);
            return -1;
        }
    }
    return 0;
}

static int validate(input_file *files, uint64_t *eeg_rows, uint64_t *motion_rows) {
    unsigned char header[142];
    if (files[EEG].initial.st_size % 16 || files[MOTION].initial.st_size % 12 ||
        read_exact(files[META].fd, header, sizeof header, 0)) {
        fputs("Incomplete sample row or metadata header\n", stderr);
        return -1;
    }
    *eeg_rows = (uint64_t)files[EEG].initial.st_size / 16;
    *motion_rows = (uint64_t)files[MOTION].initial.st_size / 12;
    if (header[118] || header[119]) {
        fputs("Nonzero recovery flag: motion-file alignment is not established\n", stderr);
        return -1;
    }
    if (*eeg_rows > UINT32_MAX || u32(header+134) != *eeg_rows ||
        *motion_rows != (*eeg_rows+4)/5) {
        fputs("Metadata/EEG/motion counts disagree with the normal recording cadence\n", stderr);
        return -1;
    }
    bool started = false, stopped = false, have_health = false;
    uint32_t previous = 0, previous_health = 0;
    off_t offset = 0;
    dreem_algo_event e;
    int status;
    while ((status = next_event(&files[ALGO], &offset, &e)) > 0) {
        if (stopped || (!started && e.code != 16) ||
            e.sample_counter < previous || e.sample_counter > *eeg_rows) {
            fputs("Event order/counters do not describe one completed normal recording\n", stderr);
            return -1;
        }
        if (e.code == 28) {
            fputs("Recovery event: motion-file alignment is not established\n", stderr);
            return -1;
        }
        if (e.code == 16) {
            if (started || e.sample_counter || u32(e.payload) != u32(header+110)) {
                fputs("Start event disagrees with metadata or counter origin\n", stderr);
                return -1;
            }
            started = true;
        } else if (e.code == 17) {
            if (e.sample_counter != *eeg_rows || u32(e.payload) != u32(header+114)) {
                fputs("Stop event disagrees with metadata or EEG count\n", stderr);
                return -1;
            }
            stopped = true;
        } else if (e.code == 30) {
            if (e.sample_counter % 5 || e.sample_counter >= *eeg_rows ||
                (have_health && e.sample_counter == previous_health)) {
                fputs("Motion-health events violate the observed recording cadence\n", stderr);
                return -1;
            }
            have_health = true;
            previous_health = e.sample_counter;
        }
        previous = e.sample_counter;
    }
    if (status < 0) return -1;
    if (!started || !stopped) {
        fputs("Start/stop evidence missing; recording may be incomplete\n", stderr);
        return -1;
    }
    return 0;
}

static void add(window *w, const unsigned char *row, int health) {
    w->rows++;
    if (health == 0) { w->bad++; return; }
    if (health != 1) { w->unknown++; return; }
    w->good++;
    double values[3], norm = 0;
    for (unsigned c = 0; c < 3; c++) {
        uint32_t bits = u32(row + 4*c);
        float value;
        memcpy(&value, &bits, sizeof value);
        if (!isfinite(value)) return;
        values[c] = value;
        norm += values[c]*values[c];
    }
    w->finite++;
    w->zero += norm == 0;
    w->sumsq += norm;
    for (unsigned c = 0; c < 3; c++) {
        double delta = values[c] - w->mean[c];
        w->mean[c] += delta / w->finite;
        w->m2[c] += delta * (values[c] - w->mean[c]);
    }
}

static void emit(const window *w, uint64_t first) {
    printf("{\"type\":\"motion_window\",\"motion_row_start\":%" PRIu64
           ",\"eeg_counter_start\":%" PRIu64 ",\"rows\":%u,\"reported_good\":%u,"
           "\"reported_bad\":%u,\"unknown\":%u,\"included_rows\":%u,"
           "\"nonfinite_good_rows\":%u,\"zero_good_rows\":%u,\"units\":\"g\"",
           first, first*5, w->rows, w->good, w->bad, w->unknown,
           w->finite, w->good-w->finite, w->zero);
    if (w->finite) {
        printf(",\"mean_axes\":[%.10g,%.10g,%.10g],\"vector_rms\":%.10g,\"dynamic_rms\":%.10g}\n",
               w->mean[0], w->mean[1], w->mean[2], sqrt(w->sumsq/w->finite),
               sqrt(fmax(0, (w->m2[0]+w->m2[1]+w->m2[2])/w->finite)));
    } else puts(",\"mean_axes\":null,\"vector_rms\":null,\"dynamic_rms\":null}");
}

int main(int argc, char **argv) {
    if (argc != 2 || argv[1][0] == '-') {
        fputs("Usage: session_motion /path/to/completed-native-recording\n", stderr);
        return 2;
    }
    int directory = open(argv[1], O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (directory < 0) { perror("open recording directory"); return 1; }
    input_file files[FILES];
    for (unsigned i = 0; i < FILES; i++) files[i].fd = -1;
    int result = 1;
    for (unsigned i = 0; i < FILES; i++) {
        files[i].fd = openat(directory, names[i], O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
        if (files[i].fd < 0 || fstat(files[i].fd, &files[i].initial) ||
            !S_ISREG(files[i].initial.st_mode)) {
            fprintf(stderr, "Cannot read regular, non-symlink %s\n", names[i]);
            goto done;
        }
    }
    uint64_t eeg_rows, motion_rows;
    if (validate(files, &eeg_rows, &motion_rows) || unchanged(directory, files)) goto done;
    setvbuf(stdout, NULL, _IOLBF, 0);
    puts("{\"type\":\"alignment\",\"basis\":\"normal_recording_counters\","
         "\"eeg_rows_per_motion_row\":5,\"nominal_motion_rate_hz\":50,\"recovered\":false}");
    off_t event_offset = 0;
    dreem_algo_event event;
    int have_event = next_event(&files[ALGO], &event_offset, &event), health = -1;
    uint64_t included = 0, good = 0, bad = 0, unknown = 0;
    for (uint64_t start = 0; start < motion_rows; start += WINDOW) {
        unsigned rows = motion_rows-start < WINDOW ? (unsigned)(motion_rows-start) : WINDOW;
        unsigned char buffer[WINDOW*12];
        if (read_exact(files[MOTION].fd, buffer, rows*12, (off_t)(start*12))) goto done;
        window w = {0};
        for (unsigned i = 0; i < rows; i++) {
            uint64_t counter = (start+i)*5;
            while (have_event > 0 && event.sample_counter <= counter) {
                if (event.code == 30) {
                    uint32_t value = u32(event.payload);
                    health = value <= 1 ? (int)value : -1;
                }
                have_event = next_event(&files[ALGO], &event_offset, &event);
            }
            if (have_event < 0) goto done;
            add(&w, buffer+i*12, health);
        }
        emit(&w, start);
        good += w.good; bad += w.bad; unknown += w.unknown; included += w.finite;
        if (ferror(stdout)) goto done;
    }
    if (unchanged(directory, files)) goto done;
    printf("{\"type\":\"end\",\"eeg_rows\":%" PRIu64 ",\"motion_rows\":%" PRIu64
           ",\"reported_good\":%" PRIu64 ",\"reported_bad\":%" PRIu64
           ",\"unknown\":%" PRIu64 ",\"included_rows\":%" PRIu64 "}\n",
           eeg_rows, motion_rows, good, bad, unknown, included);
    result = fflush(stdout) || ferror(stdout) ? 1 : 0;
done:
    for (unsigned i = 0; i < FILES; i++) if (files[i].fd >= 0) close(files[i].fd);
    close(directory);
    return result;
}
