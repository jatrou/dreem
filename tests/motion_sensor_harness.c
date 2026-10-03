/* SPDX-License-Identifier: Apache-2.0
 * Synthetic register services for the actual ST driver and checked transport.
 */
#include "motion_sensor.h"
#include <errno.h>
#include <linux/i2c.h>
#include <linux/i2c-dev.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>

static struct dreem_motion_sensor sensor;
static unsigned char regs[256];
static unsigned step, writes, reads, sleeps, reset_delay, reset_pending;
static unsigned fail_step[2], partial[2], corrupt_step, corrupt_byte, corrupt_value;
static int fail_result[2], fail_errno[2], wait_errno;
static unsigned inject_step;
static int injected[3];
static struct { unsigned reg, write, length; int result; unsigned char data[7]; } trace[160];
static void check(int ok) { if (!ok) abort(); }
static void defaults(void) {
    unsigned id=regs[15];
    memset(regs,0,sizeof regs); regs[15]=id; regs[0x20]=7; regs[0x23]=4;
    reset_pending=0;
}
static void produce(int x, int y, int z) {
    if (!(regs[0x20]&0x70)) return;
    int values[3]={x,y,z};
    if (regs[0x27]&8) {
        regs[0x27]|=0xf0;
        if (regs[0x20]&8) return; /* BDU holds unread words in this model. */
    }
    for (unsigned i=0;i<3;++i) {
        regs[0x28+2*i]=(unsigned)values[i];
        regs[0x29+2*i]=(unsigned)values[i]>>8;
    }
    regs[0x27]|=0x0f;
}
int __wrap_nanosleep(const struct timespec *t, struct timespec *remaining) {
    check(t->tv_sec==0 && t->tv_nsec==1000000 && !remaining); sleeps++;
    if (wait_errno) { errno=wait_errno; wait_errno=0; return -1; }
    return 0;
}
int __wrap___nanosleep64(const struct timespec *, struct timespec *)
    __attribute__((alias("__wrap_nanosleep")));
int __wrap_ioctl(int fd, unsigned long request, ...) {
    va_list ap; va_start(ap,request); check(fd==42);
    if (request==I2C_FUNCS) { *va_arg(ap,unsigned long *)=I2C_FUNC_I2C; va_end(ap); return 0; }
    if (request==I2C_SLAVE) { check(va_arg(ap,unsigned long)==sensor.address); va_end(ap); return 0; }
    check(request==I2C_RDWR);
    struct i2c_rdwr_ioctl_data *x=va_arg(ap,struct i2c_rdwr_ioctl_data *); va_end(ap);
    check(x->nmsgs==1 || x->nmsgs==2);
    unsigned writing=x->nmsgs==1, reg=x->msgs[0].buf[0];
    unsigned n=writing ? x->msgs[0].len-1u : x->msgs[1].len;
    unsigned char *p=writing ? x->msgs[0].buf+1 : x->msgs[1].buf;
    check(x->msgs[0].addr==sensor.address && x->msgs[0].flags==0 && n>0 && n<=7 && step<160);
    if (!writing) check(x->msgs[0].len==1 && x->msgs[1].addr==sensor.address && x->msgs[1].flags==I2C_M_RD);
    step++;
    if (step==inject_step) { produce(injected[0],injected[1],injected[2]); inject_step=0; }
    unsigned use=n; int result=writing ? 1 : 2, error=0;
    for (unsigned i=0;i<2;++i) if (step==fail_step[i]) {
        use=partial[i]; result=fail_result[i]; error=fail_errno[i]; fail_step[i]=0;
    }
    if (use>n) use=n;
    if (writing) {
        writes++;
        check(n==1 && ((reg>=0x1e && reg<=0x26) || reg==0x2e));
        if (use) {
            if (reg==0x24 && (p[0]&0x40)) {
                reset_pending=reset_delay;
                if (!reset_pending) defaults(); else regs[0x24]=0x40;
            } else regs[reg]=p[0];
        }
    } else {
        reads++;
        if (reg==0x24 && (regs[0x24]&0x40) && reset_pending!=0xffffffffu &&
            reset_pending && --reset_pending==0) defaults();
        unsigned increment=regs[0x23]&4;
        for (unsigned i=0;i<use;++i) p[i]=regs[reg+(increment ? i : 0)];
        if (reg==0x28 && use==6) regs[0x27]=0;
    }
    if (step==corrupt_step) { check(corrupt_byte<n); p[corrupt_byte]=corrupt_value; corrupt_step=0; }
    trace[step-1].reg=reg; trace[step-1].write=writing; trace[step-1].length=n; trace[step-1].result=result;
    memcpy(trace[step-1].data,p,n);
    errno=error; return result;
}
int __wrap___ioctl_time64(int, unsigned long, ...)
    __attribute__((alias("__wrap_ioctl")));
