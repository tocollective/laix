"""Run the checked request/reply, scheduler and MMU ASTs; never build code."""

import unittest
from collections import Counter

from source_m import KernelPanic, LAYOUT
from test_ipc_transport import TransportM
from test_ipc_handles import error
from test_task import TaskEntered, USER_CODE, USER_DATA, PAGE

CALL, ACCEPT, REPLY = 21, 22, 23
AWAIT_ACCEPT, ACCEPT_WAIT, AWAIT_REPLY = 4, 5, 6
GEN_MAX = LAYOUT['TASK_GENERATION_MAX']  # reply namespaces share the task reference layout


class ServiceM(TransportM):
    def invoke(self, number, *args):
        frame = self.field_address("context", self.current())
        self.memory[frame + LAYOUT["TF_R9"]] = number
        for i, value in enumerate(args, 1):
            self.memory[frame + LAYOUT["TF_R" + str(i)]] = value
        return self.call("userSyscall", frame)

    def run(self, id):
        for _ in range(8):
            if self.current() == id:
                return
            assert self.current() and self.field("state", id) == 1
            self.call("taskYield", self.field_address("context", self.current()))
        raise AssertionError("task did not become current")

    def request(self, token, data=b"request", response=USER_DATA, capacity=32):
        self.seed(self.pages(self.current())[1], data)
        return self.invoke(CALL, token, USER_DATA, len(data), response, capacity)

    def accept(self, token, capacity=32, buffer=USER_DATA):
        self.invoke(ACCEPT, token, buffer, capacity)
        return self.result(self.current())[1]

    def response(self, token, data=b"response"):
        self.seed(self.pages(self.current())[1], data)
        return self.invoke(REPLY, token, USER_DATA, len(data))


