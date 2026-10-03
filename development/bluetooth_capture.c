/* SPDX-License-Identifier: GPL-2.0-or-later
 * Copyright 2026 Dreem research contributors.
 * Independent bounded GATT acquisition using the public BlueZ shared library.
 * Values are raw bytes with local receive times, not calibrated sensor samples.
 */
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#include "radio_lease_writer.h"

#include "lib/bluetooth.h"
#include "lib/l2cap.h"
#include "lib/uuid.h"
#include "src/shared/mainloop.h"
#include "src/shared/att.h"
#include "src/shared/queue.h"
#include "src/shared/gatt-db.h"
#include "src/shared/gatt-client.h"

struct capture {
    FILE *output;
    struct bt_att *att;
    struct gatt_db *db;
    struct bt_gatt_client *client;
    bt_uuid_t service, characteristic;
    uint64_t deadline;
    uint32_t count, maximum, matches, changed_matches;
    uint16_t handle, changed_handle;
    uint8_t properties, att_error;
    bool notify, done, output_failed;
    int status;
    int connecting_fd;
    struct dreem_lease_writer lease;
    const char *reason;
};

static uint64_t now_ns(void)
{
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t))
        return 0;
    return (uint64_t)t.tv_sec * UINT64_C(1000000000) + (uint64_t)t.tv_nsec;
}

static void finish(struct capture *c, const char *reason, int status)
{
    if (c->done)
        return;
    c->done = true;
    c->reason = reason;
    c->status = status;
    mainloop_quit();
}

static bool flush_output(struct capture *c)
{
    if (ferror(c->output) || fflush(c->output)) {
        c->output_failed = true;
        finish(c, "output_error", 1);
        return false;
    }
    return true;
}

static void value_received(struct capture *c, const uint8_t *value, uint16_t length,
                           const char *kind)
{
    uint64_t time;
    unsigned int i;
    if (c->done)
        return;
    time = now_ns();
    if (!time || length > 512 || (length && !value)) {
        finish(c, "invalid_value_or_clock", 1);
        return;
    }
    if (time >= c->deadline) {
        finish(c, c->count ? "duration" : "timeout", c->count ? 0 : 1);
        return;
    }
    fprintf(c->output, "{\"type\":\"value\",\"kind\":\"%s\",\"sequence\":%" PRIu32
            ",\"receive_monotonic_ns\":%" PRIu64 ",\"handle\":%u,\"hex\":\"",
            kind, c->count, time, c->handle);
    for (i = 0; i < length; i++)
        fprintf(c->output, "%02x", value[i]);
    fputs("\"}\n", c->output);
    if (!flush_output(c))
        return;
    c->count++;
    if (!c->notify || c->count == c->maximum)
        finish(c, c->notify ? "value_limit" : "read_complete", 0);
}

static void read_complete(bool success, uint8_t error, const uint8_t *value,
                          uint16_t length, void *data)
{
    struct capture *c = data;
    if (!success) {
        c->att_error = error;
        finish(c, "read_error", 1);
        return;
    }
    value_received(c, value, length, "read");
}

static void notified(uint16_t handle, const uint8_t *value, uint16_t length, void *data)
{
    struct capture *c = data;
    if (handle != c->handle) {
        finish(c, "unexpected_handle", 1);
        return;
    }
    value_received(c, value, length, "notification_or_indication");
}

static void subscribed(uint16_t error, void *data)
{
    struct capture *c = data;
    if (error) {
        c->att_error = (uint8_t)error;
        finish(c, "subscribe_error", 1);
        return;
    }
    fprintf(c->output, "{\"type\":\"subscribed\",\"handle\":%u}\n", c->handle);
    flush_output(c);
}

static void characteristic_found(struct gatt_db_attribute *attr, void *data)
{
    struct capture *c = data;
    bt_uuid_t uuid;
    uint16_t value;
    uint8_t properties;
    if (!gatt_db_attribute_get_char_data(attr, NULL, &value, &properties, NULL, &uuid))
        return;
    if (bt_uuid_cmp(&uuid, &c->characteristic))
        return;
    c->matches++;
    c->handle = value;
    c->properties = properties;
}

