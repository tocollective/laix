"""The init sessions, run for real: init's M code against the kernel evaluator.

Every session below is built by user/init through the runtime syscalls, so the
kernel's own cross-checks (service start records, upstream managers, device grants,
interrupt tokens, exclusive extents) apply to the wiring. Each test checks who got
which authority and which handle, as the per-profile boot tests used to.
"""
import unittest

from init_harness import boot, run_session, UserInit, ROOT
from source_m import LAYOUT as C
from test_ipc_handles import error
from test_kernel import check_m, LAIX
from test_net_device import NetDevices
from test_screen_services import Devices
from test_simple_services import KeyboardDevices

RIGHT_SEND, RIGHT_RECEIVE = C['RIGHT_SEND'], C['RIGHT_RECEIVE']
UART, INPUT, DISK, FONT, SCREEN, NET = (C[name] for name in (
    'DEVICE_UART_TX', 'DEVICE_INPUT', 'DEVICE_DISK', 'DEVICE_FONT', 'DEVICE_SCREEN', 'DEVICE_NET'))
LOAD = C['IMAGE_LOAD_AUTHORITY']
READY, RUNNING = 1, 2
INIT = 1
SESSIONS = {name: number for number, name in enumerate(
    ('shell', 'console', 'services', 'loader', 'fs', 'net', 'screen', 'recovery', 'screenrecovery'))}


def states(vm, count):
    return [vm.field('state', task) for task in range(1, count + 1)]


def rights(vm, count):
    return [vm.field('deviceRights', task) for task in range(1, count + 1)]


def start_words(vm, task, count=16):
    block = vm.field('bootPage', task)
    return [vm.memory[block + 4 * i] for i in range(count)]


def handle_list(vm, task):
    """The start handle list in the child's data page: (handles, plain words)."""
    page = vm.pages(task)[1]
    assert vm.memory[page] == C['START_HANDLES_MAGIC'], 'no start handle list'
    return [vm.memory[page + 8 + 4 * i] for i in range(vm.memory[page + 4])]


def points_at(vm, task, token, rights_needed, server):
    """`token` is in `task`'s table with these rights and names an endpoint `server` receives on."""
    endpoint = vm.call('handleLookup', vm.field_address('handles', task), token, rights_needed)
    if not endpoint:
        return False
    manager = vm.memory[endpoint + vm.decls['endpoints'].sym.type.elem.field('manager').offset]
    return manager == vm.field('id', server)


class SessionSourceTests(unittest.TestCase):
    def test_init_sources_check(self):
        for path in ('user/init/init.m', 'user/init/session.m', 'user/init/lib.m', 'user/recovery/supervisor.m',
                     'user/recovery/screen_supervisor.m', 'user/recovery/rclient.m', 'user/recovery/sclient.m',
                     'user/apps/banner.m'):
            check_m(LAIX / path)

    def test_session_names_match_the_numbers_used_here(self):
        import sys
        sys.path.insert(0, str(LAIX / 'tools'))
        import sessions
        self.assertEqual(sessions.sessions(), SESSIONS)


class ConsoleSessionTests(unittest.TestCase):
    def test_console_server_holds_the_uart_and_the_banner_only_a_send_handle(self):
        vm, user, outcome = run_session(SESSIONS['console'], memory=Devices, writable=False)[0:3]
        self.assertEqual(outcome, ('built', None))
        console, banner = 2, 3
        self.assertEqual(states(vm, 3), [RUNNING, READY, READY])
        self.assertEqual(rights(vm, 3), [0, UART, 0])
        block = start_words(vm, banner)
        self.assertEqual((block[0], block[7], block[8]), (C['RUNTIME_START_MAGIC'], block[7], RIGHT_SEND))
        self.assertTrue(points_at(vm, banner, block[7], RIGHT_SEND, console))
        server = start_words(vm, console)
        self.assertEqual(server[8], RIGHT_RECEIVE)
        self.assertTrue(points_at(vm, console, server[7], RIGHT_RECEIVE, console))
        self.assertEqual([vm.field('createImages', t) for t in (console, banner)], [0, 0])


