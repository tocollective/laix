"""Execute the actual user assembly and checked IPC/MMU/scheduler source.

No instructions are emitted here. The assembly runner checks each memory
access and suspends at syscalls, while kernel transitions use the M evaluator.
"""

import struct
import unittest

from pathlib import Path

from test_kernel import LAIX, parse_asm, asm_constants, check_m
import asm
import test_ipc_request_reply as service_tests
from test_ipc_handles import error
from test_task import USER_DATA


def request(text=b"", version=1, kind=1, reserved=0, length=None):
    return bytes((version, kind, len(text) if length is None else length, reserved)) + text


def response(status=0, written=0):
    return struct.pack("<IiI", 0x80201, status, written)


class UserAssembly:
    def __init__(self, path, label, regs, memory):
        parser = parse_asm(path)
        self.statements = parser.stmts
        self.constants = asm_constants(parser)
        self.labels = {name: i for i, st in enumerate(parser.stmts) for name in st.labels}
        self.pc = self.labels[label]
        self.regs = regs
        self.memory = memory
        self.reads = []
        self.writes = []

    def run(self):
        def reg(name):
            return {"sp": 30, "ra": 31}[name] if name in ("sp", "ra") else int(name[1:])
        def imm(value):
            return asm.ExprParser(value, self.constants.__getitem__).parse()
        for _ in range(10000):
            st = self.statements[self.pc]
            self.pc += 1
            op, a, r = st.op, st.args, self.regs
            if not op or op.startswith(".") or op == "=":
                continue
            target = lambda name: self.labels[st.scope + name if name.startswith(".") else name]
            if op == "li":
                r[reg(a[0])] = imm(a[1]) & 0xFFFFFFFF
            elif op == "mv":
                r[reg(a[0])] = r[reg(a[1])]
            elif op in ("addi", "add"):
                r[reg(a[0])] = (r[reg(a[1])] + (imm(a[2]) if op == "addi" else r[reg(a[2])])) & 0xFFFFFFFF
            elif op in ("lw", "lbu", "sw", "sb"):
                offset, base = a[1][:-1].split("(")
                address = (r[reg(base)] + imm(offset)) & 0xFFFFFFFF
                count = 4 if op in ("lw", "sw") else 1
                if count == 4:
                    assert address % 4 == 0
                if op in ("lw", "lbu"):
                    self.reads.extend(range(address, address + count))
                    r[reg(a[0])] = sum(self.memory[address + i] << (8 * i) for i in range(count))
                else:
                    self.writes.extend(range(address, address + count))
                    for i in range(count):
                        self.memory[address + i] = (r[reg(a[0])] >> (8 * i)) & 255
            elif op in ("beq", "bne", "bltu", "bgeu"):
                x, y = r[reg(a[0])], r[reg(a[1])]
                if {"beq": x == y, "bne": x != y, "bltu": x < y, "bgeu": x >= y}[op]:
                    self.pc = target(a[2])
            elif op in ("beqz", "bnez", "bltz"):
                x = r[reg(a[0])]
                if {"beqz": x == 0, "bnez": x != 0, "bltz": bool(x & 0x80000000)}[op]:
                    self.pc = target(a[1])
            elif op == "j":
                self.pc = target(a[0])
            elif op == "syscall":
                yield tuple([r[9]] + r[1:6])
            elif op == "ret":
                return
            else:
                raise AssertionError("unhandled user instruction " + op)
        raise AssertionError("user assembly failed to block or return")


