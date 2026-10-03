/* SPDX-License-Identifier: Apache-2.0
 * Real pthread cancellation/wakeup test; controller operations remain stubs.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <semaphore.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

const unsigned int dreem_extension_peer_count = 1;
volatile unsigned int stock_state = 6;
static unsigned int active, entered, stopped;
static sem_t event;
extern int dreem_power_off(const char *, unsigned, unsigned, int);
extern int dreem_disable_probe(const char *, unsigned, unsigned, int *);
extern int dreem_event_wait(void *);

int dreem_radio_lease_active(void) { return (int)__atomic_load_n(&active, __ATOMIC_SEQ_CST); }
int stock_system(const char *command)
{
    if (!strcmp(command, "/usr/bin/btmgmt -i hci0 power off"))
        __atomic_add_fetch(&stopped, 1, __ATOMIC_SEQ_CST);
    return 0;
}
int stock_wait(void *s)
{
    while (sem_wait(s)) if (errno != EINTR) return 1;
    return 0;
}
int stock_trywait(void *s)
{
    __atomic_store_n(&entered, 1, __ATOMIC_SEQ_CST);
    return sem_trywait(s);
}
int *stock_errno(void) { return &errno; }
int stock_usleep(unsigned int n) { return usleep(n); }
void stock_testcancel(void) { pthread_testcancel(); }
int stock_cancelstate(int state, int *old_state) { return pthread_setcancelstate(state, old_state); }
void stock_syslog(int level, const char *format, ...) { (void)level; (void)format; }

static void *worker(void *unused)
{
    (void)unused;
    return (void *)(intptr_t)dreem_event_wait(&event);
}

static int wait_for(unsigned int *word)
{
    unsigned i;
    for (i = 0; i < 3000; ++i) {
        if (__atomic_load_n(word, __ATOMIC_SEQ_CST)) return 0;
        usleep(1000);
    }
    return 1;
}

int main(void)
{
    pthread_t thread;
    void *result;
    int ignored;
    unsigned phase;
    for (phase = 0; phase < 3; ++phase) {
        __atomic_store_n(&entered, 0, __ATOMIC_SEQ_CST);
        __atomic_store_n(&stopped, 0, __ATOMIC_SEQ_CST);
        __atomic_store_n(&active, 1, __ATOMIC_SEQ_CST);
        if (sem_init(&event, 0, 0) || dreem_power_off("unused", 0x36cf8, 0x67a30, 0) ||
            pthread_create(&thread, NULL, worker, NULL) || wait_for(&entered)) return 1;
        if (phase == 0) {
            if (pthread_cancel(thread) || pthread_join(thread, &result) || result != PTHREAD_CANCELED) return 2;
        } else {
            if (phase == 1) {
                __atomic_store_n(&active, 0, __ATOMIC_SEQ_CST);
                if (wait_for(&stopped)) return 3;
            }
            if (sem_post(&event) || pthread_join(thread, &result) || result) return 4;
        }
        if (__atomic_load_n(&stopped, __ATOMIC_SEQ_CST) != (unsigned)(phase == 1)) return 5;
        if (dreem_disable_probe("probe", 0, 0, &ignored) || sem_destroy(&event)) return 6;
    }
    puts("thread-guard-ok 3");
    return 0;
}
