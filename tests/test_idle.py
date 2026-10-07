"""Idle queue/WFI boundaries from source; no assembly, linking or CPU image."""

import unittest

from test_kernel import LAIX, parse_asm, asm_constants
from test_task import TaskM, TaskEntered
from test_timer_irq import TimerMemory
from test_trap_entry import EntryMachine, CODE_BASE
from source_m import KernelPanic, LAYOUT as C


class IdleTaskM(TaskM):
    wake_on_irq = False

    def call(self, name, *args):
        # A future event handler publishes Ready with EXL held, before selection.
        if name == "trapDispatch" and self.wake_on_irq:
            self.call("taskWake", 1)
        return super().call(name, *args)


class IdleMachine(EntryMachine):
    """Interpret the idle assembly with level IRQ/WFI and IRET fixtures."""

    def __init__(self, vm, wake_on_irq=False):
        super().__init__(False, vm.idle_field("kernelStackTop"), cause=0)
        self.vm = vm
        self.parser = parse_asm(LAIX / "src/task/task.asm")
        self.code = self.parser.stmts
        self.constants = asm_constants(self.parser)
        self.labels = {label: i for i, st in enumerate(self.code) for label in st.labels}
        self.pc = self.labels["taskKernelResume"]
        self.memory = vm.memory
        self.dispatcher = vm.call
        self.control.update(status=0, ptbr=vm.ptbr)
        self.waiting = False
        vm.wake_on_irq = wake_on_irq
        self.irq_count = 0
        self.sleeps = 0

    def restore(self, machine):
        status = machine.control["status"]
        machine.control["status"] = ((status & ~(1 | 4 | 16 | 32)) |
                                      ((status & 2) >> 1) | ((status & 8) >> 1) |
                                      ((status & 64) >> 1))
        self.regs, self.control = machine.regs, machine.control
        self.vm.controls[0] = self.control["status"]
        self.pc = (self.control["epc"] - CODE_BASE) // 4

    def interrupt(self):
        status = self.control["status"]
        machine = EntryMachine(False, self.get("sp"), cause=0)
        machine.memory, machine.dispatcher = self.memory, self.dispatcher
        machine.regs = dict(self.regs)
        machine.control = dict(self.control, cause=0, epc=CODE_BASE + 4 * self.pc,
                               status=(status & ~(1 | 2 | 4 | 8 | 32 | 64)) | 16 |
                               ((status & 1) << 1) | ((status & 4) << 1) |
                               ((status & 32) << 1))
        machine.run()
        self.irq_count += 1
        self.restore(machine)

    def run(self, inject=None):
        self.stop = None
        for _ in range(200):
            st = self.code[self.pc]
            if inject:
                inject(self, st)
            if self.waiting:
                if not self.memory.active():
                    self.stop = "sleep"
                    return
                self.waiting = False
            if self.control["status"] & 17 == 1 and self.memory.active():
                self.interrupt()
                if self.vm.globals["currentTask"] != self.vm.addresses["idleTask"]:
                    self.stop = "dispatch"
                    return
                continue
            self.pc += 1
            op, a = st.op, st.args
            if op is None:
                continue
            if op == "mtcr":
                self.control[a[0]] = self.get(a[1])
                self.vm.controls[0] = self.control["status"]
            elif op == "call":
                assert a == ["taskIdlePoll"]
                self.vm.cpu_sp = self.get("sp")
                self.put("r1", self.vm.call("taskIdlePoll"))
                self.control["ptbr"] = self.vm.ptbr
            elif op == "li":
                self.put(a[0], self.constant(a[1]))
            elif op == "addi":
                self.put(a[0], (self.get(a[1]) + self.constant(a[2])) & 0xFFFFFFFF)
            elif op == "bnez":
                if self.get(a[0]):
                    self.pc = self.labels[self.parser.qualify(a[1], st.scope)]
            elif op == "fence":
                pass
            elif op == "wfi":
                assert self.control["status"] == 0
                self.waiting = True
                self.sleeps += 1
            elif op == "j" and a == ["trapRestoreFrame"]:
                machine = EntryMachine(False, self.get("sp"))
                machine.memory = self.memory
                machine.control["ptbr"] = self.vm.ptbr
                machine.pc = machine.labels["trapRestoreFrame"]
                machine.put("r1", self.get("r1"))
                machine.run()
                self.restore(machine)
                self.stop = "dispatch"
                return
            elif op == "j":
                self.pc = self.labels[self.parser.qualify(a[0], st.scope)]
            else:
                raise AssertionError(f"unexpected idle instruction: {op}")
        raise AssertionError("idle did not sleep or dispatch")


