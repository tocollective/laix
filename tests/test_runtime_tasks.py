"""Execute checked task/control/IPC/MMU sources, with no code generation."""
import unittest

from test_ipc_request_reply import ServiceM
from test_task import TaskEntered, USER_DATA
from test_ipc_handles import error
from source_m import LAYOUT as C, KernelPanic
from test_kernel import check_m, parse_asm, LAIX
from mlang import syntax as msyntax
from mlang.typesys import size_of


class LifecycleM(ServiceM):
    def __init__(self):
        super().__init__()
        self.addresses.update(runtimeApprovedStart=0x14400, runtimeApprovedEnd=0x14410)
        for i in range(4):
            self.memory[0x14400 + 4 * i] = 0x3300 + i
        self.allocation_count = 0
        self.fail_allocation = None

    def field_address(self, field, id=1):
        return super().field_address(field, id & 255)

    def call(self, name, *args):
        if name in ('allocPage', 'allocPageRun') and hasattr(self, 'allocation_count'):
            self.allocation_count += 1
            if self.allocation_count == self.fail_allocation:
                return 0
        return super().call(name, *args)

    def expr(self, node, local):
        # SourceM backs local structs with word fields. Read their byte casts
        # as packed little-endian words, as the actual memory-copy ABI does.
        if isinstance(node, msyntax.Index) and size_of(node.type) == 1:
            address = self.address(node, local)
            if address >= 0x0D000000:
                return (self.memory[address & ~3] >> ((address & 3) * 8)) & 255
        return super().expr(node, local)

    def reap(self):
        task = self.globals['currentTask']
        self.cpu_sp = self.memory[task + self.task_type.field('kernelStackTop').offset] - 32
        self.controls[0] = C['STATUS_EXL']
        self.call('taskReap')

    def event(self):
        return tuple(int.from_bytes(self.read_bytes(self.pages(self.current())[1] + 4 * i, 4), 'little')
                     for i in range(11))

    def control_count(self):
        typ = self.decls['taskControls'].sym.type.elem
        return sum(bool(self.memory[self.addresses['taskControls'] + typ.size * i +
                                    typ.field('reference').offset]) for i in range(16))


def fixture(count=2, endpoint=False):
    vm = LifecycleM()
    for slot in range(1, count + 1):
        reference = vm.call('taskCreateImage', 0x14000, 0x1401C, 0)
        assert reference == slot
        assert vm.call('taskInstallRuntimeStart', reference, 0, 0, 0)
        assert vm.call('taskControlBootstrapSelf', reference)
        vm.memory[vm.field_address('createImages', reference)] = 1
        # These fixtures represent bootstrap-authorized supervisors.
        table = vm.decls['tasks'].sym.type.elem.field('handles').type
        vm.memory[vm.field_address('handles', reference) + table.field('factoryRecovery').offset] = 1
        assert vm.call('taskPublish', reference)
    if endpoint:
        vm.root_handle = vm.call('endpointBootstrapService', vm.field_address('handles'), 1)
    with unittest.TestCase().assertRaises(TaskEntered):
        vm.call('taskStart', 1000000)
    return vm


def create(vm, configure=True, publish=True, token=0, rights=0, argument=0):
    vm.invoke(C['SYS_TASK_CREATE'], 1)
    reference = vm.result(vm.current())[0]
    assert 0 < reference < 0x80000000
    if configure:
        vm.invoke(C['SYS_TASK_CONFIGURE'], reference, token, rights, argument)
        assert vm.result(vm.current())[0] == 0
    if publish:
        vm.invoke(C['SYS_TASK_PUBLISH'], reference)
        assert vm.result(vm.current())[0] == 0
    return reference


