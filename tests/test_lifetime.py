"""G4 finite-lifetime report, namespace selection and planned replacement.

Checked sources run on the source evaluator; nothing here builds code or
measures the CPU. The supervisor in the procedure test is played by Python
through the real syscalls, and the user policy helpers run as checked M.
"""
import unittest

from source_m import LAYOUT as C
from test_kernel import check_m, LAIX
from test_ipc_handles import error
from test_ipc_request_reply import GEN_MAX
from test_ipc_handles import GEN_MAX as HANDLE_GEN_MAX
from test_recovery_policy import PolicyM
from test_runtime_tasks import fixture, create
from test_task import USER_DATA

FIELDS = ('bytes', 'limit', 'replySelected', 'replyTotal', 'replyOpen', 'taskSelected',
          'handleSelected', 'retiredTasks', 'retiredHandles', 'retiredEndpoints', 'retiredIrqs')
RESERVE = C['LIFETIME_REPLY_RESERVE']
SYS = C['SYS_LIFETIME']


def report(vm, reference=0, destination=USER_DATA):
    """Invoke the syscall as the current task and return (result, fields)."""
    vm.invoke(SYS, reference, destination)
    result = vm.result(vm.current())[0]
    if result:
        return result, None
    words = [int.from_bytes(vm.read_bytes(vm.pages(vm.current())[1] + 4 * i, 4), 'little')
             for i in range(len(FIELDS))]
    return result, dict(zip(FIELDS, words))


def ok(test, vm, reference=0):
    result, fields = report(vm, reference)
    test.assertEqual(result, 0)
    return fields


def set_task(vm, field, slot, value):
    vm.memory[vm.field_address(field, slot)] = value
    # A record that was written is a record in use: scans stop at the high-water mark.
    vm.globals['taskHighWater'] = max(vm.globals['taskHighWater'], slot)


def row_address(vm, name, index, field):
    typ = vm.decls[name].sym.type.elem
    return vm.addresses[name] + typ.size * index + typ.field(field).offset


