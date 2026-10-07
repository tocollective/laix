"""Disk service sector-stage protocol (writable block path) against a fake broker."""
import unittest

from source_m import SourceM, LAYOUT as C
from test_ipc_handles import error
from test_kernel import LAIX

REQ, RES, IRQ = 0x1000000, 0x1001000, 0x104
EXTENT = 2048


class FakeDisk(SourceM):
    """Executes user/services/disk.m; traps are the kernel's block-path syscalls."""

    def __init__(self, extent=EXTENT, writable=True):
        super().__init__(LAIX / 'user/services/disk.m')
        self.extent, self.writable = extent, writable
        self.image = bytearray((i * 5 + i // 512) & 255 for i in range(extent))
        self.syscalls = []
        self.pending = None
        self.wait_result = 0
        self.finish_error = 0
        self.submit_error = 0
        self.flushes = 0

    def trap(self, cause, args=()):
        number, *a = args
        self.syscalls.append((number, *a))
        if number == C['SYS_DEVICE_INFO']:
            return self.extent
        if number == C['SYS_DEVICE_FLAGS']:
            return 1 if self.writable else 0
        if number == C['SYS_DEVICE_SUBMIT']:
            offset, length, command, source = a
            if self.submit_error:
                return self.submit_error
            if command == 1:
                self.pending = ('read', offset, length)
            elif not self.writable:
                return error(30)
            elif command == 2:
                self.pending = ('write', offset, bytes(self.memory[source + i] for i in range(length)))
            else:
                self.pending = ('flush',)
            return 1
        if number == C['SYS_IRQ_WAIT']:
            return self.wait_result
        if number == C['SYS_DEVICE_FINISH']:
            if self.finish_error:
                return self.finish_error
            kind, *rest = self.pending
            if kind == 'read':
                offset, length = rest
                for i in range(length):
                    self.memory[a[1] + i] = self.image[offset + i]
                return length
            if kind == 'write':
                offset, data = rest
                self.image[offset:offset + len(data)] = data
                return len(data)
            self.flushes += 1
            return 0
        if number in (C['SYS_IRQ_COMPLETE'], C['SYS_DEVICE_CANCEL']):
            return 0
        raise AssertionError(f'unexpected syscall {number}')

    def ask(self, words, size=None, data=b''):
        for i, word in enumerate(words):
            self.memory[REQ + 4 * i] = word
        for i, byte in enumerate(data):
            self.memory[REQ + 16 + i] = byte
        self.call('diskHandle', REQ, size or (16 + len(data)), RES, IRQ)
        return [self.memory[RES + 4 * i] for i in range(4)], bytes(self.memory.get(RES + 16 + i, 0) for i in range(16))

    def stage(self):
        return bytes(self.memory[self.addresses['diskStage'] + i] for i in range(512))


def request(name, a=0, b=0, generation=1):
    return [C['DISK_' + name + '_HEADER'], generation, a, b]


class DiskStageTests(unittest.TestCase):
    def test_load_and_peek_move_a_whole_sector_in_one_device_operation(self):
        vm = FakeDisk()
        head, _ = vm.ask(request('LOAD', 512))
        self.assertEqual(head[1:4], [0, 1, 512])
        self.assertEqual(sum(1 for call in vm.syscalls if call[0] == C['SYS_DEVICE_SUBMIT']), 1)
        self.assertEqual(vm.stage(), bytes(vm.image[512:1024]))
        before = len(vm.syscalls)
        for offset in range(0, 512, 16):
            head, data = vm.ask(request('PEEK', offset))
            self.assertEqual((head[1], head[3]), (0, 16))
            self.assertEqual(data, bytes(vm.image[512 + offset:528 + offset]))
        self.assertEqual(len(vm.syscalls), before)  # PEEK never reaches the device

    def test_poke_then_store_updates_only_that_sector(self):
        vm = FakeDisk()
        original = bytes(vm.image)
        vm.ask(request('LOAD', 1024))
        new = bytes((i * 11 + 7) & 255 for i in range(512))
        for offset in range(0, 512, 16):
            head, _ = vm.ask([C['DISK_POKE_HEADER'], 1, offset, 0], 32, new[offset:offset + 16])
            self.assertEqual(head[1], 0)
        self.assertEqual(vm.stage(), new)
        self.assertEqual(bytes(vm.image), original)  # staging alone writes nothing
        head, _ = vm.ask(request('STORE', 1024))
        self.assertEqual(head[1], 0)
        self.assertEqual(bytes(vm.image[1024:1536]), new)
        self.assertEqual(bytes(vm.image[:1024]), original[:1024])
        self.assertEqual(bytes(vm.image[1536:]), original[1536:])
        head, _ = vm.ask(request('SYNC'))
        self.assertEqual((head[1], vm.flushes), (0, 1))
        self.assertFalse(vm.globals['diskFailed'])

    def test_read_only_extent_refuses_store_and_sync_without_dying(self):
        vm = FakeDisk(writable=False)
        vm.ask(request('LOAD', 0))
        for name in ('STORE', 'SYNC'):
            head, _ = vm.ask(request(name))
            self.assertEqual(head[1], error(30), name)
            self.assertEqual(head[3], 0)
        self.assertFalse(vm.globals['diskFailed'])
        head, _ = vm.ask(request('FLAGS'))
        self.assertEqual((head[1], head[3]), (0, 0))
        vm = FakeDisk()
        head, _ = vm.ask(request('FLAGS'))
        self.assertEqual((head[1], head[3]), (0, 1))

    def test_partial_last_sector_is_zero_padded_and_cannot_be_stored(self):
        vm = FakeDisk(extent=700, writable=False)
        head, _ = vm.ask(request('LOAD', 512))
        self.assertEqual(head[3], 188)
        self.assertEqual(vm.stage(), bytes(vm.image[512:700]) + bytes(324))
        vm = FakeDisk(extent=700)
        self.assertEqual(vm.ask(request('STORE', 512))[0][1], error(22))
        self.assertEqual([c for c in vm.syscalls if c[0] == C['SYS_DEVICE_SUBMIT']], [])

    def test_malformed_and_stale_requests_never_reach_the_device(self):
        vm = FakeDisk()
        bad = [(request('LOAD', 1), 16), (request('LOAD', 2048), 16), (request('LOAD', 0, 1), 16),
               (request('LOAD', 0xFFFFFE00), 16), (request('STORE', 513), 16), (request('STORE', 2048), 16),
               (request('STORE', 0, 1), 16), (request('STORE', 1536 + 1), 16),
               (request('SYNC', 1), 16), (request('SYNC', 0, 1), 16), (request('FLAGS', 1), 16),
               (request('PEEK', 8), 16), (request('PEEK', 512), 16), (request('PEEK', 0, 1), 16),
               (request('LOAD', 0), 12), (request('LOAD', 0), 20), (request('PEEK', 0), 32)]
        for words, size in bad:
            head, _ = vm.ask(words, size)
            self.assertEqual(head[1], error(22), (words, size))
        # POKE is exactly 32 bytes with an aligned in-range offset.
        for words, size in (([C['DISK_POKE_HEADER'], 1, 8, 0], 32), ([C['DISK_POKE_HEADER'], 1, 512, 0], 32),
                            ([C['DISK_POKE_HEADER'], 1, 0, 1], 32), ([C['DISK_POKE_HEADER'], 1, 0, 0], 16),
                            ([C['DISK_POKE_HEADER'], 1, 0, 0], 20)):
            head, _ = vm.ask(words, size)
            self.assertEqual(head[1], error(22), (words, size))
        self.assertEqual(vm.stage(), bytes(512))
        for name in ('LOAD', 'STORE', 'SYNC', 'FLAGS', 'PEEK'):
            head, _ = vm.ask(request(name, generation=2))
            self.assertEqual(head[1], error(32), name)
        head, _ = vm.ask([C['DISK_POKE_HEADER'], 2, 0, 0], 32, bytes(16))
        self.assertEqual(head[1], error(32))
        self.assertEqual([c for c in vm.syscalls if c[0] != C['SYS_DEVICE_INFO']], [])
        self.assertFalse(vm.globals['diskFailed'])

    def test_device_errors_and_timeouts_end_the_service_like_reads_do(self):
        for name, setup, expected in (('wait', dict(wait_result=error(110)), error(5)),
                                      ('finish', dict(finish_error=error(5)), error(5)),
                                      ('epipe', dict(finish_error=error(32)), error(32))):
            for op in ('LOAD', 'STORE', 'SYNC'):
                with self.subTest(case=name, op=op):
                    vm = FakeDisk()
                    for key, value in setup.items():
                        setattr(vm, key, value)
                    head, _ = vm.ask(request(op))
                    self.assertEqual(head[1], expected)
                    self.assertTrue(vm.globals['diskFailed'])
                    if name == 'wait':
                        self.assertEqual(sum(1 for c in vm.syscalls if c[0] == C['SYS_DEVICE_CANCEL']), 1)
        vm = FakeDisk()
        vm.submit_error = error(16)  # a busy engine is reported, not fatal
        self.assertEqual(vm.ask(request('LOAD'))[0][1], error(16))
        self.assertFalse(vm.globals['diskFailed'])

    def test_existing_reads_and_stat_are_unchanged(self):
        vm = FakeDisk()
        head, data = vm.ask([C['DISK_REQUEST_HEADER'], 1, 5, 16])
        self.assertEqual((head[1], head[3]), (0, 16))
        self.assertEqual(data, bytes(vm.image[5:21]))
        head, _ = vm.ask([C['DISK_STAT_HEADER'], 1, 0, 0])
        self.assertEqual((head[1], head[3]), (0, EXTENT))
        self.assertEqual(vm.stage(), bytes(512))  # reads never touch the stage


if __name__ == '__main__':
    unittest.main()
