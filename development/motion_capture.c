/* SPDX-License-Identifier: Apache-2.0
 * Capture an exclusively owned LIS2HH12 through an inherited i2c-dev fd.
 * This program does not acquire ownership or stop an existing sensor consumer.
 */
#define _POSIX_C_SOURCE 200809L
#include "motion_sensor.h"
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t interrupted;
static uint64_t previous_time;

static void interrupt_capture(int signal_number) { interrupted = signal_number; }

static int monotonic_ns(uint64_t *result) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) return -errno;
    if (t.tv_sec < 0 || t.tv_nsec < 0 || t.tv_nsec >= 1000000000 ||
        (uint64_t)t.tv_sec > (UINT64_MAX - (uint64_t)t.tv_nsec) / 1000000000)
        return -EOVERFLOW;
    uint64_t now = (uint64_t)t.tv_sec * 1000000000 + (uint64_t)t.tv_nsec;
    if (now < previous_time) return -ERANGE;
    previous_time = now;
    *result = now;
    return 0;
}

static int emit(int fd, const char *format, ...) {
    char line[1024];
    va_list ap;
    va_start(ap, format);
    int length = vsnprintf(line, sizeof line, format, ap);
    va_end(ap);
    if (length < 0 || (size_t)length >= sizeof line) return -EOVERFLOW;
    size_t offset = 0;
    while (offset < (size_t)length) {
        ssize_t n = write(fd, line + offset, (size_t)length - offset);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return n < 0 ? -errno : -EIO;
        offset += (size_t)n;
    }
    return 0;
}

static int number(const char *text, unsigned long maximum, unsigned *value) {
    if (*text < '0' || *text > '9') return -1;
    char *end;
    errno = 0;
    unsigned long n = strtoul(text, &end, 0);
    if (errno || *end || n > maximum) return -1;
    *value = (unsigned)n;
    return 0;
}

static void usage(FILE *stream) {
    fputs("Usage: motion_capture --fd N --address 0x1d|0x1e --rate 10|50|100|200|400|800\n"
          "  --range 2|4|8 --high-resolution 0|1 --duration-ms 1..3600000\n"
          "  --max-samples 1..1000000 --output NEW_FILE\n"
          "Requires exclusive sensor ownership established by the parent.\n"
          "Start resets the sensor. No device is opened and no owner is stopped.\n", stream);
}