class LifetimeTests(unittest.TestCase):
    def test_abi_sources_and_assembly_constants_agree(self):
        modules = check_m(LAIX / 'src/task/control.m')
        report_type = next(m.scope['LifetimeReport'].type for m in modules
                           if 'LifetimeReport' in m.scope and m.scope['LifetimeReport'].type)
        self.assertEqual(report_type.size, C['LIFETIME_REPORT_BYTES'])
        self.assertEqual([f.name for f in report_type.fields], list(FIELDS))
        self.assertEqual(SYS, 75)
        check_m(LAIX / 'user/syscalls.m')
        check_m(LAIX / 'user/recovery/policy.m')
        check_m(LAIX / 'src/trap/trap.m')

    def test_fresh_system_reports_full_capacity_without_changing_a_counter(self):
        vm = fixture(1)
        before = [vm.field('ipcCallGeneration', slot) for slot in range(1, vm.globals['taskCapacity'] + 1)], vm.free_pages()
        fields = ok(self, vm)
        self.assertEqual(fields['bytes'], C['LIFETIME_REPORT_BYTES'])
        self.assertEqual(fields['limit'], GEN_MAX)
        # The fixture root is a recovery-class supervisor: every slot's namespace.
        self.assertEqual((fields['replyOpen'], fields['replyTotal']), (vm.globals['taskCapacity'], vm.globals['taskCapacity'] * GEN_MAX))
        for name in ('retiredTasks', 'retiredHandles', 'retiredEndpoints', 'retiredIrqs',
                     'replySelected', 'taskSelected', 'handleSelected'):
            self.assertEqual(fields[name], 0, name)
        # Repeating the call is idempotent; it never advances or resets anything.
        self.assertEqual(ok(self, vm), fields)
        self.assertEqual(([vm.field('ipcCallGeneration', slot) for slot in range(1, vm.globals['taskCapacity'] + 1)], vm.free_pages()), before)

    def test_ordinary_supervisor_sees_only_the_namespaces_it_can_construct_into(self):
        vm = fixture(1)
        table = vm.decls['tasks'].sym.type.target.field('handles').type
        vm.memory[vm.field_address('handles', 1) + table.field('factoryRecovery').offset] = 0
        fields = ok(self, vm)
        # Two slots are recovery-reserved, as in task construction.
        self.assertEqual((fields['replyOpen'], fields['replyTotal']), (vm.globals['taskCapacity'] - 2, (vm.globals['taskCapacity'] - 2) * GEN_MAX))

    def test_retirement_of_every_identity_kind_is_counted(self):
        vm = fixture(1)
        set_task(vm, 'ipcCallGeneration', 3, GEN_MAX)
        set_task(vm, 'id', 4, GEN_MAX << C['TASK_SLOT_BITS'] | 4)
        handles = vm.decls['tasks'].sym.type.target.field('handles').type
        entry = handles.field('entries').type.elem
        vm.memory[vm.field_address('handles', 5) + handles.field('entries').offset +
                  2 * entry.size + entry.field('generation').offset] = HANDLE_GEN_MAX
        vm.memory[row_address(vm, 'endpoints', 3, 'state')] = C['ENDPOINT_RETIRED'] if 'ENDPOINT_RETIRED' in C else 3
        vm.memory[row_address(vm, 'endpoints', 4, 'generation')] = 0xFFFFFFFF
        vm.memory[row_address(vm, 'irqGrants', 7, 'generation')] = HANDLE_GEN_MAX
        vm.globals['taskHighWater'] = max(vm.globals['taskHighWater'], 8)  # the poked records are in use
        fields = ok(self, vm)
        self.assertEqual(fields['retiredTasks'], 2)
        self.assertEqual(fields['replyOpen'], vm.globals['taskCapacity'] - 2)
        self.assertEqual(fields['replyTotal'], (vm.globals['taskCapacity'] - 2) * GEN_MAX)
        self.assertEqual(fields['retiredHandles'], 1)
        # A free row at the final generation can never be allocated again.
        self.assertEqual(fields['retiredEndpoints'], 2)
        self.assertEqual(fields['retiredIrqs'], 1)

    def test_selected_child_reports_its_own_reply_task_and_handle_remaining(self):
        vm = fixture(1, endpoint=True)
        child = create(vm, token=vm.root_handle, rights=1)
        set_task(vm, 'ipcCallGeneration', child, GEN_MAX - 5)
        fields = ok(self, vm, child)
        self.assertEqual(fields['replySelected'], 5)
        self.assertEqual(fields['taskSelected'], GEN_MAX - (child >> C['TASK_SLOT_BITS']))
        # The child holds one installed handle; its other 15 slots are untouched.
        self.assertEqual(fields['handleSelected'], HANDLE_GEN_MAX - 1)
        self.assertEqual(fields['replyTotal'], (vm.globals['taskCapacity'] - 1) * GEN_MAX + 5)
        set_task(vm, 'ipcCallGeneration', child, GEN_MAX)
        fields = ok(self, vm, child)
        self.assertEqual(fields['replySelected'], 0)
        self.assertEqual(fields['replyOpen'], vm.globals['taskCapacity'] - 1)

    def test_authority_faults_and_stale_references_do_not_report_or_mutate(self):
        vm = fixture(2, endpoint=True)
        child = create(vm, token=vm.root_handle, rights=1)
        baseline = vm.free_pages()
        # Possession of a reference and of control are separate; a stranger has neither.
        for reference in (0x7FFFFF01, child + 0x100, 0xFFFFFFFF):
            self.assertEqual(report(vm, reference)[0], error(1))
        set_task(vm, 'createImages', 1, 0)
        self.assertEqual(report(vm)[0], error(1))
        self.assertEqual(report(vm, child)[0], error(1))
        set_task(vm, 'createImages', 1, 1)
        # A bad destination is EFAULT and reports nothing partially.
        for destination in (0, 0x1000, USER_DATA + 4096 - 40, 0xFFFFFFF0):
            self.assertEqual(report(vm, 0, destination)[0], error(14), hex(destination))
        self.assertEqual(vm.free_pages(), baseline)
        # Collection ends the child's control; its reference then reports nothing.
        vm.invoke(C['SYS_TASK_TERMINATE'], child, 0)
        vm.reap()
        # Reaped but uncollected: the control row persists, the task itself is gone.
        self.assertEqual(report(vm, child)[0], error(3))
        vm.invoke(C['SYS_TASK_COLLECT'], child, USER_DATA)
        self.assertEqual(vm.result(1)[0], 0)
        self.assertEqual(report(vm, child)[0], error(1))
        # A second supervisor can read global state but not the first one's child.
        second = create(vm, token=vm.root_handle, rights=1)
        vm.run(2)
        self.assertEqual(report(vm, 0)[0], 0)
        self.assertEqual(report(vm, second)[0], error(1))
        vm.run(1)
        self.assertEqual(report(vm, second)[0], 0)

    def test_construction_prefers_a_fresh_namespace_over_one_near_its_limit(self):
        vm = fixture(1)
        set_task(vm, 'ipcCallGeneration', 2, GEN_MAX - RESERVE)
        first = create(vm)
        self.assertEqual(first & C['TASK_SLOT_MASK'], 3)
        # The slot just above the reserve is still preferred by order.
        vm.invoke(C['SYS_TASK_TERMINATE'], first, 0)
        vm.reap()
        vm.invoke(C['SYS_TASK_COLLECT'], first, USER_DATA)
        set_task(vm, 'ipcCallGeneration', 2, GEN_MAX - RESERVE - 1)
        self.assertEqual(create(vm) & C['TASK_SLOT_MASK'], 2)

    def test_construction_falls_back_to_a_near_limit_namespace_when_nothing_else_is_left(self):
        vm = fixture(1)
        set_task(vm, 'ipcCallGeneration', 2, GEN_MAX - 3)
        for slot in range(3, vm.globals['taskCapacity'] + 1):
            set_task(vm, 'ipcCallGeneration', slot, GEN_MAX)
        child = create(vm)
        self.assertEqual(child & C['TASK_SLOT_MASK'], 2)
        self.assertEqual(ok(self, vm, child)['replySelected'], 3)
        # Only retired namespaces remain: creation fails without allocating.
        vm.invoke(C['SYS_TASK_TERMINATE'], child, 0)
        vm.reap()
        vm.invoke(C['SYS_TASK_COLLECT'], child, USER_DATA)
        set_task(vm, 'ipcCallGeneration', 2, GEN_MAX)
        baseline = vm.free_pages()
        vm.invoke(C['SYS_TASK_CREATE'], 1)
        self.assertEqual(vm.result(1)[0], error(23))
        self.assertEqual(vm.free_pages(), baseline)
        fields = ok(self, vm)
        self.assertEqual((fields['replyOpen'], fields['replyTotal'], fields['retiredTasks']), (1, GEN_MAX, vm.globals['taskCapacity'] - 1))  # only the running root remains

    def test_planned_replacement_moves_a_client_before_it_sees_eoverflow(self):
        vm = fixture(1, endpoint=True)
        client = create(vm, token=vm.root_handle, rights=1)
        handle = vm.memory[vm.field('bootPage', client) + 28]
        # Park the client two calls above the reserve, then let it work.
        set_task(vm, 'ipcCallGeneration', client, GEN_MAX - RESERVE - 2)
        results, served = [], 0
        while ok(self, vm, client)['replySelected'] > RESERVE:
            vm.run(client)
            vm.request(handle)
            vm.run(1)
            vm.response(vm.accept(vm.root_handle))
            results.append(vm.result(client)[0])
            served += 1
        self.assertEqual(served, 2)
        self.assertEqual(results, [8, 8])
        due = ok(self, vm, client)
        self.assertEqual(due['replySelected'], RESERVE)
        total_before = due['replyTotal']
        old_generation = vm.field('ipcCallGeneration', client)

        # Drain: no wait is outstanding, so terminate, reap, collect, then replace.
        self.assertEqual(vm.field('waitReason', client), 0)
        vm.invoke(C['SYS_TASK_TERMINATE'], client, 0)
        self.assertEqual(vm.result(1)[0], 0)
        vm.reap()
        vm.invoke(C['SYS_TASK_COLLECT'], client, USER_DATA)
        self.assertEqual(vm.result(1)[0], 0)
        replacement = create(vm, token=vm.root_handle, rights=1)

        self.assertNotEqual(replacement & C['TASK_SLOT_MASK'], client & C['TASK_SLOT_MASK'])
        # No counter was reset or advanced by the replacement itself.
        self.assertEqual(vm.field('ipcCallGeneration', client), old_generation)
        fresh = ok(self, vm, replacement)
        self.assertEqual(fresh['replySelected'], GEN_MAX)
        self.assertGreater(fresh['replySelected'], RESERVE)
        self.assertEqual(fresh['replyTotal'], total_before)
        # The replacement is a different reference with its own reply namespace.
        fresh_handle = vm.memory[vm.field('bootPage', replacement) + 28]
        vm.run(replacement)
        vm.request(fresh_handle)
        self.assertNotEqual(vm.result(replacement)[0], error(75))
        vm.run(1)
        vm.response(vm.accept(vm.root_handle))
        self.assertEqual(vm.result(replacement), (8, 8))
        self.assertEqual(ok(self, vm, replacement)['replySelected'], GEN_MAX - 1)

    def test_replacement_is_still_due_when_no_fresh_namespace_remains(self):
        vm = fixture(1, endpoint=True)
        client = create(vm, token=vm.root_handle, rights=1)
        for slot in range(3, vm.globals['taskCapacity'] + 1):
            set_task(vm, 'ipcCallGeneration', slot, GEN_MAX)
        set_task(vm, 'ipcCallGeneration', client, GEN_MAX - RESERVE)
        vm.invoke(C['SYS_TASK_TERMINATE'], client, 0)
        vm.reap()
        vm.invoke(C['SYS_TASK_COLLECT'], client, USER_DATA)
        replacement = create(vm, token=vm.root_handle, rights=1)
        # Same namespace, counter preserved: the supervisor must see "still due"
        # and stop replacing instead of consuming task generations in a loop.
        self.assertEqual(replacement & C['TASK_SLOT_MASK'], client & C['TASK_SLOT_MASK'])
        self.assertEqual(ok(self, vm, replacement)['replySelected'], RESERVE)


