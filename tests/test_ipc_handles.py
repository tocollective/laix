"""Capability/lifetime checks against the checked M AST; no code generation."""

import unittest

from source_m import SourceM, KernelPanic, LAYOUT
from test_kernel import LAIX
from test_task import TaskM, TaskEntered

SEND, RECEIVE, MANAGE, ALL = 1, 2, 4, 7
LIVE, DESTROYED, RETIRED = 1, 2, 3
BADF, PERM, INVAL, MFILE, NFILE, PIPE, SRCH = 9, 1, 22, 24, 23, 32, 3
GEN_MAX = 0x7FFFFF


def error(number):
    return -number & 0xFFFFFFFF


class HandlesM(SourceM):
    def __init__(self):
        super().__init__(LAIX / "src/trap/trap.m")
        self.task_type = self.decls["tasks"].sym.type.elem
        self.endpoint_type = self.decls["endpoints"].sym.type.elem
        self.handle_type = self.task_type.field("handles").type.field("entries").type.elem
        for id in range(1, 9):
            self.memory[self.task_field(id, "id")] = id
            self.memory[self.task_field(id, "state")] = 1
        self.select(1)

    def task_field(self, id, name):
        return self.addresses["tasks"] + (id - 1) * self.task_type.size + self.task_type.field(name).offset

    def table(self, id):
        return self.task_field(id, "handles")

    def entry(self, id, slot, field):
        return self.table(id) + (slot - 1) * self.handle_type.size + self.handle_type.field(field).offset

    def object_field(self, object, field):
        return object + self.endpoint_type.field(field).offset

    def value(self, object, field):
        return self.memory[self.object_field(object, field)]

    def select(self, id):
        for task in range(1, 9):
            if self.memory[self.task_field(task, "state")] == 2:
                self.memory[self.task_field(task, "state")] = 1
        self.memory[self.task_field(id, "state")] = 2
        self.globals["currentTask"] = self.call("taskGet", id)

    def bootstrap(self, owner=1):
        token = self.call("endpointBootstrap", self.table(owner), owner)
        assert 0 < token <= 0x7FFFFFFF
        return token

    def resolve(self, id, token, rights=SEND):
        return self.call("handleLookup", self.table(id), token, rights)


