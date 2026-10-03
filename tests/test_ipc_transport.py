"""Execute IPC, scheduler and byte-copy source together; no code generation."""

import unittest
from collections import Counter

from source_m import KernelPanic, LAYOUT
from test_task import TaskM, TaskEntered, USER_CODE, USER_DATA, PAGE
from test_ipc_handles import error
from mlang import syntax as s
from mlang.typesys import size_of


class TransportM(TaskM):
    def __init__(self, root=None):
        super().__init__(root=root)
        self.wakes = Counter()
        self.copies = []

    def call(self, name, *args):
        if name == "taskWake":
            self.wakes[args[0]] += 1
        if name == "copyToUser":
            self.copies.append((args[0], args[1], self.ptbr))
        return super().call(name, *args)

    def byte_access(self, address, writing=False):
        assert not 0x40000000 <= address < 0xC0000000, hex(address)
        assert not self.controls[0] & 1 or self.controls[0] & 16
        if address < 0x0A000000:
            leaf = self.leaf(address)
            required = 1 | (4 if writing else 2)
            assert leaf & required == required, (hex(address), leaf)
            assert leaf & ~(PAGE - 1) == address & ~(PAGE - 1)

    def expr(self, node, local):
        if isinstance(node, s.Index) and size_of(node.type) == 1:
            address = self.address(node, local)
            self.byte_access(address)
            if address < 0x0A000000:
                return self.read_bytes(address, 1)[0]
        return super().expr(node, local)

    def write(self, target, value, local):
        if isinstance(target, s.Index) and size_of(target.type) == 1:
            address = self.address(target, local)
            self.byte_access(address, True)
            if address < 0x0A000000:
                self.seed(address, bytes([value & 255]))
                return
        super().write(target, value, local)

    def seed(self, address, data):
        for i, byte in enumerate(data):
            slot, shift = (address + i) & ~3, ((address + i) & 3) * 8
            self.memory[slot] = (self.memory[slot] & ~(255 << shift)) | byte << shift

    def read_bytes(self, address, count):
        return bytes((self.memory[(address + i) & ~3] >> (((address + i) & 3) * 8)) & 255
                     for i in range(count))

    def current(self):
        return self.memory[self.globals["currentTask"]]

    def syscall(self, number, token, buffer=USER_DATA, size=32):
        frame = self.field_address("context", self.current())
        for offset, value in (("R9", number), ("R1", token), ("R2", buffer), ("R3", size)):
            self.memory[frame + LAYOUT["TF_" + offset]] = value
        return self.call("userSyscall", frame)

    def result(self, id):
        frame = self.field_address("context", id)
        return tuple(self.memory[frame + LAYOUT["TF_R" + str(i)]] for i in (1, 2))

    def endpoint(self, token):
        return self.call("handleLookup", self.field_address("handles"), token, 0)

    def object_field(self, object, field):
        return object + self.decls["endpoints"].sym.type.elem.field(field).offset

    def object_value(self, object, field):
        return self.memory[self.object_field(object, field)]

    def queue(self, object, sending=True):
        name = "sender" if sending else "receiver"
        head, count = (self.object_value(object, name + suffix) for suffix in ("Head", "Count"))
        base = self.object_field(object, "senders" if sending else "receivers")
        return [self.memory[base + ((head + i) % 8) * 4] for i in range(count)]