class ServicesSessionTests(unittest.TestCase):
    def check_graph(self, vm, client_images=0):
        inp, disk, files, app = 2, 3, 4, 5
        self.assertEqual(states(vm, 5), [RUNNING, READY, READY, READY, READY])
        self.assertEqual(rights(vm, 5), [0, INPUT, DISK, 0, 0])
        roles = [start_words(vm, task)[3] for task in (inp, disk, files, app)]
        self.assertEqual(roles, [C['START_ROLE_INPUT'], C['START_ROLE_DISK'], C['START_ROLE_FILE'], C['START_ROLE_CLIENT']])
        # Files reaches Disk, the application reaches Files and Input, nothing else.
        self.assertTrue(points_at(vm, files, start_words(vm, files)[12], RIGHT_SEND, disk))
        block = start_words(vm, app)
        self.assertTrue(points_at(vm, app, block[5], RIGHT_SEND, files))
        self.assertTrue(points_at(vm, app, block[12], RIGHT_SEND, inp))
        self.assertEqual(vm.field('createImages', app), client_images)
        self.assertEqual([vm.field('createImages', t) for t in (inp, disk, files)], [0, 0, 0])
        # The keyboard and the disk are each owned by one task.
        self.assertEqual(vm.globals['inputOwner'], vm.field('id', inp))

    def test_services_wiring_and_authority(self):
        vm, user, outcome = run_session(SESSIONS['services'], writable=False)
        self.assertEqual(outcome, ('built', None))
        self.check_graph(vm)
        self.assertEqual(vm.call('diskFlags', vm.field('id', 3)), 0)  # read-only extent

    def test_loader_client_alone_holds_the_load_authority(self):
        vm, user, outcome = run_session(SESSIONS['loader'], writable=False)
        self.assertEqual(outcome, ('built', None))
        self.check_graph(vm, client_images=LOAD)

    def test_fs_disk_is_writable_and_a_read_only_root_refuses_it(self):
        vm, user, outcome = run_session(SESSIONS['fs'])
        self.assertEqual(outcome, ('built', None))
        self.check_graph(vm)
        self.assertEqual(vm.call('diskFlags', vm.field('id', 3)), 1)
        # Without the writable bit the write window is refused, and nothing stays behind.
        vm, user, outcome = run_session(SESSIONS['fs'], writable=False)
        self.assertEqual(outcome, ('exit', 2))
        self.assertEqual(states(vm, 6), [RUNNING, 0, 0, 0, 0, 0])


class ShellSessionTests(unittest.TestCase):
    def test_roles_authority_and_devices(self):
        vm, user, outcome = run_session(SESSIONS['shell'])
        self.assertEqual(outcome, ('built', None))
        console, disk, fs, exec_, shell = 2, 3, 4, 5, 6
        self.assertEqual(states(vm, 6), [RUNNING] + [READY] * 5)
        # Only Exec may load programs, and only init may create catalog images.
        self.assertEqual([vm.field('createImages', t) for t in (INIT, console, disk, fs, exec_, shell)],
                         [vm.field('createImages', INIT), 0, 0, 0, LOAD, 0])
        self.assertEqual(rights(vm, 6), [0, UART, DISK, 0, 0, INPUT])
        self.assertEqual(vm.call('diskFlags', vm.field('id', disk)), 1)
        self.assertEqual(vm.call('diskFlags', vm.field('id', fs)), error(1))
        self.assertEqual(vm.globals['inputOwner'], vm.field('id', shell))
        roles = {task: start_words(vm, task)[3] for task in (disk, fs)}
        self.assertEqual(roles, {disk: C['START_ROLE_DISK'], fs: C['START_ROLE_FILE']})
        # Handle lists: Exec [Fs, console], Shell [Fs, Exec, console], all send-only.
        for task, servers in ((exec_, (fs, console)), (shell, (fs, exec_, console))):
            tokens = handle_list(vm, task)
            self.assertEqual(len(tokens), len(servers))
            for token, server in zip(tokens, servers):
                self.assertTrue(points_at(vm, task, token, RIGHT_SEND, server), (task, server))
                self.assertFalse(vm.call('handleLookup', vm.field_address('handles', task), token, RIGHT_RECEIVE))
        for task in (exec_, console):
            self.assertTrue(points_at(vm, task, start_words(vm, task)[7], RIGHT_RECEIVE, task))
        self.assertEqual(start_words(vm, shell)[7], 0)  # the shell's list carries everything

    def test_a_read_only_root_leaves_no_child_and_init_reports_it(self):
        free = None
        vm = boot(SESSIONS['shell'], writable=False)
        free = vm.free_pages()
        user = UserInit(vm)
        self.assertEqual(user.run(), ('exit', 2))
        self.assertEqual(states(vm, 7), [RUNNING, 0, 0, 0, 0, 0, 0])
        self.assertEqual(vm.free_pages(), free)
        self.assertEqual(vm.globals['inputOwner'], 0)

    def test_an_unknown_session_is_an_error_not_the_default(self):
        vm, user, outcome = run_session(200)
        self.assertEqual(outcome, ('exit', 2))
        self.assertEqual(states(vm, 3), [RUNNING, 0, 0])


