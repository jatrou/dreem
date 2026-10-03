/* SPDX-License-Identifier: Apache-2.0
 * Read-only Linux process/descriptor inventory. No device descriptor is opened,
 * no process is signaled, and a snapshot never grants an ownership handoff.
 */
#define _GNU_SOURCE
#define _FILE_OFFSET_BITS 64
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/vfs.h>
#include <unistd.h>

#define PROC_MAGIC 0x9fa0
#define MAX_PROCESSES 4096
#define MAX_DESCRIPTORS 4096
#define TEXT_LIMIT 4096

#ifdef INVENTORY_TEST_HOOK
extern void inventory_test_hook(int process_fd);
#else
static void inventory_test_hook(int process_fd) { (void)process_fd; }
#endif

struct identity {
    unsigned long pid, parent;
    unsigned long long start;
    char state;
    bool core_name, shell_watchdog_name, hardware_watchdog_name, kernel_thread;
};

enum device_kind { EEG, DDR, I2C, SPI, UART, AUDIO, WATCHDOG, OTHER, DEVICE_KINDS };
static const char *const kind_names[] = {
    "eeg", "ddr", "i2c", "spi", "uart", "audio", "watchdog", "other_device"
};

static bool number(const char *s, unsigned long *result, bool zero) {
    if (!*s) return false;
    for (const char *p = s; *p; ++p) if (*p < '0' || *p > '9') return false;
    char *end;
    errno = 0;
    unsigned long n = strtoul(s, &end, 10);
    if (errno || *end || (!zero && !n) || n > INT_MAX) return false;
    *result = n;
    return true;
}

/* Proc entries usually have st_size==0: bound reads, not their reported size. */
static ssize_t read_text(int dir, const char *name, char *out, size_t capacity) {
    int fd = openat(dir, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) return -1;
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode)) { close(fd); return -1; }
    size_t used = 0;
    while (used < capacity) {
        ssize_t n = read(fd, out + used, capacity - used);
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) { close(fd); return -1; }
        if (!n) break;
        used += (size_t)n;
    }
    close(fd);
    if (used == capacity) return -1;
    out[used] = 0;
    return (ssize_t)used;
}

/* comm may contain whitespace and ')' characters; the final ')' ends it. */
static bool parse_stat(char *text, unsigned long expected, struct identity *out) {
    char *left = strchr(text, '('), *right = strrchr(text, ')');
    if (!left || !right || right <= left || left == text || left[-1] != ' ' ||
        right[1] != ' ' || !right[2] || right[3] != ' ') return false;
    left[-1] = 0;
    if (!number(text, &out->pid, false) || out->pid != expected) return false;
    *right = 0;
    out->core_name = !strcmp(left + 1, "nano_core") || !strcmp(left + 1, "nyx_core");
    out->shell_watchdog_name = !strcmp(left + 1, "mpu_watchdog.sh");
    out->hardware_watchdog_name = !strcmp(left + 1, "watchdog");
    out->state = right[2];
    if (!strchr("RSDZTtWXxKPI", out->state)) return false;
    char *save = NULL, *token = strtok_r(right + 4, " \n", &save);
    for (int field = 4; field <= 22; ++field) {
        if (!token) return false;
        if (field == 4 && !number(token, &out->parent, true)) return false;
        if (field == 9) {
            for (char *p = token; *p; ++p) if (*p < '0' || *p > '9') return false;
            errno = 0;
            unsigned long flags = strtoul(token, NULL, 10);
            if (errno) return false;
            out->kernel_thread = (flags & 0x00200000UL) != 0; /* Linux 4.1 PF_KTHREAD */
        }
        if (field == 22) {
            for (char *p = token; *p; ++p) if (*p < '0' || *p > '9') return false;
            errno = 0;
            out->start = strtoull(token, NULL, 10);
            if (errno) return false;
        }
        token = strtok_r(NULL, " \n", &save);
    }
    return true;
}

static bool stat_identity(int dir, unsigned long pid, struct identity *out) {
    char text[TEXT_LIMIT];
    ssize_t n = read_text(dir, "stat", text, sizeof text);
    return n > 0 && !memchr(text, 0, (size_t)n) && parse_stat(text, pid, out);
}

static const char *basename_of(const char *path) {
    const char *slash = strrchr(path, '/');
    return slash ? slash + 1 : path;
}

static bool equal_base(const char *path, const char *name) {
    return !strcmp(basename_of(path), name);
}

static bool shell_name(const char *arg) {
    return equal_base(arg, "sh") || equal_base(arg, "ash") || equal_base(arg, "bash");
}

static bool script_name(const char *arg) { return equal_base(arg, "mpu_watchdog.sh"); }

