"""Adversarial lifecycle acceptance using checked kernel sources, without building WRM."""
from collections import Counter
import unittest

from test_runtime_tasks import LifecycleM, fixture, create
from test_task import TaskEntered, USER_DATA, PAGE
from test_ipc_handles import error
from test_screen_services import Devices
from source_m import LAYOUT as C


def boot(manager=2, raw=False, receive=False, irq=False, dma=False, self_control=False):
    vm = LifecycleM()
    if irq or dma:
        vm.memory = Devices(vm)
    for ref in range(1, 5):
        assert vm.call('taskCreateImage', 0x14000, 0x1401C, 0) == ref
        assert vm.call('taskInstallRuntimeStart', ref, 0, 0, 0)
    assert vm.call('taskControlBootstrap', 2 if self_control else 1, 2, 31)
    root = vm.call('endpointBootstrap' if raw else 'endpointBootstrapService',
                   vm.field_address('handles', manager), manager)
    tokens = {manager: root}
    for ref in range(1, 5):
        if ref != manager:
            tokens[ref] = vm.call('handleCopy', vm.field_address('handles', manager), root,
                                  vm.field_address('handles', ref), manager, ref,
                                  2 if receive else 1)
            assert 0 < tokens[ref] < 0x80000000
    vm.memory[vm.field_address('reusable', 2)] = 1
    vm.memory[vm.field_address('createImages', 1)] = 1
    # Slot-pressure acceptance exercises a trusted recovery supervisor.
    assert vm.call('endpointFactoryBootstrap', vm.field_address('handles', 1), 3, 12, True)
    if irq:
        vm.irq = vm.call('irqGrant', 2, 3)
        assert vm.irq
    if dma:
        vm.addresses.update(fontData=0x16000, fontDataEnd=0x16000 + 32 + 19 * 8)
        for i, value in enumerate((0x3146414C, 1, 19, 32, 184, 16, 32, 0)):
            vm.memory[0x16000 + i * 4] = value
        assert vm.call('serviceDevicesInit', 0, 2, C['DISK0_BASE'], 1024)
    for ref in range(1, 5):
        assert vm.call('taskPublish', ref)
    with unittest.TestCase().assertRaises(TaskEntered):
        vm.call('taskStart', 1000000)
    return vm, tokens


def task_resources(vm, reference):
    """All TCB bytes except the caller's normal syscall input/result frame."""
    base = vm.call('taskGet', reference)
    context = vm.task_type.field('context')
    return tuple(vm.memory.get(base + i, 0) for i in range(vm.task_type.size)
                 if not context.offset <= i < context.offset + context.type.size)


def authority_snapshot(vm):
    controls = vm.table_base('taskControls')
    size = vm.decls['taskControls'].sym.type.target.size * 16
    queue = vm.table_base('readyQueue')
    return (vm.globals['readyHead'], vm.globals['readyCount'],
            tuple(vm.memory[queue + i * 4] for i in range(8)),
            tuple(vm.memory.get(controls + i, 0) for i in range(size)))


