"""LAIX_CONSOLE=shell boot policy: who gets which handle and which authority."""
import unittest

from source_m import LAYOUT as C
from test_ipc_handles import error
from test_kernel import LAIX, check_m
from test_simple_services import KeyboardDevices
from test_task import TaskM

ROOT = 0x15000
RIGHT_SEND, RIGHT_RECEIVE = C['RIGHT_SEND'], C['RIGHT_RECEIVE']
DISK, FS, CONSOLE, EXEC, SHELL = 1, 2, 3, 4, 5


def fixture(fail=None, flags=1):
    vm = TaskM(ram=0x200000, root=LAIX / 'tests/programs/boot/shell_main.m')
    vm.memory = KeyboardDevices(vm)
    vm.addresses.update(fontData=0x16000, fontDataEnd=0x16000 + 184,
                        bootstrapServerStart=0x14000, bootstrapServerEnd=0x1401C)
    for i, word in enumerate((0x31525357, 1, 8192, flags)):
        vm.memory[ROOT + 4 * i] = word
    for index, name in enumerate(('diskImage', 'fsImage', 'execImage', 'shellImage')):
        start = 0x20000 + index * 4096
        vm.addresses[name], vm.addresses[name + 'End'] = start, start + 272
        header = (0x464C457F, 0x00010101, 0, 0, 0x08160002, 1,
                  C['SERVICE_IMAGE_BASE'], 52, 0, 0, 0x00200034, 1, 0)
        for i, word in enumerate(header):
            vm.memory[start + 4 * i] = word
        for i, word in enumerate((1, 256, C['SERVICE_IMAGE_BASE'], 0, 16, 4096, 5, 4096)):
            vm.memory[start + 52 + 4 * i] = word
        for i in range(16):
            vm.memory[start + 256 + i] = i + 10
    info = vm.addresses['kernelBootInfo']
    typ = vm.decls['kernelBootInfo'].sym.type
    vm.memory[info + typ.field('disk').offset] = C['DISK0_BASE']
    vm.memory[info + typ.field('imageSize').offset] = 1024
    vm.fail_mapping = fail
    return vm


def handle_list(vm, task):
    page = vm.pages(task)[1]
    count = vm.memory[page + 4]
    assert vm.memory[page] == C['START_HANDLES_MAGIC']
    return [vm.memory[page + 8 + 4 * i] for i in range(count)], [vm.memory[page + 8 + 4 * i] for i in range(count, 6)]


def points_at(vm, task, token, rights, server):
    """The handle is in `task`'s table with exactly these rights and its endpoint is managed by `server`."""
    endpoint = vm.call('handleLookup', vm.field_address('handles', task), token, rights)
    if not endpoint:
        return False
    typ = vm.decls['tasks'].sym.type
    manager = vm.memory[endpoint + vm.decls['endpoints'].sym.type.elem.field('manager').offset]
    return manager == vm.field('id', server)