class NetSessionTests(unittest.TestCase):
    def test_only_the_driver_holds_the_card_and_the_client_reaches_ip_and_console(self):
        vm, user, outcome = run_session(SESSIONS['net'], memory=NetDevices, writable=False)
        self.assertEqual(outcome, ('built', None))
        console, driver, ip, client = 2, 3, 4, 5
        self.assertEqual(states(vm, 5), [RUNNING] + [READY] * 4)
        self.assertEqual(rights(vm, 5), [0, UART, NET, 0, 0])
        # The driver's one start word is its interrupt token; IP holds the driver, the client IP and console.
        irq = handle_list(vm, driver)
        self.assertEqual(len(irq), 1)
        self.assertTrue(vm.call('irqTokenValid', vm.field('id', driver), irq[0], C['ETH_IRQ']))
        self.assertTrue(points_at(vm, ip, handle_list(vm, ip)[0], RIGHT_SEND, driver))
        tokens = handle_list(vm, client)
        self.assertTrue(points_at(vm, client, tokens[0], RIGHT_SEND, ip))
        self.assertTrue(points_at(vm, client, tokens[1], RIGHT_SEND, console))
        for task in (driver, ip):
            self.assertTrue(points_at(vm, task, start_words(vm, task)[7], RIGHT_RECEIVE, task))
        self.assertEqual(start_words(vm, client)[7], 0)


class ScreenSessionTests(unittest.TestCase):
    def test_display_storage_and_application(self):
        vm, user, outcome = run_session(SESSIONS['screen'], memory=Devices, writable=False)
        self.assertEqual(outcome, ('built', None))
        storage, screen, app = 2, 3, 4
        self.assertEqual(states(vm, 4), [RUNNING] + [READY] * 3)
        self.assertEqual(rights(vm, 4), [0, FONT, SCREEN, 0])
        roles = [start_words(vm, task)[3] for task in (storage, screen, app)]
        self.assertEqual(roles, [C['START_ROLE_STORAGE'], C['START_ROLE_SERVER'], C['START_ROLE_CLIENT']])
        server = start_words(vm, screen)
        self.assertTrue(points_at(vm, screen, server[12], RIGHT_SEND, storage))  # bitmap storage
        self.assertTrue(vm.call('irqTokenValid', vm.field('id', screen), server[15], C['VIDEO_IRQ']))
        self.assertTrue(points_at(vm, app, start_words(vm, app)[5], RIGHT_SEND, screen))
        # The display rows are mapped for Screen alone.
        root = vm.field('directory', screen)
        self.assertEqual(vm.leaf(C['SCREEN_VIDEO_VA'], root), C['VIDEO_BASE'] | 19)
        self.assertEqual(vm.leaf(C['SCREEN_VIDEO_VA'], vm.field('directory', app)), 0)


class RecoverySessionTests(unittest.TestCase):
    def test_recovery_init_is_the_supervisor_of_echo_disk_and_files(self):
        vm, user, outcome = run_session(SESSIONS['recovery'], memory=Devices, writable=False)
        self.assertEqual(outcome, ('built', None))
        echo, disk, files, client = 2, 3, 4, 5
        self.assertEqual(states(vm, 5), [RUNNING] + [READY] * 4)
        self.assertEqual(rights(vm, 5), [0, 0, DISK, 0, 0])
        # Init holds the control row of every child; the children hold none.
        for task in (echo, disk, files, client):
            self.assertTrue(vm.call('taskGet', vm.field('id', task)))
        # Only the client may resolve names, and only those of its mask (1 and 2).
        self.assertEqual([vm.field('resolverMask', task) for task in (echo, disk, files)], [0, 0, 0])
        self.assertEqual(vm.field('resolverMask', client), 3)

    def test_screenrecovery_runs_the_display_chain_under_init(self):
        vm, user, outcome = run_session(SESSIONS['screenrecovery'], memory=Devices, writable=False)
        self.assertEqual(outcome, ('built', None))
        echo, bitmap, screen, client = 2, 3, 4, 5
        self.assertEqual(states(vm, 5), [RUNNING] + [READY] * 4)
        self.assertEqual(rights(vm, 5), [0, 0, FONT, SCREEN, 0])
        self.assertEqual(vm.field('resolverMask', client), 5)  # names 1 and 3


if __name__ == '__main__':
    unittest.main()