class RuntimeTaskTests(unittest.TestCase):
    def test_runtime_abi_and_assembly_parse(self):
        modules = check_m(LAIX / 'src/task/control.m')
        structs = {name: module.scope[name].type for module in modules
                   for name in ('RuntimeStart', 'TaskEvent') if name in module.scope}
        self.assertEqual(structs['RuntimeStart'].size, C['RUNTIME_START_BYTES'])
        self.assertEqual(structs['TaskEvent'].size, C['TASK_EVENT_BYTES'])
        parse_asm(LAIX / 'src/task/control.asm')
        check_m(LAIX / 'src/kernel/supervisor_main.m')
        check_m(LAIX / 'user/syscalls.m')

    def test_40_lifetimes_reuse_resources_reject_stale_and_retain_history(self):
        vm = fixture(1)
        baseline = vm.free_pages()
        references = []
        for code in range(40):
            ref = create(vm, argument=code)
            references.append(ref)
            self.assertEqual(ref & 255, 2)
            self.assertEqual(ref >> 8, code)
            vm.run(ref)
            vm.invoke(C['SYS_EXIT'], code)
            self.assertEqual(vm.current(), 1)
            # The retiring root/stack are still allocated until selected-stack reaping.
            self.assertFalse(vm.field('reaped', ref))
            vm.reap()
            self.assertEqual(vm.field('state', ref), 0)
            self.assertEqual(vm.call('taskGet', ref), 0)
            self.assertEqual(vm.free_pages(), baseline)
            vm.invoke(C['SYS_TASK_COLLECT'], ref, USER_DATA)
            self.assertEqual(vm.result(1)[0], 0)
            event = vm.event()
            self.assertEqual(event[:5], (ref, 2, 3, code, C['TASK_EVENT_RECLAIMED']))
            vm.invoke(C['SYS_TASK_COLLECT'], ref, USER_DATA)
            self.assertEqual(vm.result(1)[0], error(1))
            if code:
                self.assertEqual(vm.call('taskGet', references[-2]), 0)
                vm.invoke(C['SYS_TASK_TERMINATE'], references[-2], 99)
                self.assertEqual(vm.result(1)[0], error(1))
        self.assertEqual(len(set(references)), 40)
        self.assertEqual(vm.globals['taskHistoryCount'], 32)
        self.assertEqual(vm.control_count(), 1)

    def test_created_unpublished_mapping_and_context_validation(self):
        vm = fixture()
        ref = create(vm, configure=False, publish=False)
        self.assertEqual(vm.field('state', ref), 5)
        self.assertFalse(vm.field('queued', ref))
        vm.invoke(C['SYS_TASK_PUBLISH'], ref)
        self.assertEqual(vm.result(1)[0], error(16))
        with self.assertRaisesRegex(KernelPanic, 'ready transition'):
            vm.call('taskEnqueue', vm.call('taskGet', ref))
        vm.invoke(C['SYS_TASK_CONFIGURE'], ref, 0, 0, 17)
        self.assertEqual(vm.result(1)[0], 0)
        boot = vm.field('bootPage', ref)
        self.assertEqual([vm.memory[boot + 4 * i] for i in range(10)],
                         [C['RUNTIME_START_MAGIC'], 1, 40, ref, 3, USER_DATA, 4096, 0, 0, 17])
        self.assertEqual(vm.leaf(C['START_BLOCK_VA'], vm.field('directory', ref)) & 31, 19)
        epc = vm.field_address('context', ref) + C['TF_EPC']
        vm.memory[epc] = USER_DATA
        vm.invoke(C['SYS_TASK_PUBLISH'], ref)
        self.assertEqual(vm.result(1)[0], error(22))
        self.assertEqual(vm.field('state', ref), 5)
        vm.memory[epc] = 0x40000000
        vm.invoke(C['SYS_TASK_PUBLISH'], ref)
        self.assertEqual(vm.result(1)[0], 0)

    def test_creation_capability_does_not_authorize_foreign_control_or_images(self):
        vm = fixture()
        ref = create(vm, configure=False, publish=False)
        vm.run(2)
        for number, args in ((37, (ref, 0, 0, 0)), (38, (ref,)), (39, (ref, USER_DATA)),
                             (40, (ref, 7)), (41, (ref, USER_DATA))):
            vm.invoke(number, *args)
            self.assertEqual(vm.result(2)[0], error(1))
            self.assertEqual(vm.field('state', ref), 5)
        for image in (0, 2, 0x14400, 0xFFFFFFFF):
            vm.invoke(36, image)
            self.assertEqual(vm.result(2)[0], error(1))
        vm.memory[vm.field_address('createImages', 2)] = 0
        vm.invoke(36, 1)
        self.assertEqual(vm.result(2)[0], error(1))
        vm.run(1)
        vm.invoke(40, ref, 0)
        self.assertEqual(vm.result(1)[0], 0)
        self.assertEqual(vm.call('taskGet', ref), 0)

    def test_completion_copy_failure_does_not_consume_and_fault_has_diagnostics(self):
        vm = fixture(1)
        ref = create(vm)
        vm.invoke(41, ref, USER_DATA)
        self.assertEqual(vm.result(1)[0], error(11))
        vm.run(ref)
        frame = vm.field_address('context', ref)
        for name, value in (('CAUSE', 3), ('EPC', 0x4000000C), ('BADADDR', 1), ('FCSR', 9)):
            vm.memory[frame + C['TF_' + name]] = value
        vm.call('taskFinish', frame, 3, True)
        vm.reap()
        vm.invoke(41, ref, USER_DATA + 4096 - 20)
        self.assertEqual(vm.result(1)[0], error(14))
        vm.invoke(41, ref, USER_DATA)
        self.assertEqual(vm.result(1)[0], 0)
        event = vm.event()
        self.assertEqual(event[:8], (ref, 2, 3, 3, 5, 3, 0x4000000C, 1))
        self.assertEqual(event[10], 9)

    def test_ready_termination_unqueues_and_running_self_switches_before_reap(self):
        vm = fixture()
        ref = create(vm)
        vm.invoke(40, ref, 123)
        self.assertEqual(vm.result(1)[0], 0)
        self.assertEqual(vm.field('state', ref), 3)
        self.assertFalse(vm.field('queued', ref))
        self.assertEqual(vm.globals['readyCount'], 1)
        vm.reap()
        vm.invoke(41, ref, USER_DATA)
        self.assertEqual(vm.event()[:5], (ref, 3, 3, 123, 6))
        old_root, old_stack = vm.field('directory'), vm.field('kernelStackBottom')
        vm.invoke(40, 1, 71)
        self.assertEqual(vm.current(), 2)
        self.assertEqual(vm.field('state'), 3)
        self.assertFalse(vm.field('reaped'))
        self.assertEqual(vm.field('directory'), old_root)
        self.assertEqual(vm.field('kernelStackBottom'), old_stack)
        vm.reap()
        self.assertTrue(vm.field('reaped'))

    def test_every_allocation_and_mapping_failure_rolls_back(self):
        # Count actual allocator entries in the successful runtime path.
        probe = fixture(1)
        probe.allocation_count = 0
        create(probe)
        count = probe.allocation_count
        for step in range(1, count + 1):
            with self.subTest(allocation=step):
                vm = fixture(1)
                baseline = vm.free_pages()
                vm.allocation_count = 0
                vm.fail_allocation = step
                vm.invoke(36, 1)
                ref = vm.result(1)[0]
                if ref < 0x80000000:
                    vm.invoke(37, ref, 0, 0, 0)
                    self.assertEqual(vm.result(1)[0], error(23))
                else:
                    self.assertEqual(ref, error(23))
                self.assertEqual(vm.field('state', 2), 0)
                self.assertEqual(vm.free_pages(), baseline)
                self.assertEqual(vm.control_count(), 1)
        for mapping in range(1, 5):
            with self.subTest(mapping=mapping):
                vm = fixture(1)
                baseline = vm.free_pages()
                vm.fail_mapping = vm.mapping_calls + mapping
                vm.invoke(36, 1)
                ref = vm.result(1)[0]
                if ref < 0x80000000:
                    vm.invoke(37, ref, 0, 0, 0)
                    self.assertEqual(vm.result(1)[0], error(23))
                else:
                    self.assertEqual(ref, error(23))
                self.assertEqual(vm.free_pages(), baseline)
                self.assertEqual(vm.field('state', 2), 0)
                self.assertEqual(vm.control_count(), 1)

    def test_termination_cancels_raw_send_receive_accept_call_and_reply_waits(self):
        for wait in ('send', 'receive', 'accept', 'call', 'reply'):
            with self.subTest(wait=wait):
                vm = LifecycleM()
                for ref in (1, 2):
                    self.assertEqual(vm.call('taskCreateImage', 0x14000, 0x1401C, 0), ref)
                    self.assertTrue(vm.call('taskInstallRuntimeStart', ref, 0, 0, 0))
                self.assertTrue(vm.call('taskControlBootstrap', 1, 2, 31))
                vm.memory[vm.field_address('reusable', 2)] = 1
                service = wait in ('call', 'reply', 'accept')
                manager = 1 if wait in ('call', 'reply') else 2
                root = vm.call('endpointBootstrapService' if service else 'endpointBootstrap',
                               vm.field_address('handles', manager), manager)
                if manager == 1:
                    child = vm.call('handleCopy', vm.field_address('handles'), root,
                                    vm.field_address('handles', 2), 1, 2, 1)
                else:
                    child = root
                for ref in (1, 2):
                    self.assertTrue(vm.call('taskPublish', ref))
                with self.assertRaises(TaskEntered):
                    vm.call('taskStart', 1000000)
                vm.run(2)
                if wait == 'send':
                    vm.invoke(19, child, USER_DATA, 4)
                elif wait == 'receive':
                    vm.invoke(20, child, USER_DATA, 32)
                elif wait == 'accept':
                    vm.invoke(22, child, USER_DATA, 32)
                else:
                    vm.request(child)
                vm.run(1)
                if wait == 'reply':
                    reply = vm.accept(root)
                    self.assertEqual(vm.field('ipcReplyOwner', 2), 1)
                self.assertEqual(vm.field('state', 2), 4)
                vm.invoke(40, 2, 42)
                self.assertEqual(vm.result(1)[0], 0)
                self.assertEqual(vm.field('state', 2), 3)
                self.assertEqual(vm.field('ipcEndpoint', 2), 0)
                self.assertEqual(vm.field('waitReason', 2), 0)
                self.assertFalse(vm.wakes[2])
                vm.reap()
                vm.invoke(41, 2, USER_DATA)
                self.assertEqual(vm.event()[:5], (2, 2, 3, 42, 6))
                if wait == 'reply':
                    vm.response(reply)
                    self.assertEqual(vm.result(1)[0], error(9))

    def test_stale_reply_handle_and_allocator_owner_after_reuse(self):
        vm = LifecycleM()
        for ref in (1, 2):
            self.assertEqual(vm.call('taskCreateImage', 0x14000, 0x1401C, 0), ref)
            self.assertTrue(vm.call('taskInstallRuntimeStart', ref, 0, 0, 0))
        self.assertTrue(vm.call('taskControlBootstrap', 1, 2, 31))
        vm.memory[vm.field_address('createImages')] = 1
        vm.memory[vm.field_address('reusable', 2)] = 1
        root = vm.call('endpointBootstrapService', vm.field_address('handles'), 1)
        old_handle = vm.call('handleCopy', vm.field_address('handles'), root,
                             vm.field_address('handles', 2), 1, 2, 1)
        for ref in (1, 2):
            self.assertTrue(vm.call('taskPublish', ref))
        with self.assertRaises(TaskEntered):
            vm.call('taskStart', 1000000)
        vm.run(2)
        vm.request(old_handle)
        vm.run(1)
        old_reply = vm.accept(root)
        vm.invoke(40, 2, 42)
        vm.reap()
        vm.invoke(41, 2, USER_DATA)
        replacement = create(vm, token=root, rights=1)
        self.assertEqual(replacement, 258)
        self.assertEqual(vm.call('taskGet', 2), 0)
        self.assertEqual(vm.call('handleEntry', vm.field_address('handles', replacement), old_handle), 0)
        self.assertFalse(vm.call('physicalPageOwned', vm.pages(replacement)[1], 2, 5))
        new_handle = vm.memory[vm.field('bootPage', replacement) + 28]
        vm.run(replacement)
        vm.request(new_handle)
        vm.run(1)
        new_reply = vm.accept(root)
        self.assertNotEqual(old_reply, new_reply)
        vm.response(old_reply)
        self.assertEqual(vm.result(1)[0], error(9))
        self.assertEqual(vm.field('state', replacement), 4)
        vm.response(new_reply)
        self.assertEqual(vm.wakes[replacement], 1)
        vm.invoke(18, root)
        self.assertEqual(vm.result(1)[0], 0)

    def test_inherited_rights_rollback_and_revoked_startup_never_publish(self):
        for failure in ('allocation', 'mapping'):
            with self.subTest(failure=failure):
                vm = fixture(1, endpoint=True)
                endpoint = vm.endpoint(vm.root_handle)
                baseline = vm.free_pages()
                ref = create(vm, configure=False, publish=False)
                if failure == 'allocation':
                    vm.fail_allocation = vm.allocation_count + 1
                else:
                    vm.fail_mapping = vm.mapping_calls + 1
                vm.invoke(37, ref, vm.root_handle, 1, 0)
                self.assertEqual(vm.result(1)[0], error(23))
                self.assertEqual(vm.field('state', ref), 0)
                self.assertEqual(vm.free_pages(), baseline)
                self.assertEqual(vm.object_value(endpoint, 'references'), 1)
                self.assertEqual(vm.control_count(), 1)
        vm = fixture(1, endpoint=True)
        ref = create(vm, publish=False, token=vm.root_handle, rights=1)
        vm.invoke(18, vm.root_handle)
        vm.invoke(38, ref)
        self.assertEqual(vm.result(1)[0], error(1))
        self.assertEqual(vm.field('state', ref), 5)
        self.assertFalse(vm.field('queued', ref))
        vm.invoke(40, ref, 0)
        self.assertEqual(vm.result(1)[0], 0)
        self.assertEqual(vm.field('state', ref), 0)

    def test_generation_exhaustion_never_wraps(self):
        vm = fixture(1)
        for slot in range(2, 9):
            vm.memory[vm.field_address('id', slot)] = (0x7FFFFF << 8) | slot
        baseline = vm.free_pages()
        vm.invoke(36, 1)
        self.assertEqual(vm.result(1)[0], error(23))
        self.assertEqual(vm.free_pages(), baseline)

    def test_irq_cancel_and_dma_quarantine_prevent_reuse_until_physical_quiescence(self):
        from test_screen_services import Devices
        vm = LifecycleM()
        vm.memory = Devices(vm)
        vm.addresses.update(fontData=0x16000, fontDataEnd=0x16000 + 32 + 19 * 8)
        for i, value in enumerate((0x3146414C, 1, 19, 32, 184, 16, 32, 0)):
            vm.memory[0x16000 + i * 4] = value
        for ref in (1, 2, 3):
            self.assertEqual(vm.call('taskCreateImage', 0x14000, 0x1401C, 0), ref)
            self.assertTrue(vm.call('taskInstallRuntimeStart', ref, 0, 0, 0))
        self.assertTrue(vm.call('taskControlBootstrap', 3, 2, 31))
        self.assertTrue(vm.call('serviceDevicesInit', 1, 2, C['DISK0_BASE'], 1024))
        irq = vm.call('irqGrant', 2, 3)
        vm.memory[vm.field_address('reusable', 2)] = 1
        vm.memory[vm.field_address('createImages', 3)] = 1
        for ref in (1, 2, 3):
            self.assertTrue(vm.call('taskPublish', ref))
        with self.assertRaises(TaskEntered):
            vm.call('taskStart', 1000000)
        vm.run(2)
        self.assertEqual(vm.call('irqComplete', 2, irq), 0)
        self.assertGreater(vm.call('deviceSubmit', 2, 0, 16, 1, 0), 0)
        root, stack, bounce = vm.field('directory', 2), vm.field('kernelStackBottom', 2), vm.globals['deviceBounce']
        vm.call('irqWait', vm.field_address('context', 2), irq, 5)
        self.assertEqual(vm.current(), 3)
        vm.invoke(40, 2, 83)
        for _ in range(3):
            vm.reap()
            self.assertEqual(vm.field('state', 2), 3)
            self.assertEqual(vm.field('directory', 2), root)
            self.assertEqual(vm.field('kernelStackBottom', 2), stack)
            self.assertFalse(vm.call('physicalPageAvailable', bounce))
            self.assertEqual(vm.call('irqLookup', 2, irq), C['PIC_LINE_COUNT'])
        vm.invoke(39, 2, USER_DATA)
        self.assertEqual(vm.event()[4], C['TASK_EVENT_TERMINATED'] | C['TASK_EVENT_QUARANTINED'])
        # Early collection cannot turn logical death into physical reclamation.
        vm.invoke(41, 2, USER_DATA)
        newer = create(vm)
        self.assertEqual(newer & 255, 4)
        self.assertTrue(vm.call('irqNotify', 3) == 0)
        self.assertFalse(vm.wakes[2])
        vm.memory.complete()
        vm.reap()
        self.assertEqual(vm.field('state', 2), 0)
        self.assertTrue(vm.call('physicalPageAvailable', bounce))
        replacement = create(vm)
        self.assertEqual(replacement, 258)
        self.assertEqual(vm.call('irqLookup', replacement, irq), C['PIC_LINE_COUNT'])

    def test_slow_collector_is_bounded_without_lost_events(self):
        vm = fixture(1)
        refs = []
        for _ in range(15):
            ref = create(vm)
            refs.append(ref)
            vm.run(ref)
            vm.invoke(1, 0)
            vm.reap()
        baseline = vm.free_pages()
        vm.invoke(36, 1)
        self.assertEqual(vm.result(1)[0], error(23))
        self.assertEqual(vm.free_pages(), baseline)
        for ref in refs:
            vm.invoke(41, ref, USER_DATA)
            self.assertEqual(vm.result(1)[0], 0)
            self.assertEqual(vm.event()[0], ref)
        self.assertEqual(vm.control_count(), 1)

    def test_supervisor_death_discards_created_but_does_not_kill_published_children(self):
        vm = fixture()
        private = create(vm, configure=False, publish=False)
        published = create(vm)
        vm.invoke(1, 0)
        self.assertEqual(vm.current(), 2)
        self.assertEqual(vm.field('state', private), 0)
        self.assertEqual(vm.field('state', published), 1)
        vm.reap()
        vm.run(published)
        vm.invoke(1, 42)
        vm.reap()
        self.assertEqual(vm.field('state', published), 0)
        self.assertEqual(vm.control_count(), 1)


if __name__ == '__main__':
    unittest.main()
