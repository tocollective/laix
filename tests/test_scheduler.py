"""Round-robin scheduling from checked M/assembly sources, without building."""

import unittest

from test_task import TaskM, TaskEntered, PAGE, USER_DATA
from source_m import KernelPanic, LAYOUT
from test_trap_entry import EntryMachine


class SchedulerTests(unittest.TestCase):
    def start(self, count=2):
        vm = TaskM()
        for id in range(1, count + 1):
            self.assertEqual(vm.call("taskCreate"), id)
        with self.assertRaises(TaskEntered):
            vm.call("taskStart", 1000000)
        return vm

    def current(self, vm):
        task = vm.globals["currentTask"]
        return vm.memory[task] if task else 0

    def queue(self, vm):
        return [vm.memory[vm.table_base("readyQueue") + 4 *
                          ((vm.globals["readyHead"] + i) % vm.globals["taskCapacity"])]
                for i in range(vm.globals["readyCount"])]

    def check_queue(self, vm):
        queue = self.queue(vm)
        self.assertEqual(len(queue), len(set(queue)))
        for id in range(1, vm.globals["taskCapacity"] + 1):
            ready = vm.field("state", id) == 1
            self.assertEqual(id in queue, ready)
            self.assertEqual(bool(vm.field("queued", id)), ready)
            self.assertEqual(vm.field("state", id) == 2, id == self.current(vm))

    def trap(self, vm, number=2, arg=0, cause=12):
        id = self.current(vm)
        machine = EntryMachine(True, 0, kernel_sp=vm.field("kernelStackTop", id),
            bottom=vm.field("kernelStackBottom", id), top=vm.field("kernelStackTop", id), cause=cause)
        machine.memory, machine.dispatcher = vm.memory, vm.call
        context = vm.field_address("context", id)
        for i in range(1, 32):
            machine.put(f"r{i}", vm.memory[context + i * 4])
        for name in ("epc", "status", "fcsr", "ptbr"):
            machine.control[name] = vm.memory[context + LAYOUT["TF_" + name.upper()]]
        if cause == 12:
            machine.put("r9", number)
            machine.put("r1", arg)
        machine.original = dict(machine.regs)
        machine.initial_control = dict(machine.control)
        machine.run()
        self.assertEqual(machine.stop, "iret")
        return machine

    def test_states_table_capacity_and_queue_wrap(self):
        allowed = {(0, 5), (5, 1), (5, 0), (1, 2), (1, 3), (2, 1),
                   (2, 3), (2, 4), (4, 1), (4, 3), (3, 0)}
        vm = self.start(8)
        slots = 8
        # Every other slot this boot has is taken (dead, reclaimed): the table is full.
        for slot in range(slots + 1, vm.globals["taskCapacity"] + 1):
            vm.memory[vm.field_address("state", slot)] = 3
            vm.memory[vm.field_address("reaped", slot)] = 1
        for previous in range(6):
            for next in range(6):
                self.assertEqual(bool(vm.call("taskTransitionAllowed", previous, next)),
                                 (previous, next) in allowed)
        baseline = vm.free_pages()
        self.assertEqual(vm.call("taskCreate"), 0)
        self.assertEqual(vm.free_pages(), baseline)
        for i in range(32):
            self.assertEqual(self.current(vm), i % slots + 1)
            self.check_queue(vm)
            self.trap(vm)
        with self.assertRaisesRegex(KernelPanic, "ready transition"):
            vm.call("taskEnqueue", vm.call("taskGet", 2))

    def test_round_robin_preserves_distinct_complete_contexts_and_switch_protocol(self):
        vm = self.start()
        for id in (1, 2):
            frame = vm.field_address("context", id)
            self.assertEqual(frame % 8, 0)
            for register in range(1, 32):
                vm.memory[frame + register * 4] = id * 0x100000 + register * 8
            vm.memory[frame + LAYOUT["TF_FCSR"]] = id * 0x20 + 1
            vm.memory[frame + LAYOUT["TF_EPC"]] = 0x40000020 * id
            vm.memory[vm.pages(id)[1]] = id * 111
        for i in range(512):
            outgoing, selected = i % 2 + 1, (i + 1) % 2 + 1
            vm.events.clear()
            machine = self.trap(vm)
            self.assertEqual(self.current(vm), selected)
            self.check_queue(vm)
            frame = vm.field_address("context", outgoing)
            self.assertEqual(vm.memory[frame + LAYOUT["TF_EPC"]], machine.initial_control["epc"] + 4)
            self.assertEqual(vm.memory[frame + LAYOUT["TF_R1"]], 0)
            for reg in range(2, 32):
                self.assertEqual(vm.memory[frame + reg * 4], machine.original[f"r{reg}"])
            target = vm.field_address("context", selected)
            self.assertEqual([machine.get(f"r{n}") for n in range(32)],
                             [vm.memory[target + n * 4] for n in range(32)])
            self.assertEqual(machine.control["fcsr"], vm.memory[target + LAYOUT["TF_FCSR"]])
            self.assertEqual(machine.control["ptbr"], vm.field("ptbr", selected))
            self.assertEqual(vm.ptbr, vm.field("directory", selected) | (selected << 4) | 1)
            events = [event for event in vm.events if event[0] != "fence"]
            self.assertEqual(events, [("tlbi", [0, 2]), ("mtcr", [6, vm.ptbr])])
            for slot, field in (("KERNEL_SP", "kernelStackTop"),
                    ("KERNEL_STACK_BOTTOM", "kernelStackBottom"), ("KERNEL_STACK_TOP", "kernelStackTop")):
                self.assertEqual(vm.memory[LAYOUT[slot]], vm.field(field, selected))
            self.assertEqual(machine.accesses[-1], ("lw", target + LAYOUT["TF_R30"]))
            # Same virtual data address, different physical frames and values.
            for id in (1, 2):
                self.assertEqual(vm.leaf(USER_DATA, vm.field("directory", id)) & ~4095, vm.pages(id)[1])
                self.assertEqual(vm.memory[vm.pages(id)[1]], id * 111)

    def test_single_task_yield_resumes_once_and_advances_epc(self):
        vm = self.start(1)
        for _ in range(16):
            machine = self.trap(vm)
            self.assertEqual(self.current(vm), 1)
            self.assertEqual(machine.control["epc"], machine.initial_control["epc"] + 4)
            self.assertEqual(machine.regs, dict(machine.original, r1=0))
            self.check_queue(vm)

    def test_block_wake_does_not_duplicate_or_run_blocked_tasks(self):
        vm = self.start()
        frame = vm.field_address("context")
        epc = vm.memory[frame + LAYOUT["TF_EPC"]]
        self.assertEqual(vm.call("taskBlock", frame, 17), vm.field_address("context", 2))
        self.assertEqual(vm.field("state"), 4)
        self.assertEqual(vm.field("waitReason"), 17)
        self.check_queue(vm)
        self.trap(vm)
        self.assertEqual(self.current(vm), 2)
        self.assertFalse(vm.call("taskWake", 0))
        self.assertFalse(vm.call("taskWake", 9))
        self.assertTrue(vm.call("taskWake", 1))
        self.assertFalse(vm.call("taskWake", 1))
        self.assertEqual(vm.field("waitReason"), 0)
        self.check_queue(vm)
        self.trap(vm)
        self.assertEqual(self.current(vm), 1)
        self.assertEqual(vm.memory[frame + LAYOUT["TF_EPC"]], epc)

    def test_exit_and_fault_release_only_after_moving_to_selected_stack(self):
        for cause in (12, 9):
            vm = self.start()
            root, pages = vm.field("directory"), vm.pages()
            bottom, top = vm.field("kernelStackBottom"), vm.field("kernelStackTop")
            held = [root] + pages + list(range(bottom - PAGE, top, PAGE))
            frame = vm.field_address("context")
            vm.memory[frame + LAYOUT["TF_CAUSE"]] = cause
            selected = vm.call("taskFinish", frame, 0xFFFFFF85, cause != 12)
            self.assertEqual(selected, vm.field_address("context", 2))
            self.assertEqual(vm.field("state"), 3)
            self.assertEqual(vm.field("exitCode"), 0xFFFFFF85)
            self.assertFalse(vm.call("taskWake", 1))
            self.assertTrue(all(not vm.call("physicalPageAvailable", address) for address in held))
            vm.cpu_sp = top - 160
            with self.assertRaisesRegex(KernelPanic, "cleanup stack"):
                vm.call("taskReap")
            vm.cpu_sp = vm.field("kernelStackTop", 2) - 64
            vm.call("taskReap")
            self.assertTrue(all(vm.call("physicalPageAvailable", address) for address in held))
            self.assertEqual(vm.field("directory"), 0)
            self.assertEqual(vm.field("kernelStackBottom"), 0)
            self.assertEqual(vm.field("reaped"), 1)
            self.assertEqual(vm.leaf(bottom - PAGE) & 31, 7)
            self.assertEqual(vm.field("state", 2), 2)
            self.check_queue(vm)
            self.trap(vm)
            self.assertEqual(self.current(vm), 2)
            # Last exit restores only the trusted supervisor frame; assembly
            # reaps on the idle stack before reading its GPRs, including r10.
            other = vm.pages(2) + [vm.field("directory", 2)]
            machine = self.trap(vm, number=1, arg=123)
            self.assertEqual(self.current(vm), 0)
            self.assertEqual(machine.control["epc"], vm.addresses["taskKernelResume"])
            self.assertEqual(machine.control["ptbr"], vm.kernel_root | 1)
            self.assertTrue(all(vm.call("physicalPageAvailable", address) for address in other))
            self.check_queue(vm)

    def test_guard_and_both_stacks_are_shared_in_all_roots(self):
        vm = self.start()
        for id in (1, 2):
            bottom, top = vm.field("kernelStackBottom", id), vm.field("kernelStackTop", id)
            self.assertEqual(top - bottom, 8192)
            self.assertEqual(vm.memory[bottom], LAYOUT["STACK_CANARY"])
            self.assertEqual(vm.leaf(bottom - PAGE), 0)
            for root in (vm.kernel_root, vm.field("directory"), vm.field("directory", 2)):
                self.assertEqual(vm.leaf(bottom - PAGE, root), 0)
                for page in (bottom, top - PAGE, 0x1000):
                    self.assertEqual(vm.leaf(page, root) & 31, 7)
                self.assertEqual(vm.leaf(0x10000, root) & 31, 11)

    def test_terminal_trap_enters_the_fresh_second_task_and_avoids_the_freed_stack(self):
        for cause in (12, 9):
            with self.subTest(cause=cause):
                vm = self.start()
                bottom, top = vm.field("kernelStackBottom"), vm.field("kernelStackTop")
                held = vm.pages() + [vm.field("directory")] + list(range(bottom - PAGE, top, PAGE))
                machine = self.trap(vm, number=1, arg=123, cause=cause)
                self.assertEqual(self.current(vm), 2)
                expected = [0] * 32
                expected[1], expected[2], expected[3], expected[30] = USER_DATA, PAGE, 2, 0xC0000000
                self.assertEqual([machine.get(f"r{i}") for i in range(32)], expected)
                self.assertEqual(machine.control["epc"], 0x40000000)
                self.assertEqual(machine.control["status"], 26)
                self.assertEqual(machine.control["fcsr"], 0)
                self.assertEqual(vm.field("faulted"), cause != 12)
                self.assertEqual(vm.field("exitCode"), 123 if cause == 12 else cause)
                self.assertTrue(all(vm.call("physicalPageAvailable", page) for page in held))
                moved = max(i for i, access in enumerate(machine.accesses)
                            if access == ("lw", LAYOUT["KERNEL_SP"]))
                self.assertFalse(any(bottom <= address < top for _, address in machine.accesses[moved + 1:]))
                self.check_queue(vm)

    def test_dynamic_stack_can_cross_a_superpage_boundary(self):
        vm = TaskM(ram=0x800000)
        self.assertTrue(vm.call("taskPrepare"))
        task_root = vm.field("directory")
        while vm.globals["nextFreePage"] < 0x3FF:
            self.assertNotEqual(vm.call("allocPage", 9, 2), 0)
        bottom = vm.call("mmuAllocKernelStack", 7)
        self.assertEqual(bottom, 0x400000)
        for root in (vm.kernel_root, task_root):
            self.assertEqual(vm.leaf(bottom - PAGE, root), 0)
            self.assertEqual(vm.leaf(bottom, root) & 31, 7)
            self.assertEqual(vm.leaf(bottom + PAGE, root) & 31, 7)
        self.assertTrue(vm.call("mmuFreeKernelStack", bottom, 7))
        self.assertTrue(all(vm.call("physicalPageAvailable", page)
                            for page in (bottom - PAGE, bottom, bottom + PAGE)))
        self.assertEqual(vm.leaf(bottom - PAGE, task_root) & 31, 7)

    def test_fragmented_stack_oom_restores_the_creation_ledger(self):
        vm = TaskM()
        held = []
        while page := vm.call("allocPage", 9, 2):
            held.append(page)
        for page in held[::2][:12]:
            self.assertTrue(vm.call("freePage", page, 9, 2))
        baseline = vm.free_pages()
        self.assertEqual(vm.call("taskCreate"), 0)
        self.assertEqual(vm.free_pages(), baseline)
        self.assertEqual(vm.pages(), [0, 0, 0])
        self.assertEqual(vm.field("state"), 0)
        self.assertEqual(self.queue(vm), [])


if __name__ == "__main__":
    unittest.main()
