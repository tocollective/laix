"""WFS1 service (user/services/fs.m) against a Disk model with power-loss injection."""
import random
import struct
import sys
import unittest
import zlib

from source_m import SourceM, LAYOUT as C
from test_ipc_handles import error
from test_kernel import LAIX

sys.path.insert(0, str(LAIX / 'tools'))
import wfs  # noqa: E402

REQ, RES, DISK = 0x1000000, 0x1001000, 0x102
OK, ENOENT, EIO, EINVAL, EPIPE, EEXIST, ENODEV, ENOSPC, EROFS = 0, 2, 5, 22, 32, 17, 19, 28, 30
OPEN, STAT, READ, WRITE, SYNC, DELETE, LIST, INFO = (C['FS_' + n + '_HEADER'] for n in
    ('OPEN', 'STAT', 'READ', 'WRITE', 'SYNC', 'DELETE', 'LIST', 'INFO'))
CREATE, EXCL, TRUNC = C['FS_CREATE'], C['FS_EXCL'], C['FS_TRUNC']


class PowerLoss(Exception):
    pass


class DiskModel:
    """The Disk service's sector-stage protocol over an image.

    A STORE lands in a volatile cache; SYNC makes the cache durable. Power loss
    keeps the durable image plus any subset of the unflushed stores (a sector is
    always stored whole).
    """

    def __init__(self, image, writable=True, crash_at=None):
        self.durable = bytearray(image)
        self.pending = []
        self.stage = bytearray(512)
        self.writable = writable
        self.crash_at = crash_at
        self.events = 0
        self.fail = None  # status the device reports for the next STORE/SYNC
        self.counts = {}

    def view(self):
        image = bytearray(self.durable)
        for sector, data in self.pending:
            image[sector * 512:sector * 512 + 512] = data
        return image

    def crash_image(self, mode):
        image = bytearray(self.durable)
        if mode == 'all':
            keep = self.pending
        elif mode == 'none':
            keep = []
        else:
            keep = [p for p in self.pending if random.Random(f'{mode}{p[0]}{self.events}').random() < 0.5]
        for sector, data in keep:
            image[sector * 512:sector * 512 + 512] = data
        return bytes(image)

    def event(self):
        if self.crash_at is not None and self.events == self.crash_at:
            raise PowerLoss()
        self.events += 1

    def handle(self, words, size):
        header, generation, a, b = words[:4]
        names = {C['DISK_' + n + '_HEADER']: n for n in ('STAT', 'LOAD', 'PEEK', 'POKE', 'STORE', 'SYNC', 'FLAGS')}
        op = names[header]
        self.counts[op] = self.counts.get(op, 0) + 1
        if generation != 1:
            return error(EPIPE), 0, [0] * 4
        if op == 'STAT':
            return 0, len(self.durable), [0] * 4
        if op == 'FLAGS':
            return 0, 1 if self.writable else 0, [0] * 4
        if op == 'LOAD':
            self.stage[:] = self.view()[a:a + 512]
            return 0, 512, [0] * 4
        if op == 'PEEK':
            return 0, 16, list(struct.unpack('<4I', bytes(self.stage[a:a + 16])))
        if op == 'POKE':
            self.stage[a:a + 16] = struct.pack('<4I', *words[4:8])
            return 0, 0, [0] * 4
        if not self.writable:
            return error(EROFS), 0, [0] * 4
        self.event()
        if self.fail:
            status, self.fail = self.fail, None
            return error(status), 0, [0] * 4
        if op == 'STORE':
            self.pending.append((a // 512, bytes(self.stage)))
        else:
            for sector, data in self.pending:
                self.durable[sector * 512:sector * 512 + 512] = data
            self.pending = []
        return 0, 0, [0] * 4


class FsVM(SourceM):
    def __init__(self, image, writable=True, crash_at=None, real_crc=False):
        super().__init__(LAIX / 'user/services/fs.m')
        self.disk = DiskModel(image, writable, crash_at)
        self.real_crc = real_crc
        self.crcs = 0

    def call(self, name, *args):
        if name in ('call', 'callTimed'):
            _, req, size, res = args[:4]
            words = [self.memory[req + 4 * i] for i in range((size + 3) // 4)]
            status, value, data = self.disk.handle(words, size)
            for i, word in enumerate([C['DISK_RESPONSE_HEADER'], status & 0xFFFFFFFF, 1, value] + data):
                self.memory[res + 4 * i] = word & 0xFFFFFFFF
            return 32
        if name == 'fsCrc' and not self.real_crc:
            self.crcs += 1
            table = bytearray(struct.pack('<256I', *(self.memory[args[0] + 4 * i] for i in range(256))))
            table[20:24] = bytes(4)
            return zlib.crc32(bytes(table)) & 0xFFFFFFFF
        return super().call(name, *args)

    def send(self, words, size):
        for i, word in enumerate(words):
            self.memory[REQ + 4 * i] = word
        self.call('fsHandle', REQ, size, RES, DISK)
        out = [self.memory[RES + 4 * i] for i in range(8)]
        status = out[1] - (1 << 32) if out[1] >= 1 << 31 else out[1]
        return status, out[3:8]

    def request(self, header, a=0, b=0, extra=(), size=16):
        return self.send([header, 1, a, b, *extra], size)

    def name_words(self, name):
        return list(struct.unpack('<4I', name.encode().ljust(16, b'\0')))

    def open(self, name, flags=0, capacity=0):
        status, out = self.send([OPEN, 1, flags, capacity, *self.name_words(name)], 32)
        return (status, out[0], out[1], out[2]) if status == 0 else (status,)

    def stat(self, file_id):
        status, out = self.request(STAT, file_id)
        return (status, out[0], out[1]) if status == 0 else (status,)

    def read_at(self, file_id, offset, count=16):
        status, out = self.request(READ, file_id, offset, (count,), 20)
        if status < 0:
            return status, b''
        return out[0], struct.pack('<4I', *out[1:5])[:out[0]]

    def write_at(self, file_id, offset, data):
        assert 1 <= len(data) <= 16
        padded = data.ljust(16, b'\0')
        return self.send([WRITE, 1, file_id, offset, *struct.unpack('<4I', padded)], 16 + len(data))

    def read_file(self, file_id):
        size = self.stat(file_id)[1]
        data = b''
        while len(data) < size:
            count, piece = self.read_at(file_id, len(data))
            assert count > 0 and count == len(piece), (count, len(data))
            data += piece
        return data

    def write_file(self, file_id, data, offset=0):
        done = 0
        while done < len(data):
            status, out = self.write_at(file_id, offset + done, data[done:done + 16])
            if status != 0:
                return status
            done += out[0]
        return 0

    def sync(self):
        return self.request(SYNC)[0]

    def delete(self, name):
        return self.send([DELETE, 1, 0, 0, *self.name_words(name)], 32)[0]

    def list(self):
        names = {}
        for index in range(40):
            status, out = self.request(LIST, index)
            if status != 0:
                assert status == -ENOENT
                break
            names[struct.pack('<4I', *out[1:5]).rstrip(b'\0').decode()] = out[0]
        return names

    def info(self):
        status, out = self.request(INFO)
        assert status == 0
        return dict(data=out[0], free=out[1], generation=out[2], flags=out[3])


def content(tag, size):
    return bytes((tag * 31 + i * 7 + i // 13) & 255 for i in range(size))


def reboot(image, **kwargs):
    return FsVM(image, **kwargs)


def digest(files):
    """Short stand-in for a file set in failure messages."""
    return {name: (len(data), zlib.crc32(data)) for name, data in files.items()}


def committed_files(image):
    return wfs.files(bytes(image))


class FsTests(unittest.TestCase):
    def test_crc_matches_the_reference(self):
        vm = FsVM(wfs.mkfs(16), real_crc=True)
        table = wfs.pack_table(7, 3, 16, [('x', 5, 4, 1, 1)] + [None] * 30)
        for i in range(256):
            vm.memory[0x2000000 + 4 * i] = struct.unpack_from('<I', table, 4 * i)[0]
        self.assertEqual(vm.call('fsCrc', 0x2000000), struct.unpack_from('<I', table, 20)[0])
        vm.memory[0x2000000 + 24] ^= 1
        self.assertNotEqual(vm.call('fsCrc', 0x2000000), struct.unpack_from('<I', table, 20)[0])

    def test_mounts_a_reference_image_and_reads_every_file(self):
        files = {'alpha': content(1, 1500), 'b': content(2, 16), 'empty': b'', 'long': content(3, 4096)}
        files.pop('empty')
        image = wfs.mkfs(40, files)
        vm = FsVM(image)
        self.assertEqual(vm.list(), {name: len(data) for name, data in files.items()})
        for name, data in files.items():
            status, file_id, size, capacity = vm.open(name)
            self.assertEqual((status, size), (0, len(data)))
            self.assertEqual(capacity, -(-len(data) // 512) * 512)
            self.assertEqual(vm.stat(file_id), (0, len(data), capacity))
            self.assertEqual(vm.read_file(file_id), data, name)
        status, out = vm.request(INFO)
        self.assertEqual(out[0], 40 - 4)
        self.assertEqual(vm.info()['flags'], 1)  # writable, nothing pending
        self.assertEqual(vm.disk.events, 0)  # reading never writes

    def test_a_sector_is_loaded_once_and_only_the_bytes_asked_for_are_peeked(self):
        vm = FsVM(wfs.mkfs(12, {'f': content(4, 1100)}))
        file_id = vm.open('f')[1]
        mounted = dict(vm.disk.counts)
        data = b''.join(vm.read_at(file_id, offset)[1] for offset in range(0, 512, 16))
        self.assertEqual(data, content(4, 1100)[:512])
        self.assertEqual(vm.disk.counts['LOAD'] - mounted['LOAD'], 1)
        self.assertEqual(vm.disk.counts['PEEK'] - mounted['PEEK'], 32)
        # A 16-byte read that straddles two chunks peeks both, nothing else.
        before = dict(vm.disk.counts)
        self.assertEqual(vm.read_at(file_id, 8, 16)[1], content(4, 1100)[8:24])
        self.assertEqual(vm.disk.counts['PEEK'] - before['PEEK'], 2)
        self.assertEqual(vm.disk.counts['LOAD'], before['LOAD'])
        # A dirty sector is served from the buffer without touching the device.
        vm.write_at(file_id, 20, b'dirty')
        before = dict(vm.disk.counts)
        self.assertEqual(vm.read_at(file_id, 16)[1][4:9], b'dirty')
        self.assertEqual(vm.disk.counts, before)

    def test_reads_stop_at_sector_and_file_ends(self):
        vm = FsVM(wfs.mkfs(12, {'f': content(4, 700)}))
        file_id = vm.open('f')[1]
        self.assertEqual(vm.read_at(file_id, 510, 16), (2, content(4, 700)[510:512]))
        self.assertEqual(vm.read_at(file_id, 512, 16), (16, content(4, 700)[512:528]))
        self.assertEqual(vm.read_at(file_id, 695, 16), (5, content(4, 700)[695:]))
        self.assertEqual(vm.read_at(file_id, 700, 16), (0, b''))
        for offset, count in ((701, 16), (0, 0), (0, 17), (0xFFFFFFFF, 1)):
            self.assertEqual(vm.read_at(file_id, offset, count)[0], -EINVAL, (offset, count))

    def test_create_write_sync_is_visible_to_the_reference_parser(self):
        vm = FsVM(wfs.mkfs(16, {'old': content(1, 100)}))
        status, file_id, size, capacity = vm.open('new', CREATE, 2)
        self.assertEqual((status, size, capacity), (0, 0, 1024))
        data = content(5, 900)
        self.assertEqual(vm.write_file(file_id, data), 0)
        self.assertEqual(vm.read_file(file_id), data)  # read your own writes
        before = committed_files(vm.disk.view())
        self.assertEqual(before, {'old': content(1, 100)})  # nothing durable yet
        self.assertEqual(vm.sync(), 0)
        after = committed_files(vm.disk.durable)
        self.assertEqual(after, {'old': content(1, 100), 'new': data})
        slot, table = wfs.current(bytes(vm.disk.durable))
        self.assertEqual((slot, table['generation']), (1, 2))  # the other copy, one newer
        wfs.check_table(table)
        self.assertEqual(vm.disk.pending, [])
        self.assertEqual(vm.sync(), 0)  # nothing to do: no new generation
        self.assertEqual(wfs.current(bytes(vm.disk.durable))[1]['generation'], 2)
        vm2 = reboot(vm.disk.durable)
        self.assertEqual(vm2.read_file(vm2.open('new')[1]), data)
        self.assertEqual(vm2.info()['generation'], 2)

    def test_capacity_is_fixed_and_writes_clip_at_it(self):
        vm = FsVM(wfs.mkfs(16))
        file_id = vm.open('f', CREATE, 1)[1]
        self.assertEqual(vm.write_file(file_id, content(1, 512)), 0)
        self.assertEqual(vm.write_at(file_id, 512, b'x')[0], -ENOSPC)
        self.assertEqual(vm.write_at(file_id, 600, b'x')[0], -EINVAL)  # no holes
        file_id2 = vm.open('g', CREATE, 1)[1]
        self.assertEqual(vm.write_file(file_id2, content(2, 505)), 0)
        status, out = vm.write_at(file_id2, 505, b'0123456789')
        self.assertEqual((status, out[0]), (0, 7))  # only what fits in the sector
        self.assertEqual(vm.write_at(file_id2, 512, b'x')[0], -ENOSPC)
        self.assertEqual(vm.stat(file_id2), (0, 512, 512))

    def test_overwrite_in_place_and_unaligned_writes(self):
        vm = FsVM(wfs.mkfs(16, {'f': content(1, 1000)}))
        file_id = vm.open('f')[1]
        expected = bytearray(content(1, 1000))
        for offset, data in ((3, b'hello'), (510, b'sector-edge'), (998, b'tail!!'), (0, b'Z' * 16), (700, b'mid')):
            status, out = vm.write_at(file_id, offset, data)
            self.assertEqual(status, 0)
            taken = out[0]
            expected[offset:offset + taken] = data[:taken]
        self.assertEqual(vm.read_file(file_id), bytes(expected))
        self.assertEqual(vm.sync(), 0)
        self.assertEqual(committed_files(vm.disk.durable)['f'], bytes(expected))

    def test_truncate_replaces_atomically_and_old_ids_go_stale(self):
        old = content(1, 600)
        vm = FsVM(wfs.mkfs(16, {'f': old}))
        old_id = vm.open('f')[1]
        status, new_id, size, capacity = vm.open('f', TRUNC)
        self.assertEqual((status, size, capacity), (0, 0, 1024))
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(vm.stat(old_id), (-ENOENT,))
        self.assertEqual(vm.read_at(old_id, 0)[0], -ENOENT)
        self.assertEqual(vm.write_at(old_id, 0, b'x')[0], -ENOENT)
        self.assertEqual(vm.write_file(new_id, content(2, 300)), 0)
        self.assertEqual(committed_files(vm.disk.view())['f'], old)  # the old file is untouched
        self.assertEqual(vm.sync(), 0)
        self.assertEqual(committed_files(vm.disk.durable)['f'], content(2, 300))
        # A different capacity may be asked for when replacing.
        status, new_id, size, capacity = vm.open('f', TRUNC, 3)
        self.assertEqual((status, capacity), (0, 1536))

    def test_open_flags_and_names(self):
        vm = FsVM(wfs.mkfs(16, {'f': content(1, 10)}))
        self.assertEqual(vm.open('missing'), (-ENOENT,))
        self.assertEqual(vm.open('f', CREATE | EXCL, 1), (-EEXIST,))
        self.assertEqual(vm.open('f', EXCL, 1), (-EINVAL,))
        self.assertEqual(vm.open('f', 8), (-EINVAL,))
        self.assertEqual(vm.open('zero', CREATE, 0), (-EINVAL,))
        self.assertEqual(vm.open('huge', CREATE, 13), (-ENOSPC,))
        self.assertEqual(vm.open('f', CREATE, 5)[0], 0)  # exists: capacity is ignored
        for bad in (b'', b' x', b'x y', b'\x7fx', b'a' * 17, b'ok\0junk'):
            words = list(struct.unpack('<4I', bad[:16].ljust(16, b'\0')))
            if len(bad) == 17:
                words = list(struct.unpack('<4I', bad[:16]))  # a 16-byte name is fine
                self.assertEqual(vm.send([OPEN, 1, CREATE, 1, *words], 32)[0], 0)
                continue
            self.assertEqual(vm.send([OPEN, 1, CREATE, 1, *words], 32)[0], -EINVAL, bad)
        vm.open('x' * 16, CREATE, 1)
        self.assertEqual(vm.open('x' * 16)[0], 0)

    def test_delete_frees_space_only_after_a_commit(self):
        vm = FsVM(wfs.mkfs(8, {'big': content(1, 4 * 512)}))  # four data sectors: the volume is full
        self.assertEqual(vm.info()['free'], 0)
        self.assertEqual(vm.delete('big'), 0)
        self.assertEqual(vm.delete('big'), -ENOENT)
        self.assertEqual(vm.info()['free'], 0)  # the committed table still owns the sectors
        self.assertEqual(vm.open('next', CREATE, 1), (-ENOSPC,))
        self.assertEqual(vm.sync(), 0)
        self.assertEqual(vm.info()['free'], 4)
        status, file_id, _, _ = vm.open('next', CREATE, 4)
        self.assertEqual(status, 0)
        self.assertEqual(committed_files(vm.disk.durable), {})
        self.assertEqual(vm.list(), {'next': 0})

    def test_free_space_accounting_matches_a_recount(self):
        rng = random.Random(7)
        vm = FsVM(wfs.mkfs(60))
        names = []
        for step in range(40):
            action = rng.choice(('create', 'create', 'delete', 'sync', 'replace'))
            if action == 'create':
                name = f'f{step}'
                if vm.open(name, CREATE, rng.randint(1, 6))[0] == 0:
                    names.append(name)
            elif action == 'delete' and names:
                self.assertEqual(vm.delete(names.pop(rng.randrange(len(names)))), 0)
            elif action == 'replace' and names:
                vm.open(rng.choice(names), TRUNC, rng.randint(1, 4))
            elif action == 'sync':
                self.assertEqual(vm.sync(), 0)
            info = vm.info()
            used = set()
            for table in (wfs.current(bytes(vm.disk.durable))[1], None):
                if table is None:
                    break
                for e in table['entries']:
                    if e:
                        used.update(range(e['first'], e['first'] + e['capacity']))
            for slot in range(31):
                base = 8 + slot * 8
                if vm.memory[vm.addresses['fsTable'] + 4 * base] & 255:
                    first = vm.memory[vm.addresses['fsTable'] + 4 * (base + 5)]
                    capacity = vm.memory[vm.addresses['fsTable'] + 4 * (base + 6)]
                    used.update(range(first, first + capacity))
            self.assertEqual(info['free'], 56 - len(used), step)
        vm.sync()
        wfs.check_table(wfs.current(bytes(vm.disk.durable))[1])

    def test_read_only_mount_refuses_changes(self):
        image = wfs.mkfs(16, {'f': content(1, 100)})
        vm = FsVM(image, writable=False)
        file_id = vm.open('f')[1]
        self.assertEqual(vm.read_file(file_id), content(1, 100))
        self.assertEqual(vm.open('g', CREATE, 1), (-EROFS,))
        self.assertEqual(vm.open('f', TRUNC), (-EROFS,))
        self.assertEqual(vm.write_at(file_id, 0, b'x')[0], -EROFS)
        self.assertEqual(vm.delete('f'), -EROFS)
        self.assertEqual(vm.sync(), 0)
        self.assertEqual(vm.info()['flags'], 0)
        self.assertEqual((vm.disk.events, bytes(vm.disk.durable)), (0, image))

    def test_unformatted_or_damaged_media_is_not_mounted(self):
        for name, image in (('blank', bytes(16 * 512)),
                            ('both copies bad', bytes(2 * 512) + bytes(2 * 512) + bytes(12 * 512)),
                            ('wrong size', wfs.mkfs(17)[:16 * 512]),
                            ('tiny', bytes(3 * 512))):
            with self.subTest(name):
                vm = FsVM(image)
                self.assertEqual(vm.request(INFO)[0], -ENODEV)
                self.assertEqual(vm.open('f', CREATE, 1), (-ENODEV,))
                self.assertEqual(vm.disk.events, 0)
                self.assertFalse(vm.globals['fsFailed'])

    def test_the_newest_valid_copy_wins_and_damage_falls_back(self):
        image = bytearray(wfs.mkfs(16, {'f': content(1, 100)}))
        vm = FsVM(bytes(image))
        vm.open('g', CREATE, 1)
        vm.write_file(vm.open('g')[1], b'second')
        vm.sync()
        newest = bytearray(vm.disk.durable)
        self.assertEqual(set(committed_files(newest)), {'f', 'g'})
        for damage in (lambda b: b.__setitem__(1024 + 100, b[1024 + 100] ^ 1),   # a checksum failure in copy B
                       lambda b: b.__setitem__(1024 + 520, b[1024 + 520] ^ 0x40),  # second sector of copy B
                       lambda b: b.__setitem__(1024, 0)):
            broken = bytearray(newest)
            damage(broken)
            reopened = FsVM(bytes(broken))
            self.assertEqual(reopened.list(), {'f': 100})  # the previous commit
        # An older copy that is damaged does not matter.
        broken = bytearray(newest)
        broken[10] ^= 1
        self.assertEqual(FsVM(bytes(broken)).list(), {'f': 100, 'g': 6})
        # The service never leaves a damaged copy as the only copy: after a fallback
        # the next commit writes the damaged slot again.
        fallback = FsVM(bytes(bytearray(newest[:1024 + 100]) + bytes([newest[1024 + 100] ^ 1]) + newest[1024 + 101:]))
        fallback.open('h', CREATE, 1)
        self.assertEqual(fallback.sync(), 0)
        self.assertEqual(set(committed_files(fallback.disk.durable)), {'f', 'h'})

    def test_invariants_are_checked_before_a_copy_is_trusted(self):
        base = wfs.mkfs(16, {'a': content(1, 600), 'b': content(2, 600)})
        table = bytearray(base[:1024])

        def forged(edit):
            words = list(struct.unpack('<256I', bytes(table)))
            edit(words)
            raw = bytearray(struct.pack('<256I', *words))
            raw[20:24] = struct.pack('<I', wfs.crc(raw))  # a valid checksum over bad contents
            return bytes(raw) + base[1024:]

        cases = {
            'overlap': lambda w: w.__setitem__(8 + 8 + 5, w[8 + 5]),
            'extent past the end': lambda w: w.__setitem__(8 + 6, 99),
            'extent in the metadata': lambda w: w.__setitem__(8 + 5, 1),
            'size beyond capacity': lambda w: w.__setitem__(8 + 4, 5000),
            'serial not below next': lambda w: w.__setitem__(8 + 7, w[2]),
            'zero serial': lambda w: w.__setitem__(8 + 7, 0),
            'bad name': lambda w: w.__setitem__(8, 0x20202020),
            'duplicate names': lambda w: [w.__setitem__(16 + i, w[8 + i]) for i in range(4)],
            'dirty free entry': lambda w: w.__setitem__(8 + 8 * 5 + 6, 3),
            'wrong volume size': lambda w: w.__setitem__(3, 20),
            'zero generation': lambda w: w.__setitem__(1, 0),
            'reserved set': lambda w: w.__setitem__(6, 1),
        }
        for name, edit in cases.items():
            with self.subTest(name):
                self.assertEqual(FsVM(forged(edit)).request(INFO)[0], -ENODEV)
                if name != 'wrong volume size':  # only the service knows the real size
                    with self.assertRaises(wfs.FormatError):
                        wfs.check_table(wfs.parse_table(forged(edit)[:1024]))
        self.assertEqual(FsVM(forged(lambda w: None)).request(INFO)[0], 0)

    def test_message_validation_and_generation(self):
        vm = FsVM(wfs.mkfs(16, {'f': content(1, 100)}))
        file_id = vm.open('f')[1]
        for header, size in ((OPEN, 16), (OPEN, 31), (STAT, 12), (STAT, 20), (READ, 16), (READ, 32),
                             (WRITE, 16), (WRITE, 33), (SYNC, 20), (DELETE, 16), (LIST, 12), (INFO, 20),
                             (0, 32), (C['DISK_LOAD_HEADER'], 16)):
            self.assertEqual(vm.send([header, 1, file_id, 0, 0, 0, 0, 0], size)[0], -EINVAL, (hex(header), size))
        for words, size in (([STAT, 1, file_id, 1], 16), ([SYNC, 1, 1, 0], 16), ([SYNC, 1, 0, 1], 16),
                            ([INFO, 1, 1, 0], 16), ([LIST, 1, 0, 1], 16), ([DELETE, 1, 1, 0, 1, 0, 0, 0], 32)):
            self.assertEqual(vm.send(words, size)[0], -EINVAL, words)
        for header, size in ((STAT, 16), (READ, 20), (INFO, 16), (SYNC, 16)):
            self.assertEqual(vm.send([header, 2, file_id, 0, 1], size)[0], -EPIPE)
        for stale in (0, 1, 31, file_id + 32, file_id ^ 1, 0xFFFFFFFF):
            self.assertEqual(vm.stat(stale), (-ENOENT,), stale)
        self.assertFalse(vm.globals['fsFailed'])

    def test_list_reports_every_file_in_slot_order(self):
        vm = FsVM(wfs.mkfs(70))
        for name in ('c', 'a', 'b'):
            vm.open(name, CREATE, 1)
        vm.delete('a')
        vm.open('d', CREATE, 1)
        self.assertEqual(list(vm.list()), ['c', 'd', 'b'])  # a freed slot is reused first
        for i in range(31 - 3):
            self.assertEqual(vm.open(f'n{i}', CREATE, 1)[0], 0)
        self.assertEqual(vm.open('overflow', CREATE, 1), (-ENOSPC,))
        self.assertEqual(len(vm.list()), 31)

    def test_device_failure_ends_the_service_and_the_commit_stays_old(self):
        for failing in ('store', 'sync'):
            vm = FsVM(wfs.mkfs(16, {'f': content(1, 100)}))
            file_id = vm.open('g', CREATE, 1)[1]
            vm.write_file(file_id, b'pending')
            events = vm.disk.events
            vm.disk.fail = EIO
            self.assertEqual(vm.sync(), -EIO)
            self.assertTrue(vm.globals['fsFailed'])
            self.assertEqual(committed_files(vm.disk.view()), {'f': content(1, 100)})
            reopened = FsVM(vm.disk.crash_image('none'))
            self.assertEqual(reopened.list(), {'f': 100})

    def test_crash_during_a_failed_commit_can_be_retried(self):
        vm = FsVM(wfs.mkfs(16, {'f': content(1, 100)}))
        file_id = vm.open('g', CREATE, 1)[1]
        vm.write_file(file_id, b'abc')
        vm.sync()
        vm.write_file(file_id, b'defg', 3)
        vm.disk.fail = EIO
        self.assertEqual(vm.sync(), -EIO)
        vm.disk.fail = None
        self.assertEqual(vm.sync(), 0)  # the retry commits everything that was pending
        self.assertEqual(committed_files(vm.disk.durable)['g'], b'abcdefg')
        self.assertEqual(wfs.current(bytes(vm.disk.durable))[1]['generation'], 3)


SCENARIO = [
    ('create', 'c', 2, content(3, 600)),
    ('sync',),
    ('replace', 'b', 0, content(4, 300)),
    ('sync',),
    # The lowest free space is the extent of the file being deleted: it must not
    # be reused before the commit that removes the file.
    ('delete', 'a'),
    ('create', 'd', 2, content(5, 700)),
    ('sync',),
    ('append', 'c', content(6, 300)),
    ('sync',),
]


def play(vm, steps, states=None, current=None):
    """Runs the scenario; returns how many SYNC requests completed."""
    done = 0
    current = dict(current or {})
    for step in steps:
        kind = step[0]
        if kind == 'create':
            file_id = vm.open(step[1], CREATE, step[2])[1]
            assert vm.write_file(file_id, step[3]) == 0
            current[step[1]] = step[3]
        elif kind == 'replace':
            file_id = vm.open(step[1], TRUNC, step[2])[1]
            assert vm.write_file(file_id, step[3]) == 0
            current[step[1]] = step[3]
        elif kind == 'delete':
            assert vm.delete(step[1]) == 0
            del current[step[1]]
        elif kind == 'append':
            file_id = vm.open(step[1])[1]
            assert vm.write_file(file_id, step[2], offset=len(current[step[1]])) == 0
            current[step[1]] = current[step[1]] + step[2]
        else:
            assert vm.sync() == 0
            done += 1
            if states is not None:
                states.append(dict(current))
    return done


class CrashTests(unittest.TestCase):
    """Power is lost at every device event of a scenario, in several ways."""

    def initial(self):
        return wfs.mkfs(24, {'a': content(1, 700), 'b': content(2, 100)})

    def test_every_crash_point_leaves_a_committed_state(self):
        start = {'a': content(1, 700), 'b': content(2, 100)}
        states = [start]
        reference = FsVM(self.initial())
        play(reference, SCENARIO, states, start)
        total = reference.disk.events
        self.assertGreater(total, 20)
        self.assertEqual(committed_files(reference.disk.durable), states[-1])
        for crash_at in range(total):
            vm = FsVM(self.initial(), crash_at=crash_at)
            completed = []
            with self.assertRaises(PowerLoss):
                play_with_count(vm, SCENARIO, completed, start)
            done = len(completed)
            # Only a crash inside a SYNC may already show the state it was committing.
            allowed = [states[done]] + ([states[done + 1]] if vm.in_sync else [])
            for mode in ('none', 'all', 'a', 'b', 'c'):
                image = vm.disk.crash_image(mode)
                got = committed_files(image)
                self.assertIn(digest(got), [digest(a) for a in allowed], (crash_at, mode, done))
                wfs.check_table(wfs.current(image)[1])
                # The recovered service agrees with the reference parser and keeps working.
                back = FsVM(image)
                self.assertEqual(back.list(), {n: len(d) for n, d in got.items()})
                for name, data in got.items():
                    self.assertEqual(back.read_file(back.open(name)[1]), data)
                self.assertEqual(back.open('fresh', CREATE, 1)[0], 0)
                self.assertEqual(back.sync(), 0)
                self.assertEqual(set(committed_files(back.disk.durable)), set(got) | {'fresh'})


def play_with_count(vm, steps, completed, current=None):
    """Like play(), recording each finished SYNC and whether a SYNC was running."""
    vm.in_sync = False
    original = vm.sync

    def counted():
        vm.in_sync = True
        status = original()
        vm.in_sync = False
        completed.append(1)
        return status

    vm.sync = counted
    play(vm, steps, current=current)


if __name__ == '__main__':
    unittest.main()
