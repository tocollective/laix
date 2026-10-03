"""Execute the M accept helper and parsed assembly shim without emitting code."""

import unittest

from test_kernel import LAIX, parse_asm, asm_constants
from test_user_syscalls import UserM
from source_m import LAYOUT
import asm


class AcceptUserM(UserM):
    def __init__(self, result):
        super().__init__()
        self.result = result
        parser = parse_asm(LAIX / "user/syscalls.asm")
        self.statements = parser.stmts
        self.constants = asm_constants(parser)
        self.labels = {name: i for i, st in enumerate(self.statements) for name in st.labels}

    def call(self, name, *args):
        if name != "ipcAcceptResult":
            return super().call(name, *args)
        regs = [0] * 32
        regs[1:5] = args
        pc = self.labels[name]
        def immediate(value):
            return asm.ExprParser(value, self.constants.__getitem__).parse()
        def register(value):
            return 31 if value == "ra" else int(value[1:])
        for _ in range(100):
            st = self.statements[pc]
            pc += 1
            if st.op is None or st.op.startswith(".") or st.op == "=":
                continue
            if st.op == "li":
                regs[register(st.args[0])] = immediate(st.args[1])
            elif st.op == "syscall":
                self.calls.append((LAYOUT["CAUSE_SYSCALL"], tuple([regs[9]] + regs[1:4])))
                regs[1], regs[2] = self.result
            elif st.op == "bltz":
                if regs[register(st.args[0])] & 0x80000000:
                    target = st.scope + st.args[1] if st.args[1].startswith(".") else st.args[1]
                    pc = self.labels[target]
            elif st.op == "mv":
                regs[register(st.args[0])] = regs[register(st.args[1])]
            elif st.op == "sw":
                offset, base = st.args[1][:-1].split("(")
                self.memory[regs[register(base)] + immediate(offset)] = regs[register(st.args[0])]
            elif st.op == "ret":
                return regs[1]
            else:
                raise AssertionError(f"unexpected helper instruction: {st.op}")
        raise AssertionError("helper did not return")


class ServiceHelperTests(unittest.TestCase):
    def test_accept_retains_token_on_zero_and_maximal_success_and_clears_it_on_errors(self):
        output = 0x40001080
        for result in ((0, 0x102), (32, 0x7FFFFF02), (0xFFFFFFA6, 32), (0xFFFFFFF2, 0)):
            with self.subTest(result=result):
                vm = AcceptUserM(result)
                vm.memory[output + 4] = 0x12345
                returned = vm.call("accept", 0x301, 0x40001001, 32, output)
                self.assertEqual(returned, result[0])
                self.assertEqual(vm.memory[output], result[0])
                self.assertEqual(vm.memory[output + 4], 0 if result[0] & 0x80000000 else result[1])
                self.assertEqual(vm.calls, [(LAYOUT["CAUSE_SYSCALL"], (22, 0x301, 0x40001001, 32))])


if __name__ == "__main__":
    unittest.main()