static void service_found(struct gatt_db_attribute *attr, void *data)
{
    gatt_db_service_foreach_char(attr, characteristic_found, data);
}

static void changed_characteristic(struct gatt_db_attribute *attr, void *data)
{
    struct capture *c = data;
    bt_uuid_t uuid, changed;
    uint16_t handle;
    bt_uuid16_create(&changed, 0x2a05);
    if (gatt_db_attribute_get_char_data(attr, NULL, &handle, NULL, NULL, &uuid) &&
        !bt_uuid_cmp(&uuid, &changed)) {
        c->changed_handle = handle;
        c->changed_matches++;
    }
}

static void changed_service(struct gatt_db_attribute *attr, void *data)
{
    gatt_db_service_foreach_char(attr, changed_characteristic, data);
}

static void observe_update(uint8_t opcode, const void *data, uint16_t length, void *user_data)
{
    struct capture *c = user_data;
    const uint8_t *pdu = data;
    uint16_t handle;
    (void)opcode;
    if (!pdu || length < 2) {
        finish(c, "malformed_notification", 1);
        return;
    }
    handle = (uint16_t)pdu[0] | (uint16_t)pdu[1] << 8;
    if (c->changed_handle && handle == c->changed_handle)
        finish(c, "service_changed", 1);
}

static void ready(bool success, uint8_t error, void *data)
{
    struct capture *c = data;
    unsigned int id;
    bt_uuid_t gatt_service;
    if (c->done)
        return;
    if (!success) {
        c->att_error = error;
        finish(c, "discovery_error", 1);
        return;
    }
    gatt_db_foreach_service(c->db, &c->service, service_found, c);
    if (c->matches != 1) {
        finish(c, c->matches ? "ambiguous_characteristic" : "characteristic_missing", 1);
        return;
    }
    if (!(c->properties & (c->notify ? 0x30 : 0x02))) {
        finish(c, "unsupported_operation", 1);
        return;
    }
    bt_uuid16_create(&gatt_service, 0x1801);
    gatt_db_foreach_service(c->db, &gatt_service, changed_service, c);
    if (c->changed_matches > 1) {
        finish(c, "ambiguous_service_changed", 1);
        return;
    }
    fprintf(c->output, "{\"type\":\"selected\",\"handle\":%u,\"properties\":%u}\n",
            c->handle, c->properties);
    if (!flush_output(c))
        return;
    if (c->notify)
        id = bt_gatt_client_register_notify(c->client, c->handle, subscribed, notified, c, NULL);
    else
        id = bt_gatt_client_read_long_value(c->client, c->handle, 0, read_complete, c, NULL);
    if (!id)
        finish(c, "request_rejected", 1);
}

static void disconnected(int error, void *data)
{
    (void)error;
    finish(data, "disconnected", 1);
}

static void service_added(struct gatt_db_attribute *attr, void *data)
{
    /* BlueZ 5.52 dispatches both database callbacks without NULL checks. */
    (void)attr;
    (void)data;
}

static void service_removed(struct gatt_db_attribute *attr, void *data)
{
    struct capture *c = data;
    uint16_t start, end;
    if (c->handle && gatt_db_attribute_get_service_handles(attr, &start, &end) &&
        start <= c->handle && c->handle <= end)
        finish(c, "service_changed", 1);
}

static void deadline_expired(int id, void *data)
{
    struct capture *c = data;
    (void)id;
    finish(c, c->count ? "duration" : "timeout", c->count ? 0 : 1);
}

static void renew_radio_lease(int id, void *data)
{
    struct capture *c = data;
    if (c->done) return;
    if (dreem_lease_writer_renew(&c->lease) || mainloop_modify_timeout(id, 1000))
        finish(c, "radio_lease_error", 1);
}

static void interrupted(int signal_number, void *data)
{
    finish(data, "interrupted", 128 + signal_number);
}

static bool integer(const char *text, uint32_t low, uint32_t high, uint32_t *out)
{
    char *end;
    unsigned long value;
    if (!text[0] || strspn(text, "0123456789") != strlen(text))
        return false;
    errno = 0;
    value = strtoul(text, &end, 10);
    if (errno || *end || value < low || value > high)
        return false;
    *out = (uint32_t)value;
    return true;
}

