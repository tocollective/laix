"""G7 write/flush contract (DEVICE_CONTRACT) against the checked broker source."""
import sys
import unittest

from source_m import LAYOUT as C
from test_ipc_handles import error
from test_kernel import LAIX
from test_screen_services import Devices, kernel_fixture
from test_task import USER_DATA

ROOT = 0x15000
SECTORS = 64
READ, WRITE, FLUSH = 1, 2, 3
EROFS, EINVAL, EFAULT, EBUSY, EPIPE, EIO, EOVERFLOW = 30, 22, 14, 16, 32, 5, 75
NOT_MAPPED = 0x61000000


class ImageDevices(Devices):
    """Disk with contents: READ and WRITE move bytes between RAM and an image."""

    def __init__(self, vm):
        super().__init__(vm)
        dict.__setitem__(self, self.disk + 4, SECTORS)
        self.image = bytearray((i * 7 + i // 512) & 255 for i in range(SECTORS * 512))
        self.read_only_drive = False
        self.flushes = 0

    def __getitem__(self, address):
        if address == self.disk:
            return self.disk_state | (C['DISK_STATUS_READONLY'] if self.read_only_drive else 0)
        return super().__getitem__(address)

    def complete(self, error_code=0, changed=False):
        value, physical, sector, count = self.commands[-1]
        code = value & 255
        if code == READ:
            for i, byte in enumerate(self.image[sector * 512:(sector + count) * 512]):
                dict.__setitem__(self, physical + i, byte)
        elif code == WRITE and not error_code:
            self.image[sector * 512:(sector + count) * 512] = bytes(
                self.get(physical + i, 0) for i in range(count * 512))
        elif code == FLUSH and not error_code:
            self.flushes += 1
        self.disk_state = C['DISK_PRESENT'] | C['DISK_DONE']
        if changed:
            self.disk_state |= C['DISK_CHANGED']
        self.disk_error = error_code
        self.lines |= 1 << 3


def storage(flags=1, size=8192, extent=True, display=True):
    """Task 2 owns the Disk role; the root is writable and 8 KiB (16 sectors)."""
    vm = kernel_fixture()
    vm.memory = ImageDevices(vm)
    vm.call('serviceDevicesRollback')
    for i, word in enumerate((0x31525357, 1, size, flags)):
        vm.memory[ROOT + 4 * i] = word
    if display:
        assert vm.call('serviceDevicesInit', 1, 2, C['DISK0_BASE'], 1024)
    else:  # no sealed Screen: the Disk role can be regranted
        assert vm.call('diskDevicesInit', 2, C['DISK0_BASE'], 1024)
    if extent:
        assert vm.call('deviceExtentConfigure', 2, 0, size, 1) == 0
    return vm


def user_bytes(vm, data, task=2):
    page = vm.pages(task)[1]
    for i, byte in enumerate(data):
        vm.memory[page + i] = byte


def sector_bytes(vm, sector):
    return bytes(vm.memory.image[(2 + sector) * 512:(3 + sector) * 512])


def write_sectors(vm, offset, data):
    user_bytes(vm, data)
    token = vm.call('deviceSubmit', 2, offset, len(data), WRITE, USER_DATA)
    if token > 0:
        vm.memory.complete()
        return vm.call('deviceFinish', 2, token, 0)
    return token


class BlockWriteTests(unittest.TestCase):
    def test_write_authority_needs_root_extent_and_drive(self):
        cases = {
            'read-only root': dict(flags=0, extent=False),
            'extent never selected': dict(flags=1, extent=False),
        }
        for name, args in cases.items():
            with self.subTest(name):
                vm = storage(**args)
                free = vm.free_pages()
                for command, length in ((WRITE, 512), (FLUSH, 0)):
                    self.assertEqual(vm.call('deviceSubmit', 2, 0, length, command, USER_DATA if length else 0), error(EROFS))
                self.assertEqual(vm.memory.commands, [])
                self.assertEqual(vm.free_pages(), free)
                self.assertEqual(vm.call('diskFlags', 2), 0)
        vm = storage()
        vm.memory.read_only_drive = True
        for command, length in ((WRITE, 512), (FLUSH, 0)):
            self.assertEqual(vm.call('deviceSubmit', 2, 0, length, command, USER_DATA if length else 0), error(EROFS))
        self.assertEqual(vm.memory.commands, [])
        self.assertEqual(vm.call('diskFlags', 2), 0)
        self.assertGreater(vm.call('deviceSubmit', 2, 0, 16, READ, 0), 0)

    def test_manager_selection_is_checked_and_defaults_to_read_only(self):
        vm = storage(extent=False)
        for offset, length, flags in ((0, 8192, 2), (0, 8192, 0xFFFFFFFF), (0, 8191, 1), (512, 4097, 1)):
            self.assertEqual(vm.call('deviceExtentConfigure', 2, offset, length, flags), error(EINVAL))
        self.assertEqual(vm.call('diskFlags', 2), 0)
        # A read-only window of a writable root stays read-only.
        self.assertEqual(vm.call('deviceExtentConfigure', 2, 0, 608, 0), 0)
        self.assertEqual(vm.call('deviceSubmit', 2, 0, 512, WRITE, USER_DATA), error(EROFS))
        self.assertEqual(vm.call('deviceExtentConfigure', 2, 1024, 2048, 1), 0)
        self.assertEqual(vm.call('diskInfo', 2), 2048)
        self.assertEqual(vm.call('diskFlags', 2), 1)
        vm = storage(flags=0, extent=False)
        self.assertEqual(vm.call('deviceExtentConfigure', 2, 0, 512, 1), error(EROFS))
        self.assertEqual(vm.call('diskFlags', 2), 0)

    def test_storage_root_flag_validation(self):
        for words, expected in (((0x31525357, 1, 8192, 1), 8192), ((0x31525357, 1, 8192, 0), 8192),
                                ((0x31525357, 1, 8191, 1), 0), ((0x31525357, 1, 8192, 2), 0),
                                ((0x31525357, 1, 8192, 3), 0), ((0x31525357, 1, 608, 0), 608)):
            with self.subTest(words=words):
                vm = kernel_fixture()
                for i, word in enumerate(words):
                    vm.memory[ROOT + 4 * i] = word
                self.assertEqual(vm.call('approvedStorageBytes'), expected)
                self.assertEqual(bool(vm.call('approvedStorageWritable')), bool(expected and words[3] == 1))

    def test_storage_root_tool_writes_the_flag(self):
        sys.path.insert(0, str(LAIX / 'tools'))
        import storage_root
        data = storage_root.pack(8192, writable=True)
        self.assertEqual(storage_root.unpack(data), 8192)
        self.assertEqual(storage_root.flags(data), 1)
        self.assertEqual(storage_root.flags(storage_root.pack(8192)), 0)
        with self.assertRaises(ValueError):
            storage_root.pack(700, writable=True)

    def test_write_flush_and_read_back(self):
        vm = storage()
        pattern = bytes((i * 3 + 1) & 255 for i in range(1024))
        before = bytes(vm.memory.image)
        self.assertEqual(write_sectors(vm, 1024, pattern), 1024)
        image = bytes(vm.memory.image)
        self.assertEqual(vm.memory.commands[-1][0], WRITE)
        self.assertEqual(vm.memory.commands[-1][2:], (4, 2))
        self.assertEqual(sector_bytes(vm, 2) + sector_bytes(vm, 3), pattern)
        self.assertEqual(image[:4 * 512], before[:4 * 512])
        self.assertEqual(image[6 * 512:], before[6 * 512:])
        self.assertEqual(vm.call('diskFlags', 2), 3)  # writable and dirty
        token = vm.call('deviceSubmit', 2, 0, 0, FLUSH, 0)
        self.assertGreater(token, 0)
        self.assertEqual(vm.call('diskFlags', 2), 3)  # still dirty until it finishes
        vm.memory.complete()
        self.assertEqual(vm.call('deviceFinish', 2, token, 0), 0)
        self.assertEqual((vm.memory.flushes, vm.call('diskFlags', 2)), (1, 1))
        # Read it back through the same broker.
        token = vm.call('deviceSubmit', 2, 1024 + 5, 700, READ, 0)
        self.assertGreater(token, 0)
        self.assertEqual(vm.memory.commands[-1][2:], (4, 2))
        vm.memory.complete()
        self.assertEqual(vm.call('deviceFinish', 2, token, USER_DATA), 700)
        page = vm.pages(2)[1]
        self.assertEqual(bytes(vm.memory[page + i] for i in range(700)), pattern[5:705])

    def test_write_is_bounded_aligned_and_inside_the_extent(self):
        vm = storage()
        free = vm.free_pages()
        bad = [(1, 512), (512, 513), (0, 0), (0, 511), (0, 4608), (8192, 512), (7680, 1024),
               (0xFFFFFE00, 1024), (512, 0xFFFFFE00), (0, 0xFFFFFFFF)]
        for offset, length in bad:
            self.assertEqual(vm.call('deviceSubmit', 2, offset, length, WRITE, USER_DATA), error(EINVAL), (offset, length))
        self.assertEqual(vm.call('deviceSubmit', 2, 0, 512, WRITE, 0), error(EINVAL))
        for offset, length, source in ((1, 0, 0), (0, 512, 0), (0, 0, USER_DATA)):
            self.assertEqual(vm.call('deviceSubmit', 2, offset, length, FLUSH, source), error(EINVAL))
        # READ takes no source; it also stops at eight sectors.
        self.assertEqual(vm.call('deviceSubmit', 2, 0, 16, READ, USER_DATA), error(EINVAL))
        self.assertEqual(vm.call('deviceSubmit', 2, 0, 4097, READ, 0), error(EINVAL))
        self.assertEqual(vm.call('deviceSubmit', 2, 511, 4097, READ, 0), error(EINVAL))
        self.assertEqual(vm.memory.commands, [])
        self.assertEqual(vm.free_pages(), free)
        self.assertEqual(vm.call('physicalPageReferences', vm.pages(2)[1]), 1)
        # The largest legal operations: eight sectors, anywhere.
        self.assertEqual(write_sectors(vm, 4096, bytes(range(256)) * 16), 4096)
        self.assertGreater(vm.call('deviceSubmit', 2, 511, 3585, READ, 0), 0)
        self.assertEqual(vm.memory.commands[-1][2:], (2, 8))

    def test_write_cannot_pass_the_drive_end(self):
        vm = storage()
        # The drive has 64 sectors; the root starts at sector 2.
        dict.__setitem__(vm.memory, C['DISK0_BASE'] + 4, 3)
        self.assertEqual(vm.call('deviceSubmit', 2, 0, 1024, WRITE, USER_DATA), error(EPIPE))
        self.assertEqual(vm.memory.commands, [])

    def test_faulting_user_buffer_issues_no_command_and_leaves_the_page_free(self):
        for source in (NOT_MAPPED, USER_DATA + 4096 - 8, 0xFFFFFFF8, 4):
            with self.subTest(source=source):
                vm = storage()
                free = vm.free_pages()
                self.assertEqual(vm.call('deviceSubmit', 2, 0, 512, WRITE, source), error(EFAULT))
                self.assertEqual(vm.memory.commands, [])
                self.assertEqual(vm.free_pages(), free)
                self.assertEqual(vm.globals['deviceBounce'], 0)
                self.assertEqual(vm.call('diskFlags', 2), 1)  # not dirty
                user_bytes(vm, bytes(512))
                self.assertGreater(vm.call('deviceSubmit', 2, 0, 512, WRITE, USER_DATA), 0)

    def test_dirty_is_set_by_write_and_cleared_only_by_a_successful_flush(self):
        vm = storage()
        self.assertEqual(write_sectors(vm, 0, bytes(512)), 512)
        self.assertEqual(vm.call('diskFlags', 2), 3)
        # A flush that fails leaves the extent dirty.
        token = vm.call('deviceSubmit', 2, 0, 0, FLUSH, 0)
        vm.memory.complete(error_code=6)
        self.assertEqual(vm.call('deviceFinish', 2, token, 0), error(EIO))
        self.assertEqual((vm.memory.flushes, vm.call('diskFlags', 2)), (0, 3))
        # So does a failed write: the device may have taken part of it.
        user_bytes(vm, bytes(512))
        token = vm.call('deviceSubmit', 2, 512, 512, WRITE, USER_DATA)
        vm.memory.complete(error_code=6)
        self.assertEqual(vm.call('deviceFinish', 2, token, 0), error(EIO))
        self.assertEqual(vm.call('diskFlags', 2), 3)
        token = vm.call('deviceSubmit', 2, 0, 0, FLUSH, 0)
        vm.memory.complete()
        self.assertEqual(vm.call('deviceFinish', 2, token, 0), 0)
        self.assertEqual(vm.call('diskFlags', 2), 1)
        # Reads never dirty the extent.
        token = vm.call('deviceSubmit', 2, 0, 16, READ, 0)
        vm.memory.complete()
        vm.call('deviceFinish', 2, token, USER_DATA)
        self.assertEqual(vm.call('diskFlags', 2), 1)

    def test_device_readonly_error_maps_to_erofs(self):
        vm = storage()
        user_bytes(vm, bytes(512))
        token = vm.call('deviceSubmit', 2, 0, 512, WRITE, USER_DATA)
        vm.memory.complete(error_code=5)
        self.assertEqual(vm.call('deviceFinish', 2, token, 0), error(EROFS))
        self.assertTrue(vm.call('physicalPageAvailable', vm.memory.commands[-1][1]))

    def test_instances_are_exactly_once_and_never_wrap(self):
        vm = storage()
        first = vm.call('deviceSubmit', 2, 0, 0, FLUSH, 0)
        self.assertEqual(vm.call('deviceSubmit', 2, 0, 0, FLUSH, 0), error(EBUSY))
        self.assertEqual(vm.call('deviceFinish', 2, first + 1, 0), error(EINVAL))
        vm.memory.complete()
        self.assertEqual(vm.call('deviceFinish', 2, first, 0), 0)
        self.assertEqual(vm.call('deviceFinish', 2, first, 0), error(EINVAL))
        typ = vm.decls['deviceOperation'].sym.type
        vm.memory[vm.addresses['deviceOperation'] + typ.field('instance').offset] = 0x7FFFFFFF
        free = vm.free_pages()
        user_bytes(vm, bytes(512))
        self.assertEqual(vm.call('deviceSubmit', 2, 0, 512, WRITE, USER_DATA), error(EOVERFLOW))
        self.assertEqual(vm.free_pages(), free)
        self.assertEqual(len(vm.memory.commands), 1)

    def test_orphaned_write_keeps_the_page_pinned_and_still_lands(self):
        vm = storage()
        data = bytes((i * 5 + 3) & 255 for i in range(512))
        user_bytes(vm, data)
        token = vm.call('deviceSubmit', 2, 512, 512, WRITE, USER_DATA)
        bounce = vm.globals['deviceBounce']
        self.assertEqual(vm.call('deviceCancel', 2, token), 0)
        before = sector_bytes(vm, 1)
        for _ in range(12):
            vm.call('deviceReap')
            self.assertFalse(vm.call('serviceDevicesQuiescent', 2))
            self.assertEqual(vm.call('physicalPageReferences', bounce), 1)
            self.assertEqual(vm.call('deviceSubmit', 2, 0, 0, FLUSH, 0), error(EPIPE))
        self.assertEqual(sector_bytes(vm, 1), before)
        vm.memory.complete()  # cancellation does not undo the write
        self.assertEqual(sector_bytes(vm, 1), data)
        vm.call('deviceReap')
        vm.call('deviceReap')
        self.assertTrue(vm.call('physicalPageAvailable', bounce))
        self.assertEqual(vm.call('deviceFinish', 2, token, 0), error(EINVAL))

    def test_regrant_returns_read_only_and_reports_dirty_at_regrant(self):
        vm = storage(display=False)
        self.assertEqual(write_sectors(vm, 0, bytes(512)), 512)
        self.assertEqual(vm.call('diskFlags', 2), 3)
        # Owner 2 dies with the extent dirty. Task 3 takes over the Disk role.
        vm.memory[vm.field_address('state', 2)] = 3  # TASK_DEAD
        self.assertEqual(vm.call('diskDevicesRegrant', 3, C['DISK0_BASE'], 1024), 0)
        self.assertEqual(vm.call('diskFlags', 3), 4)  # read-only, dirty-at-regrant
        self.assertEqual(vm.call('deviceSubmit', 3, 0, 512, WRITE, USER_DATA), error(EROFS))
        self.assertEqual(vm.call('deviceSubmit', 3, 0, 0, FLUSH, 0), error(EROFS))
        # The manager's explicit selection acknowledges it and restores write.
        self.assertEqual(vm.call('deviceExtentConfigure', 3, 0, 8192, 1), 0)
        self.assertEqual(vm.call('diskFlags', 3), 1)


if __name__ == '__main__':
    unittest.main()
