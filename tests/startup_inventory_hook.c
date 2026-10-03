/* SPDX-License-Identifier: Apache-2.0
 * Deterministic proc-fixture mutations at the observer's second identity check.
 * Linked only into the test executables, never the deployment binary.
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

void inventory_test_hook(int directory) {
    const char *action = getenv("INVENTORY_TEST_MUTATION");
    if (!action) return;
    if (!strcmp(action, "start")) {
        int fd = openat(directory, "stat", O_WRONLY | O_TRUNC);
        FILE *stream = fd < 0 ? NULL : fdopen(fd, "w");
        if (!stream) abort();
        fputs("101 (nano_core) S 1 1 1 0 0 0 0 0 0 0 0 0 0 0 0 1 0 999\n", stream);
        fclose(stream);
    } else if (!strcmp(action, "command")) {
        int fd = openat(directory, "cmdline", O_WRONLY | O_TRUNC);
        const char command[] = "changed\0private-argument";
        if (fd < 0 || write(fd, command, sizeof command) != (ssize_t)sizeof command) abort();
        close(fd);
    } else if (!strcmp(action, "executable")) {
        if (unlinkat(directory, "exe", 0) || symlinkat("../replacement", directory, "exe")) abort();
    } else if (!strcmp(action, "new-process")) {
        if (mkdirat(directory, "../202", 0700)) abort();
    } else if (!strcmp(action, "vanish")) {
        if (unlinkat(directory, "stat", 0)) abort();
    } else abort();
}