class ConsoleServiceTests(unittest.TestCase):
    def server(self, message):
        regs = [0] * 32
        regs[10], regs[11] = 0x102, USER_DATA
        memory = dict(enumerate(message, USER_DATA))
        # Old bytes deliberately remain after a short request. They may never
        # influence parsing or reach UART. Strict reads expose buffer overruns.
        memory.update({USER_DATA + i: 0xCC for i in range(len(message), 44)})
        machine = UserAssembly(LAIX / "tests/programs/boot/uart_bootstrap.asm", "bootstrapServerStart.accept", regs, memory)
        run = machine.run()
        self.assertEqual(next(run)[:4], (22, 0x102, USER_DATA, 32))
        regs[1:3] = [len(message), 0x202]
        text = bytearray()
        while True:
            call = next(run)
            if call[0] == 0:
                text.append(call[1])
                regs[1:3] = [0, 0]
            else:
                self.assertEqual(call[:4], (23, 0x202, USER_DATA + 32, 12))
                result = bytes(memory[USER_DATA + 32 + i] for i in range(12))
                regs[1:3] = [12, 12]
                self.assertEqual(next(run)[:4], (22, 0x102, USER_DATA, 32))
                return bytes(text), result, machine

    def test_valid_zero_maximal_and_control_text_and_exact_counts(self):
        for text in (b"", b"LA/IX microkernel v1.0.0\n", b"A" * 28, b" \t\r\n~"):
            output, result, machine = self.server(request(text))
            self.assertEqual((output, result), (text, response(0, len(text))))
            self.assertTrue(set(machine.reads) <= set(range(USER_DATA, USER_DATA + 4 + len(text))))
            self.assertEqual(machine.writes, list(range(USER_DATA + 32, USER_DATA + 44)))

    def test_invalid_requests_reply_without_reading_outside_message_or_printing_prefix(self):
        cases = [(b"", 22), (b"\x01", 22), (b"\x01\x01\x00", 22),
                 (request(b"x", version=2), 22), (request(b"x", kind=2), 22),
                 (request(b"x", reserved=1), 22), (request(b"x", length=0), 22),
                 (request(b"x", length=2), 22), (request(length=29), 90),
                 (request(length=255), 90), (request(b"prefix\0"), 22),
                 (request(b"prefix\x1b"), 22), (request(b"\x7f"), 22), (request(b"\xff"), 22)]
        for message, errno in cases:
            with self.subTest(message=message):
                output, result, machine = self.server(message)
                self.assertEqual((output, result), (b"", response(-errno)))
                self.assertTrue(set(machine.reads) <= set(range(USER_DATA, USER_DATA + len(message))))

    def helper(self, text, result=None, transport=12, length=None):
        regs = [0xABC00000 + i for i in range(32)]
        regs[0] = 0
        regs[1:4] = [0x301, USER_DATA, len(text) if length is None else length]
        regs[30] = 0x50001000
        before = regs[:]
        memory = dict(enumerate(text, USER_DATA))
        machine = UserAssembly(LAIX / "user/console.asm", "consoleWrite", regs, memory)
        run = machine.run()
        calls = []
        for call in run:
            calls.append(call)
            self.assertEqual(call[:4], (21, 0x301, before[30] - 64, len(text) + 4))
            self.assertEqual(call[4:], (before[30] - 32, 12))
            wire = bytes(memory[call[2] + i] for i in range(call[3]))
            self.assertEqual(wire, request(text))
            if result is not None:
                memory.update(dict(enumerate(result, call[4])))
            regs[1:3] = [transport, 0]
        self.assertEqual(regs[10:32], before[10:32])
        return regs[1], calls, machine

    def test_client_uses_call_private_stack_and_preserves_m_abi(self):
        check_m(LAIX / "user/console.m")
        for text in (b"", b"hello", b"A" * 28):
            result, calls, _ = self.helper(text, response(0, len(text)))
            self.assertEqual(result, len(text))
            self.assertEqual(len(calls), 1)
        self.assertEqual(self.helper(b"x", response(-22))[0], error(22))
        result, _, machine = self.helper(b"x", transport=error(32))
        self.assertEqual(result, error(32))
        self.assertFalse(set(machine.reads) & set(range(0x50001000 - 32, 0x50001000 - 20)))

    def test_client_rejects_oversize_before_reads_and_malformed_responses(self):
        result, calls, machine = self.helper(b"", length=0xFFFFFFFF)
        self.assertEqual(result, error(90))
        self.assertEqual((calls, machine.reads, machine.writes), ([], [], []))
        for wire, delivered in ((response(), 4), (struct.pack("<IiI", 0, 0, 1), 12),
                                (response(1, 1), 12), (response(0, 0), 12),
                                (response(-22, 2), 12)):
            self.assertEqual(self.helper(b"x", wire, delivered)[0], error(71))

    def test_multiple_clients_fifo_whole_messages_and_replies_under_timer_rotations(self):
        vm, tokens, endpoint = service_tests.RequestReplyTests().fixture(4)
        expected = [(3, b"third\n"), (2, b"second\n"), (4, b"fourth\n")]
        for id, text in expected:
            vm.run(id)
            vm.request(tokens[id], request(text), response=USER_DATA + 128, capacity=12)
        self.assertEqual(vm.queue(endpoint), [id for id, _ in expected])
        vm.run(1)
        output = bytearray()
        for id, text in expected:
            token = vm.accept(tokens[1])
            payload = vm.read_bytes(vm.pages(1)[1], len(text) + 4)
            printed, wire, _ = self.server(payload)
            output.extend(printed)
            # Interrupts rotate the server before it finishes its one reply.
            frame = vm.field_address("context", 1)
            vm.call("taskTick", frame)
            vm.run(1)
            vm.response(token, wire)
            self.assertEqual(vm.result(id), (12, 12))
            self.assertEqual(vm.read_bytes(vm.pages(id)[1] + 128, 12), response(0, len(text)))
        self.assertEqual(output, b"third\nsecond\nfourth\n")
        vm.accept(tokens[1])
        self.assertEqual(vm.field("state", 1), 4)
        self.assertEqual(vm.field("waitReason", 1), 5)

    def test_kernel_main_has_no_console_disk_or_demo_dependency(self):
        modules = check_m(LAIX / "tests/programs/boot/uart_main.m")
        self.assertNotIn("console.m", {Path(m.path).name for m in modules})
        text = (LAIX / "tests/programs/boot/uart_main.m").read_text()
        self.assertNotIn("microkernel v1.0.0", text)
        panic = check_m(LAIX / "src/kernel/panic.m")
        self.assertEqual({Path(m.path).name for m in panic},
                         {"panic.m", "trap_frame.m", "debug_uart.m", "defs.m"})


if __name__ == "__main__":
    unittest.main()
