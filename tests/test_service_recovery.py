"""Execute the checked recovery/IPC/task/broker sources; no emulator build."""
import unittest
from collections import Counter
from test_runtime_objects import ObjectsM
from test_runtime_tasks import create
from test_task import TaskEntered, USER_DATA
from test_ipc_handles import error
from test_kernel import check_m, LAIX
from source_m import LAYOUT as C
from test_screen_services import Devices


def fixture(devices=False, catalog=False):
    vm = ObjectsM()
    if devices:
        vm.memory = Devices(vm)
        vm.addresses.update(fontData=0x16000, fontDataEnd=0x16000 + 184)
        for i, value in enumerate((0x3146414C, 1, 19, 32, 184, 16, 32, 0)):
            vm.memory[0x16000 + i * 4] = value
        info = vm.addresses['kernelBootInfo']
        typ = vm.decls['kernelBootInfo'].sym.type
        vm.memory[info + typ.field('disk').offset] = C['DISK0_BASE']
        vm.memory[info + typ.field('imageSize').offset] = 1024
    if catalog:
        start = 0x20000
        header = (0x464C457F, 0x00010101, 0, 0, 0x08160002, 1,
                  C['SERVICE_IMAGE_BASE'], 52, 0, 0, 0x00200034, 1, 0)
        for i, word in enumerate(header): vm.memory[start+4*i] = word
        for i, word in enumerate((1,256,C['SERVICE_IMAGE_BASE'],0,16,4096,5,4096)):
            vm.memory[start+52+4*i] = word
        for i in range(16): vm.memory[start+256+i] = i+10
        assert vm.call('taskRegisterImage', 2, start, start+272)
    assert vm.call('taskCreateImage', 0x14000, 0x1401C, 0) == 1
    assert vm.call('serviceBootstrap', 1)
    assert vm.call('taskInstallRuntimeStart', 1, 0, 0, 0)
    assert vm.call('taskControlBootstrapSelf', 1)
    vm.memory[vm.field_address('createImages')] = 3 if catalog else 1
    vm.memory[vm.field_address('deviceFactory')] = C['DEVICE_DISK'] | C['DEVICE_INPUT']
    assert vm.call('endpointFactoryBootstrap', vm.field_address('handles'), 3, 12, True)
    assert vm.call('taskPublish', 1)
    with unittest.TestCase().assertRaises(TaskEntered):
        vm.call('taskStart', 1000000)
    return vm


def client(vm, mask=15):
    ref = create(vm, configure=False, publish=False)
    assert vm.call('serviceAllow', ref, mask) == 0
    vm.invoke(C['SYS_TASK_CONFIGURE'], ref, 0, 0, 0)
    assert vm.result(1)[0] == 0
    vm.invoke(C['SYS_TASK_PUBLISH'], ref)
    assert vm.result(1)[0] == 0
    return ref


def replacement(vm, generation, name=1, dependency=0, devices=0):
    child = create(vm, configure=False, publish=False)
    root = vm.factory(1, child)
    assert 0 < root < 0x80000000
    irq = 0
    if devices:
        irq = vm.call('taskRuntimeDevices', child, devices)
        assert 0 < irq < 0x80000000
    assert vm.call('serviceConfigure', child, root, dependency, generation, irq) == 0
    assert vm.call('servicePublish', name, child, root, generation) == 0
    receive = vm.memory[vm.field('bootPage', child) + 28]
    return child, root, receive, irq


def resolve(vm, name=1):
    vm.invoke(C['SYS_SERVICE_RESOLVE'], name, USER_DATA)
    assert vm.result(vm.current())[0] == 0
    return tuple(int.from_bytes(vm.read_bytes(vm.pages(vm.current())[1] + 4*i, 4), 'little') for i in range(4))


def retire(vm, ref, root):
    vm.invoke(C['SYS_TASK_TERMINATE'], ref, 0)
    vm.reap()
    vm.invoke(C['SYS_TASK_COLLECT'], ref, USER_DATA)
    assert vm.result(1)[0] == 0
    assert vm.call('ipcClose', root) == 0


