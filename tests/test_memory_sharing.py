"""Explicit grant lifetimes and both teardown orders through checked syscalls."""
import unittest

from test_runtime_tasks import fixture, create
from test_runtime_memory import invoke, space, allocate, frames, used, VA, PAGE, RW, RO, RX
from source_m import LAYOUT as C


def grant(vm, cap, region, borrower=2, permissions=RW):
    token = invoke(vm, 'SYS_MEM_GRANT', cap, region, borrower, permissions)
    assert token > 0
    return token


def row(vm, token):
    typ = vm.decls['memoryGrants'].sym.type.elem
    base = vm.addresses['memoryGrants'] + ((token & 255) - 1) * typ.size
    return {field.name: vm.memory[base + field.offset] for field in typ.fields}


class MemorySharingTests(unittest.TestCase):
    def setup_shared(self, permissions=RW):
        vm = fixture(3)
        cap = space(vm, rights=47) # ordinary rights plus SHARE, no POPULATE
        region = allocate(vm, cap)
        pages = frames(vm, region)
        self.assertEqual(invoke(vm, 'SYS_MEM_MAP', cap, region, VA, 0, 2, RW), 0)
        vm.seed(pages[0], b'live')
        token = grant(vm, cap, region, permissions=permissions)
        vm.run(2)
        borrowed = space(vm)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_MAP', borrowed, token, VA, permissions), 0)
        return vm, cap, region, pages, token, borrowed

    def test_owner_first_survives_owner_slot_reuse_then_returns_exact_frames(self):
        vm, cap, region, pages, token, borrowed = self.setup_shared()
        self.assertEqual([vm.call('physicalPageReferences', p) for p in pages], [3, 3])
        vm.run(1)
        vm.memory[vm.field_address('reusable', 1)] = 1
        vm.invoke(C['SYS_EXIT'], 0)
        vm.reap()
        self.assertEqual(vm.field('state', 1), 0)
        self.assertEqual(vm.field('directory', 1), 0)
        self.assertEqual(used(vm, 1), 0)
        self.assertEqual(vm.globals['memoryOrphanPages'], 2)
        self.assertEqual([vm.call('physicalPageReferences', p) for p in pages], [2, 2])
        for p in pages:
            self.assertFalse(vm.call('physicalPageAvailable', p))
            self.assertTrue(vm.call('physicalPageOwned', p, 1, 5))
        self.assertFalse(row(vm, token)['open'])
        vm.run(2)
        self.assertEqual(vm.call('mmuUserLeaf', vm.field('directory', 2), 2, VA) & ~4095, pages[0])
        vm.memory[0x81000] = 0
        self.assertEqual(vm.call('copyFromUser', vm.field('directory', 2), 2, 0x81000, VA, 4), 0)
        self.assertEqual(vm.read_bytes(0x81000, 4), b'live')
        replacement = create(vm)
        self.assertEqual(replacement, 4097)
        charged = used(vm, replacement)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_MAP', borrowed, token, VA + 8 * PAGE, RW), -1)
        # The borrower dies with its shared mappings intact.
        vm.invoke(C['SYS_EXIT'], 0)
        vm.reap()
        self.assertEqual(vm.globals['memoryOrphanPages'], 0)
        self.assertEqual(used(vm, replacement), charged)
        for p in pages:
            self.assertTrue(vm.call('physicalPageAvailable', p))
        self.assertEqual(row(vm, token)['lender'], 0)
        vm.run(3)
        observer = space(vm)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_MAP', observer, token, VA, RW), -1)
        vm.globals['nextFreePage'] = pages[0] // PAGE
        new = allocate(vm, observer)
        self.assertEqual(frames(vm, new), pages)
        for p in pages:
            self.assertEqual(vm.read_bytes(p, PAGE), bytes(PAGE))

    def test_borrower_first_keeps_allocation_owned_until_owner_death(self):
        vm, cap, region, pages, token, borrowed = self.setup_shared()
        vm.invoke(C['SYS_EXIT'], 0)
        vm.reap()
        self.assertEqual(row(vm, token)['lender'], 0)
        self.assertEqual(vm.globals['memoryOrphanPages'], 0)
        self.assertEqual([vm.call('physicalPageReferences', p) for p in pages], [1, 1])
        vm.run(1)
        for p in pages:
            self.assertFalse(vm.call('physicalPageAvailable', p))
        self.assertEqual(vm.read_bytes(pages[0], 4), b'live')
        self.assertEqual(invoke(vm, 'SYS_MEM_RELEASE', cap, region), -16)
        vm.invoke(C['SYS_EXIT'], 0)
        vm.reap()
        self.assertEqual(used(vm, 1), 0)
        for p in pages:
            self.assertTrue(vm.call('physicalPageAvailable', p))

    def test_authority_permission_ceiling_revoke_and_partial_unmap(self):
        vm, cap, region, pages, token, borrowed = self.setup_shared(RO)
        self.assertEqual(invoke(vm, 'SYS_MEM_PROTECT', borrowed, VA, 2, RW), -1)
        self.assertEqual(invoke(vm, 'SYS_MEM_PROTECT', borrowed, VA, 2, RX), -1)
        self.assertEqual(invoke(vm, 'SYS_MEM_RELEASE', borrowed, region), -1)
        self.assertEqual(invoke(vm, 'SYS_MEM_CLOSE', cap), -1) # foreign space
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_CLOSE', token), -16)
        vm.run(3)
        other = space(vm)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_MAP', other, token, VA, RO), -1)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_CLOSE', token), -1)
        vm.run(1)
        self.assertEqual(invoke(vm, 'SYS_MEM_RELEASE', cap, region), -16)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_CLOSE', token), 0)
        vm.run(2)
        self.assertEqual(invoke(vm, 'SYS_MEM_UNMAP', borrowed, VA, 1), 0)
        self.assertEqual(row(vm, token)['mapped'], 2)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_MAP', borrowed, token, VA + 8 * PAGE, RO), -1)
        self.assertEqual(invoke(vm, 'SYS_MEM_UNMAP', borrowed, VA + PAGE, 1), 0)
        self.assertEqual(row(vm, token)['lender'], 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_CLOSE', token), -1)
        vm.run(1)
        self.assertEqual(invoke(vm, 'SYS_MEM_UNMAP', cap, VA, 2), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_RELEASE', cap, region), 0)

    def test_cross_task_wx_and_failed_table_staging_leave_lease_intact(self):
        vm = fixture(3)
        cap = space(vm, rights=47)
        region = allocate(vm, cap)
        pages = frames(vm, region)
        self.assertEqual(invoke(vm, 'SYS_MEM_MAP', cap, region, VA, 0, 2, RX), 0)
        token = grant(vm, cap, region, permissions=RW)
        vm.run(2)
        borrowed = space(vm)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_MAP', borrowed, token, VA, RW), -16)
        vm.run(1)
        self.assertEqual(invoke(vm, 'SYS_MEM_PROTECT', cap, VA, 2, RO), 0)
        vm.run(2)
        baseline = vm.free_pages(), used(vm, 2)
        for failed in (1, 2):
            vm.fail_allocation = vm.allocation_count + failed
            self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_MAP', borrowed, token, VA + 0x3FF000, RW), -12)
            self.assertEqual((vm.free_pages(), used(vm, 2)), baseline)
            self.assertEqual(row(vm, token)['mapped'], 0)
            self.assertEqual([vm.call('physicalPageReferences', p) for p in pages], [2, 2])
        vm.fail_allocation = None
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_MAP', borrowed, token, VA, RW), 0)
        vm.run(1)
        self.assertEqual(invoke(vm, 'SYS_MEM_PROTECT', cap, VA, 2, RX), -16)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_CLOSE', token), 0)
        vm.run(2)
        self.assertEqual(invoke(vm, 'SYS_MEM_PROTECT', borrowed, VA, 2, RO), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_PROTECT', borrowed, VA, 2, RW), -1)

    def test_unmapped_grants_close_at_death_and_multiple_borrowers_delay_orphan_release(self):
        vm, cap, region, pages, token, borrowed = self.setup_shared()
        vm.run(1)
        second = grant(vm, cap, region, borrower=3, permissions=RO)
        vm.run(3)
        observer = space(vm)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_MAP', observer, second, VA, RO), 0)
        vm.run(1)
        vm.invoke(C['SYS_EXIT'], 0)
        vm.reap()
        self.assertEqual(vm.globals['memoryOrphanPages'], 2)
        vm.run(2)
        vm.invoke(C['SYS_EXIT'], 0)
        vm.reap()
        self.assertEqual(vm.globals['memoryOrphanPages'], 2)
        for p in pages:
            self.assertFalse(vm.call('physicalPageAvailable', p))
        vm.run(3)
        self.assertEqual(invoke(vm, 'SYS_MEM_UNMAP', observer, VA, 2), 0)
        self.assertEqual(vm.globals['memoryOrphanPages'], 0)
        for p in pages:
            self.assertTrue(vm.call('physicalPageAvailable', p))

    def test_grant_limits_stale_generations_and_denied_partial_lease_acquisition(self):
        vm = fixture(3)
        cap = space(vm, rights=47)
        region = allocate(vm, cap)
        pages = frames(vm, region)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT', cap, region, 1, RW), -1)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT', cap, region, 258, RW), -1)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT', cap, region, 2, RW | C['PTE_X']), -22)
        refs = vm.globals['pageReferences']
        vm.memory[refs + pages[1] // PAGE * 4] = 0xFFFFFFFF
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT', cap, region, 2, RW), -16)
        self.assertEqual(vm.call('physicalPageReferences', pages[0]), 0)
        vm.memory[refs + pages[1] // PAGE * 4] = 0
        tokens = [grant(vm, cap, region) for _ in range(8)]
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT', cap, region, 2, RW), -23)
        self.assertEqual([vm.call('physicalPageReferences', p) for p in pages], [8, 8])
        for token in tokens:
            self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_CLOSE', token), 0)
        newer = grant(vm, cap, region)
        self.assertNotEqual(newer, tokens[0])
        vm.run(2)
        borrowed = space(vm)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_MAP', borrowed, tokens[0], VA, RW), -1)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_CLOSE', tokens[0]), -1)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_CLOSE', newer), 0)
        vm.run(1)
        typ = vm.decls['memoryGrants'].sym.type.elem
        for i in range(64):
            vm.memory[vm.addresses['memoryGrants'] + i * typ.size + typ.field('generation').offset] = 0x7FFFFF
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT', cap, region, 2, RW), -23)
        self.assertEqual([vm.call('physicalPageReferences', p) for p in pages], [0, 0])

    def test_unmapped_grant_drop_and_orphan_dma_pin_require_final_reference(self):
        vm = fixture(3)
        cap = space(vm, rights=47)
        region = allocate(vm, cap, 1)
        pages = frames(vm, region)
        token = grant(vm, cap, region)
        self.assertEqual(invoke(vm, 'SYS_MEM_RELEASE', cap, region), -16)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_CLOSE', token), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_RELEASE', cap, region), 0)
        vm, cap, region, pages, token, borrowed = self.setup_shared()
        for p in pages:
            self.assertTrue(vm.call('retainPage', p, 1, 5))
        vm.run(1)
        vm.invoke(C['SYS_EXIT'], 0)
        vm.reap()
        vm.run(2)
        self.assertEqual(invoke(vm, 'SYS_MEM_UNMAP', borrowed, VA, 2), 0)
        self.assertEqual(vm.globals['memoryOrphanPages'], 2)
        for p in pages:
            self.assertFalse(vm.call('physicalPageAvailable', p))
            self.assertTrue(vm.call('releasePage', p, 1, 5))
        vm.reap()
        self.assertEqual(vm.globals['memoryOrphanPages'], 0)
        for p in pages:
            self.assertTrue(vm.call('physicalPageAvailable', p))


if __name__ == '__main__':
    unittest.main()
