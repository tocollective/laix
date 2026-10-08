"""Runtime factory acceptance against checked M sources and real syscall dispatch."""
import unittest
from collections import Counter

from test_runtime_tasks import LifecycleM, create
from test_ipc_handles import error, GEN_MAX
from test_task import TaskEntered, USER_DATA
from source_m import LAYOUT as C


class ObjectsM(LifecycleM):
    def __init__(self):
        super().__init__()
        self.install_fault = False
        self.memory[C['KEYBOARD_BASE']] = 0

    def call(self, name, *args):
        if name == 'handleInstallScoped' and self.install_fault:
            self.install_fault = False
            return error(24)
        return super().call(name, *args)

    def copy_fixture(self, token, target, rights):
        owner = self.current()
        return self.call('handleCopy', self.field_address('handles', owner), token,
                         self.field_address('handles', target), owner, target, rights)

    def factory(self, mode=0, receiver=0):
        self.invoke(C['SYS_ENDPOINT_CREATE'], mode, receiver)
        return self.result(self.current())[0]

    def root(self, token, owner=1):
        return self.call('handleLookup', self.field_address('handles', owner), token, 0)


def fixture(count=4, ordinary=False):
    vm = ObjectsM()
    for slot in range(1, count + 1):
        assert vm.call('taskCreateImage', 0x14000, 0x1401C, 0) == slot
        assert vm.call('taskInstallRuntimeStart', slot, 0, 0, 0)
        assert vm.call('taskControlBootstrapSelf', slot)
        if slot == 1:
            vm.memory[vm.field_address('createImages')] = 1
            vm.memory[vm.field_address('deviceFactory')] = C['DEVICE_UART_TX'] | C['DEVICE_INPUT']
            assert vm.call('endpointFactoryBootstrap', vm.field_address('handles'), 3, 12, True)
        if ordinary and slot != 1:
            assert vm.call('endpointFactoryBootstrap', vm.field_address('handles', slot), 3, 12, False)
        assert vm.call('taskPublish', slot)
    with unittest.TestCase().assertRaises(TaskEntered):
        vm.call('taskStart', 1000000)
    return vm


