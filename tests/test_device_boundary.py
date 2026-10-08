"""A7 authority, instance identity, policy separation and pin conservation."""
import unittest
from pathlib import Path

from source_m import SourceM, LAYOUT as C
from test_kernel import LAIX, check_m
from test_ipc_handles import error
from test_task import USER_DATA
from test_screen_services import kernel_fixture
from test_service_recovery import fixture, replacement, client, resolve
from test_runtime_tasks import create


def record(vm, name, field, value=None):
    address = vm.addresses[name] + vm.decls[name].sym.type.field(field).offset
    if value is not None:
        vm.memory[address] = value
    return vm.memory[address]


class DeviceBoundaryTests(unittest.TestCase):
    def test_manager_selects_window_with_scoped_child_and_device_authority(self):
        vm = fixture(devices=True)
        child = create(vm, configure=False, publish=False)
        self.assertGreater(vm.call('taskRuntimeDevices', child, C['DEVICE_DISK']), 0)
        before = record(vm, 'deviceExtent', 'generation')
        for offset, length in ((1, 16), (512, 97), (608, 1), (0xFFFFFE00, 16), (608, 0)):
            self.assertEqual(vm.call('taskRuntimeExtent', child, offset, length, 0), error(22))
        self.assertEqual(record(vm, 'deviceExtent', 'generation'), before)
        vm.memory[vm.field_address('deviceFactory', 1)] = 0
        self.assertEqual(vm.call('taskRuntimeExtent', child, 512, 96, 0), error(1))
        vm.memory[vm.field_address('deviceFactory', 1)] = C['DEVICE_DISK']
        self.assertEqual(vm.call('taskRuntimeExtent', child ^ 256, 512, 96, 0), error(1))
        self.assertEqual(vm.call('taskRuntimeExtent', child, 512, 96, 0), 0)
        self.assertEqual(vm.call('diskInfo', child), 96)
        token = vm.call('deviceSubmit', child, 0, 96, 1, 0)
        self.assertGreater(token, 0)
        self.assertEqual(vm.memory.commands[-1][2:], (3, 1))
        self.assertEqual(vm.call('taskRuntimeExtent', child, 0, 16, 0), error(16))
        vm.memory.complete()
        self.assertEqual(vm.call('deviceFinish', child, token, USER_DATA), 96)

    def test_zero_bytes_select_the_rest_of_the_approved_root(self):
        vm = fixture(devices=True)
        child = create(vm, configure=False, publish=False)
        self.assertGreater(vm.call('taskRuntimeDevices', child, C['DEVICE_DISK']), 0)
        # A manager that does not know the root's size (init) still gets all of it.
        self.assertEqual(vm.call('taskRuntimeExtent', child, 0, 0, 0), 0)
        self.assertEqual(vm.call('diskInfo', child), 608)
        self.assertEqual(vm.call('taskRuntimeExtent', child, 512, 0, 0), 0)
        self.assertEqual(vm.call('diskInfo', child), 96)
        self.assertEqual(vm.call('taskRuntimeExtent', child, 608, 0, 0), error(22))

    def test_arbitrary_resource_constructor_ignores_font_bytes(self):
        vm = kernel_fixture()
        vm.call('serviceDevicesRollback')
        vm.memory[vm.addresses['fontData']] = 0
        self.assertTrue(vm.call('deviceExtentInit', 1, 2, C['DISK0_BASE'], 7, 700))
        self.assertEqual(vm.call('diskInfo', 2), 700)
        token = vm.call('deviceSubmit', 2, 512, 188, 1, 0)
        self.assertEqual(vm.memory.commands[-1][2], 8)
        vm.memory.complete()
        self.assertEqual(vm.call('deviceFinish', 2, token, USER_DATA), 188)

    def test_storage_root_is_validated_and_producer_neutral(self):
        root = 0x15000
        good = (0x31525357, 1, 608, 0)
        for words in ((0x31525358, 1, 608, 0), (0x31525357, 0, 608, 0), (0x31525357, 2, 608, 0),
                      (0x31525357, 1, 0, 0), (0x31525357, 1, 0x80000000, 0), (0x31525357, 1, 608, 1)):
            vm = kernel_fixture(root=None)
            vm.call('serviceDevicesRollback')
            for i, word in enumerate(words):
                vm.memory[root + 4 * i] = word
            self.assertEqual(vm.call('approvedStorageBytes'), 0)
            self.assertFalse(vm.call('serviceDevicesInit', 1, 2, C['DISK0_BASE'], 1024))
            self.assertEqual(vm.globals['devicesInitialized'], 0)
            for i, word in enumerate(good):
                vm.memory[root + 4 * i] = word
            self.assertTrue(vm.call('serviceDevicesInit', 1, 2, C['DISK0_BASE'], 1024))
        # The covered bytes (font or otherwise) are never read: only the framing is.
        vm = kernel_fixture()
        self.assertEqual(vm.call('approvedStorageBytes'), 608)
        self.assertEqual(vm.call('diskInfo', 2), 608)

    def test_storage_root_tool_matches_kernel_framing(self):
        import sys
        sys.path.insert(0, str(LAIX / 'tools'))
        import storage_root
        data = storage_root.pack(700)
        self.assertEqual(len(data), 16)
        self.assertEqual(storage_root.unpack(data), 700)
        for bad in (0, 0x80000000, -1):
            with self.assertRaises(ValueError):
                storage_root.pack(bad)
        for corrupt in (b'WSR2' + data[4:], data[:4] + b'\x02' + data[5:], data[:12] + b'\x01' + data[13:], data[:-1]):
            with self.assertRaises(ValueError):
                storage_root.unpack(corrupt)
        self.assertGreater(storage_root.unpack((LAIX / 'fonts/storage-extent.bin').read_bytes()), 0)

    def test_unsafe_commands_and_address_injection_issue_no_dma(self):
        vm = kernel_fixture()
        free = vm.free_pages()
        for command in (0, 4, 0x101, 0xFFFFFFFF):
            self.assertEqual(vm.call('deviceSubmit', 2, 0, 16, command, 0), error(22))
        # WRITE and FLUSH exist, but a read-only extent issues nothing.
        for command in (2, 3):
            self.assertEqual(vm.call('deviceSubmit', 2, 0, 512 if command == 2 else 0, command, USER_DATA), error(30))
        for offset in (USER_DATA, C['DISK0_BASE'], 0xFFFFFFFF):
            self.assertEqual(vm.call('deviceSubmit', 2, offset, 16, 1, 0), error(22))
        self.assertEqual(vm.memory.commands, [])
        self.assertEqual(vm.free_pages(), free)

    def test_instance_tokens_and_exactly_once_completion(self):
        vm = kernel_fixture()
        first = vm.call('deviceSubmit', 2, 0, 512, 1, 0)
        bounce = vm.globals['deviceBounce']
        self.assertEqual(vm.call('deviceFinish', 1, first, USER_DATA), error(1))
        self.assertEqual(vm.call('deviceCancel', 2, first + 1), error(22))
        self.assertEqual(vm.call('deviceFinish', 2, first + 1, USER_DATA), error(22))
        self.assertEqual(vm.call('physicalPageReferences', bounce), 1)
        vm.memory.complete()
        self.assertEqual(vm.call('deviceFinish', 2, first, USER_DATA), 512)
        self.assertEqual(vm.call('deviceFinish', 2, first, USER_DATA), error(22))
        self.assertTrue(vm.call('physicalPageAvailable', bounce))
        second = vm.call('deviceSubmit', 2, 0, 16, 1, 0)
        self.assertGreater(second, first)
        self.assertEqual(vm.call('deviceFinish', 2, first, USER_DATA), error(22))
        self.assertEqual(vm.call('deviceCancel', 2, first), error(22))
        vm.memory.complete()
        self.assertEqual(vm.call('deviceFinish', 2, second, USER_DATA), 16)

    def test_cancel_canaries_and_immutable_owner_survive_late_dma(self):
        vm = kernel_fixture()
        token = vm.call('deviceSubmit', 2, 0, 512, 1, 0)
        bounce = vm.globals['deviceBounce']
        for address in (bounce - 4, bounce + 512, bounce + 4092):
            vm.memory[address] = 0xC0FFEE
        self.assertEqual(vm.call('deviceCancel', 2, token), 0)
        self.assertEqual(record(vm, 'deviceOperation', 'owner'), 2)
        self.assertEqual(record(vm, 'deviceOperation', 'instance'), token)
        free = vm.free_pages()
        for _ in range(12):
            vm.call('deviceReap')
            self.assertFalse(vm.call('serviceDevicesQuiescent', 2))
            self.assertEqual(vm.call('deviceSubmit', 2, 0, 16, 1, 0), error(32))
            self.assertNotIn(bounce, vm.free_pages())
            self.assertEqual(vm.call('physicalPageReferences', bounce), 1)
        self.assertEqual(vm.free_pages(), free)
        vm.memory.complete()
        self.assertEqual(bytes(vm.memory[bounce + i] for i in range(512)), bytes(range(256)) * 2)
        for address in (bounce - 4, bounce + 512, bounce + 4092):
            self.assertEqual(vm.memory[address], 0xC0FFEE)
        vm.call('deviceReap')
        vm.call('deviceReap')
        self.assertTrue(vm.call('physicalPageAvailable', bounce))
        self.assertEqual(vm.call('deviceFinish', 2, token, USER_DATA), error(22))

    def test_instance_exhaustion_never_wraps_or_allocates(self):
        vm = kernel_fixture()
        record(vm, 'deviceOperation', 'instance', 0x7FFFFFFF)
        self.assertEqual(vm.call('deviceSubmit', 2, 0, 16, 1, 0), error(75))
        self.assertEqual(vm.globals['deviceBounce'], 0)
        self.assertEqual(vm.memory.commands, [])

    def test_user_display_policy_changes_mode_palette_and_scanout(self):
        vm = kernel_fixture()
        for offset in (0, 8, 32):
            vm.memory[C['VIDEO_BASE'] + offset] = 0
        for mode in (0x20, 0x21, 0x01):
            self.assertEqual(vm.call('screenControl', 1, 8, mode), 0)
        for register, value in ((40, 7), (44, 0x123456), (32, 4096), (4, 5), (0, 10)):
            self.assertEqual(vm.call('screenControl', 1, register, value), 0)
        before = dict(vm.memory)
        for register, value in ((8, 0x41), (8, 0x80), (32, 0xFFFFFFFF),
                                (4, 7), (40, 256), (44, 0x1000000), (128, 1),
                                (0x40, 4), (0x6C, USER_DATA), (0x70, 4), (0, 1)):
            self.assertEqual(vm.call('screenControl', 1, register, value), error(22))
        self.assertEqual(dict(vm.memory), before)

    def test_kernel_device_closure_contains_no_font_parser_or_renderer(self):
        modules = check_m(LAIX / 'src/drivers/service_devices.m')
        self.assertFalse(any(Path(module.path).name in ('font.m', 'glyph_cache.m', 'console.m', 'videocard.m')
                             for module in modules))
        source = (LAIX / 'src/drivers/service_devices.m').read_text()
        self.assertNotIn('fontData', source)
        self.assertNotIn('GLYPH_BYTES', source)
        normal = (LAIX / 'build.sh').read_text().split('for module in ', 1)[1].split('; do', 1)[0]
        for module in ('drivers/videocard', 'console/console', 'console/font/font', 'console/font/glyph_cache'):
            self.assertNotIn(module, normal)

    def test_cancelled_dma_reaps_on_idle_without_a_ready_task(self):
        vm = kernel_fixture(start=True)
        token = vm.call('deviceSubmit', 2, 0, 16, 1, 0)
        bounce = vm.globals['deviceBounce']
        self.assertEqual(vm.call('deviceCancel', 2, token), 0)
        for owner in (1, 2, 3):
            vm.call('taskBlock', vm.field_address('context', owner), 1)
        vm.controls[0] = 0
        vm.cpu_sp = vm.idle_field('kernelStackTop')
        vm.memory.complete()
        self.assertEqual(vm.globals['readyCount'], 0)
        self.assertEqual(vm.call('taskIdlePoll'), 0)
        self.assertEqual(vm.globals['deviceBounce'], 0)
        self.assertTrue(vm.call('physicalPageAvailable', bounce))

    def test_shared_video_causes_require_complete_servicing_before_rearm(self):
        from test_screen_services import Devices
        vm = kernel_fixture(start=True)
        class SharedVideo(Devices):
            causes = 10
            def __getitem__(device, address):
                if address == C['VIDEO_BASE']:
                    return device.causes
                return super().__getitem__(address)
            def __setitem__(device, address, value):
                if address == C['VIDEO_BASE']:
                    device.causes &= ~(value & 10)
                    if device.causes == 0:
                        device.lines &= ~(1 << 5)
                    return
                super().__setitem__(address, value)
        vm.memory = SharedVideo(vm)
        vm.memory.lines |= 1 << 5
        self.assertEqual(vm.call('irqComplete', 1, vm.screen_irq), error(16))
        self.assertEqual(vm.call('screenControl', 1, 0, 2), 0)
        for _ in range(8):
            self.assertEqual(vm.call('irqComplete', 1, vm.screen_irq), error(16))
            self.assertEqual(vm.memory[C['PIC_ENABLE']] & (1 << 5), 0)
        self.assertEqual(vm.call('screenControl', 1, 0, 8), 0)
        self.assertEqual(vm.call('irqComplete', 1, vm.screen_irq), 0)
        self.assertNotEqual(vm.memory[C['PIC_ENABLE']] & (1 << 5), 0)

    def test_stuck_engine_quarantine_preserves_unrelated_service_calls(self):
        vm = fixture(devices=True)
        caller = client(vm)
        disk, _, _, _ = replacement(vm, 1, name=3, devices=C['DEVICE_DISK'])
        peer, _, rx, _ = replacement(vm, 1, name=1)
        vm.run(disk)
        self.assertGreater(vm.call('deviceSubmit', disk, 0, 16, 1, 0), 0)
        bounce = vm.globals['deviceBounce']
        vm.run(1)
        vm.invoke(C['SYS_TASK_TERMINATE'], disk, 0)
        vm.run(caller)
        tx, _, _, _ = resolve(vm, 1)
        for _ in range(8):
            vm.request(tx, b'live')
            vm.run(peer)
            token = vm.accept(rx)
            vm.response(token, b'live')
            self.assertEqual(vm.result(caller)[0], 4)
            vm.run(1)
            vm.reap()
            self.assertEqual(vm.field('state', disk), 3)
            self.assertFalse(vm.call('physicalPageAvailable', bounce))
            self.assertEqual(vm.call('physicalPageReferences', bounce), 1)
            vm.run(caller)

    def test_register_inventory_covers_every_hardware_header_register(self):
        import re
        contract = (LAIX / 'docs/DEVICE_CONTRACT.md').read_text()
        for header in (LAIX.parent / 'include/devices').glob('*.h'):
            for name in re.findall(r'^#define\s+(\w+_REG_\w+)\s', header.read_text(), re.M):
                self.assertIn('`' + name + '`', contract)

    def test_user_video_rearm_services_racing_shared_causes_with_a_bound(self):
        class Video(SourceM):
            def __init__(vm, held=False):
                super().__init__(LAIX / 'user/screen/video.m')
                vm.acks = vm.rearms = 0
                vm.held = held
            def call(vm, name, *args):
                if name == 'screenControl':
                    self.assertEqual(args, (0, 10))
                    vm.acks += 1
                    return 0
                if name == 'irqComplete':
                    vm.rearms += 1
                    return error(16) if vm.held or vm.rearms < 3 else 0
                return super().call(name, *args)
        vm = Video()
        self.assertEqual(vm.call('videoRearm'), 0)
        self.assertEqual((vm.acks, vm.rearms), (3, 3))
        vm = Video(held=True)
        self.assertEqual(vm.call('videoRearm'), error(16))
        self.assertEqual((vm.acks, vm.rearms), (4, 4))

    def test_user_font_translation_rejects_wrapping_offsets_before_syscall(self):
        class User(SourceM):
            def __init__(self):
                super().__init__(LAIX / 'user/syscalls.m')
                self.calls = []
            def trap(self, cause, args=()):
                self.calls.append(tuple(args))
                return 17
        vm = User()
        for glyph, chunk in ((0xFFFFFFFF, 0), (0, 2), (0, 0xFFFFFFFF)):
            self.assertEqual(vm.call('fontBegin', glyph, chunk), error(22))
        self.assertEqual(vm.calls, [])
        self.assertEqual(vm.call('fontBegin', 18, 1), 17)
        self.assertEqual(vm.calls, [(C['SYS_DEVICE_SUBMIT'], 592, 16, 1, 0)])


if __name__ == '__main__':
    unittest.main()
