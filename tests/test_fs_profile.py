"""LAIX_CONSOLE=fs boot policy: a writable Disk owner under the approved root."""
import unittest

from source_m import LAYOUT as C
from test_ipc_handles import error
from test_kernel import LAIX, check_m
from test_simple_services import KeyboardDevices
from test_task import TaskM

ROOT = 0x15000
EROFS = 30


def fixture(fail=None, flags=1, size=8192):
    vm = TaskM(ram=0x200000, root=LAIX / 'src/kernel/fs_main.m')
    vm.memory = KeyboardDevices(vm)
    vm.addresses.update(fontData=0x16000, fontDataEnd=0x16000 + 184)
    for i, word in enumerate((0x31525357, 1, size, flags)):
        vm.memory[ROOT + 4 * i] = word
    for index, name in enumerate(('inputImage', 'diskImage', 'fsImage', 'fsClientImage')):
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


class FsProfileTests(unittest.TestCase):
    def test_sources_check(self):
        for path in ('src/kernel/fs_main.m', 'src/kernel/fs_bootstrap.m', 'user/services/fs.m',
                     'user/services/fsclient.m', 'tests/programs/fs/client.m'):
            check_m(LAIX / path)

    def test_roles_rights_and_a_writable_whole_root_extent(self):
        vm = fixture()
        self.assertTrue(vm.call('bootstrapFsInit'))
        # (task, role, protocol, devices, rights): Input, Disk, Fs, client.
        for id, role, protocol, devices, rights in ((1, 4, 5, 8, 2), (2, 5, 6, 16, 2), (3, 6, 7, 0, 2), (4, 2, 7, 0, 1)):
            block = vm.field('bootPage', id)
            words = [vm.memory[block + i * 4] for i in range(16)]
            self.assertEqual(words[:5], [C['START_MAGIC'], 2, 64, role, id])
            self.assertEqual((words[6], words[7], words[11]), (rights, devices, protocol))
            self.assertEqual(vm.field('deviceRights', id), devices)
            self.assertEqual(vm.field('createImages', id), 0)  # nobody may create or load tasks
            self.assertEqual(vm.field('state', id), 1)
        self.assertEqual(vm.call('diskInfo', 2), 8192)
        self.assertEqual(vm.call('diskFlags', 2), 1)  # EXTENT_WRITE
        self.assertEqual(vm.call('diskInfo', 3), error(1))  # the filesystem has no device
        self.assertEqual(vm.call('diskFlags', 3), error(1))
        # The filesystem reaches Disk only through its endpoint; the client has no
        # device and no Disk endpoint at all.
        self.assertEqual(vm.call('deviceSubmit', 3, 0, 512, C['DISK_WRITE'], 0x40001000), error(1))
        self.assertEqual(vm.call('deviceSubmit', 4, 0, 0, C['DISK_FLUSH'], 0), error(1))

    def test_a_read_only_root_boots_nothing(self):
        vm = fixture(flags=0)
        free = vm.free_pages()
        self.assertFalse(vm.call('bootstrapFsInit'))
        self.assertEqual(vm.free_pages(), free)
        self.assertTrue(vm.call('taskInitAvailable'))
        # Fixing the root lets the same machine boot: nothing was left behind.
        vm.memory[ROOT + 12] = 1
        vm.memory[ROOT + 8] = 8192
        self.assertTrue(vm.call('bootstrapFsInit'))

    def test_a_misaligned_writable_root_is_not_a_root(self):
        vm = fixture(size=8191)
        self.assertEqual(vm.call('approvedStorageBytes'), 0)
        self.assertFalse(vm.call('bootstrapFsInit'))

    def test_every_construction_failure_rolls_back_and_can_retry(self):
        for fail in range(1, 30):
            vm = fixture(fail)
            free = vm.free_pages()
            if vm.call('bootstrapFsInit'):
                self.assertGreater(fail, 10)  # past the last mapping the boot succeeds
                continue
            self.assertEqual(vm.free_pages(), free, fail)
            self.assertTrue(vm.call('taskInitAvailable'))
            self.assertEqual(vm.globals['devicesInitialized'], 0)
            self.assertEqual(vm.globals['inputOwner'], 0)
            vm.fail_mapping = None
            self.assertTrue(vm.call('bootstrapFsInit'), fail)
            typ = vm.decls['deviceExtent'].sym.type
            owner = vm.memory[vm.addresses['deviceExtent'] + typ.field('owner').offset]
            self.assertEqual(vm.call('diskFlags', owner), 1)  # discarded tasks leave no authority behind


if __name__ == '__main__':
    unittest.main()
