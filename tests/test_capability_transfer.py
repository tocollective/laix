"""A6 consent, isolation and lifetime regressions against checked kernel sources."""
import unittest

from test_runtime_objects import fixture
from test_runtime_tasks import create
from test_runtime_memory import invoke, space, allocate, frames, used, VA, PAGE, RW
from test_ipc_handles import error, GEN_MAX
from source_m import LAYOUT as C


class TransferTests(unittest.TestCase):
    def setup_vm(self, service=False):
        vm = fixture(3, ordinary=True)
        vm.memory[C['TIMER_COUNT_LO']] = 0
        vm.memory[C['TIMER_COUNT_HI']] = 0
        return vm, vm.factory(int(service))

    def reserve(self, vm, receiver=2, sender=1, rights=1, slot=5, seconds=1):
        vm.run(receiver)
        vm.invoke(C['SYS_TRANSFER_RESERVE'], slot, sender, rights, seconds)
        return vm.result(receiver)[0]

    def commit(self, vm, source, ticket, sender=1, receiver=2, rights=1):
        vm.run(sender)
        vm.invoke(C['SYS_TRANSFER_COMMIT'], source, receiver, ticket, rights)
        return vm.result(sender)[0]

    def collect(self, vm, ticket, receiver=2):
        vm.run(receiver)
        vm.invoke(C['SYS_TRANSFER_COLLECT'], ticket)
        frame = vm.field_address('context', receiver)
        return tuple(vm.memory[frame + 4 * i] for i in (1, 2, 3))

    def handle_field(self, vm, receiver, slot, field):
        typ = vm.decls['tasks'].sym.type.target.field('handles').type.field('entries').type.elem
        return vm.field_address('handles', receiver) + (slot - 1) * typ.size + typ.field(field).offset

    def record(self, vm, receiver=2):
        typ = vm.decls['transfers'].sym.type.target
        base = vm.table_base('transfers') + ((receiver & C['TASK_SLOT_MASK']) - 1) * typ.size
        return {f.name: vm.memory[base + f.offset] for f in typ.fields if f.name != 'deadline'}

    def test_sixteen_unsolicited_raw_and_service_copies_leave_foreign_budget_unchanged(self):
        for service in (False, True):
            vm, source = self.setup_vm(service)
            # Reproduce the audit literally: task 1 delegates SEND to task 2,
            # then that legitimate SEND-only holder attacks task 3 sixteen times.
            ticket = self.reserve(vm)
            self.assertEqual(self.commit(vm, source, ticket), 0)
            sender, _, _ = self.collect(vm, ticket)
            before = dict(vm.memory)
            for _ in range(16):
                self.assertEqual(vm.call('ipcCopy', sender, 3, 1), error(1))
            self.assertEqual(vm.memory, before)
            self.assertEqual(self.record(vm, 3)['state'], 0)
            vm.run(3)
            self.assertTrue(0 < vm.factory() < 0x80000000)

    def test_exact_slot_rights_and_authenticated_atomic_notification(self):
        for service, rights in ((False, 1), (False, 2), (False, 3), (True, 1)):
            vm, source = self.setup_vm(service)
            obj = vm.root(source)
            ticket = self.reserve(vm, rights=rights)
            self.assertTrue(0 < ticket < 0x80000000)
            self.assertEqual(vm.object_value(obj, 'references'), 1)
            self.assertEqual(self.collect(vm, ticket), (error(11), 0, 0))
            self.assertEqual(self.commit(vm, source, ticket, rights=rights), 0)
            handle, sender, exact = self.collect(vm, ticket)
            self.assertEqual((handle & 255, sender, exact), (5, 1, rights))
            self.assertEqual(vm.call('handleLookup', vm.field_address('handles', 2), handle, rights), obj)
            self.assertEqual(vm.memory[self.handle_field(vm, 2, 5, 'rights')], rights)
            self.assertEqual(vm.object_value(obj, 'references'), 2)
            self.assertEqual(self.collect(vm, ticket), (error(9), 0, 0))
            self.assertEqual(vm.call('ipcClose', handle), 0)
            self.assertEqual(vm.object_value(obj, 'references'), 1)

    def test_forged_wrong_sender_rights_consumed_and_cancelled_permissions(self):
        vm, source = self.setup_vm()
        ticket = self.reserve(vm)
        baseline = vm.object_value(vm.root(source), 'references')
        for forged in (0, ticket + 256, ticket ^ 1, 0x80000000 | ticket):
            self.assertEqual(self.commit(vm, source, forged), error(9))
        self.assertEqual(self.commit(vm, source, ticket, sender=3), error(9))
        self.assertEqual(self.commit(vm, source, ticket, rights=3), error(1))
        self.assertEqual(vm.object_value(vm.root(source), 'references'), baseline)
        self.assertEqual(self.commit(vm, source, ticket), 0)
        self.assertEqual(self.commit(vm, source, ticket), error(9))
        handle, _, _ = self.collect(vm, ticket)
        vm.call('ipcClose', handle)
        fresh = self.reserve(vm)
        self.assertNotEqual(fresh, ticket)
        vm.invoke(C['SYS_TRANSFER_CANCEL'], fresh)
        self.assertEqual(vm.result(2)[0], 0)
        self.assertEqual(self.commit(vm, source, fresh), error(9))
        self.assertFalse(vm.memory[self.handle_field(vm, 2, 5, 'reserved')])

    def test_expiry_without_waiters_and_commit_without_timer_irq(self):
        for irq in (False, True):
            vm, source = self.setup_vm()
            ticket = self.reserve(vm)
            vm.memory[C['TIMER_COUNT_LO']] = 1000000
            if irq:
                vm.call('ipcTimerTick')
                self.assertFalse(vm.memory[self.handle_field(vm, 2, 5, 'reserved')])
            self.assertEqual(self.commit(vm, source, ticket), error(9))
            self.assertEqual(self.collect(vm, ticket), (error(9), 0, 0))
            self.assertEqual(vm.object_value(vm.root(source), 'references'), 1)
            self.assertTrue(0 < self.reserve(vm) < 0x80000000)

    def test_slot_reservation_skipped_by_self_copy_and_recovery_slots_protected(self):
        vm, source = self.setup_vm()
        local = vm.copy_fixture(source, 2, 1)
        ticket = self.reserve(vm, slot=2)
        copy = vm.call('ipcCopy', local, 2, 1)
        self.assertEqual(copy & 255, 3)
        self.assertEqual(self.commit(vm, source, ticket), 0)
        vm.run(1)
        vm.invoke(C['SYS_TRANSFER_RESERVE'], 15, 2, 1, 1)
        self.assertEqual(vm.result(1)[0], error(1))

    def test_full_retired_busy_and_invalid_reservations_do_not_pin(self):
        vm, source = self.setup_vm()
        local = vm.copy_fixture(source, 2, 1)
        for _ in range(15):
            vm.run(2)
            self.assertTrue(0 < vm.call('ipcCopy', local, 2, 1) < 0x80000000)
        self.assertEqual(self.reserve(vm, slot=1), error(24))
        vm.run(2)
        vm.call('ipcClose', local)
        vm.memory[self.handle_field(vm, 2, 1, 'generation')] = GEN_MAX
        self.assertEqual(self.reserve(vm, slot=1), error(24))
        for slot, rights, seconds in ((0, 1, 1), (17, 1, 1), (2, 0, 1), (2, 8, 1)):
            self.assertEqual(self.reserve(vm, slot=slot, rights=rights, seconds=seconds), error(22))
        vm.call('handleClose', vm.field_address('handles', 2), 0x102)
        self.assertEqual(self.reserve(vm, slot=2, seconds=0), error(22))
        self.assertEqual(self.reserve(vm, slot=2, seconds=61), error(22))
        ticket = self.reserve(vm, slot=2)
        self.assertEqual(self.reserve(vm, slot=2), error(16))
        vm.invoke(C['SYS_TRANSFER_CANCEL'], ticket)
        self.assertEqual(self.record(vm)['state'], 0)

    def test_revoked_closed_and_insufficient_source_preserve_reservation_and_references(self):
        for failure in ('revoked', 'closed', 'rights', 'service_receive'):
            vm, source = self.setup_vm(failure == 'service_receive')
            obj = vm.root(source)
            rights = 2 if failure == 'service_receive' else 1
            ticket = self.reserve(vm, rights=rights)
            vm.run(1)
            expected = 1
            if failure == 'revoked':
                vm.call('ipcDestroy', source)
                expected = 32
            elif failure == 'closed':
                vm.call('ipcClose', source)
                expected = 9
            elif failure == 'rights':
                source = vm.call('ipcCopy', source, 1, 2)
            references = vm.object_value(obj, 'references')
            self.assertEqual(self.commit(vm, source, ticket, rights=rights), error(expected))
            self.assertEqual(vm.object_value(obj, 'references'), references)
            self.assertEqual(self.record(vm)['state'], 1)
            vm.run(2)
            vm.invoke(C['SYS_TRANSFER_CANCEL'], ticket)
            self.assertFalse(vm.memory[self.handle_field(vm, 2, 5, 'reserved')])

    def test_sender_and_receiver_death_before_and_after_commit_release_quota(self):
        for owner in (1, 2):
            for committed in (False, True):
                vm, source = self.setup_vm()
                obj = vm.root(source)
                ticket = self.reserve(vm)
                if committed:
                    self.assertEqual(self.commit(vm, source, ticket), 0)
                vm.run(owner)
                vm.invoke(C['SYS_EXIT'], 0)
                if owner == 2 or not committed:
                    self.assertEqual(self.record(vm)['state'], 0)
                self.assertFalse(vm.memory[self.handle_field(vm, 2, 5, 'reserved')])
                if owner == 1 and committed:
                    handle, sender, rights = self.collect(vm, ticket)
                    self.assertEqual((sender, rights), (1, 1))
                    self.assertEqual(vm.call('ipcResolve', handle, 1), 0)
                    vm.call('ipcClose', handle)
                vm.run(3)
                for survivor in (1, 2):
                    if survivor != owner:
                        vm.run(survivor)
                        vm.invoke(C['SYS_EXIT'], 0)
                self.assertEqual(vm.object_value(obj, 'references'), 0)

    def test_receiver_lifetime_reuse_and_ticket_generation_retirement(self):
        vm, source = self.setup_vm()
        old = self.reserve(vm)
        vm.memory[vm.field_address('reusable', 2)] = 1
        vm.run(2)
        vm.invoke(C['SYS_EXIT'], 0)
        vm.run(1)
        vm.reap()
        replacement = create(vm)
        self.assertEqual(replacement, 4098)
        new = self.reserve(vm, receiver=replacement)
        self.assertNotEqual(old, new)
        self.assertEqual(self.commit(vm, source, old), error(3))
        self.assertEqual(self.commit(vm, source, old, receiver=replacement), error(9))
        self.assertEqual(self.commit(vm, source, new, receiver=replacement), 0)
        handle, sender, rights = self.collect(vm, new, receiver=replacement)
        self.assertEqual((sender, rights), (1, 1))
        vm.call('ipcClose', handle)
        typ = vm.decls['transfers'].sym.type.target
        address = vm.table_base('transfers') + typ.size + typ.field('generation').offset
        vm.memory[address] = C['TASK_GENERATION_MAX']
        self.assertEqual(self.reserve(vm, receiver=replacement), error(75))
        self.assertFalse(vm.memory[self.handle_field(vm, replacement, 5, 'reserved')])

    def test_task_quota_charges_uncollected_events_and_reserves_supervisor_slots(self):
        vm = fixture(2, ordinary=True)
        vm.run(2)
        vm.memory[vm.field_address('createImages', 2)] = 1
        completed = []
        for _ in range(4):
            child = create(vm)
            completed.append(child)
            vm.run(child)
            vm.invoke(C['SYS_EXIT'], 0)
            vm.run(2)
            vm.reap()
        vm.invoke(C['SYS_TASK_CREATE'], 1)
        self.assertEqual(vm.result(2)[0], error(23))
        vm.run(1)
        self.assertTrue(0 < create(vm) < 0x80000000)
        vm.run(2)
        vm.invoke(C['SYS_TASK_COLLECT'], completed[0], C['START_DATA_VA'])
        self.assertEqual(vm.result(2)[0], 0)
        self.assertTrue(0 < create(vm) < 0x80000000)
        for ref in completed[1:]:
            vm.invoke(C['SYS_TASK_COLLECT'], ref, C['START_DATA_VA'])
            self.assertEqual(vm.result(2)[0], 0)
        # Fill all ordinary task slots (marked occupied directly: building 250 real
        # tasks would exhaust the fixture's frames first); the supervisor can still
        # create twice, in the two reserved slots.
        slots = vm.globals['taskCapacity']
        for slot in range(1, slots - 1):
            if vm.field('state', slot) == 0:
                vm.memory[vm.field_address('state', slot)] = 1
        vm.invoke(C['SYS_TASK_CREATE'], 1)
        self.assertEqual(vm.result(2)[0], error(23))
        vm.run(1)
        self.assertEqual(create(vm) & C['TASK_SLOT_MASK'], slots - 1)
        self.assertEqual(create(vm) & C['TASK_SLOT_MASK'], slots)

    def test_borrower_quota_charges_only_accepted_mappings_and_refunds_final_unmap(self):
        vm = fixture(3, ordinary=True)
        cap = space(vm, rights=47)
        region = allocate(vm, cap, 1)
        offers = [invoke(vm, 'SYS_MEM_GRANT', cap, region, 2, RW) for _ in range(8)]
        vm.run(2)
        borrower = space(vm)
        for i, token in enumerate(offers):
            self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_MAP', borrower, token, VA + i * PAGE, RW), 0)
        vm.run(3)
        third = space(vm, rights=47)
        own = allocate(vm, third, 1)
        ninth = invoke(vm, 'SYS_MEM_GRANT', third, own, 2, RW)
        page = frames(vm, own)[0]
        vm.run(2)
        before = used(vm, 2), vm.call('physicalPageReferences', page)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_MAP', borrower, ninth, VA + 8 * PAGE, RW), -23)
        self.assertEqual((used(vm, 2), vm.call('physicalPageReferences', page)), before)
        self.assertEqual(invoke(vm, 'SYS_MEM_UNMAP', borrower, VA, 1), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_MAP', borrower, ninth, VA + 8 * PAGE, RW), 0)

    def test_memory_offers_do_not_spend_borrower_quota_and_lender_cannot_monopolize(self):
        vm = fixture(3, ordinary=True)
        cap = space(vm, rights=47)
        region = allocate(vm, cap, 1)
        pages = frames(vm, region)
        before = used(vm, 2)
        for _ in range(8):
            self.assertGreater(invoke(vm, 'SYS_MEM_GRANT', cap, region, 2, RW), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT', cap, region, 2, RW), -23)
        self.assertEqual(used(vm, 2), before)
        vm.run(3)
        third = space(vm, rights=47)
        own = allocate(vm, third, 1)
        accepted = invoke(vm, 'SYS_MEM_GRANT', third, own, 2, RW)
        self.assertGreater(accepted, 0)
        vm.run(2)
        borrowed = space(vm)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_MAP', borrowed, accepted, VA, RW), 0)
        vm.run(1)
        vm.invoke(C['SYS_EXIT'], 0)
        vm.run(2)
        vm.reap()
        self.assertTrue(vm.call('physicalPageAvailable', pages[0]))


if __name__ == '__main__':
    unittest.main()