static bool start_gatt(struct capture *c, int fd)
{
    int flags = fcntl(fd, F_GETFL);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) {
        close(fd);
        return false;
    }
    c->att = bt_att_new(fd, false);
    if (!c->att || !bt_att_set_close_on_unref(c->att, true)) {
        close(fd);
        return false;
    }
    if (!bt_att_register_disconnect(c->att, disconnected, c, NULL))
        return false;
    if (!bt_att_register(c->att, BT_ATT_OP_HANDLE_VAL_NOT, observe_update, c, NULL) ||
        !bt_att_register(c->att, BT_ATT_OP_HANDLE_VAL_IND, observe_update, c, NULL))
        return false;
    c->db = gatt_db_new();
    if (!c->db || !gatt_db_register(c->db, service_added, service_removed, c, NULL))
        return false;
    c->client = bt_gatt_client_new(c->db, c->att, 23);
    return c->client && bt_gatt_client_ready_register(c->client, ready, c, NULL);
}

static void connected(int fd, uint32_t events, void *data)
{
    struct capture *c = data;
    int error = 0;
    socklen_t size = sizeof(error);
    mainloop_remove_fd(fd);
    c->connecting_fd = -1;
    if (c->done || (events & (EPOLLERR | EPOLLHUP)) ||
        getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &size) || error) {
        close(fd);
        finish(c, "connect_error", 1);
    } else if (!start_gatt(c, fd)) {
        finish(c, "initialization_error", 1);
    }
}

static int connect_peer(const bdaddr_t *local, const bdaddr_t *peer, uint8_t type,
                        uint8_t security, bool *pending)
{
    struct sockaddr_l2 address = { .l2_family = AF_BLUETOOTH,
                                  .l2_cid = htobs(4), .l2_bdaddr_type = BDADDR_LE_PUBLIC };
    struct bt_security settings = { .level = security };
    int fd;
    fd = socket(AF_BLUETOOTH, SOCK_SEQPACKET | SOCK_CLOEXEC | SOCK_NONBLOCK, BTPROTO_L2CAP);
    if (fd < 0)
        return -1;
    bacpy(&address.l2_bdaddr, local);
    if (bind(fd, (struct sockaddr *)&address, sizeof(address)) ||
        setsockopt(fd, SOL_BLUETOOTH, BT_SECURITY, &settings, sizeof(settings)))
        goto fail;
    address.l2_bdaddr_type = type;
    bacpy(&address.l2_bdaddr, peer);
    if (!connect(fd, (struct sockaddr *)&address, sizeof(address))) {
        *pending = false;
        return fd;
    }
    if (errno != EINPROGRESS)
        goto fail;
    *pending = true;
    return fd;
fail:
    close(fd);
    return -1;
}

static int socket_family(int fd)
{
    struct sockaddr_storage address;
    socklen_t size = sizeof(address), option_size;
    int type;
    option_size = sizeof(type);
    if (fd < 3 || getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &option_size) ||
        type != SOCK_SEQPACKET || getpeername(fd, (struct sockaddr *)&address, &size))
        return -1;
    if (address.ss_family == AF_UNIX)
        return AF_UNIX;
    if (address.ss_family == AF_BLUETOOTH && size >= sizeof(struct sockaddr_l2)) {
        struct sockaddr_l2 *l2 = (struct sockaddr_l2 *)&address;
        if (l2->l2_cid == htobs(4) && l2->l2_psm == 0 &&
            (l2->l2_bdaddr_type == BDADDR_LE_PUBLIC || l2->l2_bdaddr_type == BDADDR_LE_RANDOM))
            return AF_BLUETOOTH;
    }
    return -1;
}

