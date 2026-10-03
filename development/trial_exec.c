/* SPDX-License-Identifier: Apache-2.0
 * Bound one trial command and report its Linux process resource usage.
 * Only the child/process group created here is signaled. No shell is invoked.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t interrupted;
static void on_signal(int number) { interrupted = number; }

static double monotonic(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) return -1;
    return (double)now.tv_sec + (double)now.tv_nsec / 1e9;
}

static void stop_child(pid_t child) {
    /* The unreaped child retains its PID, so neither target can have been reused. */
    kill(-child, SIGKILL);
    kill(child, SIGKILL);
}

int main(int argc, char **argv) {
    if (argc < 5 || argv[3][0] != '-' || argv[3][1] != '-' || argv[3][2]) {
        fputs("Usage: trial_exec SECONDS NEW_REPORT -- /path/to/program [args...]\n", stderr);
        return 2;
    }
    char *end;
    errno = 0;
    double limit = strtod(argv[1], &end);
    if (errno || end == argv[1] || *end || !isfinite(limit) || limit <= 0 || limit > 60) {
        fputs("Trial duration must satisfy 0 < seconds <= 60\n", stderr);
        return 2;
    }
    int report_fd = open(argv[2], O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (report_fd < 0) { perror("create trial report"); return 125; }
    FILE *report = fdopen(report_fd, "w");
    if (!report) { close(report_fd); return 125; }
    int error_pipe[2];
    if (pipe2(error_pipe, O_CLOEXEC)) { fclose(report); return 125; }
    struct sigaction action = { .sa_handler = on_signal };
    sigemptyset(&action.sa_mask);
    struct sigaction normal = { .sa_handler = SIG_DFL };
    sigemptyset(&normal.sa_mask);
    if (sigaction(SIGINT, &action, NULL) || sigaction(SIGTERM, &action, NULL) ||
        sigaction(SIGCHLD, &normal, NULL)) {
        close(error_pipe[0]); close(error_pipe[1]); fclose(report); return 125;
    }
    double start = monotonic();
    if (start < 0) { close(error_pipe[0]); close(error_pipe[1]); fclose(report); return 125; }
    pid_t parent = getpid(), child = fork();
    if (child < 0) { close(error_pipe[0]); close(error_pipe[1]); fclose(report); return 125; }
    if (!child) {
        close(error_pipe[0]);
        sigaction(SIGINT, &normal, NULL);
        sigaction(SIGTERM, &normal, NULL);
        int setup_errno = 0;
        if (setpgid(0, 0) || prctl(PR_SET_PDEATHSIG, SIGKILL)) setup_errno = errno;
        if (getppid() != parent) setup_errno = ESRCH;
        struct rlimit core_limit = {0, 0};
        if (!setup_errno && setrlimit(RLIMIT_CORE, &core_limit)) setup_errno = errno;
        errno = 0;
        if (!setup_errno && nice(10) == -1 && errno) setup_errno = errno;
        if (!setup_errno) execv(argv[4], argv+4);
        int failure = setup_errno ? setup_errno : errno;
        (void)!write(error_pipe[1], &failure, sizeof failure);
        _exit(127);
    }
    close(error_pipe[1]);
    /* Either parent or child establishes the group before the deadline. */
    if (setpgid(child, child) && errno != EACCES && errno != ESRCH) interrupted = SIGTERM;
    int status = 0;
    struct rusage usage = {0};
    bool timed_out = false, wait_failed = false, child_owned = true;
    for (;;) {
        pid_t got = wait4(child, &status, WNOHANG, &usage);
        if (got == child) { child_owned = false; break; }
        if (got < 0) {
            if (errno == EINTR) continue;
            wait_failed = true;
            if (errno == ECHILD) child_owned = false;
            break;
        }
        double now = monotonic();
        if (now < 0 || interrupted || now-start >= limit) {
            timed_out = now >= 0 && !interrupted && now-start >= limit;
            if (now < 0) wait_failed = true;
            stop_child(child);
            do { got = wait4(child, &status, 0, &usage); } while (got < 0 && errno == EINTR);
            if (got == child || (got < 0 && errno == ECHILD)) child_owned = false;
            if (got < 0) {
                wait_failed = true;
            }
            break;
        }
        struct timespec pause = { .tv_sec = 0, .tv_nsec = 10000000 };
        nanosleep(&pause, NULL);
    }
    if (wait_failed && child_owned) {
        stop_child(child);
        while (wait4(child, &status, 0, &usage) < 0 && errno == EINTR) {}
    }
    int exec_error = 0;
    ssize_t n;
    do { n = read(error_pipe[0], &exec_error, sizeof exec_error); } while (n < 0 && errno == EINTR);
    close(error_pipe[0]);
    if (n < 0 || (n != 0 && n != (ssize_t)sizeof exec_error)) wait_failed = true;
    double finish = monotonic();
    if (finish < 0) wait_failed = true;
    fprintf(report, "{\"timed_out\":%s,\"interrupted\":%s,\"monitor_error\":%s,",
            timed_out ? "true" : "false", interrupted ? "true" : "false", wait_failed ? "true" : "false");
    if (exec_error) fprintf(report, "\"exec_errno\":%d,", exec_error);
    else fputs("\"exec_errno\":null,", report);
    if (WIFEXITED(status) && !wait_failed) fprintf(report, "\"exit_code\":%d,", WEXITSTATUS(status));
    else fputs("\"exit_code\":null,", report);
    if (WIFSIGNALED(status) && !wait_failed) fprintf(report, "\"signal\":%d,", WTERMSIG(status));
    else fputs("\"signal\":null,", report);
    fprintf(report, "\"wall_seconds\":%.6f,\"user_seconds\":%.6f,\"system_seconds\":%.6f,"
            "\"max_rss_kib\":%ld}\n", finish >= start ? finish-start : 0,
            usage.ru_utime.tv_sec + usage.ru_utime.tv_usec/1e6,
            usage.ru_stime.tv_sec + usage.ru_stime.tv_usec/1e6, usage.ru_maxrss);
    bool report_failed = ferror(report);
    if (fclose(report) || report_failed) return 125;
    if (wait_failed) return 125;
    if (interrupted) return 128+interrupted;
    if (timed_out) return 124;
    if (exec_error) return 127;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128+WTERMSIG(status);
}
