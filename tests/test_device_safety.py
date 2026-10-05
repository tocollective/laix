"""IRQ boundary and DMA ownership acceptance against checked M source."""
import unittest

from source_m import LAYOUT as C
from test_ipc_handles import error
from test_screen_services import Devices, kernel_fixture
from test_task import TaskEntered, USER_DATA


def start(vm):
    for id in (1, 2, 3):
        vm.call('taskEnqueue', vm.call('taskGet', id))
    with unittest.TestCase().assertRaises(TaskEntered):
        vm.call('taskStart', 1000000)


def resume_owner(vm):
    for id in (2, 3):
        vm.call('taskYield', vm.field_address('context', id))


class DeviceSafetyTests(unittest.TestCase):
    def test_duplicate_level_after_wake_cannot_create_second_notification(self):
        vm = kernel_fixture(start=True)
        frame = vm.field_address('context')
        self.assertEqual(vm.call('irqComplete', 1, vm.screen_irq), 0)
        vm.call('irqWait', frame, vm.screen_irq, 5)
        vm.memory.lines |= 1 << 5
        vm.call('irqNotify', 5)
        for _ in range(8):
            vm.call('irqNotify', 5)
        resume_owner(vm)
        vm.call('irqWait', frame, vm.screen_irq, 5)
        self.assertEqual(vm.memory[frame + C['TF_R1']], error(16))
        self.assertEqual(vm.call('irqComplete', 1, vm.screen_irq), error(16))
        vm.memory.lines &= ~(1 << 5)
        self.assertEqual(vm.call('irqComplete', 1, vm.screen_irq), 0)

    def test_other_owned_line_cannot_satisfy_selected_irq_wait(self):
        vm = kernel_fixture()
        other = vm.call('irqGrant', 1, C['KEYBOARD_IRQ'])
        self.assertNotEqual(other, 0)
        start(vm)
        frame = vm.field_address('context')
        for token in (vm.screen_irq, other):
            self.assertEqual(vm.call('irqComplete', 1, token), 0)
        vm.call('irqWait', frame, vm.screen_irq, 5)
        vm.memory.lines |= 1
        vm.call('irqNotify', 0)
        self.assertEqual((vm.field('state'), vm.field('waitReason')), (4, 7))
        vm.memory.count = 5000000
        vm.call('irqTimerTick')
        self.assertEqual(vm.memory[frame + C['TF_R1']], error(110))
        resume_owner(vm)
        vm.call('irqWait', frame, other, 5)
        self.assertEqual(vm.memory[frame + C['TF_R1']], 0)
        vm.call('irqWait', frame, other, 5)
        self.assertEqual(vm.memory[frame + C['TF_R1']], error(16))

    def test_level_arriving_at_block_boundary_wakes_exactly_once(self):
        vm = kernel_fixture(start=True)
        frame = vm.field_address('context')
        self.assertEqual(vm.call('irqComplete', 1, vm.screen_irq), 0)
        original = vm.call

        def inject(name, *args):
            if name == 'taskBlock':
                self.assertEqual(vm.controls[0] & C['STATUS_IE'], 0)
                vm.memory.lines |= 1 << 5
            return original(name, *args)

        vm.call = inject
        vm.call('irqWait', frame, vm.screen_irq, 5)
        self.assertEqual(vm.field('state'), 4)
        self.assertEqual(vm.memory[C['PIC_CLAIM']], 5)
        vm.call('timerInterrupt')
        self.assertEqual(vm.field('state'), 1)
        self.assertEqual(vm.memory[frame + C['TF_R1']], 0)
        self.assertEqual(vm.memory[C['PIC_CLAIM']], C['PIC_NO_IRQ'])
        self.assertEqual(vm.globals['readyCount'], 2)

    def test_level_between_pending_check_and_enable_is_retained(self):
        for boundary in ('check', 'enable'):
            with self.subTest(boundary=boundary):
                vm = kernel_fixture(start=True)

                class RearmDevices(Devices):
                    def __getitem__(device, address):
                        value = super().__getitem__(address)
                        if boundary == 'check' and address == C['PIC_PENDING']:
                            device.lines |= 1 << 5
                        return value

                    def __setitem__(device, address, value):
                        super().__setitem__(address, value)
                        if boundary == 'enable' and address == C['PIC_ENABLE'] and value & (1 << 5):
                            device.lines |= 1 << 5

                vm.memory = RearmDevices(vm)
                self.assertEqual(vm.call('irqComplete', 1, vm.screen_irq), 0)
                self.assertEqual(vm.memory[C['PIC_CLAIM']], 5)
                vm.call('timerInterrupt')
                self.assertEqual(vm.memory[C['PIC_CLAIM']], C['PIC_NO_IRQ'])
                frame = vm.field_address('context')
                vm.call('irqWait', frame, vm.screen_irq, 5)
                self.assertEqual(vm.memory[frame + C['TF_R1']], 0)
                for _ in range(8):
                    self.assertEqual(vm.call('irqComplete', 1, vm.screen_irq), error(16))
                    self.assertEqual(vm.memory[C['PIC_CLAIM']], C['PIC_NO_IRQ'])
                self.assertEqual(vm.memory[C['PIC_ENABLE']] & C['TIMER_IRQ_MASK'], C['TIMER_IRQ_MASK'])

    def test_foreign_stale_and_malformed_irq_tokens_change_no_owner_state(self):
        vm = kernel_fixture(start=True)
        frame = vm.field_address('context')
        before = dict(vm.memory)
        for token in (0, 33, vm.screen_irq ^ 256, vm.disk_irq, 0xFFFFFFFF):
            vm.call('irqWait', frame, token, 5)
            self.assertEqual(vm.memory[frame + C['TF_R1']], error(1))
            self.assertEqual(vm.call('irqComplete', 1, token), error(1))
        grant = vm.addresses['irqGrants']
        size = vm.decls['irqGrants'].sym.type.size
        self.assertEqual({a: v for a, v in vm.memory.items() if grant <= a < grant + size},
                         {a: v for a, v in before.items() if grant <= a < grant + size})
        self.assertEqual(vm.memory[C['PIC_ENABLE']], before[C['PIC_ENABLE']])

    def test_dma_rejects_overflow_sector_crossing_and_extent_before_submission(self):
        vm = kernel_fixture()
        free = vm.free_pages()
        for offset, length in ((0, 0), (0, 513), (608, 1), (607, 2),
                               (511, 2), (0xFFFFFFFF, 1), (0xFFFFFFF8, 16), (0, 0xFFFFFFFF)):
            self.assertEqual(vm.call('deviceSubmit', 2, offset, length, 1), error(22))
        self.assertEqual(vm.call('deviceSubmit', 1, 0, 16, 1), error(1))
        self.assertEqual(vm.free_pages(), free)
        self.assertEqual(vm.memory.commands, [])
        self.assertGreater(vm.call('deviceSubmit', 2, 607, 1, 1), 0)
        vm.memory.complete()
        self.assertEqual(vm.call('deviceFinish', 2, vm.operation_instance(), USER_DATA), 1)

    def test_dma_destination_foreign_page_or_partial_range_publishes_nothing(self):
        for destination in (USER_DATA, USER_DATA + 4096 - 8, 0xFFFFFFF8):
            with self.subTest(destination=destination):
                vm = kernel_fixture()
                foreign = vm.pages(3)[1]
                root = vm.field('directory', 2)
                entry = vm.memory[root + 4 * (USER_DATA >> 22)]
                pte = (entry & ~4095) + 4 * ((USER_DATA >> 12) & 1023)
                if destination == USER_DATA:
                    vm.memory[pte] = foreign | 23
                own = vm.pages(2)[1]
                for physical in (own, foreign):
                    for offset in range(4096):
                        vm.memory[physical + offset] = 0xAA
                before = {a: vm.memory[a] for p in (own, foreign) for a in range(p, p + 4096)}
                self.assertGreater(vm.call('deviceSubmit', 2, 0, 16, 1), 0)
                bounce = vm.globals['deviceBounce']
                vm.memory.complete()
                self.assertEqual(vm.call('deviceFinish', 2, vm.operation_instance(), destination), error(14))
                self.assertEqual({a: vm.memory[a] for a in before}, before)
                self.assertTrue(vm.call('physicalPageAvailable', bounce))

    def test_early_finish_and_repeated_reap_keep_busy_dma_pinned(self):
        vm = kernel_fixture()
        self.assertGreater(vm.call('deviceSubmit', 2, 0, 16, 1), 0)
        bounce = vm.globals['deviceBounce']
        self.assertEqual(vm.call('deviceFinish', 1, vm.operation_instance(), USER_DATA), error(1))
        self.assertEqual(vm.call('deviceFinish', 2, vm.operation_instance(), USER_DATA), error(5))
        for _ in range(8):
            vm.call('deviceReap')
            self.assertFalse(vm.call('serviceDevicesQuiescent', 2))
            self.assertFalse(vm.call('freePage', bounce, 0xFFFFFFFE, 2))
            self.assertFalse(vm.call('releasePage', bounce, 2, 2))
            self.assertEqual(vm.call('physicalPageReferences', bounce), 1)
        vm.memory.complete()
        vm.call('deviceReap')
        vm.call('deviceReap')
        self.assertTrue(vm.call('physicalPageAvailable', bounce))
        self.assertEqual(vm.globals['deviceBounce'], 0)
        self.assertEqual(len(vm.memory.commands), 1)


if __name__ == '__main__':
    unittest.main()
