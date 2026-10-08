"""Run the checked init (root server) kernel side and runtime calls; no code generation."""
import re
import unittest

from test_ipc_handles import error
from test_kernel import check_m, LAIX
from test_runtime_tasks import create
from test_service_recovery import fixture as recovery_fixture
from test_task import USER_DATA, TaskM
from source_m import LAYOUT as C

EBUSY, EPERM, EINVAL = 16, 1, 22
RIGHT_SEND, RIGHT_RECEIVE = C['RIGHT_SEND'], C['RIGHT_RECEIVE']
SERVICE = 1


def root_fixture(devices=False, catalog=False):
    """Task 1 holds the root authority, as init does after boot."""
    return recovery_fixture(devices=devices, catalog=catalog)


def handle_table_count(vm, ref):
    """Live handles in a task's table."""
    table_type = vm.decls['tasks'].sym.type.target.field('handles').type
    entries = table_type.field('entries')
    handle = entries.type.elem
    base = vm.field_address('handles', ref) + entries.offset
    return sum(bool(vm.memory[base + i * handle.size + handle.field('object').offset])
               for i in range(entries.type.n))


def put_words(vm, task, offset, words):
    page = vm.pages(task)[1]
    for i, word in enumerate(words):
        vm.memory[page + offset + 4 * i] = word
    return USER_DATA + offset


def endpoint_for(vm, receiver):
    vm.invoke(C['SYS_ENDPOINT_CREATE'], SERVICE, receiver)
    token = vm.result(vm.current())[0]
    assert 0 < token < 0x80000000
    return token


class SourceTests(unittest.TestCase):
    def test_sources_check(self):
        for path in ('src/kernel/main.m', 'src/kernel/init_bootstrap.m', 'user/init/init.m',
                     'user/services/consolesrv.m', 'user/syscalls.m'):
            check_m(LAIX / path)

    def test_catalog_rows_match_the_image_constants_and_the_build(self):
        asm = (LAIX / 'src/kernel/init_bootstrap.asm').read_text()
        rows = re.findall(r'\.word\s+image_(\w+),\s*image_\1_end', asm)
        files = re.findall(r'image_(\w+):\s*\.incbin "../../build/init/([\w-]+)\.elf"', asm)
        constants = dict(re.findall(r'let IMAGE_(\w+): UWord = (\d+)', (LAIX / 'user/init/images.m').read_text()))
        self.assertEqual(len(rows), len(constants))
        self.assertEqual(len(rows), len(files))
        self.assertLessEqual(len(rows), 30)  # catalog IDs 2..31
        script = (LAIX / 'tools/build_services.sh').read_text()
        built = re.search(r"init\) images='([^']*)'", script).group(1).split()
        snake = lambda name: re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', name).upper()
        for index, name in enumerate(rows):
            self.assertEqual(int(constants[snake(name)]), index + 2, name)
            self.assertIn(dict(files)[name], built, name)  # the build produces every embedded file
        self.assertIn('init', built)

    def test_session_numbers_are_defined_once(self):
        sys_path = str(LAIX / 'tools')
        import sys
        sys.path.insert(0, sys_path)
        import sessions
        table = sessions.sessions()
        self.assertEqual(table['shell'], 0)
        self.assertEqual(len(set(table.values())), len(table))
        self.assertEqual(max(table.values()), len(table) - 1)
        build = (LAIX / 'build.sh').read_text()
        self.assertIn('tools/sessions.py', build)


