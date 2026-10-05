"""Execute user policy M with syscall-result fixtures; kernel tested separately."""
import unittest
from source_m import SourceM, LAYOUT as C
from test_kernel import LAIX
from test_ipc_handles import error


class PolicyM(SourceM):
    def __init__(self, quarantined=False, creation_error=False):
        super().__init__(LAIX/'user/recovery/policy.m')
        self.quarantined=quarantined
        self.creation_error=creation_error
        self.operations=[]
        self.next_reference=10
    def call(self,name,*args):
        if name in ('createTask','createEndpoint','configureService','publishTask','publishService',
                    'withdrawService','grantTaskDevices','closeHandle','terminateTask','inspectTask',
                    'collectTask','sleep'):
            self.operations.append((name,args))
            if name=='createTask':
                if self.creation_error: return error(23)
                self.next_reference+=1
                return self.next_reference
            if name=='createEndpoint': return 0x101
            if name=='grantTaskDevices': return 0x203
            if name=='inspectTask':
                typ=self.decls['recoveryEvent'].sym.type
                self.memory[args[1]+typ.field('state').offset]=3
                self.memory[args[1]+typ.field('flags').offset]=C['TASK_EVENT_QUARANTINED'] if self.quarantined else C['TASK_EVENT_RECLAIMED']
            return 0
        return super().call(name,*args)
    def service(self,addr=0x1000000,reference=1,attempts=1):
        # The type is available through the checked policy module.
        typ=self.decls['launchService'].params[0].var.type.target
        for field in typ.fields: self.memory[addr+field.offset]=0
        for name,value in [('reference',reference),('root',0x101),('generation',1),('attempts',attempts)]:
            self.memory[addr+typ.field(name).offset]=value
        return addr,typ


class PolicyTests(unittest.TestCase):
    def test_backoff_and_restart_limit_keep_the_resolver_terminal(self):
        vm=PolicyM()
        for attempt,seconds in enumerate((1,2,4,8,8)):
            self.assertEqual(vm.call('recoveryBackoff',attempt),0)
            self.assertEqual(vm.operations[-1],('sleep',(seconds,)))
        before=len(vm.operations)
        self.assertEqual(vm.call('recoveryBackoff',5),error(32))
        self.assertEqual(len(vm.operations),before)
        service,typ=vm.service(attempts=5)
        self.assertEqual(vm.call('recoverService',service,2,1,0,0),error(32))
        self.assertFalse(any(name=='createTask' for name,_ in vm.operations))
        self.assertEqual(vm.operations[-1],('withdrawService',(1,error(32))))
        self.assertEqual(vm.memory[service+typ.field('unavailable').offset],True)

    def test_permanent_busy_quarantine_sleeps_five_times_and_never_collects_pin(self):
        vm=PolicyM(quarantined=True)
        service,typ=vm.service()
        self.assertEqual(vm.call('retireService',service,3),error(16))
        self.assertEqual(sum(name=='sleep' for name,_ in vm.operations),5)
        self.assertFalse(any(name in ('collectTask','closeHandle','createTask','grantTaskDevices') for name,_ in vm.operations))
        self.assertEqual(vm.memory[service+typ.field('reference').offset],1)
        self.assertEqual(vm.memory[service+typ.field('unavailable').offset],True)
        self.assertEqual(vm.operations[-1],('withdrawService',(3,error(32))))

    def test_dependency_recovery_retires_consumers_then_launches_producers(self):
        vm=PolicyM()
        disk,typ=vm.service(0x1000000,1)
        files,_=vm.service(0x1000100,2)
        self.assertEqual(vm.call('recoverFilesDisk',disk,files),0)
        terminated=[args[0] for name,args in vm.operations if name=='terminateTask']
        self.assertEqual(terminated,[2,1])
        created=[args[0] for name,args in vm.operations if name=='createTask']
        self.assertEqual(created,[3,4])
        configured=[args for name,args in vm.operations if name=='configureService']
        self.assertEqual(configured[0][2:4],(0,2))
        self.assertEqual(configured[1][2:4],(0x101,2))
        names=[args[0] for name,args in vm.operations if name=='publishService']
        self.assertEqual(names,[3,2])


if __name__=='__main__':
    unittest.main()
