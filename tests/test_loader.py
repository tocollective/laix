"""G1 Files-backed loading: SYS_TASK_LOAD on checked sources.

Nothing here builds code or measures the CPU. A user supervisor is played by
Python through the real syscalls; the kernel path (authority, snapshot, image
checks, quota, rollback) runs as checked M. The Disk -> Files -> loader path and
the real child are CPU evidence (probe_loader_cpu.py), not claims made here.
"""
import struct
import unittest

from source_m import LAYOUT as C
from test_kernel import check_m, LAIX
from test_ipc_handles import error
from test_runtime_tasks import fixture
from test_task import USER_DATA

SYS = C['SYS_TASK_LOAD']
BASE = C['SERVICE_IMAGE_BASE']
PAGE = 4096
LOAD = C['IMAGE_LOAD_AUTHORITY']
# Header words 0..12, then three program headers at 52 (R+X, R, RW), file data
# from offset 256. The whole image fits the supervisor's one data page.
HEADER = [0x464C457F, 0x00010101, 0, 0, 0x08160002, 1, BASE, 52, 0, 0, 0x00200034, 3, 0]
SEGMENTS = [(1, 256, BASE, BASE, 16, PAGE, 5, PAGE),
            (1, 272, BASE + PAGE, BASE + PAGE, 16, PAGE, 4, PAGE),
            (1, 288, BASE + 2 * PAGE, BASE + 2 * PAGE, 16, 2 * PAGE, 6, PAGE)]
LENGTH = 304


def image(mutate=None):
    words = list(HEADER) + [word for segment in SEGMENTS for word in segment]
    if mutate:
        mutate(words)
    data = b''.join(struct.pack('<I', word) for word in words)
    return data + bytes(256 - len(data)) + bytes(range(100, 148))


def put(vm, data, address=None):
    vm.seed(vm.pages(vm.current())[1] if address is None else address, data)


def load(vm, length=LENGTH, source=USER_DATA):
    vm.invoke(SYS, source, length)
    return vm.result(vm.current())[0]


def supervisor(authority=LOAD, recovery=False):
    vm = fixture(1)
    vm.memory[vm.field_address('createImages', 1)] = authority
    table = vm.decls['tasks'].sym.type.elem.field('handles').type
    vm.memory[vm.field_address('handles', 1) + table.field('factoryRecovery').offset] = int(recovery)
    return vm


def snapshot(vm):
    return len(vm.free_pages()), vm.control_count()


def word(index, value):
    def mutate(words):
        words[index] = value
    return mutate


