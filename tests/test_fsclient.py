"""The WFS1 client library (user/services/fsclient.m) against the real service source."""
import unittest

from source_m import SourceM, LAYOUT as C
from test_ipc_handles import error
from test_kernel import LAIX
import sys
from test_fs import FsVM, content, wfs, DISK
from mlang import syntax

sys.path.insert(0, str(LAIX / 'tools'))

HANDLE = 0x77
NAME, FILE, BUFFER, OTHER = 0x2000000, 0x2001000, 0x2002000, 0x2100000
CREATE, EXCL, TRUNC = C['FS_CREATE'], C['FS_EXCL'], C['FS_TRUNC']
ENOENT, EINVAL, ENOSPC, EPROTO, EROFS = 2, 22, 28, 71, 30


class ClientVM(SourceM):
    """fsclient.m; each call() crosses into a running fs.m instance."""

    def __init__(self, image, writable=True, mangle=None, root='user/services/fsclient.m'):
        super().__init__(LAIX / root)
        self.service = FsVM(image, writable=writable)
        self.mangle = mangle
        self.requests = []
        self.strings = {}
        self.next_string = 0x2200000

    def expr(self, node, local):
        # String literals are NUL-terminated rodata in the program.
        if isinstance(node, syntax.StringLit):
            raw = node.value if isinstance(node.value, bytes) else node.value.encode()
            if raw not in self.strings:
                self.strings[raw] = self.next_string
                for i, byte in enumerate(raw + b'\0'):
                    self.memory[self.next_string + i] = byte
                self.next_string += len(raw) + 1
            return self.strings[raw]
        return super().expr(node, local)

    def call(self, name, *args):
        if name == 'call':
            handle, request, size, response, capacity = args
            assert handle == HANDLE and capacity == 32
            words = [self.memory[request + 4 * i] for i in range(8 if size > 16 else (size + 3) // 4)]
            self.requests.append((size, words))
            for i, word in enumerate(words):
                self.service.memory[0x1000000 + 4 * i] = word
            self.service.call('fsHandle', 0x1000000, size, 0x1001000, DISK)
            out = [self.service.memory[0x1001000 + 4 * i] for i in range(8)]
            if self.mangle:
                out = self.mangle(out)
                if isinstance(out, int):
                    return out
            for i, word in enumerate(out):
                self.memory[response + 4 * i] = word
            return 32
        return super().call(name, *args)

    def cstring(self, text, address=NAME):
        for i, byte in enumerate(text.encode() + b'\0'):
            self.memory[address + i] = byte
        return address

    def word(self, address, index=0):
        return self.memory[address + 4 * index]

    def open(self, name, flags=0, capacity=0):
        status = self.call('fsOpen', HANDLE, self.cstring(name), flags, capacity, FILE)
        return status, tuple(self.memory.get(FILE + 4 * i) for i in range(3))

    def bytes_at(self, address, count):
        return bytes(self.memory[address + i] for i in range(count))


class ClientTests(unittest.TestCase):
    def test_open_read_write_sync_list_remove_round_trip(self):
        vm = ClientVM(wfs.mkfs(24, {'motd': b'hello, world\n'}))
        status, (file_id, size, capacity) = vm.open('motd')
        self.assertEqual((status, size, capacity), (0, 13, 512))
        self.assertEqual(vm.call('fsReadAll', HANDLE, file_id, BUFFER, 64), 13)
        self.assertEqual(vm.bytes_at(BUFFER, 13), b'hello, world\n')
        self.assertEqual(vm.call('fsReadAll', HANDLE, file_id, BUFFER, 12), error(EINVAL))

        status, (new_id, size, capacity) = vm.open('log', CREATE, 3)
        self.assertEqual((status, size, capacity), (0, 0, 1536))
        payload = content(9, 1300)
        for i, byte in enumerate(payload):
            vm.memory[OTHER + i] = byte
        self.assertEqual(vm.call('fsWriteAll', HANDLE, new_id, 0, OTHER, 1300), 0)
        self.assertEqual(vm.call('fsSync', HANDLE), 0)
        self.assertEqual(wfs.files(vm.service.disk.durable)['log'], payload)
        self.assertEqual(vm.call('fsReadAll', HANDLE, new_id, BUFFER, 2000), 1300)
        self.assertEqual(vm.bytes_at(BUFFER, 1300), payload)

        names = []
        for index in range(5):
            status = vm.call('fsListAt', HANDLE, index, BUFFER, OTHER + 4096)
            if status != 0:
                self.assertEqual(status, error(ENOENT))
                break
            names.append((vm.bytes_at(BUFFER, 16).rstrip(b'\0').decode(), vm.word(OTHER + 4096)))
        self.assertEqual(names, [('motd', 13), ('log', 1300)])

        self.assertEqual(vm.call('fsRemove', HANDLE, vm.cstring('motd')), 0)
        self.assertEqual(vm.call('fsRemove', HANDLE, vm.cstring('motd')), error(ENOENT))
        self.assertEqual(vm.call('fsSync', HANDLE), 0)
        self.assertEqual(set(wfs.files(vm.service.disk.durable)), {'log'})
        self.assertEqual(vm.call('fsInfo', HANDLE, FILE), 0)
        self.assertEqual([vm.word(FILE, i) for i in range(4)], [20, 20 - 3, 3, 1])

    def test_whole_file_helpers(self):
        vm = ClientVM(wfs.mkfs(24))
        text = b'The quick brown fox jumps over the lazy dog' * 5
        for i, byte in enumerate(text):
            vm.memory[OTHER + i] = byte
        self.assertEqual(vm.call('fsWriteFile', HANDLE, vm.cstring('fox'), OTHER, len(text)), 0)
        self.assertEqual(wfs.files(vm.service.disk.durable), {'fox': text})
        self.assertEqual(vm.call('fsReadFile', HANDLE, vm.cstring('fox'), BUFFER, 512), len(text))
        self.assertEqual(vm.bytes_at(BUFFER, len(text)), text)
        # Rewriting with something shorter or longer replaces the file atomically.
        for replacement in (b'short', content(2, 1200), b''):
            for i, byte in enumerate(replacement):
                vm.memory[OTHER + i] = byte
            self.assertEqual(vm.call('fsWriteFile', HANDLE, vm.cstring('fox'), OTHER, len(replacement)), 0)
            self.assertEqual(wfs.files(vm.service.disk.durable)['fox'], replacement)
        self.assertEqual(vm.call('fsReadFile', HANDLE, vm.cstring('nothing'), BUFFER, 512), error(ENOENT))

    def test_arguments_are_checked_before_anything_is_sent(self):
        vm = ClientVM(wfs.mkfs(24, {'f': b'x'}))
        file_id = vm.open('f')[1][0]
        before = len(vm.requests)
        for name in ('', 'x' * 17, 'a b'):
            status = vm.call('fsOpen', HANDLE, vm.cstring(name), CREATE, 1, FILE)
            self.assertIn(status, (error(EINVAL),), name)
        self.assertEqual(vm.call('fsOpen', HANDLE, 0, 0, 0, FILE), error(EINVAL))
        self.assertEqual(vm.call('fsRemove', HANDLE, vm.cstring('')), error(EINVAL))
        self.assertEqual(len(vm.requests) - before, 1)  # only the 'a b' name reached the service
        for count in (0, 17):
            self.assertEqual(vm.call('fsReadAt', HANDLE, file_id, 0, BUFFER, count), error(EINVAL))
            self.assertEqual(vm.call('fsWriteAt', HANDLE, file_id, 0, BUFFER, count), error(EINVAL))
        self.assertEqual(vm.call('fsReadAt', HANDLE, file_id, 0, 0, 1), error(EINVAL))
        self.assertEqual(vm.call('fsWriteAt', HANDLE, file_id, 0, 0, 1), error(EINVAL))
        self.assertEqual(vm.call('fsListAt', HANDLE, 0, 0, FILE), error(EINVAL))
        self.assertEqual(vm.call('fsInfo', HANDLE, 0), error(EINVAL))
        self.assertEqual(len(vm.requests) - before, 1)

    def test_a_dishonest_service_cannot_make_the_client_overrun_its_buffers(self):
        image = wfs.mkfs(24, {'f': content(1, 200)})
        cases = {
            'wrong header': lambda out: [0] + out[1:],
            'wrong generation': lambda out: out[:2] + [2] + out[3:],
            'short reply': lambda out: 12,
            'error with a value': lambda out: out[:1] + [error(ENOENT)] + out[2:3] + [5] + out[4:],
            'positive status': lambda out: out[:1] + [1] + out[2:],
            'oversized count': lambda out: out[:3] + [99] + out[4:],
        }
        for name, mangle in cases.items():
            with self.subTest(name):
                vm = ClientVM(image)
                file_id = vm.open('f')[1][0]
                vm.mangle = mangle
                for i in range(64):
                    vm.memory[BUFFER + i] = 0xEE
                status = vm.call('fsReadAt', HANDLE, file_id, 0, BUFFER, 16)
                self.assertEqual(status, error(EPROTO))
                self.assertEqual(vm.bytes_at(BUFFER + 16, 48), b'\xee' * 48)

    def test_the_acceptance_client_passes_against_the_service(self):
        import build_fs_volume
        vm = ClientVM(build_fs_volume.volume(), root='tests/programs/fs/client.m')
        self.assertEqual(vm.call('fcMain', HANDLE), 0)
        self.assertEqual(wfs.files(vm.service.disk.durable), build_fs_volume.FINAL_FILES)
        wfs.check_table(wfs.current(bytes(vm.service.disk.durable))[1])
        self.assertEqual(vm.service.disk.pending, [])
        self.assertFalse(vm.service.globals['fsFailed'])

    def test_the_acceptance_client_names_its_failing_step(self):
        import build_fs_volume
        read_only = ClientVM(build_fs_volume.volume(), writable=False, root='tests/programs/fs/client.m')
        self.assertEqual(read_only.call('fcMain', HANDLE), 3)  # not writable
        full = ClientVM(wfs.mkfs(40, {'motd': build_fs_volume.MOTD, 'big': (bytes(10), 34)}),
                        root='tests/programs/fs/client.m')
        self.assertNotEqual(full.call('fcMain', HANDLE), 0)

    def test_read_only_service_refuses_changes_through_the_client(self):
        vm = ClientVM(wfs.mkfs(24, {'f': b'x'}), writable=False)
        self.assertEqual(vm.open('g', CREATE, 1)[0], error(EROFS))
        self.assertEqual(vm.call('fsWriteFile', HANDLE, vm.cstring('f'), OTHER, 1), error(EROFS))
        self.assertEqual(vm.call('fsSync', HANDLE), 0)


if __name__ == '__main__':
    unittest.main()
