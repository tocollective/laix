"""The filesystem CPU probe's judgement, fed with a modelled run (no emulator)."""
import struct
import sys
import unittest

from source_m import LAYOUT as C
from test_kernel import LAIX

sys.path.insert(0, str(LAIX / 'tools'))
import build_fs_volume  # noqa: E402
import wfs  # noqa: E402
from test_fsclient import ClientVM, HANDLE  # noqa: E402
import probe_fs_cpu  # noqa: E402

BOOT_SECTORS = 5
STORE, SYNC = C['DISK_STORE_HEADER'], C['DISK_SYNC_HEADER']


def boot_image():
    header = struct.pack('<4sIII', b'WRMB', BOOT_SECTORS, 0, 0)
    return header + bytes(BOOT_SECTORS * 512 - len(header))


def modelled_run():
    """What the probe would collect: a snapshot before each WRITE and FLUSH."""
    boot = boot_image()
    vm = ClientVM(build_fs_volume.volume(), root='tests/programs/fs/client.m')
    disk = vm.service.disk
    snapshots = []
    handle = disk.handle

    def watching(words, size):
        if words[0] in (STORE, SYNC):
            command = probe_fs_cpu.WRITE if words[0] == STORE else probe_fs_cpu.FLUSH
            snapshots.append((command, boot + bytes(disk.crash_image('all'))))
        return handle(words, size)

    disk.handle = watching
    assert vm.call('fcMain', HANDLE) == 0
    result = dict(final=boot + bytes(disk.durable), snapshots=snapshots, boot_bytes=len(boot),
                  submits={probe_fs_cpu.WRITE: sum(1 for c, _ in snapshots if c == probe_fs_cpu.WRITE),
                           probe_fs_cpu.FLUSH: sum(1 for c, _ in snapshots if c == probe_fs_cpu.FLUSH)},
                  finished=[])
    return result, boot + build_fs_volume.volume()


class ProbeJudgementTests(unittest.TestCase):
    def test_a_modelled_run_is_judged_consistent(self):
        result, original = modelled_run()
        report = probe_fs_cpu.judge(result, original)
        self.assertGreater(report['snapshots'], 20)
        self.assertEqual(report['committed_states'], 7)
        self.assertEqual(report['snapshots'], report['write_commands'] + report['flush_commands'])

    def test_corruption_and_reordering_are_rejected(self):
        result, original = modelled_run()
        probe_fs_cpu.judge(result, original)
        boot = result['boot_bytes']

        def with_change(edit):
            changed = dict(result, snapshots=list(result['snapshots']), final=bytearray(result['final']))
            edit(changed)
            return changed

        def damage_snapshot(changed):
            command, image = changed['snapshots'][10]
            image = bytearray(image)
            image[boot + 12] ^= 0x40  # inside the checksummed header of copy A
            image[boot + 1024 + 12] ^= 0x40  # and of copy B
            changed['snapshots'][10] = (command, bytes(image))

        def reorder(changed):
            changed['snapshots'].append(changed['snapshots'][0])  # an old state after the final one

        def touch_kernel_image(changed):
            changed['final'][3] ^= 1

        def wrong_final(changed):
            changed['final'][boot + 4 * 512] ^= 1  # a byte of a committed file

        def resize(changed):
            changed['final'] = changed['final'][:-512]

        for name, edit in (('damaged snapshot', damage_snapshot), ('reordered', reorder),
                           ('kernel image', touch_kernel_image), ('final contents', wrong_final),
                           ('size', resize)):
            with self.subTest(name):
                with self.assertRaises((ValueError, wfs.FormatError)):
                    probe_fs_cpu.judge(with_change(edit), original)


if __name__ == '__main__':
    unittest.main()