class ShellProfileTests(unittest.TestCase):
    def test_sources_check(self):
        for path in ('tests/programs/boot/shell_main.m', 'tests/programs/boot/shell_bootstrap.m', 'user/apps/shell.m',
                     'user/services/exec.m', 'user/bin/hello.m', 'user/bin/count.m', 'user/bin/spin.m'):
            check_m(LAIX / path)

    def test_roles_authority_and_devices(self):
        vm = fixture()
        self.assertTrue(vm.call('bootstrapShellInit'))
        for task in range(1, 6):
            self.assertEqual(vm.field('state', task), 1)
        # Only Exec may load programs; nobody may create catalog images.
        self.assertEqual([vm.field('createImages', t) for t in range(1, 6)], [0, 0, 0, C['IMAGE_LOAD_AUTHORITY'], 0])
        # Devices: Disk the disk, the console server UART transmit, the shell the keyboard.
        self.assertEqual([vm.field('deviceRights', t) for t in range(1, 6)],
                         [C['DEVICE_DISK'], 0, C['DEVICE_UART_TX'], 0, C['DEVICE_INPUT']])
        self.assertEqual(vm.call('diskFlags', vm.field('id', DISK)), 1)
        self.assertEqual(vm.call('diskFlags', vm.field('id', FS)), error(1))
        self.assertEqual(vm.globals['inputOwner'], vm.field('id', SHELL))
        # Start records: servers get the checked service block, Exec and the shell the runtime one.
        disk, fs = (vm.field('bootPage', t) for t in (DISK, FS))
        self.assertEqual([vm.memory[disk + 4 * i] for i in (3, 6, 7, 11)], [5, 2, 16, 6])
        self.assertEqual([vm.memory[fs + 4 * i] for i in (3, 6, 7, 11)], [6, 2, 0, 7])
        console = vm.field('bootPage', CONSOLE)
        self.assertEqual([vm.memory[console + 4 * i] for i in (3, 6, 7, 11)], [1, 2, 1, 2])
        for task in (EXEC, SHELL):
            block = vm.field('bootPage', task)
            self.assertEqual(vm.memory[block], C['RUNTIME_START_MAGIC'])
        exec_block = vm.field('bootPage', EXEC)
        self.assertNotEqual(vm.memory[exec_block + 4 * 7], 0)       # its receive handle
        self.assertEqual(vm.memory[exec_block + 4 * 8], RIGHT_RECEIVE)
        self.assertEqual(vm.memory[vm.field('bootPage', SHELL) + 4 * 7], 0)  # the shell's list carries everything

    def test_each_handle_reaches_exactly_the_intended_server(self):
        vm = fixture()
        self.assertTrue(vm.call('bootstrapShellInit'))
        handles, unused = handle_list(vm, EXEC)
        self.assertEqual((len(handles), unused), (2, [0, 0, 0, 0]))
        self.assertTrue(points_at(vm, EXEC, handles[0], RIGHT_SEND, FS))
        self.assertTrue(points_at(vm, EXEC, handles[1], RIGHT_SEND, CONSOLE))
        shell, unused = handle_list(vm, SHELL)
        self.assertEqual((len(shell), unused), (3, [0, 0, 0]))
        self.assertTrue(points_at(vm, SHELL, shell[0], RIGHT_SEND, FS))
        self.assertTrue(points_at(vm, SHELL, shell[1], RIGHT_SEND, EXEC))
        self.assertTrue(points_at(vm, SHELL, shell[2], RIGHT_SEND, CONSOLE))
        # Exec's start endpoint receives; the others hold send rights only to their targets.
        exec_receive = vm.memory[vm.field('bootPage', EXEC) + 4 * 7]
        self.assertTrue(points_at(vm, EXEC, exec_receive, RIGHT_RECEIVE, EXEC))
        self.assertFalse(points_at(vm, SHELL, shell[0], RIGHT_SEND, DISK))  # no route to Disk
        self.assertFalse(points_at(vm, EXEC, handles[0], RIGHT_RECEIVE, FS))  # send only
        # Fs reaches Disk through its start record's auxiliary endpoint.
        aux = vm.memory[vm.field('bootPage', FS) + 4 * 12]
        self.assertTrue(points_at(vm, FS, aux, RIGHT_SEND, DISK))

    def test_a_read_only_root_boots_nothing(self):
        vm = fixture(flags=0)
        free = vm.free_pages()
        self.assertFalse(vm.call('bootstrapShellInit'))
        self.assertEqual(vm.free_pages(), free)
        self.assertTrue(vm.call('taskInitAvailable'))

    def test_every_construction_failure_rolls_back_and_can_retry(self):
        for fail in range(1, 40):
            vm = fixture(fail)
            free = vm.free_pages()
            if vm.call('bootstrapShellInit'):
                self.assertGreater(fail, 12)
                continue
            self.assertEqual(vm.free_pages(), free, fail)
            self.assertTrue(vm.call('taskInitAvailable'))
            self.assertEqual(vm.globals['devicesInitialized'], 0)
            self.assertEqual(vm.globals['inputOwner'], 0)
            vm.fail_mapping = None
            self.assertTrue(vm.call('bootstrapShellInit'), fail)


if __name__ == '__main__':
    unittest.main()
