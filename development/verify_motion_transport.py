#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Execute isolated original LIS2HH12 startup/read code with synthetic services.

The private executable is hash pinned. No process, adapter or sensor is opened.
This verifies the observed register trace, not physical timing or calibration.
"""
import argparse
import hashlib
import json
from pathlib import Path

from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_PC, UC_ARM_REG_LR
from verify_optical_transport import Stock, REGISTERS, OUTPUT, CORE_SHA256, require


class MotionStock(Stock):
    def __init__(self, path):
        super().__init__(path, additional_ranges=((0x8fd94, 0x902b8),),
                         additional_services={0x16ae8:'open', 0x16cd4:'close',
                                              0x167dc:'usleep', 0x16e78:'strerror'})

    def prepare(self, identity=65, bad_id_reads=0, selection_fail_at=0,
                transfer_fail_at=0, read_result=6, opened=42):
        self.reset()
        self.identity, self.bad_id_reads = identity, bad_id_reads
        self.selection_fail_at, self.transfer_fail_at = selection_fail_at, transfer_fail_at
        self.opened, self.sample_read_result = opened, read_result
        self.id_reads, self.transfer, self.current_register = 0, 0, None
        self.closes, self.sleeps, self.register_writes, self.read_registers = [], [], [], []
        self.sample_bytes = b'\x00\x80\x00\x00\xff\x7f'

    def code(self, cpu, address, size, user):
        name = self.services.get(address)
        a,b,c,d = [cpu.reg_read(r) for r in REGISTERS]
        if name == 'open':
            require((a,b)==(0x1065c4,2), 'unexpected adapter open')
            result = self.opened
        elif name == 'close':
            require(a==42, 'unexpected close')
            self.closes.append(a); result=0
        elif name == 'usleep':
            self.sleeps.append(a); result=0
        elif name == 'strerror':
            result=0
        elif name == 'ioctl':
            require((a,b,c)==(42,0x703,0x1e), 'unexpected sensor selection')
            self.selects+=1
            result=-1 if self.selects==self.selection_fail_at else 0
        elif name == 'write':
            require(a==42 and c in (1,2), 'unexpected register write')
            raw=bytes(cpu.mem_read(b,c)); self.current_register=raw[0]
            self.transfer+=1
            result=c-1 if self.transfer==self.transfer_fail_at else c
            if c==2: self.register_writes.append([*raw,result])
        elif name == 'read':
            require(a==42 and c in (1,6), 'unexpected register read')
            self.transfer+=1; self.read_registers.append(self.current_register)
            if c==1:
                require(self.current_register==15, 'startup read configuration unexpectedly')
                self.id_reads+=1
                value=0 if self.id_reads<=self.bad_id_reads else self.identity
                result=0 if self.transfer==self.transfer_fail_at else 1
                if result: cpu.mem_write(b,bytes([value]))
            else:
                require(self.current_register==0x28, 'sample started at wrong register')
                result=self.sample_read_result
                if result>0: cpu.mem_write(b,self.sample_bytes[:result])
        else:
            return super().code(cpu,address,size,user)
        cpu.reg_write(UC_ARM_REG_R0,result & 0xffffffff)
        cpu.reg_write(UC_ARM_REG_PC,cpu.reg_read(UC_ARM_REG_LR))

    def start(self, **kwargs):
        self.prepare(**kwargs)
        result=self.call(0x8fd94)
        return {'return':result,'writes':self.register_writes,'reads':self.read_registers,
                'sleeps_us':self.sleeps,'closes':len(self.closes),'selects':self.selects}


def verify(path):
    stock=MotionStock(path)
    expected=[[0x1e,0,2],[0x20,0xaf,2],[0x21,0,2],[0x22,0,2],[0x23,4,2],[0x24,0,2]]
    cases=[]
    baseline=stock.start()
    require(baseline['return']==42 and baseline['writes']==expected and
            baseline['reads']==[15] and baseline['sleeps_us']==[100000] and
            baseline['closes']==0,'startup baseline differs')
    cases.append(baseline)
    for bad in range(1,5):
        row=stock.start(bad_id_reads=bad)
        require(row['return']==42 and row['writes']==expected and
                row['sleeps_us']==[10]*bad+[100000],'identity retry differs')
        cases.append(row)
    for identity in (0,64,66,255):
        row=stock.start(identity=identity)
        require(row['return']==-1 and row['writes']==[] and row['closes']==1 and
                row['sleeps_us']==[10]*5,'wrong identity handling differs')
        cases.append(row)
    row=stock.start(opened=-1)
    require(row['return']==-1 and row['selects']==0 and row['closes']==0,'open failure differs')
    cases.append(row)
    # Seven register operations: identity read, then six writes.
    for selection in range(1,8):
        row=stock.start(selection_fail_at=selection)
        require(row['return']==-1 and row['closes']==1,'selection failure differs')
        cases.append(row)
    # Two identity transfers followed by six register/value writes.
    for transfer in range(1,9):
        row=stock.start(transfer_fail_at=transfer)
        require(row['return']==-1 and row['closes']==1,'transfer failure differs')
        cases.append(row)
    cancellations=0
    for selection in range(2,8):
        row=stock.start(selection_fail_at=selection,transfer_fail_at=selection+1)
        require(row['return']==42 and row['closes']==0 and len(row['writes'])==6,
                'original failure cancellation differs')
        require(row['writes'][selection-2][2]==1,'cancellation did not include short write')
        cases.append(row); cancellations+=1
    samples=[]
    for received in (-1,0,1,2,3,4,5,6):
        for selection in (0,1):
            stock.prepare(selection_fail_at=selection,read_result=received)
            stock.cpu.mem_write(OUTPUT,b'\xa5'*16)
            result=stock.call(0x90288,42,OUTPUT)
            raw=bytes(stock.cpu.mem_read(OUTPUT,16))
            expected_status=(-selection+int(received!=6)) & 255
            require(raw[6]==expected_status,'sample status differs')
            require(raw[7:]==b'\xa5'*9,'sample write exceeds observed record')
            require(raw[:6]==stock.sample_bytes[:max(0,received)]+b'\xa5'*(6-max(0,received)),
                    'partial read no longer preserves stale destination bytes')
            require(stock.read_registers==[0x28],'sample now consults data-ready status')
            samples.append({'received':received,'selection_failed':bool(selection),
                            'status':raw[6],'return':result,'data':raw[:6].hex()})
    return {'core_sha256':CORE_SHA256,'startup_cases':len(cases),'sample_cases':len(samples),
            'false_successful_initializations':cancellations,'startup_register_writes':expected,
            'startup_configuration_readbacks':0,'sample_reads_status_register':False,
            'additional_original_code_bytes':0x902b8-0x8fd94,
            'fixture_sha256':hashlib.sha256(json.dumps([cases,samples],sort_keys=True).encode()).hexdigest(),
            'boundaries':['synthetic bus and system services','isolated startup and sample routines',
                          'no manager ownership, physical timing or calibration proof']}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('nano_core',type=Path)
    print(json.dumps(verify(parser.parse_args().nano_core),indent=2))