class IdleTests(unittest.TestCase):
    def idle(self):
        vm = IdleTaskM()
        vm.memory = TimerMemory(vm)
        self.assertEqual(vm.call("taskCreate"), 1)
        with self.assertRaises(TaskEntered):
            vm.call("taskStart", 1000000)
        frame = vm.call("taskBlock", vm.field_address("context"), 17)
        self.assertEqual(frame, vm.idle_address("context"))
        vm.controls[0] = 0
        return vm

    def test_idle_has_a_private_guarded_stack_and_never_enters_user_queue(self):
        vm = self.idle()
        self.assertEqual(vm.globals["currentTask"], vm.addresses["idleTask"])
        self.assertEqual(vm.idle_field("id"), 0)
        self.assertEqual(vm.idle_field("state"), 2)
        self.assertFalse(vm.idle_field("queued"))
        self.assertEqual(vm.globals["readyCount"], 0)
        self.assertEqual(vm.call("taskGet", 0), 0)
        with self.assertRaisesRegex(KernelPanic, "ready transition"):
            vm.call("taskEnqueue", vm.addresses["idleTask"])
        bottom, top = vm.idle_field("kernelStackBottom"), vm.idle_field("kernelStackTop")
        self.assertEqual(top - bottom, C["KERNEL_STACK_BYTES"])
        self.assertNotEqual(top, vm.addresses["kernelStackTop"])
        self.assertEqual(vm.memory[bottom], C["STACK_CANARY"])
        for root in (vm.kernel_root, vm.field("directory")):
            self.assertEqual(vm.leaf(bottom - 4096, root), 0)
            for page in (bottom, top - 4096):
                self.assertEqual(vm.leaf(page, root) & 31, 7)  # supervisor RW/NX
                self.assertTrue(vm.call("physicalPageOwned", page, 9, 7))
        for slot, field in (("KERNEL_SP", "kernelStackTop"),
                            ("KERNEL_STACK_TOP", "kernelStackTop"),
                            ("KERNEL_STACK_BOTTOM", "kernelStackBottom")):
            self.assertEqual(vm.memory[C[slot]], vm.idle_field(field))

    def test_ready_before_idle_check_dispatches_without_sleep_or_timer_tick(self):
        vm = self.idle()
        self.assertTrue(vm.call("taskWake", 1))
        machine = IdleMachine(vm)
        machine.run()
        self.assertEqual(machine.stop, "dispatch")
        self.assertEqual(machine.sleeps, 0)
        self.assertEqual(machine.irq_count, 0)
        self.assertEqual(machine.control["epc"], 0x40000000)
        self.assertEqual(machine.control["status"] & 5, 5)
        self.assertEqual(vm.idle_field("state"), 1)

    def test_pending_irq_at_every_queue_to_wfi_boundary_is_not_lost(self):
        for boundary in ("call", "bnez", "fence", "wfi", "li", "enable"):
            with self.subTest(boundary=boundary):
                vm = self.idle()
                machine = IdleMachine(vm, wake_on_irq=True)
                injected = False

                def inject(cpu, statement):
                    nonlocal injected
                    if boundary == "enable" and statement.op == "li":
                        vm.memory.advance(vm.memory.value)  # release the masked WFI first
                    match = (statement.op == boundary or boundary == "enable" and
                             statement.op == "mtcr" and statement.args == ["status", "r1"])
                    if not injected and match:
                        self.assertEqual(cpu.control["status"], 0)
                        vm.memory.advance(vm.memory.value)
                        injected = True

                machine.run(inject)
                self.assertTrue(injected)
                self.assertEqual(machine.stop, "dispatch")
                self.assertEqual(machine.irq_count, 1)
                self.assertFalse(vm.memory.active())
                self.assertEqual(vm.globals["currentTask"], vm.call("taskGet", 1))
                self.assertEqual(vm.globals["readyCount"], 0)
                self.assertEqual(machine.get("sp"), 0xC0000000)

    def test_sleeping_idle_wakes_and_empty_irq_returns_to_check_and_wfi(self):
        vm = self.idle()
        machine = IdleMachine(vm)
        machine.run()
        self.assertEqual(machine.stop, "sleep")
        self.assertEqual(machine.control["status"], 0)
        vm.memory.advance(vm.memory.value)
        machine.run()
        self.assertEqual(machine.stop, "sleep")
        self.assertEqual(machine.irq_count, 1)
        self.assertEqual(machine.sleeps, 2)
        self.assertFalse(vm.memory.active())
        vm.wake_on_irq = True
        vm.memory.advance(vm.memory.value)
        machine.run()
        self.assertEqual(machine.stop, "dispatch")
        self.assertEqual(machine.irq_count, 2)
        self.assertFalse(vm.memory.active())

    def test_idle_refuses_to_sleep_without_a_configured_periodic_irq(self):
        for register, value in (("PIC_ENABLE", 0), ("PIC_ENABLE", 1),
                                ("TIMER_CONTROL", 0), ("TIMER_CONTROL", 1),
                                ("TIMER_CONTROL", 2), ("TIMER_RELOAD", 0)):
            with self.subTest(register=register, value=value):
                vm = self.idle()
                vm.memory[C[register]] = value
                with self.assertRaisesRegex(KernelPanic, "without wakeup IRQ"):
                    IdleMachine(vm).run()
        vm = self.idle()
        vm.globals["timerReady"] = False
        with self.assertRaisesRegex(KernelPanic, "without wakeup IRQ"):
            vm.call("taskIdlePoll")

    def test_idle_check_requires_the_idle_context_and_cpu_irqs_off(self):
        for status in (1, 4, 16, 17):
            vm = self.idle()
            vm.controls[0] = status
            with self.assertRaisesRegex(KernelPanic, "invalid idle context"):
                vm.call("taskIdlePoll")
        vm = self.idle()
        vm.globals["currentTask"] = vm.call("taskGet", 1)
        with self.assertRaisesRegex(KernelPanic, "invalid idle context"):
            vm.call("taskIdlePoll")

    def test_idle_stack_oom_never_enables_an_irq_or_starts_scheduler(self):
        vm = TaskM()
        self.assertEqual(vm.call("taskCreate"), 1)
        while vm.call("allocPage", 10, 2):
            pass
        with self.assertRaisesRegex(KernelPanic, "allocate idle stack"):
            vm.call("taskStart", 1000000)
        self.assertFalse(vm.globals["schedulerStarted"])
        self.assertFalse(vm.globals["timerReady"])
        self.assertEqual(vm.memory[C["PIC_ENABLE"]], 0)
        self.assertEqual(vm.controls[0], 0)


if __name__ == "__main__":
    unittest.main()