class RuntimeObjectTests(unittest.TestCase):
    def test_post_start_raw_exchange_destroy_and_authority_denial(self):
        vm = fixture()
        token = vm.factory()
        self.assertTrue(0 < token < 0x80000000)
        obj = vm.root(token)
        peer = vm.copy_fixture(token, 2, 3)
        vm.seed(vm.pages(1)[1], b'raw')
        vm.invoke(C['SYS_IPC_SEND'], token, USER_DATA, 3)
        vm.run(2)
        vm.invoke(C['SYS_IPC_RECEIVE'], peer, USER_DATA, 32)
        self.assertEqual(vm.read_bytes(vm.pages(2)[1], 3), b'raw')
        self.assertEqual(vm.factory(), error(1))
        self.assertEqual(vm.call('ipcCopy', peer, 3, 7), error(1))
        vm.run(1)
        self.assertEqual(vm.call('ipcDestroy', token), 0)
        self.assertEqual(vm.call('handleLookup', vm.field_address('handles', 2), peer, 1), 0)
        self.assertEqual(vm.call('endpointBootstrap', vm.field_address('handles'), 1), error(1))
        self.assertFalse(vm.call('endpointFactoryBootstrap', vm.field_address('handles', 3), 3, 12, True))
        vm.call('ipcClose', token)
        vm.run(2)
        vm.call('ipcClose', peer)
        self.assertEqual(vm.object_value(obj, 'references'), 0)

    def test_service_binding_to_controlled_child_and_split_ownership(self):
        vm = fixture(2)
        child = create(vm, configure=False, publish=False)
        token = vm.factory(1, child)
        obj = vm.root(token)
        self.assertEqual(vm.object_value(obj, 'creator'), 1)
        self.assertEqual(vm.object_value(obj, 'manager'), child)
        self.assertEqual(vm.object_value(obj, 'receiveReferences'), 0)
        self.assertEqual(vm.factory(1, 2), error(1))
        self.assertEqual(vm.factory(0, child), error(22))
        self.assertEqual(vm.call('ipcCopy', token, 2, 2), error(1))
        vm.invoke(C['SYS_IPC_ACCEPT'], token, USER_DATA, 32)
        self.assertEqual(vm.result(1)[0], error(1))
        vm.invoke(C['SYS_TASK_CONFIGURE'], child, token, 3, 0)
        self.assertEqual(vm.result(1)[0], 0)
        child_handle = 0x101
        self.assertEqual(vm.object_value(obj, 'receiveReferences'), 1)
        vm.invoke(C['SYS_TASK_PUBLISH'], child)
        vm.run(1)
        vm.request(token, b'new service')
        vm.run(child)
        reply = vm.accept(child_handle)
        vm.response(reply, b'ok')
        self.assertEqual(vm.result(1)[0], 2)
        self.assertEqual(vm.call('ipcDestroy', child_handle), error(1))
        self.assertEqual(vm.call('ipcCopy', child_handle, 2, 2), error(1))
        # Last usable receiver closes: creator staging rights do not keep it live.
        vm.call('ipcClose', child_handle)
        self.assertEqual(vm.object_value(obj, 'state'), 2)
        vm.run(1)
        vm.call('ipcClose', token)
        self.assertEqual(vm.object_value(obj, 'references'), 0)

    def test_queued_and_accepted_calls_cancel_exactly_once(self):
        vm = fixture()
        token = vm.factory(1)
        obj = vm.root(token)
        peers = {i: vm.copy_fixture(token, i, 1) for i in (2, 3)}
        for i in (2, 3):
            vm.run(i)
            vm.request(peers[i])
        vm.run(1)
        reply = vm.accept(token)
        self.assertEqual(vm.object_value(obj, 'references'), 5)
        vm.wakes.clear()
        vm.call('ipcDestroy', token)
        self.assertEqual(vm.wakes, Counter({2: 1, 3: 1}))
        self.assertEqual(vm.object_value(obj, 'references'), 3)
        for i in (2, 3):
            self.assertEqual(vm.result(i)[0], error(32))
            self.assertEqual(vm.field('ipcEndpoint', i), 0)
        vm.response(reply)
        self.assertEqual(vm.result(1)[0], error(9))
        self.assertEqual(vm.call('ipcDestroy', token), error(32))
        self.assertEqual(vm.wakes, Counter({2: 1, 3: 1}))
        for i, handle in [(1, token), *peers.items()]:
            vm.call('handleClose', vm.field_address('handles', i), handle)
        self.assertEqual(vm.object_value(obj, 'references'), 0)

    def test_quota_counts_destroyed_references_and_recovers(self):
        vm = fixture(2)
        tokens = [vm.factory() for _ in range(12)]
        self.assertTrue(all(0 < t < 0x80000000 for t in tokens))
        self.assertEqual(vm.factory(), error(23))
        vm.call('ipcDestroy', tokens[0])
        self.assertEqual(vm.factory(), error(23))
        vm.call('ipcClose', tokens[0])
        replacement = vm.factory()
        self.assertTrue(0 < replacement < 0x80000000)
        self.assertNotEqual(replacement, tokens[0])
        self.assertEqual(vm.call('ipcDestroy', tokens[0]), error(9))
        vm.invoke(C['SYS_YIELD'])
        self.assertEqual(vm.current(), 2)
        vm.run(1)
        self.assertTrue(0 < create(vm) < 0x80000000)

    def test_object_and_handle_reserves_allow_supervisor_recovery(self):
        vm = fixture(3, ordinary=True)
        vm.run(2)
        ordinary = [vm.factory() for _ in range(12)]
        vm.run(3)
        self.assertTrue(all(0 < vm.factory() < 0x80000000 for _ in range(2)))
        self.assertEqual(vm.factory(), error(23))
        vm.run(1)
        roots = [vm.factory() for _ in range(2)]
        self.assertTrue(all(0 < t < 0x80000000 for t in roots))
        self.assertEqual(vm.factory(), error(23))
        # Fill ordinary supervisor installations to 14; factory can use slot 15.
        for _ in range(12):
            self.assertTrue(0 < vm.call('ipcCopy', roots[0], 1, 1) < 0x80000000)
        self.assertEqual(vm.call('ipcCopy', roots[0], 1, 1), error(24))
        vm.run(2)
        vm.call('ipcClose', ordinary[0])
        vm.run(1)
        self.assertEqual(vm.factory() & 255, 15)

    def test_faulted_installation_and_full_table_leave_no_hidden_root(self):
        vm = fixture(2)
        vm.install_fault = True
        self.assertEqual(vm.factory(), error(24))
        obj = vm.addresses['endpoints']
        for field in ('references', 'creator', 'manager', 'state'):
            self.assertEqual(vm.object_value(obj, field), 0)
        root = vm.factory()
        self.assertEqual(vm.root(root), obj)
        self.assertEqual(vm.object_value(obj, 'generation'), 2)
        # Retire remaining empty handles, exercising the real installation error.
        typ = vm.decls['tasks'].sym.type.target.field('handles').type.field('entries').type.elem
        for i in range(1, 16):
            vm.memory[vm.field_address('handles') + i * typ.size + typ.field('generation').offset] = GEN_MAX
        self.assertEqual(vm.factory(), error(24))
        second = obj + vm.decls['endpoints'].sym.type.elem.size
        self.assertEqual(vm.object_value(second, 'creator'), 0)
        self.assertEqual(vm.object_value(second, 'references'), 0)

    def test_service_construction_faults_revoke_unpublished_receivers(self):
        for failure in ('install', 'allocate', 'map'):
            vm = fixture(2)
            child = create(vm, configure=False, publish=False)
            token = vm.factory(1, child)
            obj = vm.root(token)
            free = vm.free_pages()
            original = vm.call
            if failure == 'install':
                vm.install_fault = True
            elif failure == 'allocate':
                vm.fail_allocation = vm.allocation_count + 1
            else:
                def fail_map(name, *args):
                    if name == 'mapPage':
                        return False
                    return original(name, *args)
                vm.call = fail_map
            vm.invoke(C['SYS_TASK_CONFIGURE'], child, token, 3, 0)
            self.assertEqual(vm.result(1)[0], error(24 if failure == 'install' else 23))
            self.assertEqual(vm.object_value(obj, 'references'), 1)
            self.assertEqual(vm.object_value(obj, 'receiveReferences'), 0)
            self.assertEqual(vm.field('state', child), 5 if failure == 'install' else 0)
            if failure == 'install':
                self.assertEqual(vm.object_value(obj, 'state'), 1)
                vm.invoke(C['SYS_TASK_CONFIGURE'], child, token, 3, 0)
                self.assertEqual(vm.result(1)[0], 0)
                vm.invoke(C['SYS_TASK_TERMINATE'], child, 0)
            else:
                self.assertEqual(vm.object_value(obj, 'state'), 2)
                self.assertGreater(len(vm.free_pages()), len(free))
            vm.call('ipcClose', token)
            self.assertEqual(vm.object_value(obj, 'creator'), 0)
            self.assertEqual(vm.object_value(obj, 'references'), 0)

    def test_factory_root_validation_and_mode_attenuation(self):
        vm = ObjectsM()
        reference = vm.call('taskCreateImage', 0x14000, 0x1401C, 0)
        table = vm.field_address('handles')
        for modes, quota in ((0, 1), (4, 1), (3, 0), (3, 13)):
            self.assertFalse(vm.call('endpointFactoryBootstrap', table, modes, quota, False))
        self.assertTrue(vm.call('endpointFactoryBootstrap', table, 1, 1, False))
        self.assertFalse(vm.call('endpointFactoryBootstrap', table, 3, 12, True))
        self.assertTrue(vm.call('taskInstallRuntimeStart', reference, 0, 0, 0))
        self.assertTrue(vm.call('taskPublish', reference))
        with self.assertRaises(TaskEntered):
            vm.call('taskStart', 1000000)
        self.assertEqual(vm.factory(1), error(1))
        for mode in (2, 31, 0xFFFFFFFF):
            self.assertEqual(vm.factory(mode), error(22))
        self.assertTrue(0 < vm.factory() < 0x80000000)
        self.assertEqual(vm.factory(), error(23))

    def test_boundary_generations_retire_and_stale_handles_never_rebind(self):
        vm = fixture(2)
        typ = vm.decls['tasks'].sym.type.target.field('handles').type.field('entries').type.elem
        vm.memory[vm.field_address('handles') + typ.field('generation').offset] = GEN_MAX - 1
        obj = vm.addresses['endpoints']
        vm.memory[vm.object_field(obj, 'generation')] = 0xFFFFFFFE
        token = vm.factory()
        self.assertEqual(token, (GEN_MAX << 8) | 1)
        vm.call('ipcClose', token)
        self.assertEqual(vm.object_value(obj, 'state'), 3)
        fresh = vm.factory()
        self.assertEqual(fresh & 255, 2)
        self.assertNotEqual(vm.root(fresh), obj)
        self.assertEqual(vm.call('ipcDestroy', token), error(9))
        self.assertEqual(vm.object_value(obj, 'generation'), 0xFFFFFFFF)

    def test_creator_and_receiver_death_revoke_with_different_owners(self):
        for victim in ('creator', 'receiver'):
            vm = fixture(2)
            child = create(vm, configure=False, publish=False)
            token = vm.factory(1, child)
            obj = vm.root(token)
            vm.invoke(C['SYS_TASK_CONFIGURE'], child, token, 3, 0)
            vm.invoke(C['SYS_TASK_PUBLISH'], child)
            vm.call('handlesReleaseTask', vm.field_address('handles', 1 if victim == 'creator' else child),
                    1 if victim == 'creator' else child)
            self.assertEqual(vm.object_value(obj, 'state'), 2)
            self.assertEqual(vm.root(token), 0)
            if victim == 'creator':
                self.assertEqual(vm.factory(), error(1))
            else:
                vm.call('ipcClose', token)
                self.assertEqual(vm.object_value(obj, 'references'), 0)

    def test_device_factory_checks_control_masks_and_irq_reissue(self):
        vm = fixture(2)
        child = create(vm, configure=False, publish=False)
        vm.invoke(C['SYS_TASK_DEVICES'], child, C['DEVICE_INPUT'])
        token = vm.result(1)[0]
        self.assertTrue(0 < token < 0x80000000)
        self.assertTrue(vm.call('irqTokenValid', child, token, C['KEYBOARD_IRQ']))
        vm.invoke(C['SYS_TASK_DEVICES'], child, C['DEVICE_UART_TX'])
        self.assertEqual(vm.result(1)[0], error(16))
        other = create(vm, configure=False, publish=False)
        vm.invoke(C['SYS_TASK_DEVICES'], other, C['DEVICE_INPUT'])
        self.assertEqual(vm.result(1)[0], error(16))
        vm.invoke(C['SYS_TASK_DEVICES'], other, C['DEVICE_DISK'])
        self.assertEqual(vm.result(1)[0], error(1))
        vm.run(2)
        vm.invoke(C['SYS_TASK_DEVICES'], other, C['DEVICE_UART_TX'])
        self.assertEqual(vm.result(2)[0], error(1))
        vm.run(1)
        vm.invoke(C['SYS_TASK_TERMINATE'], child, 0)
        vm.invoke(C['SYS_TASK_DEVICES'], other, C['DEVICE_INPUT'] | C['DEVICE_UART_TX'])
        fresh = vm.result(1)[0]
        self.assertTrue(0 < fresh < 0x80000000)
        self.assertNotEqual(fresh, token)
        self.assertFalse(vm.call('irqTokenValid', other, token, C['KEYBOARD_IRQ']))
        self.assertEqual(vm.call('irqGrant', other, C['KEYBOARD_IRQ']), 0)


if __name__ == '__main__':
    unittest.main()