class RequestReplyTests(unittest.TestCase):
    def fixture(self, count=3):
        vm = ServiceM()
        for id in range(1, count + 1):
            self.assertEqual(vm.call("taskCreate"), id)
        token = vm.call("endpointBootstrapService", vm.field_address("handles", 1), 1)
        tokens = {1: token}
        for id in range(2, count + 1):
            tokens[id] = vm.call("handleCopy", vm.field_address("handles", 1), token,
                                 vm.field_address("handles", id), 1, id, 1)
        endpoint = vm.endpoint(token)
        with self.assertRaises(TaskEntered):
            vm.call("taskStart", 1000000)
        return vm, tokens, endpoint

    def accepted(self, vm, tokens, client=2, **kwargs):
        vm.run(client)
        vm.request(tokens[client], **kwargs)
        vm.run(1)
        return vm.accept(tokens[1])

    def assert_cleared(self, vm, id):
        for name in ("ipcEndpoint", "ipcKind", "ipcBuffer", "ipcSize", "ipcObjectGeneration",
                     "ipcReplyOwner", "ipcReplyBuffer", "ipcReplyCapacity"):
            self.assertEqual(vm.field(name, id), 0, name)
        base = vm.field_address("ipcMessage", id)
        self.assertEqual([vm.memory[base + i] for i in range(32)], [0] * 32)

    def test_two_clients_fifo_accept_reverse_reply_and_foreign_duplicate_rejection(self):
        vm, tokens, endpoint = self.fixture(4)
        for id, message in ((2, b"second"), (3, b"third")):
            vm.run(id)
            vm.request(tokens[id], message)
        self.assertEqual(vm.queue(endpoint), [2, 3])
        vm.run(4)
        before = vm.read_bytes(vm.pages(2)[1], 32)
        vm.invoke(REPLY, 0x1002, USER_DATA, 1)  # not accepted yet
        self.assertEqual(vm.result(4), (error(9), 0))
        vm.run(1)
        a = vm.accept(tokens[1])
        self.assertEqual((vm.result(1), vm.read_bytes(vm.pages(1)[1], 6)), ((6, 0x1002), b"second"))
        b = vm.accept(tokens[1])
        self.assertEqual((vm.result(1), vm.read_bytes(vm.pages(1)[1], 5)), ((5, 0x1003), b"third"))
        self.assertEqual(vm.wakes, Counter())
        self.assertEqual(vm.object_value(endpoint, "references"), 6)
        self.assertFalse(vm.call("taskWake", 2))
        vm.wakes.clear()
        vm.run(4)
        for bad in (a, b, 0, 1, a + 4096, a | 0x80000000, 0x1009, 0xFFFFFFFF):
            vm.invoke(REPLY, bad, USER_DATA, 1)
            self.assertEqual(vm.result(4), (error(9), 0))
        self.assertEqual(vm.read_bytes(vm.pages(2)[1], 32), before)
        vm.run(1)
        for token, client, message in ((b, 3, b"reply3"), (a, 2, b"reply2")):
            vm.response(token, message)
            self.assertEqual(vm.result(client), (6, 6))
            self.assertEqual(vm.read_bytes(vm.pages(client)[1], 6), message)
            self.assert_cleared(vm, client)
            vm.response(token)
            self.assertEqual(vm.result(1), (error(9), 0))
        self.assertEqual(vm.wakes, Counter({2: 1, 3: 1}))
        self.assertEqual(vm.object_value(endpoint, "references"), 4)

    def test_accept_first_keeps_client_blocked_and_advances_epc_once(self):
        vm, tokens, endpoint = self.fixture(2)
        frame = vm.field_address("context", 1)
        vm.invoke(ACCEPT, tokens[1], USER_DATA, 32)
        self.assertEqual(vm.current(), 2)
        self.assertEqual(vm.field("ipcKind", 1), ACCEPT_WAIT)
        client_frame = vm.field_address("context", 2)
        epc = vm.memory[client_frame + LAYOUT["TF_EPC"]]
        for i in range(6, 32):
            vm.memory[client_frame + 4 * i] = 0xABCD0000 + i
        vm.memory[client_frame + LAYOUT["TF_FCSR"]] = 0x61
        vm.request(tokens[2], b"hello")
        self.assertEqual(vm.current(), 1)
        self.assertEqual(vm.result(1), (5, 0x1002))
        self.assertEqual(vm.field("ipcKind", 2), AWAIT_REPLY)
        self.assertEqual(vm.field("state", 2), 4)
        self.assertEqual(vm.queue(endpoint), [])
        self.assertEqual(vm.wakes, Counter({1: 1}))
        vm.response(0x1002, b"world")
        self.assertEqual(vm.memory[client_frame + LAYOUT["TF_EPC"]], epc + 4)
        self.assertEqual([vm.memory[client_frame + 4 * i] for i in range(3, 6)], [5, USER_DATA, 32])
        self.assertEqual([vm.memory[client_frame + 4 * i] for i in range(6, 32)],
                         [CALL if i == 9 else 0xABCD0000 + i for i in range(6, 32)])
        self.assertEqual(vm.memory[client_frame + LAYOUT["TF_FCSR"]], 0x61)
        self.assertEqual(vm.wakes, Counter({1: 1, 2: 1}))
        self.assertEqual(vm.memory[frame + LAYOUT["TF_EPC"]], USER_CODE + 8)

    def test_failed_admission_self_call_authority_modes_and_full_ranges(self):
        vm, tokens, endpoint = self.fixture()
        for size in (0, 1):
            vm.invoke(CALL, tokens[1], 0xFFFFFFFF, size, 0xFFFFFFFF, 0)
            self.assertEqual(vm.result(1), (error(35), 0))
        self.assertEqual(vm.call("handleCopy", vm.field_address("handles", 1), tokens[1],
                                 vm.field_address("handles", 2), 1, 2, 2), error(1))
        for number in (19, 20):
            vm.invoke(number, tokens[1], USER_DATA, 32)
            self.assertEqual(vm.result(1), (error(22), 0))
        raw = vm.call("endpointBootstrap", vm.field_address("handles", 1), 1)
        self.assertEqual(raw, error(1))  # root issuance is sealed
        vm.run(2)
        for token, source, size, dest, capacity, errno in (
                (tokens[2] + 256, USER_DATA, 1, USER_DATA, 32, 9),
                (tokens[2], USER_DATA, 33, USER_DATA, 32, 90),
                (tokens[2], 0xFFFFFFFE, 4, USER_DATA, 32, 14),
                (tokens[2], USER_DATA + PAGE - 1, 2, USER_DATA, 32, 14),
                (tokens[2], USER_DATA, 1, USER_CODE, 32, 14),
                (tokens[2], USER_DATA, 1, USER_DATA, PAGE + 1, 90)):
            vm.invoke(CALL, token, source, size, dest, capacity)
            self.assertEqual(vm.result(2), (error(errno), 0))
            self.assertEqual(vm.queue(endpoint), [])
            self.assertEqual(vm.object_value(endpoint, "references"), 3)
            self.assertEqual(vm.field("ipcCallGeneration", 2), 0)
            self.assert_cleared(vm, 2)
        vm.invoke(ACCEPT, tokens[2], USER_DATA, 32)
        self.assertEqual(vm.result(2), (error(1), 0))
        vm.controls[0] = 1
        with self.assertRaisesRegex(KernelPanic, "IRQs enabled"):
            vm.invoke(CALL, tokens[2], USER_DATA, 1, USER_DATA, 32)

    def test_generation_exhaustion_and_old_token_cannot_reply_to_next_call(self):
        vm, tokens, endpoint = self.fixture(2)
        old = self.accepted(vm, tokens)
        vm.response(old)
        vm.run(2)
        vm.request(tokens[2])
        vm.run(1)
        new = vm.accept(tokens[1])
        self.assertNotEqual(old, new)
        vm.response(old)
        self.assertEqual(vm.result(1), (error(9), 0))
        self.assertEqual(vm.field("state", 2), 4)
        vm.response(new)
        vm.memory[vm.field_address("ipcCallGeneration", 2)] = GEN_MAX - 1
        final = self.accepted(vm, tokens)
        self.assertEqual(final, (GEN_MAX << LAYOUT['TASK_SLOT_BITS']) | 2)
        vm.response(final)
        vm.run(2)
        vm.request(tokens[2])
        self.assertEqual(vm.result(2), (error(75), 0))
        self.assertEqual(vm.field("ipcCallGeneration", 2), GEN_MAX)
        self.assertEqual(vm.object_value(endpoint, "references"), 2)

    def test_snapshot_small_accept_and_blocked_accept_revalidation(self):
        for blocked, bad_range in ((False, False), (True, False), (True, True)):
            with self.subTest(blocked=blocked, bad_range=bad_range):
                vm, tokens, endpoint = self.fixture(2)
                if blocked:
                    vm.invoke(ACCEPT, tokens[1], USER_DATA, 32 if bad_range else 1)
                    if bad_range:
                        self.assertTrue(vm.call("setPagePermissions", vm.field("directory", 1), 1, USER_DATA, 19))
                else:
                    vm.run(2)
                vm.request(tokens[2], b"hello")
                vm.seed(vm.pages(2)[1], b"XXXXX")
                vm.run(1)
                if not blocked:
                    vm.invoke(ACCEPT, tokens[1], USER_DATA, 1)
                self.assertEqual(vm.result(1), (error(14), 0) if bad_range else (error(90), 5))
                self.assertEqual(vm.queue(endpoint), [2])
                self.assertEqual(vm.field("ipcKind", 2), AWAIT_ACCEPT)
                self.assertEqual(vm.field("ipcReplyOwner", 2), 0)
                if bad_range:
                    self.assertTrue(vm.call("setPagePermissions", vm.field("directory", 1), 1, USER_DATA, 23))
                token = vm.accept(tokens[1])
                self.assertEqual(vm.read_bytes(vm.pages(1)[1], 5), b"hello")
                vm.response(token)
                self.assertEqual(vm.result(2), (8, 8))

    def test_reply_source_retry_and_terminal_destination_failures(self):
        for bad_range in (False, True):
            vm, tokens, endpoint = self.fixture(2)
            token = self.accepted(vm, tokens, capacity=32 if bad_range else 1)
            for source, length, errno in ((USER_DATA, 33, 90), (USER_DATA + PAGE - 1, 2, 14), (0, 1, 14)):
                vm.invoke(REPLY, token, source, length)
                self.assertEqual(vm.result(1), (error(errno), 0))
                self.assertEqual(vm.field("ipcKind", 2), AWAIT_REPLY)
                self.assertEqual(vm.wakes, Counter())
            before = vm.read_bytes(vm.pages(2)[1], 32)
            if bad_range:
                self.assertTrue(vm.call("setPagePermissions", vm.field("directory", 2), 2, USER_DATA, 19))
            vm.response(token, b"big")
            expected = (error(14), 0) if bad_range else (error(90), 3)
            self.assertEqual(vm.result(1), expected)
            self.assertEqual(vm.result(2), expected)
            self.assertEqual(vm.read_bytes(vm.pages(2)[1], 32), before)
            self.assert_cleared(vm, 2)
            self.assertEqual(vm.wakes, Counter({2: 1}))
            vm.response(token)
            self.assertEqual(vm.result(1), (error(9), 0))

    def test_destroy_exit_fault_and_last_receive_close_cancel_both_states(self):
        for event in ("destroy", "exit", "fault", "last_receive", "last_receive_with_copy"):
            with self.subTest(event=event):
                vm, tokens, endpoint = self.fixture(4)
                reply = self.accepted(vm, tokens)
                vm.run(3)
                vm.request(tokens[3])
                vm.run(1)
                if event == "destroy":
                    vm.invoke(18, tokens[1])
                elif event in ("exit", "fault"):
                    receive = vm.call("handleCopy", vm.field_address("handles", 1), tokens[1],
                                      vm.field_address("handles", 1), 1, 1, 2)
                    vm.invoke(16, tokens[1])  # no management handle remains
                    self.assertEqual(vm.object_value(endpoint, "state"), 1)
                    vm.call("taskFinish", vm.field_address("context", 1), 9, event == "fault")
                    self.assertGreater(receive, 0)
                else:
                    if event == "last_receive_with_copy":
                        other = vm.call("handleCopy", vm.field_address("handles", 1), tokens[1],
                                        vm.field_address("handles", 1), 1, 1, 2)
                        vm.invoke(16, tokens[1])
                        self.assertEqual(vm.wakes, Counter())
                        self.assertEqual(vm.object_value(endpoint, "state"), 1)
                        vm.invoke(16, other)
                    else:
                        vm.invoke(16, tokens[1])
                for id in (2, 3):
                    self.assertEqual(vm.result(id), (error(32), 0))
                    self.assert_cleared(vm, id)
                self.assertEqual(vm.wakes, Counter({2: 1, 3: 1}))
                self.assertEqual(vm.queue(endpoint), [])
                vm.run(4)
                vm.invoke(CALL, tokens[4], USER_DATA, 1, USER_DATA, 32)
                self.assertEqual(vm.result(4), (error(32), 0))
                vm.invoke(REPLY, reply, USER_DATA, 1)
                self.assertEqual(vm.result(4), (error(9), 0))

    def test_blocked_service_termination_cancels_accepted_and_queued_calls(self):
        vm, tokens, endpoint = self.fixture(4)
        self.accepted(vm, tokens)
        vm.run(3)
        vm.request(tokens[3])
        vm.run(1)
        vm.accept(tokens[1])
        vm.invoke(ACCEPT, tokens[1], USER_DATA, 32)  # service itself is now suspended
        self.assertEqual(vm.current(), 4)
        self.assertTrue(vm.call("taskAbortBlocked", 1, 9, True))
        self.assertEqual(vm.wakes, Counter({2: 1, 3: 1}))
        self.assertEqual(vm.field("state", 1), 3)
        self.assertEqual(vm.object_value(endpoint, "references"), 3)
        for id in (1, 2, 3):
            self.assert_cleared(vm, id)

    def test_client_death_invalidates_right_before_reap_and_preserves_peer_fifo(self):
        for accepted in (False, True):
            vm, tokens, endpoint = self.fixture(4)
            vm.run(2)
            vm.request(tokens[2])
            vm.run(3)
            vm.request(tokens[3])
            vm.run(1)
            token = vm.accept(tokens[1]) if accepted else 0x1002
            self.assertTrue(vm.call("taskAbortBlocked", 2, 9, True))
            self.assertEqual(vm.queue(endpoint), [3])
            self.assertEqual(vm.wakes, Counter())
            vm.cpu_sp = vm.field("kernelStackTop", 1) - 64
            pages, root = vm.pages(2), vm.field("directory", 2)
            vm.call("taskReap")
            self.assertTrue(all(vm.call("physicalPageAvailable", page) for page in pages + [root]))
            self.assert_cleared(vm, 2)
            vm.response(token)
            self.assertEqual(vm.result(1), (error(9), 0))
            good = vm.accept(tokens[1])
            vm.response(good)
            self.assertEqual(vm.result(3), (8, 8))
            self.assertEqual(vm.wakes, Counter({3: 1}))

    def test_zero_maximum_cross_page_overlapping_buffers_and_timer_rotations(self):
        vm, tokens, endpoint = self.fixture(2)
        for id in (1, 2):
            page = vm.call("allocPage", id, 5)
            self.assertTrue(vm.call("mapPage", vm.field("directory", id), id, USER_DATA + PAGE, page, 23))
        for iteration in range(32):
            vm.run(2)
            size = 0 if iteration == 0 else 32
            message = bytes((iteration + i) & 255 for i in range(size))
            address = 0xFFFFFFFF if not size else USER_DATA + PAGE - 16
            if size:
                second = vm.leaf(USER_DATA + PAGE, vm.field("directory", 2)) & ~4095
                vm.seed(vm.pages(2)[1] + PAGE - 16, message[:16])
                vm.seed(second, message[16:])
            vm.invoke(CALL, tokens[2], address, size, address, size)
            token = vm.accept(tokens[1], size, 0xFFFFFFFF if not size else USER_DATA)
            self.assertEqual(vm.read_bytes(vm.pages(1)[1], size), message)
            vm.seed(vm.pages(1)[1], message[::-1])
            frame = vm.field_address("context", 1)
            vm.call("taskTick", frame)
            self.assertEqual(vm.current(), 1)  # client must remain blocked
            vm.invoke(REPLY, token, 0xFFFFFFFF if not size else USER_DATA, size)
            vm.call("taskTick", frame)
            self.assertEqual(vm.current(), 2)
            if size:
                self.assertEqual(vm.read_bytes(vm.pages(2)[1] + PAGE - 16, 16) + vm.read_bytes(second, 16), message[::-1])
            self.assertEqual(vm.result(2), (size, size))
            self.assert_cleared(vm, 2)
            self.assertEqual(vm.object_value(endpoint, "references"), 2)
        self.assertEqual(vm.wakes, Counter({2: 32}))


if __name__ == "__main__":
    unittest.main()
