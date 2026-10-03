#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check original sensor-owner retirement using bounded, isolated ARM execution.

Only the hash-pinned private core is accepted. Thread services are modeled:
cancel requests do not terminate a thread; successful joins establish retirement.
No original executable is launched and no adapter, process or thread is stopped.
"""
import argparse
import errno
import hashlib
import itertools
import json
from pathlib import Path
import struct

from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_PC, UC_ARM_REG_LR
from verify_optical_transport import Stock, REGISTERS, DATA, CORE_SHA256, require

THREADS = {
    'eeg': (0xecb064, 0xecb068, 0xecb06c, 103),
    'motion': (0xecb074, 0xecb078, 0xecb07c, 104),
    'pulse': (0xecb084, 0xecb088, 0xecb08c, 106),
    'storage': (0xecb094, 0xecb098, 0xecb09c, 102),
    'microphone': (0xecb0a0, 0xecb0a4, 0xecb0a8, 105),
    'record': (0xecb0ac, 0xecb0b0, 0xecb0b4, 101),
}
RANGES = ((0x905d0,0x90758), (0x90bf4,0x90e2c), (0x901d0,0x90288),
          (0x2c524,0x2cf50))
SERVICES = {0x16e90:'cancel', 0x15f78:'join', 0x16944:'clock', 0x16cd4:'close',
            0x8616c:'destroy', 0x16e78:'strerror', 0x8ef28:'power_event',
            0x954d4:'alarm_stop', 0x74b70:'sip_stop', 0x1b5b8:'m4_stop',
            0x270c0:'record_cleanup', 0x89db0:'wlan_enable', 0x2b824:'nap_unload',
            0x93854:'record_tail', 0x90758:'pulse_restart'}
CONTROL_SHA256 = {
    'S99_dreem':'06db035ca120f31fde838000e26efab8b111604f45ffd16ec13a9ad83ef9a6dc',
    'S99_watchdog':'7f444b5eaa959944e70434c59fd1bf15983fa4ff514d515d8aa66a1a3f07a4bf',
    'S15watchdog':'9ff88f4e5fe5e69ac88cdb70d8dbb0e50efe067a4c2e8a6c46cdc5f79b0a7424',
    'mpu_watchdog.sh':'52cbb0baca66143060d7a900951e5770f101b633a3e43ff3a4e5e087bff301c9',
}


def verify_control_scripts(directory):
    """Pin the scripts underlying the manual control-flow review; never run them."""
    for name,expected in CONTROL_SHA256.items():
        require(hashlib.sha256((directory/name).read_bytes()).hexdigest()==expected,
                'unreviewed startup/control script: '+name)
    return {'script_sha256':CONTROL_SHA256,'method':'hash-pinned manual shell control-flow review',
            'scripts_executed':False,'core_stop_sends_TERM_before_watchdog_hold':True,
            'core_stop_waits_for_process_exit':False,
            'core_stop_final_echo_can_mask_signal_or_write_failure':True,
            'mpu_supervisor_restarts_absent_core_unless_counter_is_42':True,
            'mpu_supervisor_start_resets_counter_to_zero':True,
            'separate_hardware_watchdog_daemon_present':True,
            'live_supervisor_configuration_verified':False}


class Ownership(Stock):
    def __init__(self, path):
        super().__init__(path, additional_ranges=RANGES, additional_services=SERVICES)
        self.cpu.mem_map(0xecb000,0x2000,3)
        self.core_context=self.get(0x2cf4c)
        require(self.core_context==0xecbf78, 'unexpected context literal')

    def get(self, address):
        return struct.unpack('<I',self.cpu.mem_read(address,4))[0]

    def prepare(self, active=('pulse',), *, running=True, cancel=0, join=0,
                clock=0, target='pulse', restart=0, close=0, read=1, write=2):
        self.reset()
        self.results={'cancel':cancel,'join':join,'clock':clock,'pulse_restart':restart,
                      'close':close,'read':read,'write':write}
        self.target=target
        self.events=[]; self.live=set(active); self.retired=set()
        self.current_reg=None; self.mode=3; self.bus_address=None
        for name,(tid,run,joinable,value) in THREADS.items():
            self.word(tid,value);self.word(run,int(name in active and running))
            self.word(joinable,int(name in active))
        self.word(self.core_context+0x454,0)
        self.word(self.core_context+0x484,0)
        self.word(self.core_context+0x488,0)
        self.word(0xda2e08,42)

    def code(self,cpu,address,size,user):
        name=self.services.get(address)
        a,b,c,d=[cpu.reg_read(r) for r in REGISTERS]
        if name in ('cancel','join'):
            thread=next((n for n,t in THREADS.items() if t[3]==a),None)
            require(thread is not None,'unknown thread handle')
            result=self.results[name] if thread==self.target else 0
            if name=='join':
                require(b==0,'unexpected join output')
                seconds,nanos=struct.unpack('<II',cpu.mem_read(c,8))
                expected=120 if thread=='storage' else 105
                require((seconds,nanos)==(expected,123456789),'unexpected join deadline')
                if result==0:self.live.discard(thread);self.retired.add(thread)
            self.events.append([name,thread,result])
        elif name=='clock':
            require(a==0,'clock is no longer realtime')
            result=self.results['clock']
            if result==0:cpu.mem_write(b,struct.pack('<II',100,123456789))
            self.events.append([name,result])
        elif name=='close':
            require(a==42,'unexpected descriptor close')
            result=self.results['close']
            self.events.append([name,a,sorted(self.live),result])
        elif name=='destroy':
            require(a==0xecae74,'unexpected mutex destruction')
            result=0;self.events.append([name])
        elif name=='strerror':result=0
        elif name=='ioctl':
            require(a==42 and b==0x703 and c in (0x1e,0x57),'unexpected I2C selection')
            self.bus_address=c; result=0
        elif name=='write':
            require(a==42 and c in (1,2),'unexpected I2C write')
            raw=bytes(cpu.mem_read(b,c));self.current_reg=raw[0]
            result=1 if c==1 else self.results['write']
            self.events.append(['bus_write',self.bus_address,raw.hex(),result])
        elif name=='read':
            require(a==42 and c==1 and self.current_reg==9,'unexpected mode read')
            result=self.results['read']
            if result==1:cpu.mem_write(b,bytes([self.mode]))
            self.events.append(['bus_read',self.bus_address,self.current_reg,result])
        elif name=='pulse_restart':
            require(a==DATA,'unexpected acquisition context')
            result=self.results[name]
            self.events.append([name,result])
            # The nested start routine is a boundary here: its attempted call is
            # observed, not modeled as proof of a new physical sensor owner.
        elif name in ('power_event','alarm_stop','sip_stop','m4_stop','record_cleanup',
                      'wlan_enable','nap_unload','record_tail'):
            result=0;self.events.append([name])
        else:return super().code(cpu,address,size,user)
        cpu.reg_write(UC_ARM_REG_R0,result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC,cpu.reg_read(UC_ARM_REG_LR))

    def result(self, entry, *args):
        result=self.call(entry,*args)
        return {'return':result,'events':self.events,'unjoined_threads':sorted(self.live),
                'flags':{name:{'running':self.get(t[1]),'joinable':self.get(t[2])}
                         for name,t in THREADS.items()},
                'optical_descriptor':self.get(0xda2e08),
                'record_mode':self.get(self.core_context+0x454)}


def verify(path):
    stock=Ownership(path)
    optical=[]
    for running,cancel,join,clock in itertools.product((False,True),(0,errno.ESRCH),
                                                     (0,errno.ETIMEDOUT),(0,-1)):
        stock.prepare(running=running,cancel=cancel,join=join,clock=clock)
        r=stock.result(0x90bf4)
        failed=(running and cancel!=0) or clock!=0 or join!=0
        require(r['return']==int(failed),'unexpected optical stop result')
        require(r['optical_descriptor']==0xffffffff,'optical descriptor retained')
        require(r['flags']['pulse']=={'running':0,'joinable':0},'pulse tracking differs')
        require(('pulse' in r['unjoined_threads'])==bool(failed),'join retirement differs')
        closes=[e for e in r['events'] if e[0]=='close']
        require(len(closes)==1,'optical close count differs')
        require(bool([e for e in r['events'] if e[0]=='bus_read'])==(not failed),
                'hardware cleanup occurred before thread retirement')
        optical.append(r)
    # A subsequent stop can report success without recovering the discarded
    # descriptor or joining the thread whose first join timed out.
    stock.prepare(join=errno.ETIMEDOUT)
    first=stock.result(0x90bf4)
    stock.events=[]
    retry=stock.result(0x90bf4)
    require(first['return']==1 and retry['return']==0 and retry['events']==[] and
            retry['unjoined_threads']==['pulse'],'failed-stop retry behavior differs')
    # Chip cleanup failures still release the descriptor after a successful join.
    for read,write,close in itertools.product((0,1),(0,2),(0,-1)):
        stock.prepare(read=read,write=write,close=close)
        r=stock.result(0x90bf4)
        require(r['return']==int(read!=1 or write!=2) and not r['unjoined_threads'] and
                r['optical_descriptor']==0xffffffff,'cleanup result differs')
        optical.append(r)

    motion=[]
    for write,close in itertools.product((0,1,2,-1),(0,-1)):
        stock.prepare(active=(),write=write,close=close)
        r=stock.result(0x901d0,42)
        require(r['return']==int(write!=2),'motion close result changed return')
        require([e for e in r['events'] if e[0]=='bus_write']==[['bus_write',30,'2440',write]],
                'motion cleanup no longer requests reset')
        require(not any(e[0]=='bus_read' for e in r['events']),'motion reset now read back')
        require(len([e for e in r['events'] if e[0]=='close'])==1,'motion descriptor not closed')
        motion.append(r)

    record=[]
    stock.prepare(active=('pulse',))
    r=stock.result(0x2c524,DATA)
    require(r['return']==0 and r['events']==[] and r['unjoined_threads']==['pulse'],
            'idle record stop changed ownership')
    record.append(r)
    for restart in (0,1):
        stock.prepare(active=tuple(THREADS),restart=restart)
        r=stock.result(0x2c524,DATA)
        require(r['return']==0 and not r['unjoined_threads'] and
                ['pulse_restart',restart] in r['events'],'normal stop restart behavior differs')
        record.append(r)
    # Microphone join failure is only warned about; other reader/processor
    # retirement failures return an error but erase several tracking flags.
    for target in ('storage','record','eeg','motion','microphone'):
        for operation,error in (('cancel',errno.ESRCH),('join',errno.ETIMEDOUT)):
            stock.prepare(active=tuple(THREADS),target=target,**{operation:error})
            r=stock.result(0x2c524,DATA)
            ignored=target=='microphone' and operation=='join'
            require(r['return']==(0 if ignored else 1),'record stop error policy differs')
            require(target in r['unjoined_threads'],'failed retirement now joins thread')
            expected_flags={'running':int(target=='storage' and operation=='cancel'),
                            'joinable':int(target=='microphone' and operation=='cancel')}
            require(r['flags'][target]==expected_flags,'failed thread tracking differs')
            require(any(e[0]=='pulse_restart' for e in r['events'])==ignored,
                    'unexpected restart after failed retirement')
            record.append(r)
    stock.prepare(active=tuple(THREADS),target='motion',join=errno.ETIMEDOUT)
    failed_record=stock.result(0x2c524,DATA)
    stock.events=[]
    retried_record=stock.result(0x2c524,DATA)
    require(failed_record['return']==1 and retried_record['return']==0 and
            retried_record['events']==[] and retried_record['unjoined_threads']==['motion','pulse'],
            'record stop retry now verifies reader retirement')
    record.extend((failed_record,retried_record))
    combined=[optical,first,retry,motion,record]
    return {'core_sha256':CORE_SHA256,'optical_manager_cases':len(optical)+2,
            'motion_cleanup_cases':len(motion),'record_stop_cases':len(record),
            'optical_failed_join_discards_descriptor_and_flags':True,
            'optical_retry_after_failed_join_can_report_success_without_join':True,
            'successful_record_stop_attempts_background_optical_restart':True,
            'record_retry_after_failed_join_can_report_success_without_join':True,
            'microphone_join_failure_can_return_record_stop_success':True,
            'motion_cleanup_requests_reset_without_readback':True,
            'additional_code_and_literal_bytes':sum(end-start for start,end in RANGES),
            'fixture_sha256':hashlib.sha256(json.dumps(combined,sort_keys=True).encode()).hexdigest(),
            'boundaries':['modeled thread/cancel/join services; no scheduler or mutex proof',
                          'nested optical restart and unrelated managers are stubbed',
                          'no process termination, hardware stop, or ownership transfer proof']}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('nano_core',type=Path)
    parser.add_argument('--control-dir',type=Path,help='private inspector output with startup scripts')
    args=parser.parse_args()
    result=verify(args.nano_core)
    if args.control_dir:result['startup_controls']=verify_control_scripts(args.control_dir)
    print(json.dumps(result,indent=2))