class RecoveryTests(unittest.TestCase):
    def test_24_replacements_consent_stale_task_endpoint_reply_irq_and_no_growth(self):
        vm = fixture()
        caller = client(vm, 1)
        baseline = vm.free_pages()
        old_ref = old_reply = old_irq = old_handle = 0
        for generation in range(1, 25):
            vm.run(1)
            ref, root, rx, irq = replacement(vm, generation, devices=C['DEVICE_INPUT'])
            self.assertNotEqual(ref, old_ref)
            self.assertFalse(vm.call('irqTokenValid', ref, old_irq, C['KEYBOARD_IRQ']))
            if old_ref:
                self.assertEqual(vm.call('taskGet', old_ref), 0)
                self.assertEqual(vm.call('taskRuntimeRead', old_ref, USER_DATA, False), error(1))
            vm.run(caller)
            tx, instance, resource, name = resolve(vm)
            self.assertEqual((instance, resource, name), (ref, generation, 1))
            if old_handle:
                vm.request(old_handle)
                self.assertEqual(vm.result(caller)[0], error(32))
                vm.call('ipcClose', old_handle)
            vm.request(tx, b'cycle')
            vm.run(ref)
            reply = vm.accept(rx)
            if old_reply:
                vm.response(old_reply, b'stale')
                self.assertEqual(vm.result(ref)[0], error(9))
                self.assertEqual(vm.field('state', caller), 4)
            # Die after accept; surviving caller gets one EPIPE and no stale write.
            vm.run(1)
            vm.wakes.clear()
            retire(vm, ref, root)
            self.assertEqual(vm.result(caller)[0], error(32))
            self.assertEqual(vm.wakes, Counter({caller: 1}))
            self.assertEqual(vm.free_pages(), baseline)
            self.assertEqual(vm.control_count(), 2)
            old_ref, old_reply, old_irq, old_handle = ref, reply, irq, tx
        vm.run(caller)
        vm.call('ipcClose', old_handle)
        self.assertEqual(sum(vm.memory[vm.object_field(vm.addresses['endpoints'] + i*vm.decls['endpoints'].sym.type.elem.size, 'references')] for i in range(16)), 0)

    def test_authorization_masks_seal_private_namespace_and_resolution_rollback(self):
        vm = fixture()
        caller = client(vm, 1)
        ref, root, rx, irq = replacement(vm, 1)
        vm.run(caller)
        self.assertEqual(vm.call('servicePublish', 1, ref, root, 2), error(1))
        self.assertEqual(vm.call('serviceAllow', caller, 15), error(1))
        self.assertEqual(vm.call('serviceWithdraw', 1, error(32)), error(1))
        self.assertFalse(vm.call('serviceBootstrap', caller))
        self.assertFalse(vm.call('taskRegisterImage', 2, 0x20000, 0x20100))
        self.assertEqual(vm.call('serviceResolve', 2, USER_DATA), error(1))
        table = vm.field_address('handles', caller)
        typ = vm.decls['tasks'].sym.type.elem.field('handles').type.field('entries').type.elem
        count = lambda: sum(bool(vm.memory[table + i*typ.size + typ.field('object').offset]) for i in range(16))
        self.assertEqual(vm.call('serviceResolve', 1, USER_DATA+4090), error(14))
        self.assertEqual(count(), 0)
        resolve(vm)
        self.assertEqual(count(), 1)
        vm.run(1)
        self.assertEqual(vm.call('servicePublish', 1, ref, root, 1), error(22))
        self.assertEqual(vm.call('serviceWithdraw', 1, error(32)), 0)
        vm.run(caller)
        self.assertEqual(vm.call('serviceResolve', 1, USER_DATA), error(32))
        vm.run(1)
        vm.invoke(C['SYS_EXIT'], 0)
        vm.run(caller)
        self.assertEqual(vm.call('serviceResolve', 1, USER_DATA), error(1))

    def test_no_partial_advertisement_at_construction_and_publication_failures(self):
        for stage in ('endpoint', 'receive', 'allocate', 'map', 'context', 'registry', 'dependency'):
            with self.subTest(stage=stage):
                vm = fixture()
                caller = client(vm)
                baseline = vm.free_pages()
                ref = create(vm, configure=False, publish=False)
                if stage == 'endpoint': vm.install_fault = True
                root = vm.factory(1, ref)
                if stage == 'endpoint': self.assertEqual(root, error(24))
                else:
                    if stage == 'receive': vm.install_fault = True
                    if stage == 'allocate': vm.fail_allocation = vm.allocation_count+1
                    if stage == 'map': vm.fail_mapping = vm.mapping_calls+1
                    result = vm.call('serviceConfigure', ref, root, 0, 1, 0)
                    if stage in ('receive','allocate','map'): self.assertGreaterEqual(result, 0x80000000)
                    else:
                        self.assertEqual(result, 0)
                        self.assertEqual(vm.call('servicePublish', 1, ref, root, 0), error(22))
                        if stage == 'context': vm.memory[vm.field_address('context', ref)+C['TF_EPC']] = USER_DATA
                        vm.invoke(C['SYS_TASK_PUBLISH'], ref)
                        if stage == 'context': self.assertEqual(vm.result(1)[0], error(22))
                        else:
                            self.assertEqual(vm.result(1)[0], 0)
                            self.assertEqual(vm.call('servicePublish', 1, ref, root, 0), error(22))
                vm.run(caller)
                self.assertEqual(vm.call('serviceResolve', 1, USER_DATA), error(11))
                vm.run(1)
                vm.invoke(C['SYS_TASK_TERMINATE'], ref, 0)
                vm.reap()
                vm.call('taskRuntimeRead', ref, USER_DATA, True)
                if root < 0x80000000: vm.call('ipcClose', root)
                self.assertEqual(vm.free_pages(), baseline)

    def test_busy_quarantine_denies_irq_device_handover_then_recovers(self):
        vm = fixture(True)
        caller = client(vm)
        ref, root, rx, old_irq = replacement(vm, 1, devices=C['DEVICE_DISK'])
        vm.run(ref)
        self.assertGreater(vm.call('deviceSubmit', ref, 0, 16, 1), 0)
        bounce = vm.globals['deviceBounce']
        vm.run(1)
        vm.invoke(C['SYS_TASK_TERMINATE'], ref, 0)
        child = create(vm, configure=False, publish=False)
        for _ in range(8):
            self.assertEqual(vm.call('taskRuntimeDevices', child, C['DEVICE_DISK']), error(16))
            vm.reap()
            self.assertEqual(vm.field('state', ref), 3)
            self.assertFalse(vm.call('physicalPageAvailable', bounce))
            self.assertEqual(vm.call('irqLookup', child, old_irq), C['PIC_LINE_COUNT'])
            vm.invoke(C['SYS_YIELD'])
            vm.run(1)
        self.assertEqual(vm.call('serviceWithdraw', 1, error(32)), 0)
        vm.run(caller)
        self.assertEqual(vm.call('serviceResolve', 1, USER_DATA), error(32))
        vm.run(1)
        vm.memory.complete()
        vm.reap()
        vm.invoke(C['SYS_TASK_COLLECT'], ref, USER_DATA)
        vm.call('ipcClose', root)
        fresh_irq = vm.call('taskRuntimeDevices', child, C['DEVICE_DISK'])
        self.assertTrue(0 < fresh_irq < 0x80000000)
        self.assertNotEqual(fresh_irq, old_irq)
        self.assertFalse(vm.call('irqTokenValid', child, old_irq, 3))
        self.assertEqual(vm.call('diskInfo', child), 608)
        self.assertTrue(vm.call('physicalPageAvailable', bounce))

    def test_generation_and_dependency_validation(self):
        vm = fixture()
        ref, root, _, _ = replacement(vm, 1, name=3)
        files = create(vm, configure=False, publish=False)
        own = vm.factory(1, files)
        self.assertEqual(vm.call('serviceConfigure', files, own, root, 2, 0), error(22))
        self.assertEqual(vm.call('serviceConfigure', files, own, root, 1, 0), 0)
        retire(vm, ref, root)
        vm.invoke(C['SYS_TASK_PUBLISH'], files)
        self.assertEqual(vm.result(1)[0], error(1))

    def test_every_catalog_image_allocation_and_mapping_failure_rolls_back(self):
        probe = fixture(catalog=True)
        probe.allocation_count = probe.mapping_calls = 0
        probe.invoke(C['SYS_TASK_CREATE'], 2)
        self.assertTrue(0 < probe.result(1)[0] < 0x80000000)
        allocations, mappings = probe.allocation_count, probe.mapping_calls
        for kind, count in [('allocation',allocations),('mapping',mappings)]:
            for step in range(1,count+1):
                with self.subTest(kind=kind,step=step):
                    vm=fixture(catalog=True)
                    baseline=vm.free_pages()
                    vm.allocation_count=vm.mapping_calls=0
                    if kind=='allocation': vm.fail_allocation=step
                    else: vm.fail_mapping=step
                    vm.invoke(C['SYS_TASK_CREATE'],2)
                    self.assertEqual(vm.result(1)[0],error(23))
                    self.assertEqual(vm.control_count(),1)
                    self.assertEqual(vm.free_pages(),baseline)
                    self.assertEqual(vm.call('serviceResolve',1,USER_DATA),error(1))

    def test_second_authority_installation_failure_discards_private_instance(self):
        vm=fixture()
        ref,root,_,_=replacement(vm,1,name=3)
        baseline=vm.free_pages()
        child=create(vm,configure=False,publish=False)
        own=vm.factory(1,child)
        original=vm.call
        installs=0
        def fail_second(name,*args):
            nonlocal installs
            if name=='handleCopy':
                installs+=1
                if installs==2: return error(24)
            return original(name,*args)
        vm.call=fail_second
        self.assertEqual(vm.call('serviceConfigure',child,own,root,1,0),error(23))
        self.assertEqual(vm.call('taskGet',child),0)
        vm.call('ipcClose',own)
        self.assertEqual(vm.free_pages(),baseline)
        self.assertEqual(vm.control_count(),2)
        self.assertEqual(vm.call('servicePublish',2,child,own,1),error(1))

    def test_client_table_exhaustion_and_changed_medium_fail_closed(self):
        vm=fixture(True)
        caller=client(vm)
        ref,root,rx,irq=replacement(vm,1,devices=C['DEVICE_DISK'])
        vm.run(caller)
        tokens=[resolve(vm)[0] for _ in range(16)]
        self.assertEqual(vm.call('serviceResolve',1,USER_DATA),error(24))
        self.assertEqual(vm.field('state',ref),1)
        vm.call('ipcClose',tokens.pop())
        tokens.append(resolve(vm)[0])
        for token in tokens: vm.call('ipcClose',token)
        vm.run(1)
        retire(vm,ref,root)
        child=create(vm,configure=False,publish=False)
        vm.memory.disk_state |= C['DISK_CHANGED']
        self.assertEqual(vm.call('taskRuntimeDevices',child,C['DEVICE_DISK']),error(32))
        self.assertEqual(vm.field('deviceRights',child),0)

    def test_atomic_publication_capacity_and_context_failures_are_retryable(self):
        vm=fixture()
        caller=client(vm)
        child=create(vm,configure=False,publish=False)
        root=vm.factory(1,child)
        self.assertEqual(vm.call('serviceConfigure',child,root,0,1,0),0)
        layout=vm.decls['serviceEntries'].sym.type.elem
        base=vm.addresses['serviceEntries']
        # Other private namespaces consume the bounded registry ledger.
        for row in range(8):
            vm.memory[base+row*layout.size+layout.field('owner').offset]=0x7001
        ready=vm.globals['readyCount']
        self.assertEqual(vm.call('servicePublish',1,child,root,1),error(23))
        self.assertEqual(vm.field('state',child),5)
        self.assertEqual(vm.globals['readyCount'],ready)
        vm.memory[base+layout.field('owner').offset]=0
        pc=vm.field_address('context',child)+C['TF_EPC']
        saved=vm.memory[pc]
        vm.memory[pc]=USER_DATA
        self.assertEqual(vm.call('servicePublish',1,child,root,1),error(22))
        self.assertEqual(vm.memory[base+layout.field('owner').offset],0)
        vm.memory[pc]=saved
        self.assertEqual(vm.call('servicePublish',1,child,root,1),0)
        self.assertEqual(vm.field('state',child),1)
        vm.run(caller)
        self.assertEqual(resolve(vm)[1:3],(child,1))

    def test_resource_generation_exhaustion_never_reuses_an_incarnation(self):
        vm=fixture()
        ref,root,_,_=replacement(vm,0x7FFFFFFF)
        retire(vm,ref,root)
        child=create(vm,configure=False,publish=False)
        fresh=vm.factory(1,child)
        self.assertEqual(vm.call('serviceConfigure',child,fresh,0,0x7FFFFFFF,0),0)
        for generation in (0,1,0x7FFFFFFF,0x80000000):
            self.assertEqual(vm.call('servicePublish',1,child,fresh,generation),error(22))
            self.assertEqual(vm.field('state',child),5)

    def test_recovery_modules_and_cpu_fixture_type_check(self):
        for name in ('user/recovery/policy.m','src/kernel/recovery_main.m',
                     'tests/programs/recovery/policy.m','tests/programs/recovery/server.m',
                     'user/recovery/supervisor.m','user/recovery/server.m'):
            check_m(LAIX/name)


if __name__ == '__main__':
    unittest.main()
