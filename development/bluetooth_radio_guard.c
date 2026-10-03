/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Dreem research contributors.
 * Keep an already enabled controller alive for an explicit short sensor lease.
 * Does not start a controller or override deliberate recovery/shutdown.
 */
#include <stdint.h>

extern const unsigned int dreem_extension_peer_count;
extern int dreem_radio_lease_active(void);
extern int stock_system(const char *command);
extern int stock_wait(void *semaphore);
extern int stock_trywait(void *semaphore);
extern int *stock_errno(void);
extern int stock_usleep(unsigned int microseconds);
extern void stock_testcancel(void);
extern int stock_cancelstate(int state, int *old_state);
extern void stock_syslog(int priority, const char *format, ...);
extern volatile unsigned int stock_state;

/* Only this new state uses atomics. The stock application's synchronization
 * is unchanged. A forced power-off clears deferral before invoking system(). */
static unsigned int deferred;
static const char off[] = "/usr/bin/btmgmt -i hci0 power off";

static unsigned int held(void) { return __atomic_load_n(&deferred, __ATOMIC_SEQ_CST); }
static void hold(unsigned int value) { __atomic_store_n(&deferred, value, __ATOMIC_SEQ_CST); }

static int recording_pause(unsigned int caller, unsigned int pause_caller)
{
    return caller == 0x36cf8 && (pause_caller == 0x67a30 || pause_caller == 0x676e8) &&
           stock_state == 6;
}

int dreem_disable_probe(const char *command, unsigned int caller, unsigned int pause_caller,
                        int *probe_result)
{
    int result;
    /* Revoke before the original probe gate too: its unusual result==1 path
     * skips the power-off call entirely. Deliberate stop must not leave a
     * previously deferred lease owned by this event loop. */
    if (!recording_pause(caller, pause_caller)) hold(0);
    result = stock_system(command);
    *probe_result = result;
    if (result) hold(0);
    return result;
}

int dreem_power_off(const char *command, unsigned int caller, unsigned int pause_caller,
                    int probe_result)
{
    if (!probe_result && dreem_extension_peer_count && recording_pause(caller, pause_caller) &&
        dreem_radio_lease_active()) {
        hold(1);
        return 0;
    }
    hold(0);
    return stock_system(command);
}

int dreem_enable_probe(const char *command)
{
    static const char *const restore[] = {
        "/usr/bin/btmgmt -i hci0 connectable on",
        "/usr/bin/btmgmt -i hci0 bondable on",
        "/usr/bin/btmgmt -i hci0 discov on",
        "/usr/bin/btmgmt -i hci0 advertising on"
    };
    int result = stock_system(command);
    unsigned int i;
    if (held()) {
        /* The original enable short-circuits when hci0 is UP. Restore the
         * peripheral settings disabled by pause without cycling sensor power.
         * The companion window now owns the radio, even on a restore error. */
        hold(0);
        if (!result) {
            for (i = 0; i < sizeof restore/sizeof restore[0]; ++i)
                if (stock_system(restore[i]))
                    stock_syslog(4, "%s", "Dreem extension: Bluetooth advertising restore failed");
        }
    }
    /* A manufactured failure here would make stock enable power-cycle hci0.
     * Keep its original probe result; additional failures are logged. */
    return result;
}

int dreem_event_wait(void *semaphore)
{
    while (held() && stock_state == 6) {
        int error;
        stock_testcancel();
        if (!stock_trywait(semaphore)) return 0;
        error = *stock_errno();
        if (error != 11 && error != 4) break; /* EAGAIN / EINTR on ARM Linux. */
        *stock_errno() = 0;
        /* A queued original event takes precedence over lease reconciliation.
         * Only the original recording-idle state permits this deferred stop. */
        if (!held() || stock_state != 6) break;
        if (!dreem_radio_lease_active()) {
            int previous, result;
            /* Match stock controller operations: finish the command and state
             * bookkeeping before permitting deferred cancellation again. */
            if (stock_cancelstate(1, &previous)) {
                stock_syslog(4, "%s", "Dreem extension: cannot guard Bluetooth power-off cancellation");
                stock_usleep(1000000);
                continue;
            }
            result = stock_system(off);
            if (!result) hold(0);
            if (stock_cancelstate(previous, (int *)0))
                stock_syslog(4, "%s", "Dreem extension: cannot restore cancellation state");
            if (!result) break;
            stock_syslog(4, "%s", "Dreem extension: deferred Bluetooth power-off failed");
            stock_usleep(1000000);
        } else {
            stock_usleep(250000);
        }
    }
    return stock_wait(semaphore);
}
