"""Evaluate user wrapper ASTs without generating code or entering the kernel."""

import unittest

from source_m import SourceM, LAYOUT
from test_kernel import LAIX


class UserExited(Exception):
    pass


class UserM(SourceM):
    def __init__(self):
        super().__init__(LAIX / "user/syscalls.m")
        self.calls = []

    def trap(self, cause, args=()):
        self.calls.append((cause, tuple(args)))
        if args[0] == 1:
            raise UserExited()
        return 0xFFFFFFDA  # Arbitrary kernel error, forwarded unchanged.


class UserWrapperTests(unittest.TestCase):
    def test_handle_wrappers_forward_tokens_targets_rights_and_results(self):
        vm = UserM()
        for name, number, args in (("closeHandle", 16, (0x12301,)),
                                   ("copyHandle", 17, (0x12301, 2, 1)),
                                   ("destroyEndpoint", 18, (0x12301,))):
            self.assertEqual(vm.call(name, *args), 0xFFFFFFDA)
            self.assertEqual(vm.calls[-1], (LAYOUT["CAUSE_SYSCALL"], (number,) + args))

    def test_transport_wrappers_forward_buffer_lengths_and_results(self):
        vm = UserM()
        for name, number in (("send", 19), ("recv", 20)):
            self.assertEqual(vm.call(name, 0x12301, 0x40001001, 32), 0xFFFFFFDA)
            self.assertEqual(vm.calls[-1], (LAYOUT["CAUSE_SYSCALL"],
                                           (number, 0x12301, 0x40001001, 32)))

    def test_service_wrappers_forward_call_response_buffer_and_reply_right(self):
        vm = UserM()
        for name, number, args in (("call", 21, (0x12301, 0x40001001, 32, 0x40001080, 16)),
                                   ("reply", 23, (0x102, 0x40001001, 32))):
            self.assertEqual(vm.call(name, *args), 0xFFFFFFDA)
            self.assertEqual(vm.calls[-1], (LAYOUT["CAUSE_SYSCALL"], (number,) + args))

    def test_yield_wrapper_passes_only_number_and_forwards_result(self):
        vm = UserM()
        self.assertEqual(vm.call("yield"), 0xFFFFFFDA)
        self.assertEqual(vm.calls, [(LAYOUT["CAUSE_SYSCALL"], (2,))])

    def test_debug_wrapper_passes_number_and_word_and_returns_kernel_result(self):
        vm = UserM()
        for code in (0, 65, 255, 256, 0xFFFFFFFF):
            self.assertEqual(vm.call("debugPutChar", code), 0xFFFFFFDA)
            self.assertEqual(vm.calls[-1], (LAYOUT["CAUSE_SYSCALL"], (0, code)))

    def test_exit_wrapper_passes_entire_signed_code(self):
        for code in (0, 127, 0xFFFFFF85, 0x80000000):
            vm = UserM()
            with self.assertRaises(UserExited):
                vm.call("exit", code)
            self.assertEqual(vm.calls, [(LAYOUT["CAUSE_SYSCALL"], (1, code))])


if __name__ == "__main__":
    unittest.main()