class HandleTests(unittest.TestCase):
    def test_private_tables_require_entry_rights_and_both_generations(self):
        vm = HandlesM()
        token = vm.bootstrap()
        object = vm.resolve(1, token, ALL)
        self.assertEqual(vm.value(object, "id"), 1)
        self.assertEqual(vm.value(object, "generation"), 1)
        self.assertEqual(vm.value(object, "references"), 1)
        for name in ("senderHead", "senderCount", "receiverHead", "receiverCount"):
            self.assertEqual(vm.value(object, name), 0)
        for token_arg in (0, 1, 17, 0xFFFFFFFF, token + 256, token | 0x80000000):
            self.assertEqual(vm.call("ipcResolve", token_arg, SEND), 0)
        self.assertEqual(vm.call("ipcResolve", token, 0), 0)
        self.assertEqual(vm.call("ipcResolve", token, 8), 0)
        vm.select(2)
        self.assertEqual(vm.call("ipcResolve", token, SEND), 0)
        self.assertEqual(vm.call("ipcClose", token), error(BADF))
        vm.memory[vm.entry(1, token & 255, "objectGeneration")] += 1
        self.assertEqual(vm.resolve(1, token, ALL), 0)

    def test_subset_copy_and_management_owner(self):
        vm = HandlesM()
        token = vm.bootstrap()
        sender = vm.call("ipcCopy", token, 2, SEND)
        object = vm.resolve(2, sender)
        self.assertEqual(vm.value(object, "references"), 2)
        self.assertEqual(vm.call("ipcCopy", token, 2, MANAGE), error(PERM))
        vm.select(2)
        self.assertEqual(vm.call("ipcResolve", sender, RECEIVE), 0)
        self.assertEqual(vm.call("ipcDestroy", sender), error(PERM))
        before = dict(vm.memory)
        for rights in (RECEIVE, MANAGE, ALL):
            self.assertEqual(vm.call("ipcCopy", sender, 3, rights), error(PERM))
        for rights in (0, 8, 0xFFFFFFFF):
            self.assertEqual(vm.call("ipcCopy", sender, 3, rights), error(INVAL))
        self.assertEqual(vm.memory, before)
        third = vm.call("ipcCopy", sender, 3, SEND)
        self.assertEqual(vm.resolve(3, third), object)
        vm.select(1)
        manager_copy = vm.call("ipcCopy", token, 1, MANAGE)
        self.assertEqual(vm.call("ipcDestroy", manager_copy), 0)
        self.assertEqual(vm.resolve(3, third), 0)

    def test_numeric_collision_resolves_only_callers_own_object(self):
        vm = HandlesM()
        first = vm.bootstrap(1)
        second = vm.bootstrap(2)
        self.assertEqual(first, second)
        first_object = vm.resolve(1, first)
        second_object = vm.resolve(2, second)
        self.assertNotEqual(first_object, second_object)
        vm.select(2)
        self.assertEqual(vm.call("ipcResolve", first, SEND), second_object)
        self.assertEqual(vm.call("ipcDestroy", first), 0)
        self.assertEqual(vm.value(first_object, "state"), LIVE)

    def test_close_one_copy_and_last_reference(self):
        vm = HandlesM()
        token = vm.bootstrap()
        copy = vm.call("ipcCopy", token, 2, RECEIVE)
        object = vm.resolve(1, token)
        self.assertEqual(vm.call("ipcClose", token), 0)
        self.assertEqual(vm.call("ipcClose", token), error(BADF))
        self.assertEqual(vm.resolve(2, copy, RECEIVE), object)
        self.assertEqual(vm.value(object, "references"), 1)
        vm.select(2)
        self.assertEqual(vm.call("ipcClose", copy), 0)
        self.assertEqual(vm.value(object, "state"), 0)
        self.assertEqual(vm.value(object, "references"), 0)
        self.assertEqual(vm.value(object, "manager"), 0)

    def test_destroy_revokes_copies_and_pins_object_until_close(self):
        vm = HandlesM()
        token = vm.bootstrap()
        copy = vm.call("ipcCopy", token, 2, SEND)
        object = vm.resolve(1, token)
        self.assertEqual(vm.call("ipcDestroy", token), 0)
        self.assertEqual(vm.value(object, "state"), DESTROYED)
        self.assertEqual(vm.value(object, "references"), 2)
        self.assertEqual(vm.call("ipcDestroy", token), error(PIPE))
        self.assertEqual(vm.call("ipcCopy", token, 3, SEND), error(PIPE))
        other = vm.bootstrap()
        self.assertNotEqual(vm.resolve(1, other), object)
        self.assertEqual(vm.call("ipcClose", token), 0)
        vm.select(2)
        self.assertEqual(vm.call("ipcResolve", copy, SEND), 0)
        self.assertEqual(vm.call("ipcCopy", copy, 3, SEND), error(PIPE))
        self.assertEqual(vm.call("ipcClose", copy), 0)
        fresh = vm.bootstrap(1)
        self.assertEqual(vm.resolve(1, fresh), object)
        self.assertEqual(vm.value(object, "generation"), 2)
        self.assertEqual(vm.resolve(1, token), 0)

    def test_handle_reuse_and_generation_exhaustion(self):
        vm = HandlesM()
        token = vm.bootstrap()
        vm.call("ipcClose", token)
        fresh = vm.bootstrap()
        self.assertEqual(fresh & 255, token & 255)
        self.assertNotEqual(fresh, token)
        self.assertEqual(vm.call("ipcClose", token), error(BADF))
        vm.call("ipcClose", fresh)
        vm.memory[vm.entry(1, 1, "generation")] = GEN_MAX - 1
        last = vm.bootstrap()
        self.assertEqual(last, (GEN_MAX << 8) | 1)
        vm.call("ipcClose", last)
        next_token = vm.bootstrap()
        self.assertEqual(next_token & 255, 2)
        self.assertEqual(vm.call("ipcClose", last), error(BADF))

    def test_endpoint_generation_never_wraps(self):
        vm = HandlesM()
        object = vm.addresses["endpoints"]
        vm.memory[vm.object_field(object, "generation")] = 0xFFFFFFFE
        last = vm.bootstrap()
        self.assertEqual(vm.resolve(1, last), object)
        self.assertEqual(vm.value(object, "generation"), 0xFFFFFFFF)
        vm.call("ipcClose", last)
        self.assertEqual(vm.value(object, "state"), RETIRED)
        fresh = vm.bootstrap()
        self.assertNotEqual(vm.resolve(1, fresh), object)
        self.assertEqual(vm.value(object, "generation"), 0xFFFFFFFF)

    def test_capacity_and_bootstrap_failure_do_not_leak(self):
        vm = HandlesM()
        token = vm.bootstrap()
        object = vm.resolve(1, token)
        for _ in range(15):
            self.assertGreater(vm.call("ipcCopy", token, 1, SEND), 0)
        before = dict(vm.memory)
        self.assertEqual(vm.call("ipcCopy", token, 1, SEND), error(MFILE))
        self.assertEqual(vm.memory, before)
        self.assertEqual(vm.call("endpointBootstrap", vm.table(1), 1), error(MFILE))
        self.assertEqual(vm.value(object, "references"), 16)
        # Failed root installation frees its endpoint; another task can use it.
        for _ in range(15):
            vm.bootstrap(2)
        self.assertEqual(vm.call("endpointBootstrap", vm.table(3), 3), error(NFILE))
        vm.call("handlesReleaseTask", vm.table(1), 1)
        self.assertGreater(vm.bootstrap(3), 0)

    def test_retired_handle_table_cannot_accept_a_root(self):
        vm = HandlesM()
        for slot in range(1, 17):
            vm.memory[vm.entry(1, slot, "generation")] = GEN_MAX
        self.assertEqual(vm.call("endpointBootstrap", vm.table(1), 1), error(MFILE))
        token = vm.bootstrap(2)
        self.assertEqual(vm.value(vm.resolve(2, token), "id"), 1)

    def test_manager_death_revokes_even_without_manage_handle(self):
        vm = HandlesM()
        token = vm.bootstrap()
        peer = vm.call("ipcCopy", token, 2, SEND)
        object = vm.resolve(1, token)
        vm.call("ipcClose", token)
        vm.call("handlesReleaseTask", vm.table(1), 1)
        self.assertEqual(vm.value(object, "state"), DESTROYED)
        self.assertEqual(vm.resolve(2, peer), 0)
        vm.call("handlesReleaseTask", vm.table(2), 2)
        self.assertEqual(vm.value(object, "references"), 0)
        self.assertEqual(vm.value(object, "state"), 0)
        vm.call("handlesReleaseTask", vm.table(2), 2)

    def test_nonmanager_death_leaves_owner_and_other_peers_live(self):
        vm = HandlesM()
        token = vm.bootstrap()
        vm.call("ipcCopy", token, 2, SEND)
        third = vm.call("ipcCopy", token, 3, RECEIVE)
        object = vm.resolve(1, token)
        vm.call("handlesReleaseTask", vm.table(2), 2)
        self.assertEqual(vm.value(object, "references"), 2)
        self.assertEqual(vm.resolve(3, third, RECEIVE), object)
        self.assertEqual(vm.value(object, "state"), LIVE)

    def test_invalid_target_and_caller_cannot_mutate_tables(self):
        vm = HandlesM()
        token = vm.bootstrap()
        for target in (0, 9, 0xFFFFFFFF):
            self.assertEqual(vm.call("ipcCopy", token, target, SEND), error(SRCH))
        for state in (0, 3):
            vm.memory[vm.task_field(2, "state")] = state
            self.assertEqual(vm.call("ipcCopy", token, 2, SEND), error(SRCH))
        vm.globals["currentTask"] = 0
        self.assertEqual(vm.call("ipcClose", token), error(PERM))
        self.assertEqual(vm.call("ipcResolve", token, SEND), 0)
        vm.select(1)
        vm.controls[0] = LAYOUT["STATUS_IE"]
        with self.assertRaisesRegex(KernelPanic, "IRQs enabled"):
            vm.call("ipcCopy", token, 2, SEND)
        vm.controls[0] |= LAYOUT["STATUS_EXL"]
        self.assertNotEqual(vm.call("ipcResolve", token, SEND), 0)

    def test_bootstrap_policy_and_seal(self):
        vm = HandlesM()
        vm.memory[vm.task_field(1, "state")] = 1
        self.assertTrue(vm.call("taskBootstrapEndpoints"))
        manager = vm.memory[vm.task_field(1, "context") + LAYOUT["TF_R4"]]
        sender = vm.memory[vm.task_field(2, "context") + LAYOUT["TF_R4"]]
        self.assertEqual(vm.resolve(1, manager, ALL), vm.resolve(2, sender, SEND))
        self.assertEqual(vm.resolve(2, sender, RECEIVE), 0)
        vm.call("endpointSealBootstrap")
        self.assertEqual(vm.call("endpointBootstrap", vm.table(3), 3), error(PERM))
        self.assertEqual(vm.call("endpointBootstrap", vm.table(3), 0), error(PERM))

    def test_bootstrap_copy_failure_rolls_back_root_and_can_retry(self):
        vm = HandlesM()
        vm.memory[vm.task_field(1, "state")] = 1
        root = vm.bootstrap(2)
        copies = [vm.call("handleCopy", vm.table(2), root, vm.table(2), 2, 2, SEND)
                  for _ in range(15)]
        self.assertFalse(vm.call("taskBootstrapEndpoints"))
        self.assertEqual(vm.memory[vm.entry(1, 1, "object")], 0)
        rolled_back = vm.addresses["endpoints"] + vm.endpoint_type.size
        self.assertEqual(vm.value(rolled_back, "references"), 0)
        self.assertEqual(vm.value(rolled_back, "state"), 0)
        self.assertEqual(vm.memory[vm.task_field(1, "context") + LAYOUT["TF_R4"]], 0)
        vm.call("handleClose", vm.table(2), copies[-1])
        self.assertTrue(vm.call("taskBootstrapEndpoints"))
        token = vm.memory[vm.task_field(1, "context") + LAYOUT["TF_R4"]]
        self.assertEqual(token, 0x201)

    def test_exhausted_endpoint_pool_is_retired_without_wraparound(self):
        vm = HandlesM()
        for i in range(16):
            object = vm.addresses["endpoints"] + i * vm.endpoint_type.size
            vm.memory[vm.object_field(object, "generation")] = 0xFFFFFFFF
        self.assertEqual(vm.call("endpointBootstrap", vm.table(1), 1), error(NFILE))
        for i in range(16):
            object = vm.addresses["endpoints"] + i * vm.endpoint_type.size
            self.assertEqual(vm.value(object, "generation"), 0xFFFFFFFF)
            self.assertEqual(vm.value(object, "state"), RETIRED)

    def test_syscall_abi_and_exit_fault_cleanup(self):
        for fault in (False, True):
            vm = TaskM()
            self.assertEqual(vm.call("taskCreate"), 1)
            self.assertEqual(vm.call("taskCreate"), 2)
            self.assertTrue(vm.call("taskBootstrapEndpoints"))
            frame = vm.field_address("context")
            token = vm.memory[frame + LAYOUT["TF_R4"]]
            table = vm.field_address("handles")
            object = vm.call("handleLookup", table, token, ALL)
            with self.assertRaises(TaskEntered):
                vm.call("taskStart", 1000000)
            self.assertEqual(vm.call("endpointBootstrap", table, 1), error(PERM))
            if not fault:
                registers = [vm.memory[frame + 4 * i] for i in range(32)]
                registers[1:4] = [token, 2, SEND]
                registers[9] = 17
                for i, value in enumerate(registers):
                    vm.memory[frame + 4 * i] = value
                epc = vm.memory[frame + LAYOUT["TF_EPC"]]
                self.assertEqual(vm.call("userSyscall", frame), frame)
                result = vm.memory[frame + LAYOUT["TF_R1"]]
                self.assertGreater(result, 0)
                self.assertLess(result, 0x80000000)
                self.assertEqual([vm.memory[frame + 4 * i] for i in range(2, 32)], registers[2:])
                self.assertEqual(vm.memory[frame + LAYOUT["TF_EPC"]], epc + 4)
                vm.memory[frame + LAYOUT["TF_R1"]] = token
                vm.memory[frame + LAYOUT["TF_R9"]] = 18
                vm.call("userSyscall", frame)
                self.assertEqual(vm.memory[frame + LAYOUT["TF_R1"]], 0)
                vm.memory[frame + LAYOUT["TF_R1"]] = token
                vm.memory[frame + LAYOUT["TF_R9"]] = 16
                vm.call("userSyscall", frame)
                self.assertEqual(vm.memory[frame + LAYOUT["TF_R1"]], 0)
            selected = vm.call("taskFinish", frame, 9, fault)
            self.assertEqual(selected, vm.field_address("context", 2))
            self.assertEqual(vm.memory[object + vm.decls["endpoints"].sym.type.elem.field("state").offset], DESTROYED)
            vm.call("taskFinish", selected, 0, False)
            self.assertEqual(vm.memory[object + vm.decls["endpoints"].sym.type.elem.field("references").offset], 0)


if __name__ == "__main__":
    unittest.main()