class HandleListTests(unittest.TestCase):
    def test_list_installs_attenuated_copies_and_plain_words(self):
        vm = root_fixture()
        root = endpoint_for(vm, 0)
        child = create(vm, configure=False, publish=False)
        before = handle_table_count(vm, child)
        pairs = put_words(vm, 1, 128, [root, RIGHT_SEND, 0xABCD, 0])
        vm.invoke(C['SYS_TASK_HANDLES'], child, pairs, 2)
        self.assertEqual(vm.result(1)[0], 2)
        page = vm.pages(child)[1]
        self.assertEqual([vm.memory[page + 4 * i] for i in (0, 1)], [C['START_HANDLES_MAGIC'], 2])
        token = vm.memory[page + 8]
        self.assertTrue(vm.call('handleLookup', vm.field_address('handles', child), token, RIGHT_SEND))
        self.assertFalse(vm.call('handleLookup', vm.field_address('handles', child), token, RIGHT_RECEIVE))
        self.assertEqual([vm.memory[page + 4 * i] for i in range(3, 8)], [0xABCD, 0, 0, 0, 0])
        self.assertEqual(handle_table_count(vm, child), before + 1)
        # One list per child.
        vm.invoke(C['SYS_TASK_HANDLES'], child, pairs, 2)
        self.assertEqual(vm.result(1)[0], error(EBUSY))

    def test_a_bad_entry_installs_nothing(self):
        vm = root_fixture()
        root = endpoint_for(vm, 0)
        child = create(vm, configure=False, publish=False)
        for entries in ([root, RIGHT_SEND, 999, RIGHT_SEND],       # not a handle
                        [root, RIGHT_SEND, root, 0x80],            # rights outside the set
                        [root, RIGHT_SEND, root, RIGHT_RECEIVE]):  # receive for a foreign task fails at copy
            with self.subTest(entries=entries):
                pairs = put_words(vm, 1, 128, entries)
                vm.invoke(C['SYS_TASK_HANDLES'], child, pairs, 2)
                self.assertEqual(vm.result(1)[0], error(EPERM))
                self.assertEqual(vm.memory[vm.pages(child)[1]], 0)
                self.assertEqual(handle_table_count(vm, child), 0)

    def test_count_bounds_and_foreign_or_published_children(self):
        vm = root_fixture()
        root = endpoint_for(vm, 0)
        child = create(vm, configure=False, publish=False)
        pairs = put_words(vm, 1, 128, [root, RIGHT_SEND] * 7)
        for count in (0, 7):
            vm.invoke(C['SYS_TASK_HANDLES'], child, pairs, count)
            self.assertEqual(vm.result(1)[0], error(EINVAL))
        vm.invoke(C['SYS_TASK_HANDLES'], child, 0xDEAD0000, 1)
        self.assertEqual(vm.result(1)[0], error(14))
        self.assertEqual(handle_table_count(vm, child), 0)
        vm.invoke(C['SYS_TASK_CONFIGURE'], child, 0, 0, 0)
        vm.invoke(C['SYS_TASK_PUBLISH'], child)
        self.assertEqual(vm.result(1)[0], 0)
        vm.invoke(C['SYS_TASK_HANDLES'], child, pairs, 1)
        self.assertEqual(vm.result(1)[0], error(EBUSY))


class AuthorityTests(unittest.TestCase):
    def test_delegation_is_a_subset_before_publication(self):
        vm = root_fixture(catalog=True)
        load = C['IMAGE_LOAD_AUTHORITY']
        vm.memory[vm.field_address('createImages')] = load | 3
        child = create(vm, configure=False, publish=False)
        for images, expected in ((0, error(EINVAL)), (4, error(EPERM)), (load | 4, error(EPERM))):
            vm.invoke(C['SYS_TASK_AUTHORITY'], child, images)
            self.assertEqual(vm.result(1)[0], expected)
            self.assertEqual(vm.field('createImages', child), 0)
        vm.invoke(C['SYS_TASK_AUTHORITY'], child, load)
        self.assertEqual(vm.result(1)[0], 0)
        self.assertEqual(vm.field('createImages', child), load)
        vm.invoke(C['SYS_TASK_CONFIGURE'], child, 0, 0, 0)
        vm.invoke(C['SYS_TASK_PUBLISH'], child)
        vm.invoke(C['SYS_TASK_AUTHORITY'], child, 1)
        self.assertEqual(vm.result(1)[0], error(EBUSY))
        self.assertEqual(vm.field('createImages', child), load)

    def test_without_the_bits_nothing_is_delegated(self):
        vm = root_fixture(catalog=True)
        child = create(vm, configure=False, publish=False)
        vm.invoke(C['SYS_TASK_AUTHORITY'], child, C['IMAGE_LOAD_AUTHORITY'])
        self.assertEqual(vm.result(1)[0], error(EPERM))