static void usage(void)
{
    puts("Usage: dreem-bluetooth-capture --service UUID --characteristic UUID\n"
         "  --mode read|notify --duration-ms 1..3600000 --max-values 1..1000000 --output NEW_FILE\n"
         "  Connection: --local PUBLIC_ADDRESS --peer ADDRESS --peer-type public|random\n"
         "              --security low|medium|high\n"
         "              [--radio-lease PRIVATE_DIRECTORY] (requires matching core integration)\n"
         "  Or: --att-fd N (exclusive connected LE ATT or simulated UNIX seqpacket socket)\n"
         "Read mode takes one complete value and requires --max-values 1.\n"
         "Notify mode enables the characteristic configuration and closes the connection on exit.\n"
         "Output contains raw values and local receive times; continuity and calibration are unknown.");
}

int main(int argc, char **argv)
{
    enum { SERVICE = 256, CHARACTERISTIC, MODE, DURATION, MAXIMUM, OUTPUT,
           LOCAL, PEER, PEER_TYPE, SECURITY, ATT_FD, RADIO_LEASE };
    const struct option options[] = {
        {"service", 1, NULL, SERVICE}, {"characteristic", 1, NULL, CHARACTERISTIC},
        {"mode", 1, NULL, MODE}, {"duration-ms", 1, NULL, DURATION},
        {"max-values", 1, NULL, MAXIMUM}, {"output", 1, NULL, OUTPUT},
        {"local", 1, NULL, LOCAL}, {"peer", 1, NULL, PEER},
        {"peer-type", 1, NULL, PEER_TYPE}, {"security", 1, NULL, SECURITY},
        {"att-fd", 1, NULL, ATT_FD}, {"radio-lease", 1, NULL, RADIO_LEASE},
        {"help", 0, NULL, 'h'}, {NULL, 0, NULL, 0}
    };
    struct capture c = { .status = 1, .reason = "initialization_error", .connecting_fd = -1,
                         .lease = { .directory = -1, .lock = -1 } };
    bdaddr_t local, peer;
    const char *output = NULL;
    const char *lease_directory = NULL;
    char lease_peer[18];
    char service_text[MAX_LEN_UUID_STR], characteristic_text[MAX_LEN_UUID_STR];
    uint32_t seen = 0, milliseconds = 0, input_fd = 0;
    uint8_t type = 0, security = 0;
    uint64_t time;
    bool pending = false;
    int option, fd, file_fd, family, timer, loop_result;
    while ((option = getopt_long(argc, argv, "h", options, NULL)) != -1) {
        if (option == 'h') { usage(); return 0; }
        if (option < SERVICE || option > RADIO_LEASE || seen & (1u << (option-SERVICE)))
            goto invalid;
        seen |= 1u << (option-SERVICE);
        switch (option) {
        case SERVICE: if (bt_string_to_uuid(&c.service, optarg)) goto invalid; break;
        case CHARACTERISTIC: if (bt_string_to_uuid(&c.characteristic, optarg)) goto invalid; break;
        case MODE:
            if (strcmp(optarg, "read") && strcmp(optarg, "notify")) goto invalid;
            c.notify = !strcmp(optarg, "notify"); break;
        case DURATION: if (!integer(optarg, 1, 3600000, &milliseconds)) goto invalid; break;
        case MAXIMUM: if (!integer(optarg, 1, 1000000, &c.maximum)) goto invalid; break;
        case OUTPUT: output = optarg; break;
        case LOCAL: if (str2ba(optarg, &local) || !bacmp(&local, BDADDR_ANY)) goto invalid; break;
        case PEER: if (str2ba(optarg, &peer) || !bacmp(&peer, BDADDR_ANY)) goto invalid; break;
        case PEER_TYPE:
            if (!strcmp(optarg, "public")) type = BDADDR_LE_PUBLIC;
            else if (!strcmp(optarg, "random")) type = BDADDR_LE_RANDOM;
            else goto invalid;
            break;
        case SECURITY:
            if (!strcmp(optarg, "low")) security = BT_SECURITY_LOW;
            else if (!strcmp(optarg, "medium")) security = BT_SECURITY_MEDIUM;
            else if (!strcmp(optarg, "high")) security = BT_SECURITY_HIGH;
            else goto invalid;
            break;
        case ATT_FD: if (!integer(optarg, 3, INT_MAX, &input_fd)) goto invalid; break;
        case RADIO_LEASE: lease_directory = optarg; break;
        }
    }
    if (optind != argc || (seen & 0x3f) != 0x3f ||
        (input_fd ? seen != 0x43f : (seen & ~0x800u) != 0x3ff) || (!c.notify && c.maximum != 1))
        goto invalid;
    family = input_fd ? socket_family((int)input_fd) : AF_BLUETOOTH;
    if (family < 0) { fputs("Invalid connected ATT socket\n", stderr); return 2; }
    time = now_ns();
    if (!time) return 1;
    c.deadline = time + (uint64_t)milliseconds*1000000;
    file_fd = open(output, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (file_fd < 0) { fputs("Cannot create private output file\n", stderr); return 1; }
    c.output = fdopen(file_fd, "w");
    if (!c.output) { close(file_fd); return 1; }
    signal(SIGPIPE, SIG_IGN);
    mainloop_init();
    bt_uuid_to_string(&c.service, service_text, sizeof(service_text));
    bt_uuid_to_string(&c.characteristic, characteristic_text, sizeof(characteristic_text));
    fprintf(c.output, "{\"type\":\"request\",\"transport\":\"%s\",\"mode\":\"%s\","
            "\"service\":\"%s\",\"characteristic\":\"%s\",\"duration_ms\":%" PRIu32
            ",\"max_values\":%" PRIu32 ",\"continuity\":\"unknown\",\"calibration\":\"unknown\"}\n",
            family == AF_UNIX ? "simulation" : "bluetooth", c.notify ? "notify" : "read",
            service_text, characteristic_text, milliseconds, c.maximum);
    if (!flush_output(&c)) goto cleanup_loop;
    time = now_ns();
    if (!time || time >= c.deadline) { finish(&c, "timeout", 1); goto cleanup_loop; }
    timer = mainloop_add_timeout((unsigned int)((c.deadline-time+999999)/1000000),
                                deadline_expired, &c, NULL);
    if (timer < 0) goto cleanup_loop;
    if (lease_directory) {
        ba2str(&peer, lease_peer);
        if (dreem_lease_writer_open(&c.lease, lease_directory, lease_peer) ||
            mainloop_add_timeout(1000, renew_radio_lease, &c, NULL) < 0) {
            finish(&c, "radio_lease_error", 1);
            goto cleanup_loop;
        }
    }
    fd = input_fd ? (int)input_fd : connect_peer(&local, &peer, type, security, &pending);
    if (fd < 0) { finish(&c, "connect_error", 1); goto cleanup_loop; }
    if (pending) {
        c.connecting_fd = fd;
        if (mainloop_add_fd(fd, EPOLLOUT, connected, &c, NULL)) goto cleanup_loop;
    } else if (!start_gatt(&c, fd)) {
        goto cleanup_loop;
    }
    loop_result = mainloop_run_with_signal(interrupted, &c);
    if (loop_result || !c.done) {
        c.status = 1;
        c.reason = "event_loop_error";
        if (loop_result < 0) goto cleanup_loop;
    }
    goto cleanup;
cleanup_loop:
    mainloop_quit();
    mainloop_run();
cleanup:
    c.done = true;
    bt_gatt_client_unref(c.client);
    gatt_db_unref(c.db);
    bt_att_unref(c.att);
    if (c.connecting_fd >= 0) close(c.connecting_fd);
    if (dreem_lease_writer_close(&c.lease)) {
        c.status = 1;
        c.reason = "radio_lease_cleanup_error";
    }
    fprintf(c.output, "{\"type\":\"end\",\"reason\":\"%s\",\"values\":%" PRIu32
            ",\"att_error\":%u,\"status\":%d}\n", c.reason, c.count, c.att_error, c.status);
    if (ferror(c.output) || fflush(c.output) || fsync(fileno(c.output))) c.output_failed = true;
    if (fclose(c.output)) c.output_failed = true;
    if (c.output_failed) { fputs("Capture output failed\n", stderr); return 1; }
    return c.status;
invalid:
    fputs("Invalid or incomplete options; use --help\n", stderr);
    return 2;
}