int main(int argc, char **argv) {
    const char *options[] = {"--fd", "--address", "--rate", "--range",
                            "--high-resolution", "--duration-ms", "--max-samples", "--output"};
    const unsigned limits[] = {INT_MAX, 127, 800, 8, 1, 3600000, 1000000};
    unsigned values[7] = {0}, seen = 0;
    const char *path = NULL;
    if (argc == 2 && !strcmp(argv[1], "--help")) { usage(stdout); return 0; }
    for (int i = 1; i < argc; i += 2) {
        unsigned k;
        for (k = 0; k < 8 && strcmp(argv[i], options[k]); ++k) {}
        if (k == 8 || i + 1 == argc || (seen & (1u << k))) goto bad_arguments;
        seen |= 1u << k;
        if (k == 7) path = argv[i + 1];
        else if (number(argv[i + 1], limits[k], &values[k])) goto bad_arguments;
    }
    if (seen != 255 || values[0] < 3 || (values[1] != 29 && values[1] != 30) ||
        (values[2] != 10 && values[2] != 50 && values[2] != 100 && values[2] != 200 &&
         values[2] != 400 && values[2] != 800) ||
        (values[3] != 2 && values[3] != 4 && values[3] != 8) || !values[5] || !values[6])
        goto bad_arguments;

    int fd = (int)values[0];
    struct stat st;
    int flags = fcntl(fd, F_GETFL);
    if (flags < 0 || (flags & O_ACCMODE) != O_RDWR || fstat(fd, &st) ||
        !S_ISCHR(st.st_mode) || major(st.st_rdev) != 89) {
        fputs("An inherited read/write Linux i2c-dev descriptor is required.\n", stderr);
        return 2;
    }
    struct sigaction action;
    memset(&action, 0, sizeof action);
    action.sa_handler = interrupt_capture;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGINT, &action, NULL) || sigaction(SIGTERM, &action, NULL)) {
        fputs("Cannot install interruption handlers.\n", stderr);
        return 1;
    }
    int output = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (output < 0) { fputs("Cannot create a new private output file.\n", stderr); return 1; }
    if (fstat(output, &st) || !S_ISREG(st.st_mode)) {
        close(output);
        fputs("Output must be a new regular file.\n", stderr);
        return 1;
    }

    struct dreem_motion_sensor sensor = {0};
    struct dreem_motion_profile profile = {values[2], values[3], values[4]};
    int error = dreem_motion_sensor_init(&sensor, fd, values[1]);
    int output_error = 0, stop_error = 0, startup_cleanup_error = 0;
    unsigned samples = 0, not_ready = 0, late_samples = 0, start_attempted = 0;
    const char *reason = "initialization_error";
    uint64_t start = 0, deadline = 0, before = 0, after = 0;
    if (error) goto finish;
    output_error = emit(output, "{\"type\":\"request\",\"schema\":\"dreem.motion.capture.v1\","
        "\"sensor\":\"LIS2HH12\",\"address\":%u,\"requested_rate_hz\":%u,"
        "\"full_scale_g\":%u,\"high_resolution\":%u,\"duration_ms\":%u,\"max_samples\":%u,"
        "\"units\":\"signed_sensor_counts\",\"axes\":\"sensor_xyz\","
        "\"clock\":\"CLOCK_MONOTONIC\",\"physical_sample_timing_known\":false}\n",
        values[1], values[2], values[3], values[4], values[5], values[6]);
    if (output_error) { reason = "output_error"; goto finish; }
    if (interrupted) { reason = "signal"; goto finish; }
    start_attempted = 1;
    error = dreem_motion_sensor_start(&sensor, &profile);
    startup_cleanup_error = sensor.cleanup_error;
    if (error) { reason = "start_error"; goto finish; }
    error = monotonic_ns(&start);
    if (error || start > UINT64_MAX - (uint64_t)values[5] * 1000000) {
        if (!error) error = -EOVERFLOW;
        reason = "clock_error"; goto finish;
    }
    deadline = start + (uint64_t)values[5] * 1000000;
    reason = "sample_limit";
    while (samples < values[6]) {
        if (interrupted) { reason = "signal"; break; }
        error = monotonic_ns(&before);
        if (error) { reason = "clock_error"; break; }
        if (before >= deadline) { reason = "duration"; break; }
        struct dreem_motion_sample sample;
        int read_error = dreem_motion_sensor_read(&sensor, &sample);
        error = monotonic_ns(&after);
        if (error) { reason = "clock_error"; break; }
        if (read_error && read_error != -EAGAIN) {
            error = read_error; reason = "read_error"; break;
        }
        if (interrupted) { reason = "signal"; break; }
        if (after >= deadline) {
            late_samples += read_error == 0;
            reason = "duration"; break;
        }
        if (!read_error) {
            output_error = emit(output, "{\"type\":\"sample\",\"index\":%u,"
                "\"read_begin_ns\":%" PRIu64 ",\"read_end_ns\":%" PRIu64 ","
                "\"xyz\":[%d,%d,%d],\"flags\":%u,\"status_before\":%u,\"status_after\":%u}\n",
                samples, before, after, sample.xyz[0], sample.xyz[1], sample.xyz[2],
                sample.flags, sample.status_before, sample.status_after);
            if (output_error) { reason = "output_error"; break; }
            ++samples;
        } else ++not_ready;
        if (samples == values[6]) break;
        uint64_t now;
        error = monotonic_ns(&now);
        if (error) { reason = "clock_error"; break; }
        if (now >= deadline) { reason = "duration"; break; }
        uint64_t wait_ns = 500000000u / values[2];
        if (wait_ns > deadline - now) wait_ns = deadline - now;
        struct timespec wait = {0, (long)wait_ns};
        if (nanosleep(&wait, NULL) && errno != EINTR) {
            error = -errno; reason = "wait_error"; break;
        }
    }

finish:
    /* Even failed start/read may have configured the chip. Preserve all errors
     * and ask the checked lifecycle to establish power-down where identity is known.
     */
    if (sensor.identified) stop_error = dreem_motion_sensor_stop(&sensor);
    if (!output_error) output_error = emit(output,
        "{\"type\":\"end\",\"reason\":\"%s\",\"samples\":%u,\"not_ready\":%u,"
        "\"late_samples_discarded\":%u,\"signal\":%d,\"error\":%d,\"stop_error\":%d,"
        "\"startup_cleanup_error\":%d,\"start_attempted\":%s,\"sensor_state\":\"%s\","
        "\"shutdown_confirmed\":%s,\"continuity_known\":false}\n",
        reason, samples, not_ready, late_samples, interrupted, error, stop_error,
        startup_cleanup_error, start_attempted ? "true" : "false",
        sensor.state == DREEM_MOTION_STOPPED ? "STOPPED" : "UNKNOWN",
        sensor.state == DREEM_MOTION_STOPPED ? "true" : "false");
    if (fsync(output) && !output_error) output_error = -errno;
    if (close(output) && !output_error) output_error = -errno;
    int failed = error || stop_error || output_error ||
        (start_attempted && sensor.state != DREEM_MOTION_STOPPED);
    int exit_status = failed ? 1 : interrupted ? 128 + interrupted : 0;
    fprintf(stderr, "capture exit=%d samples=%u error=%d stop_error=%d output_error=%d shutdown_confirmed=%s\n",
            exit_status, samples, error, stop_error, output_error,
            sensor.state == DREEM_MOTION_STOPPED ? "true" : "false");
    return exit_status;

bad_arguments:
    usage(stderr);
    return 2;
}