class ScenarioTests(unittest.TestCase):
    """The CPU scenario is only checked statically here; the probe runs it."""
    def test_scenario_sources_probe_and_provenance_modes_are_consistent(self):
        import os
        import ast
        import sys
        from unittest import mock
        modules = check_m(LAIX / 'tests/programs/lifetime/policy.m')
        self.assertTrue(any('lifetimePolicyMain' in m.scope for m in modules))
        asm = (LAIX / 'tests/programs/lifetime/policy.asm').read_text()
        for symbol in ('lifetimePolicyMain', 'lifetimeArmed', 'lifetimeReplaced', 'lifetimeDone'):
            self.assertIn(symbol, asm)
        probe = (LAIX / 'tests/probe_lifetime_cpu.py').read_text()
        ast.parse(probe)
        self.assertIn(f'RESERVE = {RESERVE} ', probe)
        sys.path.insert(0, str(LAIX / 'tools'))
        import recovery_provenance as provenance
        for mode, expected in (('0', False), ('1', True), ('lifetime', 'lifetime')):
            with mock.patch.dict(os.environ, {'LAIX_RECOVERY_FIXTURES': mode}):
                self.assertEqual(provenance.fixtures(), expected)
        # Older probes test `is True` / `is False`; the string matches neither.
        with mock.patch.dict(os.environ, {'LAIX_RECOVERY_FIXTURES': 'lifetime'}):
            self.assertIsNot(provenance.fixtures(), True)
            self.assertIsNot(provenance.fixtures(), False)


