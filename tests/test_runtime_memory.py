"""Run checked memory authority and heap sources, without generating code."""
import unittest

from test_runtime_tasks import fixture, create, LifecycleM
from test_ipc_handles import error
from source_m import LAYOUT as C, SourceM
from test_kernel import check_m, LAIX
from mlang import syntax

PAGE = C['PAGE_SIZE']
VA = C['MEM_VA_START']
RW, RO, RX = (C[p] | C['PTE_U'] for p in ('PTE_RW', 'PTE_RO', 'PTE_RX'))


def invoke(vm, name, *args):
    vm.invoke(C[name], *args)
    value = vm.result(vm.current())[0]
    return value if value < 0x80000000 else value - 0x100000000


def space(vm, target=0, rights=15):
    value = invoke(vm, 'SYS_MEM_SPACE', target, rights)
    assert value > 0
    return value


def allocate(vm, cap, pages=2):
    value = invoke(vm, 'SYS_MEM_ALLOC', cap, pages)
    assert value > 0
    return value


def frames(vm, token):
    typ = vm.decls['memoryRegions'].sym.type.elem
    address = vm.addresses['memoryRegions'] + ((token & 255) - 1) * typ.size
    count = vm.memory[address + typ.field('count').offset]
    return [vm.memory[address + typ.field('pages').offset + 4 * i] for i in range(count)]


def used(vm, owner=1):
    typ = vm.decls['memoryBudgets'].sym.type.elem
    for i in range(8):
        address = vm.addresses['memoryBudgets'] + i * typ.size
        if vm.memory[address + typ.field('owner').offset] == owner:
            return vm.memory[address + typ.field('used').offset]
    return 0


