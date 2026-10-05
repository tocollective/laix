"""Execute checked IPC/timer/control source for serialized wait completion."""
import unittest
from collections import Counter

from test_ipc_request_reply import ServiceM
import test_ipc_request_reply as request_reply
import test_ipc_transport as transport
from test_task import TaskEntered, USER_DATA
from test_ipc_handles import error
from source_m import LAYOUT as C
from test_user_syscalls import UserM
from test_ipc_service_helper import AcceptUserM

TIMED, TRY_SEND, TRY_RECEIVE, TRY_ACCEPT, CANCEL, SLEEP = range(55, 61)


class LivenessTests(unittest.TestCase):
    assert_cleared = request_reply.RequestReplyTests.assert_cleared

    def fixture(self, count=6, rights=32, raw=False):
        vm = ServiceM()
        for slot in range(1, count + 1):
            self.assertEqual(vm.call('taskCreateImage', 0x14000, 0x1401C, 0), slot)
        root = vm.call('endpointBootstrapService', vm.field_address('handles', 1), 1)
        tokens = {1: root}
        for slot in range(2, count + 1):
            tokens[slot] = vm.call('handleCopy', vm.field_address('handles', 1), root,
                                   vm.field_address('handles', slot), 1, slot, 1)
            self.assertTrue(vm.call('taskControlBootstrap', count, slot, rights))
        self.assertTrue(vm.call('taskControlBootstrap', count, 1, rights))
        second = vm.call('endpointBootstrapService', vm.field_address('handles', 2), 2)
        to_second = vm.call('handleCopy', vm.field_address('handles', 2), second,
                            vm.field_address('handles', 1), 2, 1, 1)
        endpoint = vm.call('handleLookup', vm.field_address('handles', 1), root, 0)
        vm.memory[C['TIMER_COUNT_LO']] = 0
        vm.memory[C['TIMER_COUNT_HI']] = 0
        if raw:
            vm.raw = vm.call('endpointBootstrap', vm.field_address('handles', 1), 1)
            vm.raw_peer = vm.call('handleCopy', vm.field_address('handles', 1), vm.raw,
                                  vm.field_address('handles', 2), 1, 2, 3)
        for slot in range(1, count + 1):
            self.assertTrue(vm.call('taskInstallRuntimeStart', slot, 0, 0, 0))
            self.assertTrue(vm.call('taskPublish', slot))
        with self.assertRaises(TaskEntered):
            vm.call('taskStart', 1000000)
        return vm, tokens, endpoint, to_second

    def timed(self, vm, tokens, client=2, seconds=1):
        vm.run(client)
        vm.seed(vm.pages(client)[1], b'request')
        vm.invoke(TIMED, tokens[client], USER_DATA, 7, USER_DATA + 128, 32, seconds)

    def tick(self, vm, count):
        vm.memory[C['TIMER_COUNT_LO']] = count & 0xFFFFFFFF
        vm.memory[C['TIMER_COUNT_HI']] = count >> 32
        vm.call('ipcTimerTick')

    def cleared(self, vm, client):
        self.assert_cleared(vm, client)
        self.assertEqual(vm.field('waitTimed', client), 0)
        deadline = vm.field_address('waitDeadline', client)
        self.assertEqual((vm.memory[deadline], vm.memory[deadline + 4]), (0, 0))
        self.assertEqual(vm.field('waitReason', client), 0)

    def test_pre_and_post_accept_timeout_one_budget_late_reply_and_next_generation(self):
        for accepted in (False, True):
            with self.subTest(accepted=accepted):
                vm, tokens, endpoint, _ = self.fixture()
                baseline = vm.object_value(endpoint, 'references')
                self.timed(vm, tokens)
                self.tick(vm, 999999)
                vm.run(1)
                old = vm.accept(tokens[1]) if accepted else 0x102
                self.assertEqual(vm.field('state', 2), 4)
                before = vm.read_bytes(vm.pages(2)[1] + 128, 32)
                self.tick(vm, 1000000)
                self.assertEqual(vm.result(2), (error(110), 0))
                self.cleared(vm, 2)
                self.assertEqual(vm.object_value(endpoint, 'references'), baseline)
                self.tick(vm, 2000000)
                vm.response(old)
                self.assertEqual(vm.result(1), (error(9), 0))
                self.assertEqual(vm.read_bytes(vm.pages(2)[1] + 128, 32), before)
                self.timed(vm, tokens)
                vm.run(1)
                new = vm.accept(tokens[1])
                self.assertNotEqual(new, old)
                vm.response(old)
                self.assertEqual(vm.field('state', 2), 4)
                vm.response(new)
                self.assertEqual(vm.result(2), (8, 8))
                self.assertEqual(vm.wakes, Counter({2: 2}))
                self.cleared(vm, 2)

    def test_reply_expiry_boundary_both_serialized_orders(self):
        for reply_first in (False, True):
            vm, tokens, endpoint, _ = self.fixture()
            self.timed(vm, tokens)
            vm.run(1)
            token = vm.accept(tokens[1])
            before = vm.read_bytes(vm.pages(2)[1] + 128, 32)
            vm.memory[C['TIMER_COUNT_LO']] = 1000000
            if reply_first:
                vm.response(token)
                self.tick(vm, 1000000)
                self.assertEqual(vm.result(2), (8, 8))
            else:
                self.tick(vm, 1000000)
                vm.response(token)
                self.assertEqual(vm.result(2), (error(110), 0))
                self.assertEqual(vm.result(1), (error(9), 0))
                self.assertEqual(vm.read_bytes(vm.pages(2)[1] + 128, 32), before)
            self.tick(vm, 1000001)
            self.assertEqual(vm.wakes, Counter({2: 1}))
            self.cleared(vm, 2)
            self.assertEqual(vm.object_value(endpoint, 'references'), 6)

    def test_expiry_revocation_server_death_and_client_termination_both_orders(self):
        for event in ('destroy', 'server_death', 'client_death'):
            for accepted in (False, True):
                for timeout_first in (False, True):
                    with self.subTest(event=event, accepted=accepted, timeout_first=timeout_first):
                        vm, tokens, endpoint, _ = self.fixture()
                        self.timed(vm, tokens)
                        vm.run(1)
                        if accepted:
                            vm.accept(tokens[1])
                        if timeout_first:
                            self.tick(vm, 1000000)
                        if event == 'destroy':
                            vm.invoke(18, tokens[1])
                        elif event == 'server_death':
                            vm.invoke(1, 9)
                        else:
                            # The supervisor also holds scoped termination authority.
                            typ = vm.decls['taskControls'].sym.type.elem
                            for i in range(16):
                                row = vm.addresses['taskControls'] + i * typ.size
                                if vm.memory[row + typ.field('reference').offset] == 2:
                                    vm.memory[row + typ.field('rights').offset] = 40
                            vm.run(6)
                            vm.invoke(40, 2, 9)
                        self.tick(vm, 1000000)
                        self.tick(vm, 2000000)
                        self.cleared(vm, 2)
                        self.assertEqual(vm.wakes[2], int(timeout_first or event != 'client_death'))
                        if event == 'client_death':
                            self.assertEqual(vm.field('state', 2), 3)
                        else:
                            self.assertEqual(vm.result(2), (error(110 if timeout_first else 32), 0))

    def test_oldest_and_middle_expiry_and_cancel_preserve_fifo(self):
        vm, tokens, endpoint, _ = self.fixture()
        for client, seconds in ((2, 1), (3, 4), (4, 1), (5, 4)):
            self.timed(vm, tokens, client, seconds)
        self.assertEqual(vm.queue(endpoint), [2, 3, 4, 5])
        self.tick(vm, 1000000)
        self.assertEqual(vm.queue(endpoint), [3, 5])
        vm.run(6)
        vm.invoke(CANCEL, 3)
        self.assertEqual(vm.result(6)[0], 0)
        self.assertEqual(vm.queue(endpoint), [5])
        vm.run(1)
        token = vm.accept(tokens[1])
        self.assertEqual(token & 255, 5)
        vm.response(token)
        self.assertEqual(vm.wakes, Counter({2: 1, 3: 1, 4: 1, 5: 1}))
        self.assertEqual(vm.object_value(endpoint, 'references'), 6)

    def test_scoped_cancel_queued_accepted_untimed_and_sleep_and_repeated_cancel(self):
        for kind in ('queued', 'accepted', 'untimed', 'sleep'):
            vm, tokens, endpoint, _ = self.fixture()
            if kind == 'sleep':
                vm.run(2)
                vm.invoke(SLEEP, 1)
            elif kind == 'untimed':
                vm.run(2)
                vm.request(tokens[2])
            else:
                self.timed(vm, tokens)
                if kind == 'accepted':
                    vm.run(1)
                    token = vm.accept(tokens[1])
            vm.run(3)
            vm.invoke(CANCEL, 2)
            self.assertEqual(vm.result(3)[0], error(1))
            self.assertEqual(vm.field('state', 2), 4)
            vm.run(6)
            vm.invoke(CANCEL, 2)
            self.assertEqual(vm.result(6)[0], 0)
            self.assertEqual(vm.result(2), (error(125), 0))
            vm.invoke(CANCEL, 2)
            self.assertEqual(vm.result(6)[0], error(11))
            self.tick(vm, 1000000)
            self.assertEqual(vm.wakes, Counter({2: 1}))
            self.cleared(vm, 2)
            if kind == 'accepted':
                vm.run(1)
                vm.response(token)
                self.assertEqual(vm.result(1), (error(9), 0))
        vm, tokens, _, _ = self.fixture(rights=8)
        self.timed(vm, tokens)
        vm.run(6)
        vm.invoke(CANCEL, 2)
        self.assertEqual(vm.result(6)[0], error(1))  # termination right is not cancellation

    def test_supervisor_cancels_raw_send_receive_and_service_accept(self):
        for number in (19, 20, 22):
            vm, tokens, endpoint, _ = self.fixture(raw=True)
            client = 1 if number == 22 else 2
            token = tokens[1] if number == 22 else vm.raw_peer
            root = vm.call('handleLookup', vm.field_address('handles', client), token, 0)
            baseline = vm.object_value(root, 'references')
            vm.run(client)
            vm.invoke(number, token, USER_DATA, 4)
            self.assertEqual(vm.field('state', client), 4)
            vm.run(6)
            vm.invoke(CANCEL, client)
            self.assertEqual(vm.result(6)[0], 0)
            self.assertEqual(vm.result(client), (error(125), 0))
            self.cleared(vm, client)
            self.assertEqual(vm.object_value(root, 'references'), baseline)
            self.assertEqual(vm.queue(root), [])
            self.assertEqual(vm.queue(root, False), [])
            self.assertEqual(vm.wakes, Counter({client: 1}))

    def test_cancel_expiry_boundary_both_orders(self):
        for timeout_first in (False, True):
            vm, tokens, endpoint, _ = self.fixture()
            self.timed(vm, tokens)
            vm.run(6)
            vm.memory[C['TIMER_COUNT_LO']] = 1000000
            if timeout_first:
                self.tick(vm, 1000000)
            vm.invoke(CANCEL, 2)
            self.assertEqual(vm.result(6)[0], error(11) if timeout_first else 0)
            self.tick(vm, 1000000)
            self.assertEqual(vm.result(2), (error(110 if timeout_first else 125), 0))
            self.assertEqual(vm.wakes, Counter({2: 1}))
            self.cleared(vm, 2)
            self.assertEqual(vm.object_value(endpoint, 'references'), 6)
    def test_cycle_and_live_loop_restore_progress(self):
        vm, tokens, endpoint, to_second = self.fixture()
        vm.invoke(TIMED, to_second, USER_DATA, 0, USER_DATA, 0, 1)
        vm.invoke(TIMED, tokens[2], USER_DATA, 0, USER_DATA, 0, 1)
        self.assertEqual(vm.current(), 3)
        self.assertEqual((vm.field('state', 1), vm.field('state', 2)), (4, 4))
        self.tick(vm, 1000000)
        for client in (1, 2):
            self.assertEqual(vm.result(client), (error(110), 0))
            self.cleared(vm, client)
        self.assertEqual(vm.wakes, Counter({1: 1, 2: 1}))
        # An unrelated running task does not have to cooperate with expiry.
        self.timed(vm, tokens, 4)
        vm.run(1)
        token = vm.accept(tokens[1])
        frame = vm.field_address('context', 1)
        self.tick(vm, 2000000)
        vm.call('taskTick', frame)
        self.assertEqual(vm.result(4), (error(110), 0))
        self.assertEqual(vm.field('state', 1), 1)
        vm.run(6)
        vm.invoke(SLEEP, 1)
        self.tick(vm, 3000000)
        self.assertEqual(vm.result(6), (0, 0))

    def test_sleep_no_generic_wake_and_death_no_resurrection(self):
        vm, _, _, _ = self.fixture()
        vm.run(2)
        vm.invoke(SLEEP, 1)
        self.assertFalse(vm.call('taskWake', 2))
        vm.wakes.clear()
        self.tick(vm, 999999)
        self.assertEqual(vm.field('state', 2), 4)
        self.tick(vm, 1000000)
        self.assertEqual(vm.result(2), (0, 0))
        self.assertEqual(vm.wakes, Counter({2: 1}))
        self.cleared(vm, 2)
        vm.run(2)
        vm.invoke(SLEEP, 1)
        self.assertTrue(vm.call('taskAbortBlocked', 2, 9, False))
        self.tick(vm, 2000000)
        self.assertEqual(vm.wakes, Counter({2: 1}))
        self.cleared(vm, 2)
        self.assertEqual(vm.field('state', 2), 3)

    def test_clock_low_carry_full_wrap_maximum_and_invalid_timeout_no_admission(self):
        for start, rate, seconds in ((0xFFFFFFFF - 5, 1000000, 1),
                                     (0xFFFFFFFFFFFFFFFF - 5, 0xFFFFFFFF, 60)):
            vm, tokens, endpoint, _ = self.fixture()
            vm.memory[C['TIMER_FREQUENCY']] = rate
            self.tick(vm, start)
            self.timed(vm, tokens, seconds=seconds)
            deadline = (start + rate * seconds) & 0xFFFFFFFFFFFFFFFF
            address = vm.field_address('waitDeadline', 2)
            self.assertEqual(vm.memory[address] | vm.memory[address + 4] << 32, deadline)
            self.tick(vm, (deadline - 1) & 0xFFFFFFFFFFFFFFFF)
            self.assertEqual(vm.field('state', 2), 4)
            self.tick(vm, deadline)
            self.assertEqual(vm.result(2), (error(110), 0))
            self.assertEqual(vm.wakes, Counter({2: 1}))
        vm, tokens, endpoint, _ = self.fixture()
        for seconds in (0, 61, 0xFFFFFFFF):
            self.timed(vm, tokens, seconds=seconds)
            self.assertEqual(vm.result(2), (error(22), 0))
            self.cleared(vm, 2)
            self.assertEqual(vm.field('ipcCallGeneration', 2), 0)
            self.assertEqual(vm.queue(endpoint), [])
            vm.invoke(SLEEP, seconds)
            self.assertEqual(vm.result(2), (error(22), 0))

    def test_nonblocking_raw_and_service_preserve_waits_and_validation(self):
        vm, tokens, endpoint = transport.TransportTests().fixture()
        for number in (TRY_SEND, TRY_RECEIVE):
            vm.syscall(number, tokens[1], USER_DATA, 1)
            self.assertEqual(vm.result(1), (error(11), 0))
            self.assertEqual(vm.field('state', 1), 2)
            self.assertEqual(vm.queue(endpoint), [])
            self.assertEqual(vm.object_value(endpoint, 'references'), 2)
        vm.syscall(19, tokens[1], USER_DATA, 4)
        vm.syscall(TRY_RECEIVE, tokens[2], USER_DATA, 1)
        self.assertEqual(vm.result(2), (error(90), 4))
        self.assertEqual(vm.queue(endpoint), [1])
        vm.syscall(TRY_RECEIVE, tokens[2], USER_DATA, 4)
        self.assertEqual(vm.result(1), (4, 4))
        vm.syscall(20, tokens[2], USER_DATA, 4)
        vm.syscall(TRY_SEND, tokens[1], USER_DATA, 4)
        self.assertEqual(vm.result(2), (4, 4))
        vm, tokens, endpoint, _ = self.fixture()
        vm.invoke(TRY_ACCEPT, tokens[1], USER_DATA, 32)
        self.assertEqual(vm.result(1), (error(11), 0))
        self.timed(vm, tokens)
        vm.run(1)
        vm.invoke(TRY_ACCEPT, tokens[1], USER_DATA, 1)
        self.assertEqual(vm.result(1), (error(90), 7))
        self.assertEqual(vm.queue(endpoint), [2])
        vm.invoke(TRY_ACCEPT, tokens[1], USER_DATA, 32)
        token = vm.result(1)[1]
        vm.response(token)
        self.assertEqual(vm.result(2), (8, 8))

    def test_user_wrappers_and_try_accept_two_register_result(self):
        vm = UserM()
        for name, number, args in (
                ('callTimed', TIMED, (0x101, USER_DATA, 7, USER_DATA + 128, 32, 60)),
                ('trySend', TRY_SEND, (0x101, USER_DATA, 32)),
                ('tryRecv', TRY_RECEIVE, (0x101, USER_DATA, 32)),
                ('cancelTaskWait', CANCEL, (0x102,)), ('sleep', SLEEP, (1,))):
            self.assertEqual(vm.call(name, *args), 0xFFFFFFDA)
            self.assertEqual(vm.calls[-1], (C['CAUSE_SYSCALL'], (number,) + args))
        for result in ((0, 0x102), (32, 0x202), (error(11), 0), (error(90), 32)):
            vm = AcceptUserM(result)
            vm.call('tryAccept', 0x101, USER_DATA, 32, USER_DATA + 128)
            self.assertEqual(vm.memory[USER_DATA + 132], 0 if result[0] >> 31 else result[1])
            self.assertEqual(vm.calls[-1][1][0], TRY_ACCEPT)


if __name__ == '__main__':
    unittest.main()
