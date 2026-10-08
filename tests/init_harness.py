"""Run init's own M code against the kernel evaluator.

The kernel side is the real boot: `initBootstrap` builds init and hands it the root
authority, then init's checked M source (user/init) runs in a second evaluator whose
`syscall` builtin enters the kernel evaluator through `userSyscall` as init's trap.
Pointer arguments cross the boundary by copy through init's data page. The result
is the task graph a session really builds, with every kernel cross-check applied,
which is what the old per-profile boot tests checked on the kernel's own policy.
"""
from collections import Counter

from source_m import SourceM, LAYOUT as C
from test_kernel import LAIX
from mlang import syntax as msyntax
from mlang.typesys import size_of
from test_ipc_request_reply import ServiceM
from test_simple_services import KeyboardDevices
from test_task import TaskM, TaskEntered, USER_DATA

ROOT = 0x15000
IMAGE_SIZE = 272
SCRATCH = 0x800  # offsets in init's data page for crossing pointer arguments


class UserExited(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class SessionBuilt(Exception):
    """init reached its first sleep: the graph is built and published."""


class KernelM(ServiceM):
    """The kernel with init as its entry module, entered at the first task."""

    def __init__(self, ram=0x200000):
        TaskM.__init__(self, ram=ram, root=LAIX / 'src/kernel/main.m')
        self.wakes = Counter()
        self.copies = []

    def expr(self, node, local):
        # Local aggregates keep word fields; read their byte casts as packed words.
        if isinstance(node, msyntax.Index) and size_of(node.type) == 1:
            address = self.address(node, local)
            if address >= 0x0D000000:
                return (self.memory[address & ~3] >> ((address & 3) * 8)) & 255
        return super().expr(node, local)


def elf(vm, start):
    header = (0x464C457F, 0x00010101, 0, 0, 0x08160002, 1, C['SERVICE_IMAGE_BASE'], 52, 0, 0, 0x00200034, 1, 0)
    for i, word in enumerate(header):
        vm.memory[start + 4 * i] = word
    for i, word in enumerate((1, 256, C['SERVICE_IMAGE_BASE'], 0, 16, 4096, 5, 4096)):
        vm.memory[start + 52 + 4 * i] = word
    for i in range(16):
        vm.memory[start + 256 + i] = i + 10


def boot(session, memory=KeyboardDevices, volume=8192, writable=True, rows=None):
    """A kernel that has run initBootstrap for `session` and entered init."""
    vm = KernelM()
    vm.memory = memory(vm)
    vm.addresses.update(fontData=0x16000, fontDataEnd=0x16000 + 184)
    for i, word in enumerate((0x3146414C, 1, 19, 32, 184, 16, 32, 0)):
        vm.memory[0x16000 + 4 * i] = word
    for offset in range(0, 0x100, 4):  # the display registers a screen grant reads
        dict.__setitem__(vm.memory, C['VIDEO_BASE'] + offset, 0)
    # The catalog is the real one: as many rows as the kernel's asm lists.
    count = rows or len(__import__('re').findall(r'\.word\s+image_', (LAIX / 'src/kernel/init_bootstrap.asm').read_text()))
    elf(vm, 0x20000)
    vm.addresses.update(initImage=0x20000, initImageEnd=0x20000 + IMAGE_SIZE,
                        initCatalog=0x60000, initCatalogEnd=0x60000 + 8 * count)
    for row in range(count):
        start = 0x21000 + 0x1000 * row
        elf(vm, start)
        vm.memory[0x60000 + 8 * row] = start
        vm.memory[0x60000 + 8 * row + 4] = start + IMAGE_SIZE
    for i, word in enumerate((0x31525357, 1, volume, (1 if writable else 0) | session << 8)):
        vm.memory[ROOT + 4 * i] = word
    info = vm.addresses['kernelBootInfo']
    typ = vm.decls['kernelBootInfo'].sym.type
    vm.memory[info + typ.field('disk').offset] = C['DISK0_BASE']
    vm.memory[info + typ.field('imageSize').offset] = 1024
    assert vm.call('initBootstrap')
    try:
        vm.call('taskStart', 1000000)
    except TaskEntered:
        pass
    return vm


# Syscalls whose arguments point into user memory: number -> (argument index, words in, words out).
POINTERS = {
    81: ((1, None, 0),),          # SYS_TASK_HANDLES: entries, 2 words per entry, in
    39: ((1, 0, 11),),            # SYS_TASK_INSPECT: event out
    41: ((1, 0, 11),),            # SYS_TASK_COLLECT: event out
    58: ((1, 0, 8),),             # SYS_IPC_TRY_ACCEPT: message out
    22: ((1, 0, 8),),             # SYS_IPC_ACCEPT: message out
}


class UserInit(SourceM):
    """init.m (and what it imports) as user code over the kernel evaluator."""

    def __init__(self, kernel):
        super().__init__(LAIX / 'user/init/init.m')
        self.kernel = kernel
        self.syscalls = []
        init = kernel.current()
        block = kernel.field('bootPage', init)
        for i in range(10):
            self.memory[C['START_BLOCK_VA'] + 4 * i] = kernel.memory[block + 4 * i]

    def trap(self, cause, args=()):
        number, *a = args
        self.syscalls.append((number, tuple(a)))
        if number == 1:
            raise UserExited(a[0])
        if number == 60:
            raise SessionBuilt()
        kernel = self.kernel
        init = kernel.current()
        page = kernel.pages(init)[1]
        a = list(a) + [0] * (6 - len(a))
        back = []
        for index, words_in, words_out in POINTERS.get(number, ()):
            user = a[index]
            scratch = SCRATCH + 64 * len(back)
            if words_in is None:
                words = 2 * a[2]
                for i in range(words):
                    kernel.memory[page + scratch + 4 * i] = self.memory[user + 4 * i]
            else:
                words = words_out
            a[index] = USER_DATA + scratch
            back.append((user, page + scratch, words_out))
        kernel.invoke(number, *a)
        result = kernel.result(init)[0]
        for user, physical, count in back:
            for i in range(count):
                self.memory[user + 4 * i] = kernel.memory[physical + 4 * i]
        return result

    def call(self, name, *args):
        # The assembly shims that return the reply token beside the length.
        if name in ('ipcAcceptResult', 'ipcTryAcceptResult'):
            handle, buffer, capacity, result = args
            length = self.trap(C['CAUSE_SYSCALL'], (22 if name == 'ipcAcceptResult' else 58, handle, buffer, capacity))
            self.memory[result] = length
            self.memory[result + 4] = self.kernel.result(self.kernel.current())[1]
            return length
        return super().call(name, *args)

    def run(self):
        """Run initMain until it exits or sleeps; returns ('exit', code) or ('built', None)."""
        try:
            self.call('initMain', C['START_BLOCK_VA'], C['RUNTIME_START_BYTES'])
        except UserExited as e:
            return 'exit', e.code
        except SessionBuilt:
            return 'built', None
        return 'returned', None


def run_session(session, **options):
    kernel = boot(session, **options)
    user = UserInit(kernel)
    return kernel, user, user.run()