class ServiceStartTests(unittest.TestCase):
    def disk_and_fs(self, vm):
        disk = create(vm, configure=False, publish=False)
        fs = create(vm, configure=False, publish=False)
        disk_root = endpoint_for(vm, disk)
        fs_root = endpoint_for(vm, fs)
        vm.invoke(C['SYS_TASK_DEVICES'], disk, C['DEVICE_DISK'])
        irq = vm.result(1)[0]
        self.assertTrue(0 < irq < 0x80000000)
        return disk, fs, disk_root, fs_root, irq

    def test_disk_and_filesystem_records_are_the_boot_records(self):
        vm = root_fixture(devices=True, catalog=True)
        disk, fs, disk_root, fs_root, irq = self.disk_and_fs(vm)
        start = C['SYS_TASK_SERVICE_START']
        vm.invoke(start, disk, C['START_ROLE_DISK'], C['START_PROTOCOL_DISK'], disk_root, 0, irq)
        self.assertEqual(vm.result(1)[0], 0)
        block = vm.field('bootPage', disk)
        words = [vm.memory[block + 4 * i] for i in range(16)]
        self.assertEqual(words[0], C['START_MAGIC'])
        self.assertEqual((words[3], words[4], words[6], words[7]),
                         (C['START_ROLE_DISK'], disk, RIGHT_RECEIVE, C['DEVICE_DISK']))
        self.assertEqual((words[11], words[12], words[15]), (C['START_PROTOCOL_DISK'], 0, irq))
        self.assertTrue(vm.call('handleLookup', vm.field_address('handles', disk), words[5], RIGHT_RECEIVE))
        vm.invoke(start, fs, C['START_ROLE_FILE'], C['START_PROTOCOL_FILE'], fs_root, disk_root, 0)
        self.assertEqual(vm.result(1)[0], 0)
        words = [vm.memory[vm.field('bootPage', fs) + 4 * i] for i in range(16)]
        self.assertEqual((words[3], words[6], words[7]), (C['START_ROLE_FILE'], RIGHT_RECEIVE, 0))
        # The upstream handle is a send-only copy that reaches the Disk endpoint.
        self.assertTrue(vm.call('handleLookup', vm.field_address('handles', fs), words[12], RIGHT_SEND))
        self.assertFalse(vm.call('handleLookup', vm.field_address('handles', fs), words[12], RIGHT_RECEIVE))
        for task in (disk, fs):
            vm.invoke(C['SYS_TASK_PUBLISH'], task)
            self.assertEqual(vm.result(1)[0], 0, task)
            self.assertEqual(vm.field('state', task), 1)

    def test_records_are_refused_without_the_matching_grants(self):
        vm = root_fixture(devices=True, catalog=True)
        disk, fs, disk_root, fs_root, irq = self.disk_and_fs(vm)
        start = C['SYS_TASK_SERVICE_START']
        role_disk, proto_disk = C['START_ROLE_DISK'], C['START_PROTOCOL_DISK']
        for args in ((disk, role_disk, proto_disk, disk_root, 0, 0),                  # no interrupt token
                     (disk, role_disk, proto_disk, disk_root, 0, irq ^ 0x100),         # someone else's token
                     (disk, role_disk, proto_disk, 999, 0, irq),                       # not a handle
                     (disk, role_disk, C['START_PROTOCOL_FILE'], disk_root, 0, irq),   # wrong protocol
                     (disk, C['START_ROLE_INPUT'], C['START_PROTOCOL_INPUT'], disk_root, 0, irq),  # not its rights
                     (fs, C['START_ROLE_FILE'], C['START_PROTOCOL_FILE'], fs_root, 0, 0),           # no Disk below
                     (fs, C['START_ROLE_FILE'], C['START_PROTOCOL_FILE'], fs_root, disk_root, 0)):  # Disk not started
            with self.subTest(args=args):
                before = handle_table_count(vm, args[0])
                vm.invoke(start, *args)
                self.assertGreater(vm.result(1)[0], 0x7FFFFFFF)  # an error
                self.assertEqual(vm.field('bootPage', args[0]), 0)
                self.assertFalse(vm.field('configured', args[0]))
                self.assertEqual(handle_table_count(vm, args[0]), before)  # copies were closed

    def test_foreign_and_started_children_are_refused(self):
        vm = root_fixture(devices=True, catalog=True)
        disk, fs, disk_root, fs_root, irq = self.disk_and_fs(vm)
        start = C['SYS_TASK_SERVICE_START']
        vm.invoke(start, disk, C['START_ROLE_DISK'], C['START_PROTOCOL_DISK'], disk_root, 0, irq)
        self.assertEqual(vm.result(1)[0], 0)
        vm.invoke(start, disk, C['START_ROLE_DISK'], C['START_PROTOCOL_DISK'], disk_root, 0, irq)
        self.assertEqual(vm.result(1)[0], error(EBUSY))
        vm.invoke(start, 0x1234, C['START_ROLE_DISK'], C['START_PROTOCOL_DISK'], disk_root, 0, irq)
        self.assertEqual(vm.result(1)[0], error(EPERM))


class DeviceGrantTests(unittest.TestCase):
    def test_runtime_device_mask_covers_the_card_and_keeps_it_exclusive(self):
        vm = root_fixture(devices=True, catalog=True)
        vm.memory[vm.field_address('deviceFactory')] = C['DEVICE_DISK'] | C['DEVICE_NET'] | C['DEVICE_UART_TX']
        child = create(vm, configure=False, publish=False)
        for devices in (C['DEVICE_NET'] | C['DEVICE_UART_TX'], C['DEVICE_NET'] | C['DEVICE_DISK']):
            vm.invoke(C['SYS_TASK_DEVICES'], child, devices)
            self.assertEqual(vm.result(1)[0], error(EPERM))
        self.assertEqual(vm.field('deviceRights', child), 0)


