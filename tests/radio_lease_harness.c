/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include "radio_lease_writer.h"
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static unsigned checks;
#define CHECK(expression) do { ++checks; if (!(expression)) { fprintf(stderr, "check failed line %u\n", __LINE__); return 1; } } while (0)
int dreem_is_extension_peer(const char *peer) { return !strcmp(peer, "02:00:00:00:00:02"); }
int dreem_power_off(const char *command, unsigned caller, unsigned pause, int probe)
{ (void)command; (void)caller; (void)pause; (void)probe; abort(); }
int dreem_disable_probe(const char *command, unsigned caller, unsigned pause, int *probe)
{ (void)command; (void)caller; (void)pause; (void)probe; abort(); }
#ifdef TEST_ARM_READER
extern int dreem_radio_lease_active(void);
#define ACTIVE(value) CHECK(dreem_radio_lease_active() == (value))
#else
#define ACTIVE(value) ((void)0)
#endif

static int fd_count(void)
{
    int i, result = 0;
    for (i = 0; i < 128; ++i) if (fcntl(i, F_GETFD) >= 0) ++result;
    return result;
}

int main(int argc, char **argv)
{
    const unsigned char text[] = "01234567-89ab-cdef-0123-456789abcdef\n";
    unsigned char boot[16], record[DREEM_LEASE_BYTES], saved[DREEM_LEASE_BYTES];
    char peer[18];
    struct dreem_lease_writer writer, second;
    struct stat old, current;
    struct timespec now;
    int fd, baseline = fd_count();
    unsigned i;
    if (argc != 2) return 2;
    CHECK(dreem_lease_boot_id(text, boot));
    CHECK(dreem_lease_address("02:ab:CD:ef:00:02", peer) && !strcmp(peer, "02:AB:CD:EF:00:02"));
    dreem_lease_encode(record, boot, 103, 500, peer);
    CHECK(dreem_lease_decode(record, boot, 100, 500, peer));
    CHECK(!dreem_lease_decode(record, boot, 100, 499, peer));
    CHECK(dreem_lease_decode(record, boot, 103, 499, peer));
    CHECK(!dreem_lease_decode(record, boot, 103, 500, peer));
    CHECK(!dreem_lease_decode(record, boot, 104, 0, peer));
    CHECK(!dreem_lease_decode(record, boot, 100, 1000000000, peer));
    memcpy(saved, record, sizeof saved);
    for (i = 0; i < 24; ++i) {
        record[i] ^= 1;
        CHECK(!dreem_lease_decode(record, boot, 100, 500, peer));
        record[i] ^= 1;
    }
    CHECK(dreem_lease_writer_open(&writer, argv[1], "02:00:00:00:00:02") == 0);
    ACTIVE(1);
    CHECK(dreem_lease_writer_open(&second, argv[1], "02:00:00:00:00:02") == -1);
    ACTIVE(1); /* Failed competing owner must not unlink the first lease. */
    fd = openat(writer.directory, "lease", O_RDONLY | O_CLOEXEC);
    CHECK(fd >= 0 && read(fd, record, sizeof record) == sizeof record && !fstat(fd, &old));
    CHECK(!clock_gettime(CLOCK_MONOTONIC, &now));
    CHECK(dreem_lease_decode(record, writer.boot, (uint32_t)now.tv_sec, (uint32_t)now.tv_nsec, peer));
    CHECK(dreem_lease_writer_renew(&writer) == 0 && !fstatat(writer.directory, "lease", &current, 0));
    CHECK(old.st_ino != current.st_ino);
    CHECK(!close(fd));
    ACTIVE(1);
    CHECK(!fchmodat(writer.directory, "lease", 0644, 0));
    ACTIVE(0);
    CHECK(!dreem_lease_writer_renew(&writer));
    ACTIVE(1);
    fd = openat(writer.directory, "lease", O_RDWR | O_CLOEXEC);
    CHECK(fd >= 0 && read(fd, record, sizeof record) == sizeof record);
    record[8] ^= 1;
    CHECK(lseek(fd, 0, SEEK_SET) == 0 && write(fd, record, sizeof record) == sizeof record && !close(fd));
    ACTIVE(0);
    CHECK(!dreem_lease_writer_renew(&writer));
    ACTIVE(1);
    CHECK(!renameat(writer.directory, "lease", writer.directory, "kept"));
    CHECK(!symlinkat("kept", writer.directory, "lease"));
    ACTIVE(0);
    CHECK(!dreem_lease_writer_renew(&writer));
    ACTIVE(1);
    CHECK(!unlinkat(writer.directory, "kept", 0));
    CHECK(!unlinkat(writer.directory, "lease", 0) && !mkfifoat(writer.directory, "lease", 0600));
    ACTIVE(0);
    CHECK(!dreem_lease_writer_renew(&writer));
    ACTIVE(1);
    CHECK(!fchmod(writer.directory, 0755));
    ACTIVE(0);
    CHECK(dreem_lease_writer_renew(&writer) == -1);
    CHECK(!fchmod(writer.directory, 0700));
    CHECK(!dreem_lease_writer_close(&writer));
    ACTIVE(0);
    CHECK(!dreem_lease_writer_close(&writer));
    CHECK(fd_count() == baseline);
    CHECK(!dreem_lease_writer_open(&writer, argv[1], "02:00:00:00:00:01"));
    ACTIVE(0);
    CHECK(!dreem_lease_writer_close(&writer));
    CHECK(fd_count() == baseline);
    printf("lease-ok %u\n", checks);
    return 0;
}
