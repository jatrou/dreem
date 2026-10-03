/* SPDX-License-Identifier: GPL-2.0-or-later
 * Copyright 2026 Dreem research contributors.
 * Redirect only this test binary's Bluetooth connection calls to a synthetic
 * packet socket. ARM and host execute the unchanged application's connect path.
 */
#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/eventfd.h>
#include <sys/socket.h>
#include <unistd.h>
#include "lib/bluetooth.h"
#include "lib/l2cap.h"

static int simulated_fd = -1;

int __real_socket(int domain, int type, int protocol);
int __real_bind(int fd, const struct sockaddr *address, socklen_t length);
int __real_connect(int fd, const struct sockaddr *address, socklen_t length);
int __real_setsockopt(int fd, int level, int option, const void *value, socklen_t length);
int __real_getsockopt(int fd, int level, int option, void *value, socklen_t *length);

static bool scenario(const char *name)
{
    const char *selected = getenv("DREEM_BT_CONNECT_CASE");
    return selected && !strcmp(selected, name);
}

static void trace(const char *name)
{
    const char *path = getenv("DREEM_BT_CONNECT_TRACE");
    FILE *out = path ? fopen(path, "a") : NULL;
    if (!out) abort();
    fprintf(out, "%s\n", name);
    fclose(out);
}

int __wrap_socket(int domain, int type, int protocol)
{
    uint64_t full = UINT64_MAX-1;
    if (domain != AF_BLUETOOTH)
        return __real_socket(domain, type, protocol);
    trace("socket");
    if (type != (SOCK_SEQPACKET | SOCK_CLOEXEC | SOCK_NONBLOCK) ||
        protocol != BTPROTO_L2CAP) abort();
    if (scenario("socket")) { errno = EPERM; return -1; }
    if (scenario("stall")) {
        simulated_fd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
        if (simulated_fd < 0 || write(simulated_fd, &full, sizeof(full)) != sizeof(full)) abort();
    } else {
        simulated_fd = fcntl(atoi(getenv("DREEM_BT_CONNECT_FD")), F_DUPFD_CLOEXEC, 3);
    }
    return simulated_fd;
}

static void address_check(const struct sockaddr *address, socklen_t length, bool peer)
{
    const struct sockaddr_l2 *l2 = (const struct sockaddr_l2 *)address;
    bdaddr_t expected;
    str2ba(peer ? "02:00:00:00:00:02" : "02:00:00:00:00:01", &expected);
    if (length != sizeof(*l2) || l2->l2_family != AF_BLUETOOTH ||
        l2->l2_cid != htobs(4) || l2->l2_psm ||
        l2->l2_bdaddr_type != (peer ? BDADDR_LE_RANDOM : BDADDR_LE_PUBLIC) ||
        bacmp(&l2->l2_bdaddr, &expected)) abort();
}

int __wrap_bind(int fd, const struct sockaddr *address, socklen_t length)
{
    if (fd != simulated_fd) return __real_bind(fd, address, length);
    trace("bind");
    address_check(address, length, false);
    if (scenario("bind")) { errno = EADDRNOTAVAIL; return -1; }
    return 0;
}

int __wrap_setsockopt(int fd, int level, int option, const void *value, socklen_t length)
{
    const struct bt_security *security = value;
    if (fd != simulated_fd) return __real_setsockopt(fd, level, option, value, length);
    trace("security");
    if (level != SOL_BLUETOOTH || option != BT_SECURITY ||
        length != sizeof(*security) || security->level != BT_SECURITY_MEDIUM) abort();
    if (scenario("security")) { errno = EACCES; return -1; }
    return 0;
}

int __wrap_connect(int fd, const struct sockaddr *address, socklen_t length)
{
    if (fd != simulated_fd) return __real_connect(fd, address, length);
    trace("connect");
    address_check(address, length, true);
    if (scenario("immediate")) return 0;
    errno = scenario("connect") ? ECONNREFUSED : EINPROGRESS;
    return -1;
}

int __wrap_getsockopt(int fd, int level, int option, void *value, socklen_t *length)
{
    if (fd != simulated_fd || level != SOL_SOCKET || option != SO_ERROR)
        return __real_getsockopt(fd, level, option, value, length);
    trace("completion");
    if (*length != sizeof(int)) abort();
    *(int *)value = scenario("soerror") ? ECONNREFUSED : 0;
    return 0;
}