class LifetimePolicyM(PolicyM):
    def __init__(self, left=0, read_error=0, terminate=0, reclaimed=True):
        super().__init__()
        self.left, self.read_error, self.terminate, self.reclaimed = left, read_error, terminate, reclaimed

    def call(self, name, *args):
        if name == 'lifetimeReport':
            self.operations.append((name, args))
            if self.read_error:
                return self.read_error
            typ = self.decls['lifetimeSnapshot'].sym.type
            self.memory[args[1] + typ.field('replySelected').offset] = self.left
            return 0
        if name == 'terminateTask' and self.terminate:
            self.operations.append((name, args))
            return self.terminate
        if name == 'inspectTask' and not self.reclaimed:
            self.operations.append((name, args))
            typ = self.decls['recoveryEvent'].sym.type
            self.memory[args[1] + typ.field('flags').offset] = 0
            return 0
        return super().call(name, *args)


class LifetimePolicyTests(unittest.TestCase):
    def test_due_is_inclusive_at_the_reserve_and_propagates_errors(self):
        for left, due in ((0, 1), (RESERVE, 1), (RESERVE + 1, 0), (GEN_MAX, 0)):
            vm = LifetimePolicyM(left=left)
            self.assertEqual(vm.call('lifetimeDue', 12, RESERVE), due, left)
            self.assertEqual(vm.operations, [('lifetimeReport', (12, vm.addresses['lifetimeSnapshot']))])
        vm = LifetimePolicyM(read_error=error(1))
        self.assertEqual(vm.call('lifetimeDue', 12, RESERVE), error(1))

    def test_retire_client_terminates_waits_for_reclaim_and_collects(self):
        vm = LifetimePolicyM()
        self.assertEqual(vm.call('retireClient', 12), 0)
        self.assertEqual([name for name, _ in vm.operations],
                         ['terminateTask', 'inspectTask', 'collectTask'])
        self.assertFalse(any(name in ('createTask', 'closeHandle', 'withdrawService') for name, _ in vm.operations))
        # A client that already exited is still collected.
        vm = LifetimePolicyM(terminate=error(3))
        self.assertEqual(vm.call('retireClient', 12), 0)
        self.assertEqual(vm.operations[-1][0], 'collectTask')

    def test_unreclaimed_client_is_not_collected_and_reports_busy(self):
        vm = LifetimePolicyM(reclaimed=False)
        self.assertEqual(vm.call('retireClient', 12), error(16))
        self.assertEqual(sum(name == 'sleep' for name, _ in vm.operations), 5)
        self.assertFalse(any(name == 'collectTask' for name, _ in vm.operations))
        vm = LifetimePolicyM(terminate=error(1))
        self.assertEqual(vm.call('retireClient', 12), error(1))
        self.assertEqual([name for name, _ in vm.operations], ['terminateTask'])


if __name__ == '__main__':
    unittest.main()
