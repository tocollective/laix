"""Reject false scheduler acceptance from malformed monitor/image fixtures."""

import io
import struct
import unittest
from unittest.mock import patch

import probe_scheduler_cpu as probe
from test_ready_runner import symbols
from test_task import TaskM, TaskEntered


class MemoryMonitor:
    def __init__(self, vm):
        self.vm = vm

    def words(self, address, count):
        # Monitor dumps pack adjacent byte fields into an aligned word.
        result = []
        for slot in range(address, address + 4 * count, 4):
            word = self.vm.memory[slot]
            for offset in range(1, 4):
                word |= self.vm.memory.get(slot + offset, 0) << (8 * offset)
            result.append(word)
        return result


class SchedulerCPUProbeTests(unittest.TestCase):
    def fixture(self):
        vm = TaskM()
        vm.call("taskCreate")
        vm.call("taskCreate")
        with self.assertRaises(TaskEntered):
            vm.call("taskStart", 1000000)
        p = object.__new__(probe.SchedulerProbe)
        p.m = MemoryMonitor(vm)
        p.s = dict(tasks=vm.addresses["tasks"], idleTask=vm.addresses["idleTask"],
                   currentTask=0xE000000, task__readyHead=0xE000004,
                   task__readyCount=0xE000008, task__readyQueue=vm.addresses["readyQueue"])
        for name, symbol in (("currentTask", "currentTask"), ("readyHead", "task__readyHead"),
                             ("readyCount", "task__readyCount")):
            vm.memory[p.s[symbol]] = vm.globals[name]
        p.size = vm.task_type.size
        p.offsets = {field.name: field.offset for field in vm.task_type.fields}
        vm.memory[vm.field_address("context") + probe.C["TF_R28"]] = probe.DATA + 16
        p.expected = {1: p.context(1)}
        p.cmd = lambda command: "40001010  11111111\n"
        frame = p.expected[1]
        regs = {f"r{i}": frame[i] for i in range(32)}
        regs.update(status=15, pc=probe.CODE, ptbr=vm.ptbr, fcsr=0)
        return vm, p, regs

    def test_restoration_rejects_wrong_gprs_tp_fcsr_epc_ptbr_and_mode(self):
        vm, p, regs = self.fixture()
        p.check_user(regs, 1)
        for name, value in (("r0", 1), ("r1", 99), ("r28", probe.DATA + 32),
                            ("r30", 0), ("r31", 99), ("fcsr", 0x61),
                            ("pc", probe.CODE + 4), ("ptbr", vm.field("ptbr", 2)),
                            ("status", 14), ("status", 11), ("status", 31)):
            with self.subTest(field=name, value=value), self.assertRaises(ValueError):
                p.check_user(dict(regs, **{name: value}), 1)

    def test_selected_context_rejects_stale_tcb_and_low_stack_words(self):
        vm, p, regs = self.fixture()
        for address, value in ((p.s["currentTask"], vm.call("taskGet", 2)),
                               (probe.C["KERNEL_SP"], vm.field("kernelStackTop", 2)),
                               (probe.C["KERNEL_STACK_BOTTOM"], vm.field("kernelStackBottom", 2)),
                               (probe.C["KERNEL_STACK_TOP"], vm.field("kernelStackTop", 2)),
                               (vm.field_address("state"), 4)):
            original = vm.memory[address]
            vm.memory[address] = value
            with self.subTest(address=address), self.assertRaises(ValueError):
                p.check_user(regs, 1)
            vm.memory[address] = original

    def test_queue_rejects_duplicate_blocked_dead_unknown_and_missing_ready(self):
        vm, p, _ = self.fixture()
        p.check_queue(1)
        head = vm.globals["readyHead"]
        first = vm.addresses["readyQueue"] + 4 * head
        for state in (3, 4):
            vm.memory[vm.field_address("state", 2)] = state
            with self.subTest(state=state), self.assertRaises(ValueError):
                p.check_queue(1)
        vm.memory[vm.field_address("state", 2)] = 1
        vm.memory[p.s["task__readyCount"]] = 0
        with self.assertRaises(ValueError):
            p.check_queue(1)
        vm.memory[p.s["task__readyCount"]] = 1
        vm.memory[first] = 7
        with self.assertRaises(ValueError):
            p.check_queue(1)
        vm.memory[first] = 2
        vm.memory[p.s["task__readyCount"]] = 2
        vm.memory[vm.addresses["readyQueue"] + 4 * ((head + 1) % 8)] = 2
        with self.assertRaises(ValueError):
            p.check_queue(1)
        vm.memory[p.s["task__readyCount"]] = 1
        vm.memory[vm.field_address("queued", 2)] = 0
        with self.assertRaises(ValueError):
            p.check_queue(1)

    def test_tls_check_rejects_a_stale_mapping(self):
        _, p, regs = self.fixture()
        p.cmd = lambda command: "40001010  22222222\n"
        with self.assertRaisesRegex(ValueError, "TLS"):
            p.check_user(regs, 1)

    def test_uart_keeps_valid_non_utf8_debug_bytes(self):
        output = io.TextIOWrapper(io.BytesIO(b"\0\xff\n"), encoding="utf-8")
        self.assertEqual(probe.uart_text(output), "\0\\xff\n")

    def test_batched_dumps_keep_words_after_monitor_prompts(self):
        text = ("r0  00000000  r1  00000001\n> 00001FF0  000AF000 000AD000 000AF000\n"
                "> 40001020  22222222\n> machine tick 1, breakpoint\n")
        self.assertEqual(probe.memory_words(text), {0x1FF0: 0xAF000, 0x1FF4: 0xAD000,
                                                   0x1FF8: 0xAF000, 0x40001020: 0x22222222})

    def test_device_dump_distinguishes_pending_and_cleared_expiry(self):
        p = probe.SchedulerProbe.parse_devices
        base = "pic     lines 00000004 enable 00000004\ntimer   count 1000 reload 100 value 100 control 3 expired\n"
        self.assertTrue(p(base)["expired"])
        clear = p(base.replace("00000004 enable", "00000000 enable").replace(" expired", ""))
        self.assertFalse(clear["expired"])
        self.assertEqual(clear["lines"], 0)
        with self.assertRaises(ValueError):
            p("machine is running\n")

    def test_preflight_rejects_old_abi_wrong_header_and_missing_user_blob(self):
        size, offsets = probe.task_layout()
        s = symbols()
        names = ("tasks", "idleTask", "currentTask", "taskStart", "taskCreate", "taskTick",
                 "taskBlock", "taskWake", "taskIdlePoll", "timerInit", "task__readyQueue",
                 "task__readyHead", "task__readyCount", "task__schedulerStarted", "trapEntry.restore",
                 "trapRestoreFrame", "userCodeStart", "userCodeEnd", "taskKernelResume.idle",
                 "trapEmergencyFrame", "kernelRamEnd", "memory__pageBitmap", "trapRegisterSelfTest", "taskKernelSp")
        s.update({name: 0x14000 for name in names})
        s.update(tasks=0x80000, currentTask=0x80000 + 8 * size, idleTask=0x81000,
                 trapEmergencyFrame=0x81000 + size, userCodeStart=0x14000, userCodeEnd=0x14018)
        data = bytearray(0x70000)
        struct.pack_into("<4I", data, 0, 0x424D5257, len(data) // 512, 16, 0)
        busy = s["userCodeEnd"] - 4
        with patch.object(probe, "instruction", side_effect=lambda data, pc: 0 if pc == busy else 7), \
                patch.object(probe, "disassemble", return_value=f"jal r0, 0x{busy:08X}"):
            self.assertEqual(probe.preflight(data, s)[:2], (size, offsets))
            for changes in ({"currentTask": s["currentTask"] + 4}, {"trapEmergencyFrame": s["idleTask"]},
                            {"userCodeEnd": s["userCodeEnd"] - 4}, {"kernelStart": 0x10014}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    probe.preflight(data, dict(s, **changes))
            old = dict(s)
            del old["tasks"]
            old["firstTask"] = 0x80000
            with self.assertRaisesRegex(ValueError, "scheduler symbols"):
                probe.preflight(data, old)
            with self.assertRaisesRegex(ValueError, "image/map"):
                probe.preflight(data[:512], s)
        with self.assertRaisesRegex(ValueError, "WRMB"):
            probe.preflight(b"", s)


if __name__ == "__main__":
    unittest.main()
