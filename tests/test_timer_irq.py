"""Timer/PIC level semantics and checked-source scheduling; no generated code."""

import unittest

from test_kernel import LAIX, parse_asm
from test_task import TaskM, TaskEntered
import test_scheduler
from test_trap_entry import EntryMachine
from source_m import KernelPanic, LAYOUT as C


class TimerMemory(dict):
    """Device contract fixture: CLAIM never clears a line; STATUS is W1C."""

    def __init__(self, vm, frequency=1000000):
        super().__init__(vm.memory)
        self.vm = vm
        self.frequency = frequency
        self.lines = 0
        self.expired = False
        self.value = 0
        self.writes = []
        self.claim_reads = 0

    def __getitem__(self, address):
        if address == C["TIMER_FREQUENCY"]:
            return self.frequency
        if address == C["TIMER_STATUS"]:
            return int(self.expired)
        if address == C["PIC_CLAIM"]:
            self.claim_reads += 1
            active = self.lines & super().__getitem__(C["PIC_ENABLE"])
            return (active & -active).bit_length() - 1 if active else C["PIC_NO_IRQ"]
        return super().__getitem__(address)

    def __setitem__(self, address, value):
        if address in (C["TIMER_STATUS"], C["TIMER_CONTROL"],
                       C["TIMER_RELOAD"], C["PIC_ENABLE"]):
            self.writes.append((address, value, self.vm.controls[0]))
        if address == C["TIMER_STATUS"]:
            if value & 1:
                self.expired = False
                self.lines &= ~C["TIMER_IRQ_MASK"]
            return
        if address == C["TIMER_CONTROL"]:
            self.value = self.get(C["TIMER_RELOAD"], 0)
        super().__setitem__(address, value)

    def advance(self, ticks):
        if not self.get(C["TIMER_CONTROL"], 0) & C["TIMER_ENABLE"]:
            return
        if ticks < self.value:
            self.value -= ticks
            return
        self.expired = True
        self.lines |= C["TIMER_IRQ_MASK"]
        period = self[C["TIMER_RELOAD"]] or 1
        self.value = period - (ticks - self.value) % period

    def active(self):
        return self.lines & self[C["PIC_ENABLE"]]