static void report(int result, const struct dreem_motion_sample *sample, int unchanged) {
    printf("{\"result\":%d,\"state\":%u,\"identified\":%u,\"last_error\":%d,\"cleanup_error\":%d,"
           "\"settling_rows\":%u,\"ctrl1\":%u,\"steps\":%u,\"writes\":%u,\"reads\":%u,\"sleeps\":%u,"
           "\"unchanged\":%s,\"trace\":[",result,sensor.state,sensor.identified,sensor.last_error,
           sensor.cleanup_error,sensor.settling_rows,regs[0x20],step,writes,reads,sleeps,unchanged ? "true":"false");
    for (unsigned i=0;i<step;++i) {
        printf("%s{\"op\":\"%c\",\"reg\":%u,\"result\":%d,\"data\":[",i ? ",":"",trace[i].write ? 'W':'R',trace[i].reg,trace[i].result);
        for (unsigned j=0;j<trace[i].length;++j) printf("%s%u",j ? ",":"",trace[i].data[j]);
        fputs("]}",stdout);
    }
    fputs("]",stdout);
    if (sample && !result) printf(",\"xyz\":[%d,%d,%d],\"flags\":%u,\"before\":%u,\"after\":%u",
                                  sample->xyz[0],sample->xyz[1],sample->xyz[2],sample->flags,sample->status_before,sample->status_after);
    puts("}");
}
int main(void) {
    char line[200], op;
    while (fgets(line,sizeof line,stdin)) {
        check(sscanf(line," %c",&op)==1);
        if (op=='N') {
            unsigned id,address; check(sscanf(line,"N %u %u %u",&id,&reset_delay,&address)==3);
            memset(&sensor,0,sizeof sensor); regs[15]=id; defaults();
            step=writes=reads=sleeps=wait_errno=corrupt_step=inject_step=0;
            memset(fail_step,0,sizeof fail_step);
            check(dreem_motion_sensor_init(&sensor,42,address)==0);
        } else if (op=='S') {
            struct dreem_motion_profile p; check(sscanf(line,"S %u %u %u",&p.samples_per_second,&p.full_scale_g,&p.high_resolution)==3);
            step=0; int result=dreem_motion_sensor_start(&sensor,&p); report(result,NULL,0);
        } else if (op=='T') {
            step=0; int result=dreem_motion_sensor_stop(&sensor); report(result,NULL,0);
        } else if (op=='R') {
            struct dreem_motion_sample sample, before; memset(&sample,0xa5,sizeof sample); before=sample;
            step=0; int result=dreem_motion_sensor_read(&sensor,&sample);
            report(result,&sample,!memcmp(&sample,&before,sizeof sample));
        } else if (op=='P' || op=='I') {
            if (op=='P') { int x,y,z; check(sscanf(line,"P %d %d %d",&x,&y,&z)==3); produce(x,y,z); }
            else check(sscanf(line,"I %u %d %d %d",&inject_step,&injected[0],&injected[1],&injected[2])==4);
        } else if (op=='V') {
            unsigned reg,value; check(sscanf(line,"V %u %u",&reg,&value)==2 && reg<256 && value<256); regs[reg]=value;
        } else if (op=='F' || op=='G') {
            unsigned i=op=='G'; check(sscanf(line+1,"%u %d %d %u",&fail_step[i],&fail_result[i],&fail_errno[i],&partial[i])==4);
        } else if (op=='C') check(sscanf(line,"C %u %u %u",&corrupt_step,&corrupt_byte,&corrupt_value)==3);
        else if (op=='W') check(sscanf(line,"W %d",&wait_errno)==1);
        else if (op=='Q') {
            struct dreem_motion_sensor s; struct dreem_motion_profile p={50,2,0}; struct dreem_motion_sample sample;
            step=0;
            check(dreem_motion_sensor_init(NULL,42,30)==-EINVAL);
            check(dreem_motion_sensor_init(&s,-1,30)==-EINVAL);
            check(dreem_motion_sensor_init(&s,42,31)==-EINVAL);
            check(dreem_motion_sensor_start(NULL,&p)==-EINVAL);
            check(dreem_motion_sensor_start(&sensor,NULL)==-EINVAL);
            check(dreem_motion_sensor_read(NULL,&sample)==-EINVAL);
            check(dreem_motion_sensor_read(&sensor,NULL)==-EINVAL);
            check(dreem_motion_sensor_stop(NULL)==-EINVAL);
            report(0,NULL,1);
        } else abort();
    }
    return ferror(stdin) || fflush(stdout) || ferror(stdout);
}