/* Classification hints only: argv and comm are mutable, not authorization. */
static bool command_hints(char *text, size_t length, bool *shell, bool *hardware) {
    if (!length) return true; /* kernel thread or exited process; exe is checked separately */
    if (text[length - 1] != 0) return false;
    const char *args[3] = {"", "", ""};
    size_t offset = 0;
    for (size_t i = 0; i < 3 && offset < length; ++i) {
        args[i] = text + offset;
        offset += strlen(text + offset) + 1;
    }
    *shell = script_name(args[0]) || (shell_name(args[0]) && script_name(args[1])) ||
        (equal_base(args[0], "busybox") && shell_name(args[1]) && script_name(args[2]));
    *hardware = equal_base(args[0], "watchdog") ||
        (equal_base(args[0], "busybox") && !strcmp(args[1], "watchdog"));
    return true;
}

static int classify_device(const char *path) {
    if (!strcmp(path, "/dev/eeg_cdev")) return EEG;
    if (!strcmp(path, "/dev/dreem_ddr")) return DDR;
    if (!strncmp(path, "/dev/i2c-", 9)) return I2C;
    if (!strncmp(path, "/dev/spidev", 11)) return SPI;
    if (!strncmp(path, "/dev/ttymxc", 11)) return UART;
    if (!strncmp(path, "/dev/snd/", 9)) return AUDIO;
    if (!strcmp(path, "/dev/watchdog") || !strcmp(path, "/dev/watchdog0")) return WATCHDOG;
    return -1;
}

static unsigned descriptor_inventory(int process, unsigned counts[DEVICE_KINDS]) {
    int fd = openat(process, "fd", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) return 1;
    DIR *dir = fdopendir(fd);
    if (!dir) { close(fd); return 1; }
    unsigned errors = 0, entries = 0;
    for (;;) {
        errno = 0;
        struct dirent *entry = readdir(dir);
        if (!entry) { if (errno) ++errors; break; }
        unsigned long n;
        if (!number(entry->d_name, &n, true)) continue;
        if (++entries > MAX_DESCRIPTORS) { ++errors; break; }
        char target[TEXT_LIMIT];
        ssize_t len = readlinkat(fd, entry->d_name, target, sizeof target - 1);
        if (len < 0 || len == (ssize_t)sizeof target - 1) { ++errors; continue; }
        target[len] = 0;
        int kind = classify_device(target);
        struct stat st;
        /* stat follows a proc link for metadata only; it never opens that device. */
        if (fstatat(fd, entry->d_name, &st, 0)) {
            ++errors;
            if (kind >= 0) ++counts[kind]; /* named target observed, type unverified */
        } else if (S_ISCHR(st.st_mode) || S_ISBLK(st.st_mode)) {
            ++counts[kind < 0 ? OTHER : kind];
        } else if (kind >= 0) {
            ++counts[kind];
            ++errors; /* a hardware-looking name is not a device identity */
        }
    }
    closedir(dir);
    return errors;
}

static int compare_pid(const void *a, const void *b) {
    unsigned long x = *(const unsigned long *)a, y = *(const unsigned long *)b;
    return (x > y) - (x < y);
}

static bool list_pids(int root, unsigned long pids[MAX_PROCESSES], size_t *used) {
    /* openat creates a fresh directory offset, unlike dup(). */
    int fd = openat(root, ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0) return false;
    DIR *dir = fdopendir(fd);
    if (!dir) { close(fd); return false; }
    bool ok = true;
    *used = 0;
    for (;;) {
        errno = 0;
        struct dirent *entry = readdir(dir);
        if (!entry) { if (errno) ok = false; break; }
        unsigned long pid;
        if (!number(entry->d_name, &pid, false)) continue;
        if (*used == MAX_PROCESSES) { ok = false; break; }
        pids[(*used)++] = pid;
    }
    closedir(dir);
    qsort(pids, *used, sizeof *pids, compare_pid);
    return ok;
}