class TimerIRQTests(unittest.TestCase):
    current = test_scheduler.SchedulerTests.current
    queue = test_scheduler.SchedulerTests.queue
    check_queue = test_scheduler.SchedulerTests.check_queue
    trap = test_scheduler.SchedulerTests.trap

    def vm(self, count=2, frequency=1000000, start=True):
        vm = TaskM()
        vm.memory = TimerMemory(vm, frequency)
        vm.memory[vm.addresses["kernelStackBottom"]] = C["STACK_CANARY"]
        for id in range(1, count + 1):
            self.assertEqual(vm.call("taskCreate"), id)
        if start:
            with self.assertRaises(TaskEntered):
                vm.call("taskStart", 1000000)
        return vm

    def idle_irq(self, vm):
        machine = EntryMachine(False, vm.memory[C["KERNEL_STACK_TOP"]], cause=0)
        machine.memory, machine.dispatcher = vm.memory, vm.call
        machine.control.update(ptbr=vm.kernel_root | 1, status=18)
        machine.initial_control = dict(machine.control)
        machine.run()
        return machine

    def test_frequency_fallback_validation_and_activation_order(self):
        for frequency, clock, hz, period in ((1234567, 999, 100, 12345),
                (0, 750000, 100, 7500), (0xFFFFFFFF, 0, 1, 0xFFFFFFFF),
                (100, 999, 100, 1)):
            vm = self.vm(start=False, frequency=frequency)
            vm.memory.expired = True
            vm.memory.lines = 4  # stale firmware timer request
            self.assertTrue(vm.call("timerInit", clock, hz))
            self.assertEqual([(a, v) for a, v, _ in vm.memory.writes], [
                (C["TIMER_CONTROL"], 0), (C["TIMER_STATUS"], 1),
                (C["TIMER_RELOAD"], period), (C["TIMER_CONTROL"], 3),
                (C["PIC_ENABLE"], 4)])
            self.assertTrue(all(status & 1 == 0 for _, _, status in vm.memory.writes))
            self.assertFalse(vm.memory.active())
            self.assertFalse(vm.call("timerInit", clock, hz))
        for frequency, clock, hz in ((0, 0, 100), (100, 100, 0), (99, 100, 100)):
            vm = self.vm(start=False, frequency=frequency)
            self.assertFalse(vm.call("timerInit", clock, hz))
            self.assertFalse(vm.memory.writes)
            self.assertFalse(vm.globals["timerReady"])
        vm = self.vm(start=False)
        vm.controls[0] = 1
        self.assertFalse(vm.call("timerInit", 1000000, 100))
        vm.controls[0] = 0
        vm.memory[C["PIC_ENABLE"]] = 1
        self.assertFalse(vm.call("timerInit", 1000000, 100))

    def test_timer_preempts_without_yield_and_preserves_epc_all_gprs_fcsr(self):
        vm = self.vm()
        for id in (1, 2):
            frame = vm.field_address("context", id)
            for register in range(1, 32):
                vm.memory[frame + 4 * register] = id * 0x100000 + register * 8
            vm.memory[frame + C["TF_EPC"]] = 0x40000020 * id
            vm.memory[frame + C["TF_FCSR"]] = id * 0x20 + 1
        for i in range(128):
            outgoing, selected = i % 2 + 1, (i + 1) % 2 + 1
            vm.memory.advance(vm.memory.value)
            self.assertEqual(vm.memory[C["PIC_CLAIM"]], 2)
            self.assertEqual(vm.memory.active(), 4)  # CLAIM did not acknowledge
            machine = self.trap(vm, cause=0)
            self.assertEqual(self.current(vm), selected)
            self.assertFalse(vm.memory.active())
            self.assertFalse(vm.memory.expired)
            frame = vm.field_address("context", outgoing)
            self.assertEqual(vm.memory[frame + C["TF_EPC"]], machine.initial_control["epc"])
            self.assertEqual([vm.memory[frame + n * 4] for n in range(32)],
                             [machine.original[f"r{n}"] for n in range(32)])
            self.assertEqual(vm.memory[frame + C["TF_FCSR"]], machine.initial_control["fcsr"])
            self.assertEqual(machine.control["status"] & 18, 18)  # EXL held until IRET
            self.assertTrue(all(status & 16 for address, _, status in vm.memory.writes[5:]
                                if address == C["TIMER_STATUS"]))
            self.check_queue(vm)
            vm.memory.advance(vm.memory.value - 1)
            self.assertFalse(vm.memory.active())
        self.assertEqual(vm.memory.claim_reads, 256)

    def test_single_task_irq_does_not_consume_an_instruction_or_change_results(self):
        vm = self.vm(count=1)
        vm.memory.advance(vm.memory.value * 5)  # EXPIRED coalesces missed periods
        machine = self.trap(vm, cause=0)
        self.assertEqual(self.current(vm), 1)
        self.assertEqual(machine.regs, machine.original)
        self.assertEqual(machine.control["epc"], machine.initial_control["epc"])
        self.assertEqual(vm.field("state"), 2)
        self.assertFalse(vm.field("faulted"))
        self.assertFalse(vm.memory.active())
        self.check_queue(vm)

    def test_unexpected_asserted_lines_are_masked_before_return_and_timer_survives(self):
        vm = self.vm()
        vm.memory.lines |= 1 << 1
        vm.memory[C["PIC_ENABLE"]] |= 1 << 1
        vm.memory.advance(vm.memory.value)
        first = self.trap(vm, cause=0)
        self.assertEqual(self.current(vm), 1)
        self.assertEqual(first.control["epc"], first.initial_control["epc"])
        self.assertEqual(vm.memory.lines & 2, 2)  # device still asserted, but masked
        self.assertEqual(vm.memory.active(), 4)
        self.assertIn((b"LA/IX: masked unexpected IRQ $u\n", 1), vm.output)
        self.trap(vm, cause=0)
        self.assertEqual(self.current(vm), 2)
        self.assertFalse(vm.memory.active())
        # Empty claim does not enter exception handling or kill the user task.
        self.trap(vm, cause=0)
        self.assertEqual(self.current(vm), 2)
        self.assertEqual(vm.field("state", 2), 2)
        # Broken timer request without EXPIRED is also masked, not retried forever.
        vm.memory.lines |= 4
        self.trap(vm, cause=0)
        self.assertFalse(vm.memory.active())
        self.assertIn((b"LA/IX: masked unexpected IRQ $u\n", 2), vm.output)

    def test_idle_tick_wakeup_and_terminal_ordering(self):
        vm = self.vm(count=1)
        vm.call("taskBlock", vm.field_address("context"), 17)
        vm.memory.advance(vm.memory.value)
        machine = self.idle_irq(vm)
        self.assertEqual(self.current(vm), 0)
        self.assertEqual(machine.control["epc"], machine.initial_control["epc"])
        self.assertEqual(machine.control["status"], 18)
        vm.controls[0] = 1  # real idle: supervisor IE=1, EXL=0
        self.assertTrue(vm.call("taskWake", 1))
        self.assertEqual(vm.controls[0], 1)  # wake preserves the caller's STATUS
        self.assertFalse(vm.call("taskWake", 1))
        vm.memory.advance(vm.memory.value)
        machine = self.idle_irq(vm)
        self.assertEqual(self.current(vm), 1)
        self.assertEqual(machine.control["status"], 26)
        # A pending expiry cannot resurrect an exited task.
        vm.memory.advance(vm.memory.value)
        self.trap(vm, number=1)
        self.assertEqual(vm.field("state"), 3)
        self.assertTrue(vm.field("reaped"))
        self.assertFalse(vm.call("taskWake", 1))
        self.idle_irq(vm)
        self.assertEqual(self.current(vm), 0)
        self.assertFalse(vm.memory.active())
        self.check_queue(vm)
        parser = parse_asm(LAIX / "src/task/task.asm")
        begin = next(i for i, st in enumerate(parser.stmts) if "taskKernelResume" in st.labels)
        end = next(i for i, st in enumerate(parser.stmts) if "userCodeStart" in st.labels)
        # Sleep path (WFI, then an IE window), the staged-reap branch (an IE
        # window without sleeping, G5) and the dispatch jump.
        self.assertEqual([st.op for st in parser.stmts[begin:end] if st.op],
                         ["mtcr", "call", "bnez", "fence", "wfi", "li", "mtcr", "j",
                          "addi", "bnez", "li", "mtcr", "j", "j"])

    def test_timer_in_active_supervisor_task_is_rejected(self):
        vm = self.vm(count=1)
        vm.memory.advance(vm.memory.value)
        with self.assertRaisesRegex(KernelPanic, "outside supervisor idle"):
            self.idle_irq(vm)
        self.assertFalse(vm.memory.active())


if __name__ == "__main__":
    unittest.main()
