"""Small source evaluator for boot/trap acceptance checks; no code generation.

UART output and CPU builtin transitions are fixtures. Integer logic, memory
validation, dispatch decisions and frame updates execute the checked M AST.
"""

from test_kernel import LAIX, parse_asm, asm_constants
from test_memory import BootstrapM, Continued
from mlang import syntax as s
from mlang.typesys import size_of

LAYOUT = asm_constants(parse_asm(LAIX / "src/trap/trap_layout.inc"))
# Synthetic kernel stack for supervisor traps raised by M builtins.
TRAP_STACK_BOTTOM = 0x9E000
TRAP_FRAME = 0x9F000


class KernelPanic(Exception):
    pass


class Broken(Exception):
    pass


COMPOUND = {"+=": "+", "-=": "-", "*=": "*", "/=": "/", "%=": "%",
            "&=": "&", "|=": "|", "<<=": "<<", ">>=": ">>"}


class SourceM(BootstrapM):
    binary = dict(BootstrapM.binary, **{
        "<<": lambda a, b: a << (b & 31), ">>": lambda a, b: a >> (b & 31),
        "%": lambda a, b: a % b, "^": lambda a, b: a ^ b})

    def __init__(self, root, memory=None):
        super().__init__(root=root)
        if memory is not None:
            self.memory = memory
        # Checked global structs need backing storage just like global arrays.
        struct_address = 0x0C000000
        for name, decl in self.decls.items():
            if isinstance(decl, s.VarDecl) and not decl.extern and decl.sym.type.kind == "struct":
                self.addresses[name] = struct_address
                for offset in range(0, size_of(decl.sym.type), 4):
                    self.memory[struct_address + offset] = 0
                struct_address += (size_of(decl.sym.type) + 7) & ~7
        for name, decl in self.decls.items():
            if isinstance(decl, s.VarDecl) and isinstance(decl.init, s.ArrayLit):
                for i, elem in enumerate(decl.init.elems):
                    self.memory[self.addresses[name] + i * size_of(elem.type)] = self.expr(elem, {})
        # Struct arrays may contain byte-sized Bool fields between words.
        # This evaluator stores primitive values at their exact field address.
        def zero_fields(address, typ):
            if typ.kind == "struct":
                for field in typ.fields:
                    zero_fields(address + field.offset, field.type)
            elif typ.kind == "array":
                for i in range(typ.n):
                    zero_fields(address + i * size_of(typ.elem), typ.elem)
            else:
                self.memory.setdefault(address, 0)
        for name, decl in self.decls.items():
            if isinstance(decl, s.VarDecl) and not decl.extern and name in self.addresses:
                zero_fields(self.addresses[name], decl.sym.type)
        self.controls = {0: 0, 6: 0}
        self.output = []
        self.local_storage = 0x0D000000

    def address(self, node, local):
        if isinstance(node, s.Name) and node.name in local and node.type.kind in ("struct", "array"):
            return local[node.name]
        if isinstance(node, s.Member):
            struct = node.obj.type
            if struct.kind == "ptr":
                struct = struct.target
                base = self.expr(node.obj, local)
            else:
                base = self.address(node.obj, local)
            return base + struct.field(node.name).offset
        if isinstance(node, s.Unary) and node.op == "*":
            return self.expr(node.operand, local)
        if isinstance(node, s.Index):
            # Arrays decay to their address; elements have their own size.
            return (self.expr(node.obj, local) +
                    size_of(node.type) * self.expr(node.index, local))
        return super().address(node, local)

    def expr(self, node, local):
        if isinstance(node, s.Name) and node.type.kind == "struct":
            return self.address(node, local)
        if isinstance(node, s.StringLit):
            return node.value
        if isinstance(node, s.NullLit):
            return 0
        if isinstance(node, s.CharLit):
            return node.value if isinstance(node.value, int) else ord(node.value)
        if isinstance(node, s.TypeQuery):
            return node.const
        if (isinstance(node, (s.Member, s.Index)) or
                isinstance(node, s.Unary) and node.op == "*"):
            address = self.address(node, local)
            if node.type.kind in ("array", "struct"):
                return address
            return self.memory[address]
        if isinstance(node, s.Unary) and node.op == "-":
            return -self.expr(node.operand, local) & 0xFFFFFFFF
        if isinstance(node, s.Binary) and node.op in ("<", "<=", ">", ">="):
            left = self.expr(node.left, local)
            right = self.expr(node.right, local)
            # Word comparisons use signed operands even though register and
            # memory fixtures store the same values as unsigned bit patterns.
            if getattr(node.left.type, "signed", False):
                bits = node.left.type.size * 8
                left = (left + (1 << (bits - 1))) % (1 << bits) - (1 << (bits - 1))
                right = (right + (1 << (bits - 1))) % (1 << bits) - (1 << (bits - 1))
            return self.binary[node.op](left, right)
        if isinstance(node, s.BuiltinCall):
            args = [self.expr(arg, local) for arg in node.args]
            if node.name == "mfcr" and args == [0]:
                return self.controls[0]
            if node.name == "breakpoint":
                self.trap(LAYOUT["CAUSE_BREAKPOINT"])
                return
            if node.name == "syscall":
                return self.trap(LAYOUT["CAUSE_SYSCALL"], args)
        return super().expr(node, local)

    def write(self, target, value, local):
        if target.type.kind == "struct":
            address = self.address(target, local)
            words = [self.memory[value + i] for i in range(0, target.type.size, 4)]
            for i, word in enumerate(words):
                self.memory[address + 4 * i] = word
        elif isinstance(target, s.Name):
            scope = local if target.name in local else self.globals
            scope[target.name] = value
        else:
            self.memory[self.address(target, local)] = value

    def statement(self, node, local):
        if isinstance(node, s.VarDecl) and node.var.type.kind == "struct":
            # Each invocation gets its own aggregate locals; never alias globals.
            address = self.local_storage
            self.local_storage += (size_of(node.var.type) + 7) & ~7
            local[node.name] = address
            if node.init is None:
                for offset in range(0, size_of(node.var.type), 4):
                    self.memory[address + offset] = 0
            else:
                raise AssertionError("unsupported aggregate local initializer")
        elif isinstance(node, s.Switch):
            value = self.expr(node.value, local)
            selected = next((case for case in node.cases if case.value is not None
                             and self.expr(case.value, local) == value), None)
            if selected is None:
                selected = next(case for case in node.cases if case.value is None)
            # Tested handlers terminate every selected case via return/panic.
            for st in selected.body:
                self.statement(st, local)
        elif isinstance(node, s.Assign):
            value = self.expr(node.value, local)
            if node.op != "=":
                value = self.binary[COMPOUND[node.op]](self.expr(node.target, local),
                                                       value) & 0xFFFFFFFF
            self.write(node.target, value, local)
        elif isinstance(node, s.While):
            try:
                while self.expr(node.cond, local):
                    try:
                        self.statement(node.body, local)
                    except Continued:
                        continue
            except Broken:
                pass
        elif isinstance(node, s.Break):
            raise Broken()
        elif isinstance(node, s.For):
            try:
                super().statement(node, local)
            except Broken:
                pass
        elif isinstance(node, s.IncDec):
            step = 1 if node.op == "++" else -1
            self.write(node.target, (self.expr(node.target, local) + step) & 0xFFFFFFFF, local)
        else:
            super().statement(node, local)

    def call(self, name, *args):
        if name == "panic":
            raise KernelPanic(args[0])
        if name == "debugPrint":
            self.output.append(args)
            return
        decl = self.decls.get(name)
        if (isinstance(decl, s.FuncDecl) and decl.params and
                isinstance(decl.params[-1].type, s.VariadicType)):
            # M 'args: ...': the trailing arguments travel as one borrowed pack.
            fixed = len(decl.params) - 1
            args = args[:fixed] + (tuple(args[fixed:]),)
        if name == "trapRegisterSelfTest":
            # The assembly body is exercised separately by EntryMachine; here
            # it arms and raises its BREAK and SYSCALL like the real one.
            for cause in (LAYOUT["CAUSE_BREAKPOINT"], LAYOUT["CAUSE_SYSCALL"]):
                self.call("trapExpect", cause)
                self.trap(cause)
            return 0
        return super().call(name, *args)

    def trap(self, cause, args=()):
        """A supervisor BREAK/SYSCALL through the real trapDispatch; returns r1.

        args are syscall(number, a1..a6): the number in r9, arguments in r1-r6.
        """
        frame = TRAP_FRAME
        for offset in range(0, LAYOUT["TF_SIZE"], LAYOUT["WORD_BYTES"]):
            self.memory[frame + offset] = 0
        if args:
            self.memory[frame + LAYOUT["TF_R9"]] = args[0]
            for i, value in enumerate(args[1:]):
                self.memory[frame + LAYOUT["TF_R1"] + i * LAYOUT["WORD_BYTES"]] = value
        self.memory[frame + LAYOUT["TF_STATUS"]] = LAYOUT["STATUS_EXL"]
        self.memory[frame + LAYOUT["TF_CAUSE"]] = cause
        self.memory[LAYOUT["KERNEL_STACK_BOTTOM"]] = TRAP_STACK_BOTTOM
        self.memory[TRAP_STACK_BOTTOM] = LAYOUT["STACK_CANARY"]
        self.call("trapDispatch", frame)
        if self.memory[frame + LAYOUT["TF_EPC"]] != LAYOUT["INSTRUCTION_BYTES"]:
            raise AssertionError("trap returned without skipping BREAK/SYSCALL")
        return self.memory[frame + LAYOUT["TF_R1"]]
