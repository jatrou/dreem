/* SPDX-License-Identifier: Apache-2.0
 * ARM Linux EABI file reader; uses kernel ABI, not a guessed libc stat layout.
 * Raw syscalls return negative errno and do not modify the caller's libc errno.
 */
#include "radio_lease.h"
#include <asm/stat.h>
#include <asm/unistd.h>
#include <linux/fcntl.h>
#include <stddef.h>

extern long dreem_syscall4(long number, long a, long b, long c, long d);
extern int dreem_is_extension_peer(const char *address);
_Static_assert(sizeof(long) == 4 && sizeof(struct stat64) == 104 &&
               offsetof(struct stat64, st_uid) == 24 &&
               offsetof(struct stat64, st_size) == 48, "ARM kernel stat64 ABI differs");

static int valid_fd(long fd, int directory, long owner)
{
    struct stat64 info;
    if (dreem_syscall4(__NR_fstat64, fd, (long)&info, 0, 0)) return 0;
    return info.st_uid == (unsigned long)owner &&
        (directory ? (info.st_mode & 0177777) == 0040700 :
                     (info.st_mode & 0177777) == 0100600 && info.st_nlink == 1 &&
                     info.st_size == DREEM_LEASE_BYTES);
}

int dreem_radio_lease_active(void)
{
    unsigned char record[DREEM_LEASE_BYTES+1], boot_text[38], boot[16];
    char peer[18];
    long directory = -1, fd = -1, owner, count, now[2];
    int result = 0;
    owner = dreem_syscall4(__NR_geteuid32, 0, 0, 0, 0);
    if (owner < 0) return 0;
    directory = dreem_syscall4(__NR_openat, AT_FDCWD, (long)DREEM_LEASE_DIRECTORY,
                              O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK, 0);
    if (directory < 0 || !valid_fd(directory, 1, owner)) goto done;
    fd = dreem_syscall4(__NR_openat, directory, (long)"lease",
                       O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK, 0);
    if (fd < 0 || !valid_fd(fd, 0, owner)) goto done;
    count = dreem_syscall4(__NR_read, fd, (long)record, sizeof record, 0);
    if (dreem_syscall4(__NR_close, fd, 0, 0, 0)) { fd = -1; goto done; }
    fd = -1;
    if (count != DREEM_LEASE_BYTES) goto done;
    fd = dreem_syscall4(__NR_openat, AT_FDCWD, (long)"/proc/sys/kernel/random/boot_id",
                       O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK, 0);
    if (fd < 0) goto done;
    count = dreem_syscall4(__NR_read, fd, (long)boot_text, sizeof boot_text, 0);
    if (count != 37 || !dreem_lease_boot_id(boot_text, boot)) goto done;
    if (dreem_syscall4(__NR_clock_gettime, 1, (long)now, 0, 0) || now[0] < 0 ||
        now[1] < 0 || now[1] >= 1000000000) goto done;
    result = dreem_lease_decode(record, boot, (uint32_t)now[0], (uint32_t)now[1], peer) &&
             dreem_is_extension_peer(peer);
done:
    if (fd >= 0 && dreem_syscall4(__NR_close, fd, 0, 0, 0)) result = 0;
    if (directory >= 0 && dreem_syscall4(__NR_close, directory, 0, 0, 0)) result = 0;
    return result;
}