ROOT = 0x15000
IMAGE_SIZE = 272


def boot_fixture(rows=5, init_valid=True):
    """The init kernel module set with an init image and `rows` catalog rows in memory."""
    vm = TaskM(ram=0x200000, root=LAIX / 'src/kernel/main.m')
    header = (0x464C457F, 0x00010101, 0, 0, 0x08160002, 1,
              C['SERVICE_IMAGE_BASE'], 52, 0, 0, 0x00200034, 1, 0)

    def image(start):
        for i, word in enumerate(header if init_valid or start != 0x20000 else (0,) * 13):
            vm.memory[start + 4 * i] = word
        for i, word in enumerate((1, 256, C['SERVICE_IMAGE_BASE'], 0, 16, 4096, 5, 4096)):
            vm.memory[start + 52 + 4 * i] = word
        for i in range(16):
            vm.memory[start + 256 + i] = i + 10

    image(0x20000)
    vm.addresses.update(initImage=0x20000, initImageEnd=0x20000 + IMAGE_SIZE,
                        initCatalog=0x30000, initCatalogEnd=0x30000 + 8 * rows)
    for row in range(rows):
        start = 0x21000 + 0x1000 * row
        image(start)
        vm.memory[0x30000 + 8 * row] = start
        vm.memory[0x30000 + 8 * row + 4] = start + IMAGE_SIZE
    return vm


class InitBootstrapTests(unittest.TestCase):
    def test_init_is_the_only_task_and_holds_the_root_authority(self):
        vm = boot_fixture()
        self.assertTrue(vm.call('initBootstrap'))
        self.assertEqual([vm.field('state', t) for t in range(1, 4)], [1, 0, 0])
        every_device = (C['DEVICE_UART_TX'] | C['DEVICE_INPUT'] | C['DEVICE_DISK'] |
                        C['DEVICE_FONT'] | C['DEVICE_SCREEN'] | C['DEVICE_NET'])
        self.assertEqual(vm.field('deviceFactory'), every_device)
        self.assertEqual(vm.field('createImages'), 0x3F | C['IMAGE_LOAD_AUTHORITY'])
        self.assertEqual(vm.field('deviceRights'), 0)  # no device is held directly
        table = vm.decls['tasks'].sym.type.target.field('handles').type
        base = vm.field_address('handles')
        self.assertEqual(vm.memory[base + table.field('factoryModes').offset], 3)
        self.assertTrue(vm.memory[base + table.field('factoryRecovery').offset])
        # Start record: runtime start, no endpoint; a self-control row and the service registry.
        block = vm.field('bootPage')
        self.assertEqual(vm.memory[block], C['RUNTIME_START_MAGIC'])
        self.assertEqual(vm.memory[block + 4 * 7], 0)
        self.assertEqual(vm.memory[block + 4 * 9], 0)  # no session asked for: the default
        self.assertEqual(vm.memory[vm.addresses['serviceSupervisors']], vm.field('id'))
        self.assertEqual(vm.call('taskControlLookup', 0, 1), 0)

    def test_the_session_in_the_storage_root_is_init_s_start_argument(self):
        vm = boot_fixture()
        vm.memory[0x15000 + 12] = 0x700  # session 7, read-only
        self.assertTrue(vm.call('initBootstrap'))
        self.assertEqual(vm.memory[vm.field('bootPage') + 4 * 9], 7)

    def test_catalog_ids_follow_the_rows(self):
        vm = boot_fixture(rows=3)
        self.assertTrue(vm.call('initBootstrap'))
        self.assertEqual(vm.field('createImages'), 0xF | C['IMAGE_LOAD_AUTHORITY'])
        for image in range(2, 5):
            self.assertEqual(vm.memory[vm.addresses['runtimeImageStart'] + 4 * (image - 1)],
                             0x21000 + 0x1000 * (image - 2))

    def test_a_missing_catalog_or_a_bad_init_image_leaves_nothing_behind(self):
        for kwargs in ({'rows': 0}, {'init_valid': False}):
            with self.subTest(kwargs):
                vm = boot_fixture(**kwargs)
                free = vm.free_pages()
                self.assertFalse(vm.call('initBootstrap'))
                self.assertEqual(vm.field('state'), 0)
                self.assertEqual(vm.free_pages(), free)


if __name__ == '__main__':
    unittest.main()