static unsigned process_inventory(int root, unsigned long pid, bool *first) {
    char name[32];
    snprintf(name, sizeof name, "%lu", pid);
    int fd = openat(root, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) return 1;
    struct identity before = {0}, after = {0};
    if (!stat_identity(fd, pid, &before)) { close(fd); return 1; }
    unsigned errors = 0, counts[DEVICE_KINDS] = {0};
    struct stat exe = {0}, exe_after = {0}, proc_stat = {0};
    int exe_status = fstatat(fd, "exe", &exe, 0), exe_error = errno;
    bool executable = exe_status == 0 && S_ISREG(exe.st_mode);
    bool exe_core = false, deleted = false, shell = false, hardware = false;
    char path[TEXT_LIMIT], command[TEXT_LIMIT], final_command[TEXT_LIMIT];
    ssize_t length = readlinkat(fd, "exe", path, sizeof path - 1);
    int link_error = errno;
    if (length > 0 && length < (ssize_t)sizeof path - 1) {
        path[length] = 0;
        const char suffix[] = " (deleted)";
        size_t suffix_length = sizeof suffix - 1;
        if ((size_t)length >= suffix_length && !strcmp(path + length - suffix_length, suffix)) {
            path[length - suffix_length] = 0;
            deleted = true;
        }
        exe_core = equal_base(path, "nano_core") || equal_base(path, "nyx_core");
    }
    if (fstat(fd, &proc_stat)) { close(fd); return 1; }
    ssize_t command_length = read_text(fd, "cmdline", command, sizeof command);
    if (command_length < 0 || !command_hints(command, (size_t)command_length, &shell, &hardware)) ++errors;
    bool kernel_without_exe = before.kernel_thread && !command_length &&
        exe_status < 0 && exe_error == ENOENT && length < 0 && link_error == ENOENT;
    if (!kernel_without_exe && (!executable || length <= 0 || length == (ssize_t)sizeof path - 1)) ++errors;
    errors += descriptor_inventory(fd, counts);
    inventory_test_hook(fd);
    bool same_start = stat_identity(fd, pid, &after) && before.start == after.start &&
        before.kernel_thread == after.kernel_thread;
    int final_exe_status = fstatat(fd, "exe", &exe_after, 0), final_exe_error = errno;
    bool same_exe = executable && !final_exe_status &&
        exe.st_dev == exe_after.st_dev && exe.st_ino == exe_after.st_ino &&
        exe.st_size == exe_after.st_size && exe.st_mtime == exe_after.st_mtime &&
        exe.st_ctime == exe_after.st_ctime;
    if (kernel_without_exe && final_exe_status < 0 && final_exe_error == ENOENT) same_exe = true;
    ssize_t final_length = read_text(fd, "cmdline", final_command, sizeof final_command);
    bool same_command = command_length >= 0 && command_length == final_length &&
        !memcmp(command, final_command, (size_t)command_length);
    if (!same_start || !same_exe || !same_command) ++errors;
    bool relevant = exe_core || before.core_name || before.shell_watchdog_name ||
        before.hardware_watchdog_name || shell || hardware;
    for (int i = 0; i < DEVICE_KINDS; ++i) if (counts[i]) relevant = true;
    if (relevant) {
        if (!*first) putchar(',');
        *first = false;
        printf("{\"pid\":%lu,\"parent_pid\":%lu,\"start_ticks\":%llu,\"state\":\"%c\","
               "\"proc_uid\":%ju,\"core_executable_name\":%s,\"core_comm_hint\":%s,"
               "\"shell_watchdog_hint\":%s,\"hardware_watchdog_hint\":%s,"
               "\"executable_deleted\":%s,\"executable\":",
               pid, before.parent, before.start, before.state, (uintmax_t)proc_stat.st_uid,
               exe_core ? "true" : "false", before.core_name ? "true" : "false",
               (shell || before.shell_watchdog_name) ? "true" : "false",
               (hardware || before.hardware_watchdog_name) ? "true" : "false", deleted ? "true" : "false");
        if (executable) printf("{\"device\":%ju,\"inode\":%ju,\"size\":%jd}",
                               (uintmax_t)exe.st_dev, (uintmax_t)exe.st_ino, (intmax_t)exe.st_size);
        else fputs("null", stdout);
        printf(",\"identity_unchanged_at_checks\":%s,\"inspection_errors\":%u,\"descriptors\":{",
               same_start && same_exe && same_command ? "true" : "false", errors);
        for (int i = 0; i < DEVICE_KINDS; ++i)
            printf("%s\"%s\":%u", i ? "," : "", kind_names[i], counts[i]);
        fputs("}}", stdout);
    }
    close(fd);
    return errors;
}

int main(int argc, char **argv) {
    const char *proc_root = "/proc";
    bool fixture = false;
#ifdef INVENTORY_FIXTURE
    if (argc == 3 && !strcmp(argv[1], "--fixture-proc")) { proc_root = argv[2]; fixture = true; }
    else
#endif
    if (argc != 1) { fputs("Usage: startup_inventory\n", stderr); return 2; }
    (void)argv;
    int root = open(proc_root, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    struct statfs fs;
    if (root < 0 || (!fixture && (fstatfs(root, &fs) || fs.f_type != PROC_MAGIC))) {
        if (root >= 0) close(root);
        fputs("Cannot inspect a proc filesystem\n", stderr);
        return 2;
    }
    unsigned long initial[MAX_PROCESSES], final[MAX_PROCESSES];
    size_t initial_count = 0, final_count = 0;
    bool initial_ok = list_pids(root, initial, &initial_count);
    printf("{\"schema\":1,\"fixture\":%s,\"exclusive_access_established\":false,"
           "\"activation_ready\":false,\"processes\":[", fixture ? "true" : "false");
    bool first = true;
    unsigned errors = initial_ok ? 0 : 1;
    for (size_t i = 0; i < initial_count; ++i) errors += process_inventory(root, initial[i], &first);
    bool final_ok = list_pids(root, final, &final_count);
    bool same_set = initial_ok && final_ok && initial_count == final_count &&
        !memcmp(initial, final, initial_count * sizeof *initial);
    if (!same_set) ++errors;
    printf("],\"processes_observed\":%zu,\"pid_set_unchanged_at_checks\":%s,"
           "\"inspection_errors\":%u,\"inspection_complete\":%s}\n",
           initial_count, same_set ? "true" : "false", errors, errors ? "false" : "true");
    close(root);
    if (ferror(stdout) || fflush(stdout)) return 2;
    return errors ? 1 : 0;
}