class TransportTests(unittest.TestCase):
    def fixture(self, count=2, peer_rights=3):
        vm = TransportM()
        for id in range(1, count + 1):
            self.assertEqual(vm.call("taskCreate"), id)
        token = vm.call("endpointBootstrap", vm.field_address("handles"), 1)
        tokens = {1: token}
        for id in range(2, count + 1):
            tokens[id] = vm.call("handleCopy", vm.field_address("handles"), token,
                                 vm.field_address("handles", id), 1, id, peer_rights)
        object = vm.endpoint(token)
        with self.assertRaises(TaskEntered):
            vm.call("taskStart", 1000000)
        return vm, tokens, object

    def test_send_first_snapshots_bytes_and_receives_in_own_directory(self):
        vm, tokens, object = self.fixture()
        message = b"\x00\xffpayload\x80"
        vm.seed(vm.pages(1)[1] + 1, message)
        vm.seed(vm.pages(2)[1], b"z" * 32)
        frame = vm.field_address("context", 1)
        epc = vm.memory[frame + LAYOUT["TF_EPC"]]
        self.assertEqual(vm.syscall(19, tokens[1], USER_DATA + 1, len(message)),
                         vm.field_address("context", 2))
        preserved = [vm.memory[frame + 4 * i] for i in range(3, 32)]
        self.assertEqual(vm.queue(object), [1])
        self.assertEqual(vm.field("state", 1), 4)
        self.assertFalse(vm.field("queued", 1))
        self.assertEqual(vm.field("ipcBuffer", 1), 0)
        self.assertEqual(vm.object_value(object, "references"), 3)
        vm.seed(vm.pages(1)[1] + 1, b"!" * len(message))
        ptbr = vm.ptbr
        vm.syscall(20, tokens[2], USER_DATA + 1, 31)
        self.assertEqual(vm.ptbr, ptbr)
        self.assertEqual(vm.read_bytes(vm.pages(2)[1], 32), b"z" + message + b"z" * (31 - len(message)))
        self.assertEqual(vm.result(1), (len(message), len(message)))
        self.assertEqual(vm.result(2), (len(message), len(message)))
        self.assertEqual(vm.memory[frame + LAYOUT["TF_EPC"]], epc + 4)
        self.assertEqual([vm.memory[frame + 4 * i] for i in range(3, 32)], preserved)
        self.assertEqual(vm.wakes, Counter({1: 1}))
        self.assertEqual(vm.field("ipcEndpoint", 1), 0)
        self.assertEqual(vm.object_value(object, "references"), 2)
        self.assertEqual(vm.queue(object), [])
        base = vm.field_address("ipcMessage", 1)
        self.assertEqual([vm.memory[base + i] for i in range(32)], [0] * 32)
        self.assertEqual(vm.copies, [(vm.field("directory", 2), 2, ptbr)])

    def test_receive_first_uses_receiver_root_while_sender_is_current(self):
        vm, tokens, object = self.fixture()
        vm.seed(vm.pages(1)[1], b"x" * 32)
        vm.seed(vm.pages(2)[1], b"hello")
        vm.syscall(20, tokens[1], USER_DATA, 32)
        self.assertEqual(vm.queue(object, False), [1])
        self.assertEqual(vm.field("state", 1), 4)
        ptbr = vm.ptbr
        vm.syscall(19, tokens[2], USER_DATA, 5)
        self.assertEqual(vm.current(), 2)
        self.assertEqual(vm.read_bytes(vm.pages(1)[1], 6), b"hellox")
        self.assertEqual(vm.read_bytes(vm.pages(2)[1], 5), b"hello")
        self.assertEqual(vm.result(1), (5, 5))
        self.assertEqual(vm.result(2), (5, 5))
        self.assertEqual(vm.wakes, Counter({1: 1}))
        self.assertEqual(vm.copies, [(vm.field("directory", 1), 1, ptbr)])
        self.assertEqual(vm.queue(object, False), [])

    def test_multiple_senders_fifo_and_small_retry_does_not_consume(self):
        vm, tokens, object = self.fixture(3)
        for id, message in ((1, b"first"), (2, b"second")):
            vm.seed(vm.pages(id)[1], message)
            vm.syscall(19, tokens[id], USER_DATA, len(message))
        self.assertEqual(vm.queue(object), [1, 2])
        before = vm.read_bytes(vm.pages(3)[1], 32)
        vm.syscall(20, tokens[3], USER_DATA, 4)
        self.assertEqual(vm.result(3), (error(90), 5))
        self.assertEqual(vm.queue(object), [1, 2])
        self.assertEqual(vm.read_bytes(vm.pages(3)[1], 32), before)
        self.assertEqual(vm.wakes, Counter())
        for message, remaining in ((b"first", [2]), (b"second", [])):
            vm.syscall(20, tokens[3], USER_DATA, 32)
            self.assertEqual(vm.read_bytes(vm.pages(3)[1], len(message)), message)
            self.assertEqual(vm.queue(object), remaining)
        self.assertEqual(vm.wakes, Counter({1: 1, 2: 1}))

    def test_multiple_receivers_fifo_with_small_waiter_and_one_delivery(self):
        vm, tokens, object = self.fixture(4)
        for id, capacity in ((1, 1), (2, 32), (3, 32)):
            vm.syscall(20, tokens[id], USER_DATA, capacity)
        self.assertEqual(vm.queue(object, False), [1, 2, 3])
        vm.seed(vm.pages(4)[1], b"AB")
        vm.syscall(19, tokens[4], USER_DATA, 2)
        self.assertEqual(vm.result(1), (error(90), 2))
        self.assertEqual(vm.result(2), (2, 2))
        self.assertEqual(vm.read_bytes(vm.pages(1)[1], 2), b"\0\0")
        self.assertEqual(vm.read_bytes(vm.pages(2)[1], 2), b"AB")
        self.assertEqual(vm.queue(object, False), [3])
        self.assertEqual(vm.wakes, Counter({1: 1, 2: 1}))
        vm.seed(vm.pages(4)[1], b"CD")
        vm.syscall(19, tokens[4], USER_DATA, 2)
        self.assertEqual(vm.read_bytes(vm.pages(3)[1], 2), b"CD")
        self.assertEqual(vm.wakes, Counter({1: 1, 2: 1, 3: 1}))

    def test_small_waiting_receiver_keeps_incoming_message_for_retry(self):
        vm, tokens, object = self.fixture()
        vm.syscall(20, tokens[1], USER_DATA, 1)
        vm.seed(vm.pages(2)[1], b"abc")
        vm.syscall(19, tokens[2], USER_DATA, 3)
        self.assertEqual(vm.current(), 1)
        self.assertEqual(vm.result(1), (error(90), 3))
        self.assertEqual(vm.queue(object), [2])
        self.assertEqual(vm.wakes, Counter({1: 1}))
        vm.syscall(20, tokens[1], USER_DATA, 32)
        self.assertEqual(vm.read_bytes(vm.pages(1)[1], 3), b"abc")
        self.assertEqual(vm.wakes, Counter({1: 1, 2: 1}))

    def test_invalid_ranges_rights_and_lengths_do_not_touch_queues_or_buffers(self):
        vm, tokens, object = self.fixture(peer_rights=1)
        for number, address, count, errno in (
                (19, 0, 1, 14), (19, 0xFFFFFFFE, 4, 14),
                (19, USER_DATA + PAGE - 1, 2, 14),
                (19, USER_DATA, 33, 90), (19, USER_DATA, 0xFFFFFFFF, 90),
                (20, USER_CODE, 32, 14), (20, USER_DATA + PAGE - 1, 2, 14),
                (20, USER_DATA, 0xFFFFFFFF, 14)):
            with self.subTest(number=number, address=address, count=count):
                before = vm.read_bytes(vm.pages(1)[1], 32)
                vm.syscall(number, tokens[1], address, count)
                self.assertEqual(vm.result(1), (error(errno), 0))
                self.assertEqual(vm.queue(object), [])
                self.assertEqual(vm.queue(object, False), [])
                self.assertEqual(vm.object_value(object, "references"), 2)
                self.assertEqual(vm.read_bytes(vm.pages(1)[1], 32), before)
                self.assertEqual(vm.field("state", 1), 2)
        vm.syscall(19, tokens[1] + 256, 0, 0)
        self.assertEqual(vm.result(1), (error(9), 0))
        vm.call("taskYield", vm.field_address("context", 1))
        vm.syscall(20, tokens[2], 0, 1)
        self.assertEqual(vm.result(2), (error(1), 0))
        vm.controls[0] = 1
        with self.assertRaisesRegex(KernelPanic, "IRQs enabled"):
            vm.syscall(19, tokens[2], USER_DATA, 1)
        self.assertEqual(vm.queue(object), [])

    def test_waiting_receive_capacity_is_revalidated_before_any_write(self):
        vm, tokens, object = self.fixture()
        vm.syscall(20, tokens[1], USER_DATA, 32)
        self.assertTrue(vm.call("setPagePermissions", vm.field("directory", 1), 1, USER_DATA, 3 | 16))
        vm.seed(vm.pages(2)[1], b"abc")
        vm.syscall(19, tokens[2], USER_DATA, 3)
        self.assertEqual(vm.current(), 1)
        self.assertEqual(vm.result(1), (error(14), 0))
        self.assertEqual(vm.read_bytes(vm.pages(1)[1], 3), b"\0\0\0")
        self.assertEqual(vm.queue(object), [2])
        self.assertTrue(vm.call("setPagePermissions", vm.field("directory", 1), 1, USER_DATA, 7 | 16))
        vm.syscall(20, tokens[1], USER_DATA, 32)
        self.assertEqual(vm.read_bytes(vm.pages(1)[1], 3), b"abc")

    def test_cross_page_copy_validates_all_pages_and_noncontiguous_frames(self):
        vm, tokens, object = self.fixture()
        for id in (1, 2):
            page = vm.call("allocPage", id, 5)
            self.assertTrue(vm.call("mapPage", vm.field("directory", id), id, USER_DATA + PAGE, page, 23))
        message = b"\xff\x80abc\0"
        vm.seed(vm.pages(1)[1] + PAGE - 2, message[:2])
        source_second = vm.leaf(USER_DATA + PAGE, vm.field("directory", 1)) & ~4095
        vm.seed(source_second, message[2:])
        vm.syscall(19, tokens[1], USER_DATA + PAGE - 2, 6)
        vm.syscall(20, tokens[2], USER_DATA + PAGE - 2, 6)
        dest_second = vm.leaf(USER_DATA + PAGE, vm.field("directory", 2)) & ~4095
        self.assertEqual(vm.read_bytes(vm.pages(2)[1] + PAGE - 2, 2) +
                         vm.read_bytes(dest_second, 4), message)
        self.assertEqual(vm.wakes, Counter({1: 1}))

    def test_zero_length_rendezvous_still_checks_authority_and_never_reads_va(self):
        vm, tokens, object = self.fixture()
        vm.syscall(19, tokens[1], 0xFFFFFFFF, 0)
        self.assertEqual(vm.field("state", 1), 4)
        vm.syscall(20, tokens[2], 0xFFFFFFFF, 0)
        self.assertEqual(vm.result(1), (0, 0))
        self.assertEqual(vm.result(2), (0, 0))
        self.assertEqual(vm.wakes, Counter({1: 1}))

    def test_destroy_cancels_all_waits_once_and_releases_pins(self):
        for number in (19, 20):
            with self.subTest(number=number):
                vm, tokens, object = self.fixture(3)
                # Owner rotates away; both peers block on the same side.
                vm.call("taskYield", vm.field_address("context", 1))
                vm.syscall(number, tokens[2], USER_DATA, 1)
                vm.syscall(number, tokens[3], USER_DATA, 1)
                self.assertEqual(vm.current(), 1)
                self.assertEqual(vm.object_value(object, "references"), 5)
                vm.syscall(18, tokens[1])
                for id in (2, 3):
                    self.assertEqual(vm.result(id), (error(32), 0))
                    self.assertEqual(vm.field("state", id), 1)
                    self.assertEqual(vm.field("ipcEndpoint", id), 0)
                self.assertEqual(vm.wakes, Counter({2: 1, 3: 1}))
                self.assertEqual(vm.object_value(object, "references"), 3)
                vm.syscall(18, tokens[1])
                self.assertEqual(vm.wakes, Counter({2: 1, 3: 1}))
                vm.syscall(number, tokens[1], USER_DATA, 1)
                self.assertEqual(vm.result(1), (error(32), 0))

    def test_owner_exit_or_fault_revokes_waits_before_reap(self):
        for faulted in (False, True):
            vm, tokens, object = self.fixture()
            vm.call("taskYield", vm.field_address("context", 1))
            vm.syscall(19, tokens[2], USER_DATA, 1)
            vm.call("taskFinish", vm.field_address("context", 1), 9, faulted)
            self.assertEqual(vm.current(), 2)
            self.assertEqual(vm.result(2), (error(32), 0))
            self.assertEqual(vm.wakes, Counter({2: 1}))
            self.assertEqual(vm.object_value(object, "references"), 1)
            vm.syscall(16, tokens[2])
            self.assertEqual(vm.object_value(object, "references"), 0)
            self.assertEqual(vm.object_value(object, "state"), 0)

    def test_blocked_task_termination_removes_middle_wait_and_preserves_fifo(self):
        for number in (19, 20):
            for faulted in (False, True):
                vm, tokens, object = self.fixture(4)
                vm.call("taskYield", vm.field_address("context", 1))
                for id in (2, 3, 4):
                    vm.syscall(number, tokens[id], USER_DATA, 1)
                self.assertEqual(vm.current(), 1)
                self.assertFalse(vm.call("taskWake", 3))  # generic wakes cannot bypass IPC
                vm.wakes.clear()
                self.assertTrue(vm.call("taskAbortBlocked", 3, 9, faulted))
                self.assertFalse(vm.call("taskAbortBlocked", 3, 9, faulted))
                self.assertEqual(vm.queue(object, number == 19), [2, 4])
                self.assertEqual(vm.field("ipcEndpoint", 3), 0)
                self.assertEqual(vm.field("state", 3), 3)
                self.assertEqual(vm.object_value(object, "references"), 5)
                self.assertEqual(vm.wakes, Counter())
                vm.cpu_sp = vm.field("kernelStackTop", 1) - 64
                root, pages = vm.field("directory", 3), vm.pages(3)
                vm.call("taskReap")
                self.assertTrue(all(vm.call("physicalPageAvailable", p) for p in [root] + pages))
                vm.syscall(20 if number == 19 else 19, tokens[1], USER_DATA, 1)
                self.assertEqual(vm.queue(object, number == 19), [4])
                self.assertEqual(vm.wakes, Counter({2: 1}))

    def test_blocked_owner_death_cancels_wait_and_releases_every_reference(self):
        vm, tokens, object = self.fixture()
        vm.syscall(19, tokens[1], USER_DATA, 1)
        self.assertTrue(vm.call("taskAbortBlocked", 1, 9, True))
        self.assertEqual(vm.queue(object), [])
        self.assertEqual(vm.object_value(object, "references"), 1)
        self.assertEqual(vm.wakes, Counter())
        vm.syscall(20, tokens[2], USER_DATA, 32)
        self.assertEqual(vm.result(2), (error(32), 0))
        vm.syscall(16, tokens[2])
        self.assertEqual(vm.object_value(object, "state"), 0)

    def test_wait_pin_survives_handle_release_until_cancellation(self):
        vm, tokens, object = self.fixture()
        vm.call("taskYield", vm.field_address("context", 1))
        vm.syscall(19, tokens[2], USER_DATA, 1)
        vm.syscall(16, tokens[1])
        self.assertEqual(vm.call("handleClose", vm.field_address("handles", 2), tokens[2]), 0)
        self.assertEqual(vm.object_value(object, "references"), 1)
        self.assertEqual(vm.object_value(object, "state"), 1)
        self.assertTrue(vm.call("taskAbortBlocked", 2, 0, False))
        self.assertEqual(vm.object_value(object, "references"), 0)
        self.assertEqual(vm.object_value(object, "state"), 0)
        self.assertEqual(vm.queue(object), [])

    def test_full_receiver_range_failure_preserves_oldest_pending_sender(self):
        vm, tokens, object = self.fixture()
        vm.seed(vm.pages(1)[1], b"abc")
        vm.syscall(19, tokens[1], USER_DATA, 3)
        # The message would fit in the first page; the declared capacity would not.
        vm.syscall(20, tokens[2], USER_DATA, PAGE + 1)
        self.assertEqual(vm.result(2), (error(14), 0))
        self.assertEqual(vm.queue(object), [1])
        self.assertEqual(vm.read_bytes(vm.pages(2)[1], 3), b"\0\0\0")
        self.assertEqual(vm.wakes, Counter())
        vm.syscall(20, tokens[2], USER_DATA, 32)
        self.assertEqual(vm.read_bytes(vm.pages(2)[1], 3), b"abc")

    def test_repeated_maximum_message_exchange_with_timer_preemption(self):
        vm, tokens, object = self.fixture()
        for iteration in range(32):
            request = bytes((iteration + i) & 255 for i in range(32))
            response = request[::-1]
            vm.seed(vm.pages(1)[1], request)
            vm.syscall(19, tokens[1], USER_DATA, 32)
            vm.syscall(20, tokens[2], USER_DATA, 32)
            self.assertEqual(vm.read_bytes(vm.pages(2)[1], 32), request)
            vm.call("taskTick", vm.field_address("context", 2))
            self.assertEqual(vm.current(), 1)
            vm.syscall(20, tokens[1], USER_DATA, 32)
            vm.seed(vm.pages(2)[1], response)
            vm.syscall(19, tokens[2], USER_DATA, 32)
            vm.call("taskTick", vm.field_address("context", 2))
            self.assertEqual(vm.current(), 1)
            self.assertEqual(vm.read_bytes(vm.pages(1)[1], 32), response)
            self.assertEqual(vm.queue(object), [])
            self.assertEqual(vm.queue(object, False), [])
            self.assertEqual(vm.object_value(object, "references"), 2)
            self.assertEqual(vm.globals["readyCount"], 1)
        self.assertEqual(vm.wakes, Counter({1: 64}))

    def test_alone_blocks_in_idle_until_destroyed_and_duplicate_wait_is_rejected(self):
        vm, tokens, object = self.fixture(1)
        vm.syscall(19, tokens[1], USER_DATA, 1)
        self.assertEqual(vm.current(), 0)
        self.assertEqual(vm.queue(object), [1])
        # Re-entering IPC from this blocked TCB cannot append a second wait.
        frame = vm.field_address("context", 1)
        self.assertEqual(vm.call("ipcSend", frame, tokens[1], USER_DATA, 1), frame)
        self.assertEqual(vm.queue(object), [1])
        self.assertEqual(vm.call("endpointDestroy", vm.field_address("handles", 1), tokens[1], 1), 0)
        self.assertEqual(vm.result(1), (error(32), 0))
        self.assertEqual(vm.wakes, Counter({1: 1}))
        self.assertEqual(vm.call("taskIdlePoll"), frame)
        self.assertEqual(vm.current(), 1)


if __name__ == "__main__":
    unittest.main()
