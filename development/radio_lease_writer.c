/* SPDX-License-Identifier: Apache-2.0 OR GPL-2.0-or-later
 * Copyright 2026 Dreem research contributors.
 * POSIX publisher for one short, renewable sensor-radio lease.
 */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "radio_lease_writer.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static int private_fd(int fd, int directory)
{
    struct stat s;
    return !fstat(fd, &s) && s.st_uid == geteuid() &&
        (directory ? S_ISDIR(s.st_mode) && (s.st_mode & 07777) == 0700 :
                     S_ISREG(s.st_mode) && (s.st_mode & 07777) == 0600 && s.st_nlink == 1);
}

int dreem_lease_writer_close(struct dreem_lease_writer *writer)
{
    int result = 0;
    if (writer->lock >= 0) {
        if (unlinkat(writer->directory, "lease", 0) && errno != ENOENT) result = -1;
        if (unlinkat(writer->directory, "lease.tmp", 0) && errno != ENOENT) result = -1;
        if (close(writer->lock)) result = -1;
        writer->lock = -1;
    }
    if (writer->directory >= 0 && close(writer->directory)) result = -1;
    writer->directory = -1;
    return result;
}

int dreem_lease_writer_renew(struct dreem_lease_writer *writer)
{
    struct timespec now;
    unsigned char record[DREEM_LEASE_BYTES];
    int fd, result = -1;
    ssize_t count;
    if (writer->lock < 0 || !private_fd(writer->directory, 1) || !private_fd(writer->lock, 0) ||
        clock_gettime(CLOCK_MONOTONIC, &now) || now.tv_sec < 0 ||
        (uint64_t)now.tv_sec > UINT32_MAX-DREEM_LEASE_SECONDS ||
        now.tv_nsec < 0 || now.tv_nsec >= 1000000000) return -1;
    dreem_lease_encode(record, writer->boot, (uint32_t)now.tv_sec+DREEM_LEASE_SECONDS,
                       (uint32_t)now.tv_nsec, writer->peer);
    if (unlinkat(writer->directory, "lease.tmp", 0) && errno != ENOENT) return -1;
    fd = openat(writer->directory, "lease.tmp", O_WRONLY | O_CREAT | O_EXCL |
                O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK, 0600);
    if (fd < 0) return -1;
    /* A transient interrupted/short write is failure, never a partial lease.
     * No fsync is needed: this is an atomic, expiring runtime request. */
    count = write(fd, record, sizeof record);
    if (count == sizeof record && private_fd(fd, 0)) result = 0;
    if (close(fd)) result = -1;
    if (!result && renameat(writer->directory, "lease.tmp", writer->directory, "lease")) result = -1;
    if (result) unlinkat(writer->directory, "lease.tmp", 0);
    return result;
}

int dreem_lease_writer_open(struct dreem_lease_writer *writer, const char *directory, const char *peer)
{
    unsigned char text[38];
    int fd = -1;
    ssize_t count;
    writer->directory = writer->lock = -1;
    if (!dreem_lease_address(peer, writer->peer)) return -1;
    if (mkdir(directory, 0700) && errno != EEXIST) return -1;
    writer->directory = open(directory, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
    if (writer->directory < 0 || !private_fd(writer->directory, 1)) goto fail;
    fd = openat(writer->directory, "owner.lock", O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK, 0600);
    if (fd < 0 || !private_fd(fd, 0) || flock(fd, LOCK_EX | LOCK_NB)) goto fail;
    writer->lock = fd;
    fd = open("/proc/sys/kernel/random/boot_id", O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0) goto fail;
    count = read(fd, text, sizeof text);
    if (close(fd)) { fd = -1; goto fail; }
    fd = -1;
    if (count != 37 || !dreem_lease_boot_id(text, writer->boot) || dreem_lease_writer_renew(writer)) goto fail;
    return 0;
fail:
    if (fd >= 0) close(fd);
    dreem_lease_writer_close(writer);
    return -1;
}