class RuntimeTaskAcceptanceTests(unittest.TestCase):
    def test_authorized_manager_termination_matrix_completes_each_survivor_once(self):
        for state in ('Ready', 'Running', 'RawSend', 'RawReceive', 'Accept', 'IRQ'):
            with self.subTest(state=state):
                raw = state.startswith('Raw')
                vm, tokens = boot(raw=raw, receive=state == 'RawReceive', irq=state == 'IRQ',
                                  self_control=state == 'Running')
                for peer in (3, 4):
                    vm.run(peer)
                    if raw:
                        vm.invoke(20 if state == 'RawReceive' else 19, tokens[peer], USER_DATA, 4)
                    else:
                        vm.request(tokens[peer], bytes([peer]))
                if state != 'Ready':
                    vm.run(2)
                    if raw:
                        vm.invoke(20 if state == 'RawReceive' else 19, tokens[2], USER_DATA, 4)
                    elif state == 'Accept':
                        vm.accept(tokens[2])
                        vm.accept(tokens[2])
                        vm.invoke(22, tokens[2], USER_DATA, 32)
                    elif state == 'IRQ':
                        vm.invoke(25, vm.irq)
                        self.assertEqual(vm.result(2)[0], 0)
                        vm.invoke(24, vm.irq, 5)
                expected_state = 1 if state == 'Ready' else 2 if state == 'Running' else 4
                self.assertEqual(vm.field('state', 2), expected_state)
                root, stack = vm.field('directory', 2), vm.field('kernelStackBottom', 2)
                if state != 'Running':
                    vm.run(1)
                vm.wakes.clear()
                vm.invoke(40, 2, 77)
                self.assertEqual(vm.field('state', 2), 3)
                self.assertEqual(vm.field('directory', 2), root)
                self.assertEqual(vm.field('kernelStackBottom', 2), stack)
                self.assertFalse(vm.field('reaped', 2))
                for peer in (3, 4):
                    self.assertEqual(vm.result(peer), (error(32), 0))
                    self.assertEqual(vm.field('state', peer), 1)
                    self.assertEqual(vm.field('waitReason', peer), 0)
                self.assertEqual(vm.wakes, Counter({3: 1, 4: 1}))
                vm.run(1)
                vm.invoke(40, 2, 88)
                self.assertEqual(vm.result(1)[0], error(1) if state == 'Running' else error(3))
                vm.reap()
                vm.reap()
                self.assertEqual(vm.wakes, Counter({3: 1, 4: 1}))
                if state == 'Running':
                    history = vm.addresses['taskHistory']
                    self.assertEqual(tuple(vm.memory[history + i * 4] for i in range(5)),
                                     (2, 2, 3, 77, 6))
                else:
                    vm.invoke(41, 2, USER_DATA)
                    self.assertEqual(vm.event()[:5], (2, 2, 3, 77, 6))
                    vm.invoke(41, 2, USER_DATA)
                    self.assertEqual(vm.result(1)[0], error(1))

    def test_await_accept_and_reply_victim_removal_preserves_survivor_fifo(self):
        for accepted in (False, True):
            with self.subTest(state='AwaitReply' if accepted else 'AwaitAccept'):
                vm, tokens = boot(manager=1)
                for ref in (2, 3, 4):
                    vm.run(ref)
                    vm.request(tokens[ref], bytes([ref]))
                vm.run(1)
                old_reply = vm.accept(tokens[1]) if accepted else None
                self.assertEqual(vm.field('waitReason', 2), 6 if accepted else 4)
                vm.wakes.clear()
                vm.invoke(40, 2, 78)
                endpoint = vm.endpoint(tokens[1])
                self.assertEqual(vm.queue(endpoint), [3, 4])
                self.assertFalse(vm.wakes)
                if accepted:
                    vm.response(old_reply)
                    self.assertEqual(vm.result(1)[0], error(9))
                replies = [vm.accept(tokens[1]), vm.accept(tokens[1])]
                for peer, reply in zip((3, 4), replies):
                    vm.response(reply, bytes([peer + 10]))
                    self.assertEqual(vm.result(peer), (1, 1))
                    self.assertEqual(vm.read_bytes(vm.pages(peer)[1], 1), bytes([peer + 10]))
                    vm.response(reply)
                    self.assertEqual(vm.result(1)[0], error(9))
                vm.reap()
                self.assertEqual(vm.wakes, Counter({3: 1, 4: 1}))
                vm.invoke(41, 2, USER_DATA)
                self.assertEqual(vm.event()[:5], (2, 2, 3, 78, 6))

    def test_foreign_and_forged_controls_preserve_tasks_queues_and_user_memory(self):
        vm = fixture(2)
        child = create(vm)
        vm.run(2)
        references = (child, 1, 0, 9, child + 256, child | 0x80000000, 0xFFFFFFFF)
        for reference in references:
            for number, args in ((37, (reference, 0, 0, 0)), (38, (reference,)),
                                 (39, (reference, USER_DATA)), (40, (reference, 99)),
                                 (41, (reference, USER_DATA))):
                with self.subTest(reference=reference, syscall=number):
                    before = tuple(task_resources(vm, ref) for ref in (1, 2, child))
                    frame_bytes = vm.task_type.field('context').type.size
                    target_frame = vm.read_bytes(vm.field_address('context', child), frame_bytes)
                    data = tuple(vm.read_bytes(vm.pages(ref)[1], PAGE) for ref in (1, 2, child))
                    free, controls = vm.free_pages(), vm.control_count()
                    authority = authority_snapshot(vm)
                    vm.invoke(number, *args)
                    self.assertEqual(vm.result(2)[0], error(1))
                    self.assertEqual(tuple(task_resources(vm, ref) for ref in (1, 2, child)), before)
                    self.assertEqual(vm.read_bytes(vm.field_address('context', child), frame_bytes), target_frame)
                    self.assertEqual(tuple(vm.read_bytes(vm.pages(ref)[1], PAGE) for ref in (1, 2, child)), data)
                    self.assertEqual(vm.free_pages(), free)
                    self.assertEqual(vm.control_count(), controls)
                    self.assertEqual(authority_snapshot(vm), authority)

    def test_replacement_rejects_old_control_reply_irq_and_endpoint_tokens(self):
        vm, tokens = boot(manager=1, irq=True)
        old_handle, old_irq = tokens[2], vm.irq
        vm.run(2)
        vm.request(old_handle)
        vm.run(1)
        old_reply = vm.accept(tokens[1])
        vm.invoke(40, 2, 90)
        vm.reap()
        # Retain the old completion capability while the new lifetime runs.
        replacement = create(vm, token=tokens[1], rights=1)
        self.assertEqual(replacement, 4098)
        vm.run(replacement)
        before = task_resources(vm, replacement)
        data = vm.read_bytes(vm.pages(replacement)[1], PAGE)
        for number, args, errno in ((24, (old_irq, 5), 1), (25, (old_irq,), 1),
                                    (21, (old_handle, USER_DATA, 1, USER_DATA, 32), 9),
                                    (18, (old_handle,), 9)):
            vm.invoke(number, *args)
            self.assertEqual(vm.result(replacement)[0], error(errno))
            self.assertEqual(task_resources(vm, replacement), before)
            self.assertEqual(vm.read_bytes(vm.pages(replacement)[1], PAGE), data)
        new_handle = vm.memory[vm.field('bootPage', replacement) + 28]
        self.assertNotEqual(new_handle, old_handle)
        vm.request(new_handle)
        vm.run(1)
        new_reply = vm.accept(tokens[1])
        self.assertNotEqual(old_reply, new_reply)
        before = task_resources(vm, replacement)
        for number, args in ((37, (2, 0, 0, 0)), (38, (2,)), (40, (2, 99))):
            vm.invoke(number, *args)
            self.assertEqual(vm.result(1)[0], error(16) if number in (37, 38) else error(3))
            self.assertEqual(task_resources(vm, replacement), before)
        vm.response(old_reply)
        self.assertEqual(vm.result(1)[0], error(9))
        self.assertEqual(task_resources(vm, replacement), before)
        vm.invoke(41, 2, USER_DATA)
        self.assertEqual(vm.event()[:5], (2, 2, 3, 90, 6))
        self.assertEqual(task_resources(vm, replacement), before)
        vm.wakes.clear()
        vm.response(new_reply, b'new')
        self.assertEqual(vm.result(replacement), (3, 3))
        self.assertEqual(vm.wakes, Counter({replacement: 1}))

    def test_failures_after_successful_acquisitions_rollback_unpublished_task(self):
        # Fail after resources have entered the ledger, including a successfully
        # installed mapping. Existing tests also fail each allocator/table
        # allocation and map before acquisition. No kernel failpoint is needed.
        steps = [('allocTaskPages', 1), *[('mapPage', i) for i in range(1, 5)],
                 ('taskInstallRuntimeStart', 1)]
        for operation, occurrence in steps:
            with self.subTest(after=operation, occurrence=occurrence):
                vm = fixture(1, endpoint=True)
                free, control_count = vm.free_pages(), vm.control_count()
                endpoint = vm.endpoint(vm.root_handle)
                original = vm.call
                observed, hit = [], [0]

                def inject(name, *args):
                    result = original(name, *args)
                    if name in ('allocPage', 'allocPageRun', 'allocTaskPages', 'mapPage',
                                'handleCopy', 'taskInstallRuntimeStart'):
                        if vm.field('state', 2) == 5:
                            observed.append(name)
                            self.assertFalse(vm.field('queued', 2))
                            self.assertEqual(vm.globals['readyCount'], 0)
                        if name == operation and result:
                            hit[0] += 1
                            if hit[0] == occurrence:
                                return False
                    return result

                vm.call = inject
                vm.invoke(36, 1)
                reference = vm.result(1)[0]
                if reference < 0x80000000:
                    vm.invoke(37, reference, vm.root_handle, 1, 7)
                    self.assertEqual(vm.result(1)[0], error(23))
                else:
                    self.assertEqual(reference, error(23))
                self.assertTrue(observed)
                self.assertEqual(hit[0], occurrence)
                self.assertEqual(vm.field('state', 2), 0)
                self.assertEqual(vm.globals['readyCount'], 0)
                self.assertEqual(vm.free_pages(), free)
                self.assertEqual(vm.control_count(), control_count)
                self.assertEqual(vm.object_value(endpoint, 'references'), 1)
                vm.call = original
                self.assertEqual(create(vm), 4098)

    def test_dma_busy_pins_every_owned_page_and_identity_under_slot_pressure(self):
        vm, tokens = boot(irq=True, dma=True)
        vm.run(2)
        vm.invoke(25, vm.irq)
        self.assertGreater(vm.call('deviceSubmit', 2, 0, 16, 1, 0), 0)
        vm.invoke(24, vm.irq, 5)
        vm.run(1)
        root = vm.field('directory', 2)
        virtuals = (0x40000000, USER_DATA, vm.field('userStackBottom', 2), C['START_BLOCK_VA'])
        leaves = tuple(vm.leaf(va, root) for va in virtuals)
        bounce = vm.globals['deviceBounce']
        # The free-page set detects directory, page-table and guard/run pages
        # as well as the obvious task data and DMA buffer pages.
        owned = set(vm.free_pages())
        resources = (root, vm.field('kernelStackBottom', 2), vm.field('kernelStackTop', 2),
                     vm.field('bootPage', 2), tuple(vm.pages(2)), bounce)
        vm.invoke(40, 2, 89)
        vm.invoke(41, 2, USER_DATA)
        self.assertEqual(vm.event()[4], 10)
        children = [create(vm) for _ in range(4)]
        self.assertEqual([ref & C['TASK_SLOT_MASK'] for ref in children], [5, 6, 7, 8])
        # Take every other slot this boot has (dead and reclaimed): none is left.
        for slot in range(9, vm.globals['taskCapacity'] + 1):
            vm.memory[vm.field_address('state', slot)] = 3
            vm.memory[vm.field_address('reaped', slot)] = 1
        vm.invoke(36, 1)
        self.assertEqual(vm.result(1)[0], error(23))
        for _ in range(4):
            vm.reap()
            vm.call('irqNotify', 3)
            vm.call('irqTimerTick')
            self.assertTrue(vm.memory.disk_state & C['DISK_BUSY'])
            self.assertEqual(vm.field('state', 2), 3)
            self.assertEqual(vm.field('id', 2), 2)
            self.assertEqual((vm.field('directory', 2), vm.field('kernelStackBottom', 2),
                              vm.field('kernelStackTop', 2), vm.field('bootPage', 2),
                              tuple(vm.pages(2)), vm.globals['deviceBounce']), resources)
            self.assertEqual(tuple(vm.leaf(va, root) for va in virtuals), leaves)
            self.assertTrue(set(vm.free_pages()) <= owned)
            self.assertFalse(vm.call('physicalPageAvailable', bounce))
            self.assertFalse(vm.wakes[2])
        vm.memory.complete()
        vm.reap()
        replacement = create(vm)
        self.assertEqual(replacement, 4098)
        self.assertEqual(vm.call('irqLookup', replacement, vm.irq), C['PIC_LINE_COUNT'])
        self.assertEqual(vm.call('taskGet', 2), 0)


if __name__ == '__main__':
    unittest.main()
