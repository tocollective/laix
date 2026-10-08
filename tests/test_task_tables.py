"""The per-slot tables are carved from RAM at boot; the task count has no compile-time limit."""
import unittest

from source_m import LAYOUT as C, SourceM
from test_kernel import check_m, LAIX
from test_task import TaskM

OWNER = 0xFFFFFFFB
FRAMES_PER_TASK = 9


class TaskTableTests(unittest.TestCase):
    def test_sources_check(self):
        check_m(LAIX / 'src/task/tables.m')

    def test_capacity_follows_the_installed_ram(self):
        seen = []
        for ram in (0x110000, 0x200000, 0x400000):
            vm = TaskM(ram=ram)
            usable = vm.globals['kernelRamEnd'] - vm.globals['kernelReservedEnd']
            capacity = vm.globals['taskCapacity']
            # Slots are budgeted nine frames each, at least eight in any case.
            self.assertEqual(capacity, max(8, min(C['TASK_SLOTS'], usable // (FRAMES_PER_TASK * 4096))))
            seen.append(capacity)
        self.assertEqual(seen, sorted(seen))
        self.assertGreater(seen[-1], 2 * seen[0])

    def test_the_ceiling_is_the_reference_layout_not_a_constant_array(self):
        vm = TaskM()
        for ram in (0x4000000, 0x8000000):
            vm.globals['kernelRamEnd'] = ram
            slots = vm.call('memorySlots')
            self.assertEqual(slots, min(C['TASK_SLOTS'], (ram - vm.globals['kernelReservedEnd']) // (FRAMES_PER_TASK * 4096)))
        # The machine's largest RAM (128 MiB) is below the ceiling: memory, not the layout, is the limit.
        self.assertLess(slots, C['TASK_SLOTS'])
        vm.globals['kernelRamEnd'] = 0xF0000000
        self.assertEqual(vm.call('memorySlots'), C['TASK_SLOTS'])
        # 4095 slots is what 12 slot bits and a positive 31-bit reference allow.
        self.assertEqual(C['TASK_SLOT_MASK'], (1 << C['TASK_SLOT_BITS']) - 1)
        self.assertEqual(C['TASK_SLOTS'], C['TASK_SLOT_MASK'])
        self.assertEqual(C['TASK_GENERATION_MAX'], 0x7FFFFFFF >> C['TASK_SLOT_BITS'])

    def test_tables_are_one_zeroed_run_owned_by_the_kernel_and_bound_once(self):
        vm = TaskM(ram=0x200000)
        capacity = vm.globals['taskCapacity']
        base = vm.table_base('tasks')
        bytes_ = vm.call('tablesBytes', capacity)
        for page in range(base, base + bytes_, 4096):
            self.assertTrue(vm.call('physicalPageOwned', page, OWNER, C['PAGE_KERNEL'] if 'PAGE_KERNEL' in C else 2))
        self.assertEqual(base % 4096, 0)
        for name, count in (('transfers', 'transferCount'), ('taskControls', 'taskControlCount'),
                            ('memoryBudgets', 'memoryBudgetCount')):
            self.assertEqual(vm.globals[count], capacity)
            self.assertGreater(vm.table_base(name), base)
            self.assertLess(vm.table_base(name), base + bytes_)
        self.assertEqual(vm.globals['endpointWaitCapacity'], capacity)
        self.assertFalse(vm.call('tablesInit'))  # once only
        self.assertEqual(vm.globals['taskCapacity'], capacity)

    def test_the_mmu_ledger_has_a_row_per_slot_and_a_checked_row_size(self):
        vm = TaskM(ram=0x200000)
        typ = vm.decls['spaceBudgets'].sym.type.target
        self.assertEqual(typ.size, vm.call('memorySlots') and 16)  # SPACE_BUDGET_BYTES
        self.assertEqual(vm.globals['spaceBudgetCount'], vm.call('memorySlots'))
        self.assertGreaterEqual(vm.globals['spaceBudgetCount'], vm.globals['taskCapacity'])

    def test_without_memory_nothing_is_bound(self):
        vm = SourceM(LAIX / 'src/task/tables.m')
        self.assertFalse(vm.call('tablesInit'))
        self.assertEqual(vm.globals['taskCapacity'], 0)

    def test_binding_rejects_empty_or_repeated_tables(self):
        vm = TaskM()
        for name, args in (('taskTableBind', (0x1000, 0x2000, 8)), ('transferTableBind', (0x1000, 8)),
                           ('taskControlTableBind', (0x1000, 8)), ('memoryBudgetBind', (0x1000, 8)),
                           ('endpointQueuesBind', (0x1000, 8))):
            self.assertFalse(vm.call(name, *args), name)

    def test_a_task_beyond_the_old_limit_has_a_full_reference(self):
        vm = TaskM(ram=0x1000000)
        capacity = vm.globals['taskCapacity']
        self.assertGreater(capacity, 300)
        for slot in range(1, 300):  # take the slots below (dead and reclaimed)
            vm.memory[vm.field_address('state', slot)] = 3
            vm.memory[vm.field_address('reaped', slot)] = 1
        reference = vm.call('taskCreate')
        self.assertEqual(reference, 300)
        self.assertEqual(vm.field('slot', 300), 300)
        self.assertEqual(vm.globals['taskHighWater'], 300)
        self.assertEqual(vm.call('taskGet', reference), vm.field_address('id', 300) - vm.task_type.field('id').offset)
        # The ASID is the slot modulo 256, in the PTBR field the CPU defines.
        self.assertEqual(vm.field('asid', 300), 300 & 255)
        self.assertEqual((vm.field('ptbr', 300) >> 4) & 255, 300 & 255)
        # A reference carries the generation above the slot bits.
        self.assertEqual(reference >> C['TASK_SLOT_BITS'], 0)
        self.assertEqual(reference & C['TASK_SLOT_MASK'], 300)
        self.assertEqual(vm.call('taskGet', reference ^ 0x2000), 0)  # wrong generation


if __name__ == '__main__':
    unittest.main()