class RuntimeMemoryTests(unittest.TestCase):
    def test_alignment_bounds_attenuation_foreign_and_stale_tokens(self):
        vm = fixture()
        cap, region = space(vm), None
        region = allocate(vm, cap)
        before = vm.free_pages(), used(vm)
        for address, count in ((0, 1), (VA + 1, 1), (VA, 0), (VA, 17),
                               (0xFFFFF000, 2), (C['MEM_VA_END'] - PAGE, 2),
                               (C['USER_STACK_GUARD'] if 'USER_STACK_GUARD' in C else 0xBFFFE000, 1)):
            self.assertEqual(invoke(vm, 'SYS_MEM_MAP', cap, region, address, 0, count, RW), -22)
        for offset, count in ((2, 1), (0xFFFFFFFF, 1), (1, 2)):
            self.assertEqual(invoke(vm, 'SYS_MEM_MAP', cap, region, VA, offset, count, RW), -22)
        self.assertEqual(invoke(vm, 'SYS_MEM_SPACE', 2, 15), -1)
        readonly = space(vm, rights=C['MEM_RIGHT_MAP'])
        self.assertEqual(invoke(vm, 'SYS_MEM_ALLOC', readonly, 1), -1)
        vm.run(2)
        self.assertEqual(invoke(vm, 'SYS_MEM_MAP', cap, region, VA, 0, 2, RW), -1)
        other = space(vm)
        self.assertEqual(invoke(vm, 'SYS_MEM_MAP', other, region, VA, 0, 2, RW), -1)
        vm.run(1)
        self.assertEqual(invoke(vm, 'SYS_MEM_CLOSE', cap), 0)
        replacement = space(vm)
        self.assertNotEqual(cap, replacement)
        self.assertEqual(invoke(vm, 'SYS_MEM_RELEASE', cap, region), -1)
        self.assertEqual((vm.free_pages(), used(vm)), before)

    def test_capability_and_region_limits_leave_peer_capacity(self):
        vm = fixture()
        caps = [space(vm) for _ in range(4)]
        self.assertEqual(invoke(vm, 'SYS_MEM_SPACE', 0, 15), -23)
        regions = [allocate(vm, caps[0], 1) for _ in range(8)]
        self.assertEqual(invoke(vm, 'SYS_MEM_ALLOC', caps[0], 1), -23)
        vm.run(2)
        peer = space(vm)
        self.assertGreater(invoke(vm, 'SYS_MEM_ALLOC', peer, 1), 0)
        vm.run(1)
        for region in regions:
            self.assertEqual(invoke(vm, 'SYS_MEM_RELEASE', caps[0], region), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_CLOSE', caps[0]), 0)
        self.assertGreater(space(vm), caps[0])

    def test_retired_rows_never_wrap_and_constructor_directory_failure_refunds(self):
        vm = fixture(1)
        for variable, count in (('memorySpaces', 32), ('memoryRegions', 64)):
            typ = vm.decls[variable].sym.type.elem
            for i in range(count):
                vm.memory[vm.addresses[variable] + i * typ.size + typ.field('generation').offset] = 0x7FFFFF
        self.assertEqual(invoke(vm, 'SYS_MEM_SPACE', 0, 15), -23)
        # Restore one previously unissued row to exercise region retirement.
        typ = vm.decls['memorySpaces'].sym.type.elem
        vm.memory[vm.addresses['memorySpaces'] + typ.field('generation').offset] = 0
        cap = space(vm)
        self.assertEqual(invoke(vm, 'SYS_MEM_ALLOC', cap, 1), -23)
        baseline = vm.free_pages(), used(vm)
        vm.fail_allocation = vm.allocation_count + 1
        self.assertEqual(invoke(vm, 'SYS_TASK_CREATE', 1), -23)
        self.assertEqual((vm.free_pages(), used(vm)), baseline)
        self.assertEqual(used(vm, 2), 0)

    def test_map_table_failure_occupied_and_edit_holes_are_transactional(self):
        vm = fixture(1)
        cap, address = space(vm), VA + 0x3FF000
        region = allocate(vm, cap)
        pages = frames(vm, region)
        baseline = vm.free_pages(), used(vm)
        # Two absent tables, fail each allocation in turn.
        for failed in (1, 2):
            vm.fail_allocation = vm.allocation_count + failed
            self.assertEqual(invoke(vm, 'SYS_MEM_MAP', cap, region, address, 0, 2, RW), -12)
            self.assertEqual((vm.free_pages(), used(vm)), baseline)
            self.assertEqual([vm.call('physicalPageReferences', p) for p in pages], [0, 0])
            self.assertEqual(vm.call('mmuUserLeaf', vm.field('directory'), 1, address), 0)
        vm.fail_allocation = None
        self.assertEqual(invoke(vm, 'SYS_MEM_MAP', cap, region, address + PAGE, 1, 1, RW), 0)
        before = vm.free_pages(), used(vm)
        self.assertEqual(invoke(vm, 'SYS_MEM_MAP', cap, region, address, 0, 2, RW), -16)
        for call, args in (('SYS_MEM_UNMAP', (cap, address, 2)),
                           ('SYS_MEM_PROTECT', (cap, address, 2, RX))):
            self.assertEqual(invoke(vm, call, *args), -1)
        self.assertEqual((vm.free_pages(), used(vm)), before)
        self.assertEqual(vm.call('mmuUserLeaf', vm.field('directory'), 1, address + PAGE) & 31, RW)
        self.assertEqual(invoke(vm, 'SYS_MEM_UNMAP', cap, address + PAGE, 1), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_RELEASE', cap, region), 0)

    def test_zero_reuse_mapping_pins_dma_pins_and_alias_wx(self):
        vm = fixture(1)
        cap = space(vm)
        region = allocate(vm, cap)
        pages = frames(vm, region)
        self.assertEqual(invoke(vm, 'SYS_MEM_MAP', cap, region, VA, 0, 2, RW), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_RELEASE', cap, region), -16)
        for p in pages:
            self.assertTrue(vm.call('retainPage', p, 1, C.get('PAGE_USER', 5)))
            vm.seed(p, b'old!')
        self.assertEqual(invoke(vm, 'SYS_MEM_PROTECT', cap, VA, 2, RW | C['PTE_X']), -22)
        self.assertEqual(invoke(vm, 'SYS_MEM_PROTECT', cap, VA, 2, RX), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_MAP', cap, region, VA + 8 * PAGE, 0, 2, RW), -16)
        self.assertEqual(invoke(vm, 'SYS_MEM_MAP', cap, region, VA + 8 * PAGE, 0, 2, RX), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_PROTECT', cap, VA, 2, RW), -16)
        self.assertEqual(invoke(vm, 'SYS_MEM_UNMAP', cap, VA, 2), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_UNMAP', cap, VA + 8 * PAGE, 2), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_RELEASE', cap, region), -16)
        for p in pages:
            self.assertTrue(vm.call('releasePage', p, 1, 5))
        self.assertEqual(invoke(vm, 'SYS_MEM_RELEASE', cap, region), 0)
        replacement = allocate(vm, cap)
        self.assertNotEqual(region, replacement)
        self.assertEqual(frames(vm, replacement), pages)
        for p in pages:
            self.assertEqual(vm.read_bytes(p, PAGE), bytes(PAGE))
        self.assertEqual(invoke(vm, 'SYS_MEM_RELEASE', cap, region), -1)

    def test_budgets_reserve_progress_and_partial_allocation_rollback(self):
        # Enough RAM to isolate the 96-page quota from physical exhaustion.
        quota = SourceM(LAIX / 'src/mm/mmu.m')
        self.assertTrue(quota.call('memoryInit', 0x200000))
        self.assertTrue(quota.call('mmuInit'))
        self.assertTrue(quota.call('memoryBudgetOpen', 1))
        directory = quota.call('mmuCreateAddressSpace', 1)
        self.assertGreater(directory, 0) # the directory is the first charge
        for _ in range(95):
            self.assertGreater(quota.call('allocPage', 1, 5), 0)
        self.assertGreater(quota.globals['memoryFreePages'], 16)
        self.assertEqual(quota.call('allocPage', 1, 5), 0)
        self.assertTrue(quota.call('memoryBudgetOpen', 2))
        self.assertGreater(quota.call('allocPage', 2, 5), 0)
        vm = fixture()
        cap = space(vm)
        before = vm.free_pages(), used(vm)
        for failed in (1, 2, 3):
            vm.fail_allocation = vm.allocation_count + failed
            self.assertEqual(invoke(vm, 'SYS_MEM_ALLOC', cap, 3), -12)
            self.assertEqual((vm.free_pages(), used(vm)), before)
        vm.fail_allocation = None
        regions = []
        while True:
            result = invoke(vm, 'SYS_MEM_ALLOC', cap, 16)
            if result < 0:
                self.assertEqual(result, -12)
                break
            regions.append(result)
        self.assertLessEqual(used(vm), 96)
        self.assertGreaterEqual(len(vm.free_pages()), 16)
        vm.run(2)
        peer = space(vm)
        self.assertGreater(invoke(vm, 'SYS_MEM_ALLOC', peer, 1), 0)
        vm.run(1)
        for region in regions:
            self.assertEqual(invoke(vm, 'SYS_MEM_RELEASE', cap, region), 0)
        # Drain unregistered trusted owner to the reserved boundary.
        while vm.globals['memoryFreePages'] > 16:
            self.assertGreater(vm.call('allocPage', 0xFFFFFFFE, 2), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_ALLOC', cap, 1), -12)
        self.assertGreater(vm.call('allocPage', 0xFFFFFFFE, 2), 0)

    def test_loader_bounded_copy_publish_revocation_and_teardown(self):
        vm = fixture()
        ref = create(vm, configure=False, publish=False)
        cap = space(vm, ref, 31)
        region = allocate(vm, cap)
        pages = frames(vm, region)
        source = vm.pages(1)[1]
        vm.seed(source, b'loader')
        self.assertEqual(invoke(vm, 'SYS_MEM_POPULATE', cap, region, PAGE - 2, 0x40001000, 6), 0)
        self.assertEqual(vm.read_bytes(pages[0] + PAGE - 2, 2), b'lo')
        self.assertEqual(vm.read_bytes(pages[1], 4), b'ader')
        snapshot = [vm.read_bytes(p, PAGE) for p in pages]
        self.assertEqual(invoke(vm, 'SYS_MEM_POPULATE', cap, region, 2 * PAGE - 2, 0x40001000, 6), -22)
        self.assertEqual(invoke(vm, 'SYS_MEM_POPULATE', cap, region, 0, 0x40002FFE, 6), -14)
        self.assertEqual([vm.read_bytes(p, PAGE) for p in pages], snapshot)
        self.assertEqual(invoke(vm, 'SYS_MEM_MAP', cap, region, VA, 0, 2, RW), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_PROTECT', cap, VA, 2, RX), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_POPULATE', cap, region, 0, 0x40001000, 6), -16)
        for p in pages:
            table = vm.memory[vm.kernel_root + (p // 0x400000) * 4] & ~4095
            self.assertEqual(vm.memory[table + (p // PAGE % 1024) * 4] & 31, 3)
        self.assertEqual(invoke(vm, 'SYS_TASK_CONFIGURE', ref, 0, 0, 0), 0)
        self.assertEqual(invoke(vm, 'SYS_TASK_PUBLISH', ref), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_ALLOC', cap, 1), -1)
        vm.run(ref)
        own = space(vm)
        unused = allocate(vm, own, 3)
        remaining = frames(vm, unused)
        vm.invoke(C['SYS_EXIT'], 0)
        vm.reap()
        self.assertEqual(used(vm, ref), 0)
        for p in pages + remaining:
            self.assertTrue(vm.call('physicalPageAvailable', p))
        vm.run(1)
        newer = create(vm, publish=False)
        self.assertNotEqual(newer, ref)
        self.assertEqual(invoke(vm, 'SYS_MEM_ALLOC', cap, 1), -1)


class HeapM(SourceM):
    def __init__(self, kernel):
        super().__init__(LAIX / 'user/heap.m')
        self.kernel = kernel

    def expr(self, node, local):
        if isinstance(node, syntax.BuiltinCall) and node.name == 'syscall':
            args = [self.expr(arg, local) for arg in node.args]
            self.kernel.invoke(*args)
            result = self.kernel.result(self.kernel.current())[0]
            return result
        return super().expr(node, local)


class HeapTests(unittest.TestCase):
    def test_checked_user_heap_grows_shrinks_and_rolls_back_map_failure(self):
        vm = fixture(1)
        heap = HeapM(vm)
        baseline = vm.free_pages(), used(vm)
        first = heap.call('heapAllocate', 10)
        second = heap.call('heapAllocate', PAGE + 1)
        self.assertEqual(first, VA)
        self.assertEqual(second, VA + 16 * PAGE)
        self.assertEqual(heap.call('heapRelease', first + 1), error(22))
        self.assertEqual(heap.call('heapRelease', first), 0)
        self.assertEqual(heap.call('heapRelease', second), 0)
        self.assertEqual((vm.free_pages(), used(vm)), baseline)
        vm.fail_allocation = vm.allocation_count + 2 # frame succeeds, table fails
        self.assertEqual(heap.call('heapAllocate', 1), 0)
        self.assertEqual((vm.free_pages(), used(vm)), baseline)
        self.assertEqual(heap.globals['heapError'], error(12))
        for count in (0, 16 * PAGE + 1, 0xFFFFFFFF):
            self.assertEqual(heap.call('heapAllocate', count), 0)


if __name__ == '__main__':
    unittest.main()
