/* SPDX-License-Identifier: Apache-2.0
 * Independently authored loader fixture. Contains no vendor code or addresses.
 */
#include <elf.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern char **environ;
static unsigned char bss[131077];
static volatile unsigned initialized = 0x23456789;
static volatile unsigned constructed;
static __thread unsigned tls = 9;

__attribute__((constructor)) static void init(void) { constructed = 37; }

static void *worker(void *unused)
{
    (void)unused;
    if (tls != 9)
        return (void *)1;
    tls = 23;
    return 0;
}

int main(int argc, char **argv)
{
    char **environment = environ;
    Elf32_auxv_t *aux;
    Elf32_Phdr *phdr = NULL;
    unsigned phnum = 0, phent = 0, i, found = 0;
    unsigned char *allocation;
    pthread_t thread;
    void *thread_result = (void *)1;
    int patched = argc == 2 && strcmp(argv[1], "patched") == 0;
    while (*environment)
        ++environment;
    aux = (Elf32_auxv_t *)(environment+1);
    for (; aux->a_type; ++aux) {
        if (aux->a_type == AT_PHDR) phdr = (void *)(uintptr_t)aux->a_un.a_val;
        if (aux->a_type == AT_PHNUM) phnum = aux->a_un.a_val;
        if (aux->a_type == AT_PHENT) phent = aux->a_un.a_val;
    }
    if (!phdr || !phnum || phent != sizeof *phdr || constructed != 37 ||
        initialized != 0x23456789 || tls != 9)
        return 10;
    for (i = 0; i < sizeof bss; ++i)
        if (bss[i]) return 11;
    memset(bss, 0x5a, sizeof bss);
    for (i = 0; i < phnum; ++i) {
        const Elf32_Phdr *p = phdr+i;
        if (p->p_type == PT_PHDR && (p->p_vaddr != (uintptr_t)phdr || p->p_filesz != phnum*phent))
            return 12;
        if (p->p_type == PT_LOAD && p->p_vaddr == 0x01000000) {
            if (p->p_flags != (PF_R | PF_X) || (uintptr_t)phdr != p->p_vaddr)
                return 13;
            if (((int (*)(void))(uintptr_t)(p->p_vaddr+4096))() != 42)
                return 14;
            ++found;
        }
    }
    if (found != (unsigned)patched)
        return 15;
    allocation = calloc(1, 1024*1024);
    if (!allocation || allocation[0] || allocation[1024*1024-1])
        return 16;
    memset(allocation, 0x75, 1024*1024);
    free(allocation);
    if (pthread_create(&thread, NULL, worker, NULL) || pthread_join(thread, &thread_result) ||
        thread_result || tls != 9)
        return 17;
    puts(patched ? "overlay-loader-ok" : "original-loader-ok");
    return 0;
}