class LoaderTests(unittest.TestCase):
    def test_abi_constants_and_sources_agree(self):
        self.assertEqual(SYS, 76)
        self.assertEqual(C['TASK_LOAD_BYTES'], 65536)
        # Catalog IDs 1..16 are bits 0..15; the load authority sits above them.
        self.assertEqual(LOAD & 0xFFFF, 0)
        self.assertEqual(LOAD & (LOAD - 1), 0)  # one bit
        for path in ('src/task/control.m', 'src/task/program.m', 'src/trap/trap.m',
                     'src/kernel/loader_main.m', 'user/syscalls.m', 'user/loadfile.m',
                     'user/services/loader.m', 'tests/programs/loader/hello.m'):
            check_m(LAIX / path)

    def test_a_valid_image_becomes_a_created_child_with_full_control_and_no_authority(self):
        vm = supervisor()
        put(vm, image())
        before = snapshot(vm)
        child = load(vm)
        self.assertGreater(child, 0)
        self.assertLess(child, 0x80000000)
        self.assertEqual(vm.field('state', child), C['TASK_CREATED'] if 'TASK_CREATED' in C else 5)
        self.assertEqual(vm.control_count(), before[1] + 1)
        self.assertEqual(vm.field('createImages', child), 0)
        self.assertEqual(vm.field('deviceRights', child), 0)
        # The image's own pages: code RX, rodata RO, data RW, bss zero.
        root = vm.field('directory', child)
        for index, permissions in enumerate((27, 19, 23)):
            leaf = vm.leaf(BASE + index * PAGE, root)
            self.assertEqual(leaf & 31, permissions)
            self.assertEqual(vm.read_bytes(leaf & ~4095, 16), bytes(range(100 + 16 * index, 116 + 16 * index)))
        self.assertEqual(vm.read_bytes(vm.leaf(BASE + 2 * PAGE + PAGE, root) & ~4095, 16), bytes(16))
        # The same configure / publish / collect lifecycle as a catalog child.
        vm.invoke(C['SYS_TASK_CONFIGURE'], child, 0, 0, 7)
        self.assertEqual(vm.result(vm.current())[0], 0)
        vm.invoke(C['SYS_TASK_PUBLISH'], child)
        self.assertEqual(vm.result(vm.current())[0], 0)
        vm.invoke(C['SYS_TASK_TERMINATE'], child, 5)
        vm.reap()
        vm.invoke(C['SYS_TASK_COLLECT'], child, USER_DATA)
        self.assertEqual(vm.result(vm.current())[0], 0)

    def test_authority_is_required_and_catalog_creation_is_unchanged(self):
        for authority in (0, 1, 0xFFFF, LOAD >> 1):
            with self.subTest(authority=authority):
                vm = supervisor(authority)
                put(vm, image())
                before = snapshot(vm)
                self.assertEqual(load(vm), error(1))
                self.assertEqual(snapshot(vm), before)
        vm = supervisor(LOAD | 1)
        vm.invoke(C['SYS_TASK_CREATE'], 1)
        self.assertGreater(vm.result(vm.current())[0], 0)
        vm = supervisor(LOAD)
        vm.invoke(C['SYS_TASK_CREATE'], 1)
        self.assertEqual(vm.result(vm.current())[0], error(1))

    def test_bad_lengths_and_buffers_change_nothing(self):
        vm = supervisor()
        put(vm, image())
        before = snapshot(vm)
        for length in (0, 1, 51, C['TASK_LOAD_BYTES'] + 1, 0xFFFFFFFF):
            with self.subTest(length=length):
                self.assertEqual(load(vm, length), error(22))
        for source, length in ((0, 304), (0x70000000, 304), (C['START_BLOCK_VA'] + PAGE - 8, 304),
                               (0xFFFFFFF0, 304), (C['START_BLOCK_VA'] + PAGE, 52)):
            with self.subTest(source=source):
                self.assertEqual(load(vm, length, source), error(14))
        self.assertEqual(snapshot(vm), before)

    def test_invalid_images_are_refused_by_the_catalog_checks_without_effect(self):
        header = len(HEADER)
        base = lambda n: header + 8 * n  # first word of program header n
        cases = {
            'magic': word(0, 0x464C457E), 'class': word(1, 0x00010102), 'type': word(4, 0x08160003),
            'machine': word(4, 0x08170002), 'entry outside code': word(6, BASE + PAGE),
            'entry unaligned': word(6, BASE + 2), 'no segments': word(11, 0), 'four segments': word(11, 4),
            'phoff low': word(7, 40), 'phoff beyond': word(7, 0x1000),
            'not a load segment': word(base(0), 2), 'bad alignment': word(base(0) + 7, 8),
            'segment below window': word(base(1) + 2, BASE - PAGE),
            'segment above window': word(base(1) + 2, C['SERVICE_IMAGE_END']),
            'unaligned address': word(base(1) + 2, BASE + PAGE + 16),
            'writable and executable': word(base(0) + 6, 7), 'no permissions': word(base(0) + 6, 0),
            'empty segment': word(base(0) + 5, 0), 'file larger than memory': word(base(0) + 4, 2 * PAGE),
            'file beyond image': word(base(2) + 1, 4000), 'overlap': word(base(1) + 2, BASE),
            'too many pages': word(base(2) + 5, 65 * PAGE),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                vm = supervisor()
                put(vm, image(mutate))
                before = snapshot(vm)
                self.assertEqual(load(vm), error(22))
                self.assertEqual(snapshot(vm), before)

    def test_snapshot_frames_are_borrowed_for_the_call_and_the_reserve_is_kept(self):
        vm = supervisor()
        put(vm, image())
        free = len(vm.free_pages())
        child = load(vm)
        self.assertGreater(child, 0)
        # Discarding the unpublished child returns every frame, so the snapshot
        # frames were released before the call returned.
        vm.invoke(C['SYS_TASK_TERMINATE'], child, 0)
        self.assertEqual(len(vm.free_pages()), free)
        # Without the progress reserve plus the snapshot there is no load at all.
        reserve = C['MEMORY_RESERVE_PAGES'] if 'MEMORY_RESERVE_PAGES' in C else 16
        vm.globals['memoryFreePages'] = reserve
        before = snapshot(vm)
        self.assertEqual(load(vm), error(23))
        self.assertEqual(snapshot(vm), before)

    def test_the_kernel_loads_a_snapshot_not_the_callers_later_bytes(self):
        vm = supervisor()
        put(vm, image())
        child = load(vm)
        self.assertGreater(child, 0)
        put(vm, bytes(PAGE))
        leaf = vm.leaf(BASE, vm.field('directory', child))
        self.assertEqual(vm.read_bytes(leaf & ~4095, 16), bytes(range(100, 116)))

    def test_loaded_children_spend_the_same_quota_as_catalog_children(self):
        vm = supervisor()
        put(vm, image())
        children = [load(vm) for _ in range(4)]
        self.assertTrue(all(child > 0 for child in children))
        before = snapshot(vm)
        self.assertEqual(load(vm), error(23))
        self.assertEqual(snapshot(vm), before)
        # A catalog child is refused by the same quota while the rows are full.
        vm.memory[vm.field_address('createImages', 1)] = LOAD | 1
        vm.invoke(C['SYS_TASK_CREATE'], 1)
        self.assertEqual(vm.result(vm.current())[0], error(23))
        # Collecting one frees its row for either path.
        vm.invoke(C['SYS_TASK_TERMINATE'], children[0], 3)
        vm.invoke(C['SYS_TASK_COLLECT'], children[0], USER_DATA)
        put(vm, image())
        self.assertGreater(load(vm), 0)

    def test_allocation_failure_at_every_step_rolls_everything_back(self):
        vm = supervisor()
        put(vm, image())
        vm.allocation_count = 0
        self.assertGreater(load(vm), 0)
        steps = vm.allocation_count
        self.assertGreater(steps, 3)
        for step in range(1, steps + 1):
            with self.subTest(step=step):
                vm = supervisor()
                put(vm, image())
                before = snapshot(vm)
                vm.allocation_count = 0
                vm.fail_allocation = step
                self.assertEqual(load(vm), error(23))
                self.assertEqual(snapshot(vm), before)
                vm.fail_allocation = None
                self.assertGreater(load(vm), 0)

    def test_a_slot_that_cannot_be_reserved_leaves_the_catalog_and_loads_working(self):
        # Rows are taken before construction, so the failed load released its row:
        # the same supervisor can still use every quota unit afterwards.
        vm = supervisor()
        put(vm, image())
        vm.fail_allocation = 1
        vm.allocation_count = 0
        self.assertEqual(load(vm), error(23))
        vm.fail_allocation = None
        children = [load(vm) for _ in range(4)]
        self.assertTrue(all(child > 0 for child in children))


if __name__ == '__main__':
    unittest.main()
