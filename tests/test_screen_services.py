"""Execute checked service/MMU/IRQ/DMA source; device bytes are fixtures."""
import struct
import unittest
from pathlib import Path

from test_kernel import LAIX, check_m, parse_asm
from source_m import SourceM, LAYOUT as C
from test_task import TaskM, TaskEntered, USER_DATA
from test_timer_irq import TimerMemory
from test_ipc_handles import error


class Devices(TimerMemory):
    def __init__(self, vm):
        super().__init__(vm)
        self.disk = C['DISK0_BASE']
        self.disk_state = C['DISK_PRESENT']
        self.disk_error = 0
        self.count = 0
        self.commands = []
        dict.__setitem__(self, self.disk + 4, 64)
        for address in (C['TIMER_COUNT_LO'], C['TIMER_COUNT_HI']):
            dict.__setitem__(self, address, 0)

    def __getitem__(self, address):
        if address == C['PIC_PENDING']:
            return self.lines
        if address == C['TIMER_COUNT_LO']:
            return self.count
        if address == self.disk:
            return self.disk_state
        if address == self.disk + 24:
            return self.disk_error
        return super().__getitem__(address)

    def __setitem__(self, address, value):
        if address == self.disk:
            self.disk_state &= ~(value & (C['DISK_DONE'] | C['DISK_CHANGED']))
            if not self.disk_state & (C['DISK_DONE'] | C['DISK_CHANGED']):
                self.lines &= ~(1 << 3)
            return
        if address == self.disk + 20:
            self.commands.append((value, self[self.disk + 16], self[self.disk + 8], self[self.disk + 12]))
            self.disk_state = C['DISK_PRESENT'] | C['DISK_BUSY']
            self.lines &= ~(1 << 3)
        super().__setitem__(address, value)

    def complete(self, error_code=0, changed=False):
        physical = self[self.disk + 16]
        for offset in range(512):
            dict.__setitem__(self, physical + offset, offset & 255)
        self.disk_state = C['DISK_PRESENT'] | C['DISK_DONE']
        if changed:
            self.disk_state |= C['DISK_CHANGED']
        self.disk_error = error_code
        self.lines |= 1 << 3


def kernel_fixture(start=False, root=None):
    vm = TaskM(ram=0x200000, root=root)
    vm.memory = Devices(vm)
    vm.addresses.update(fontData=0x16000, fontDataEnd=0x16000 + 32 + 19 * 8)
    for i, value in enumerate((0x3146414C, 1, 19, 32, 184, 16, 32, 0)):
        vm.memory[0x16000 + i * 4] = value
    for id in range(1, 4):
        assert vm.call('taskCreateImage', vm.addresses['userCodeStart'], vm.addresses['userCodeEnd'], 0) == id
    assert vm.call('serviceDevicesInit', 1, 2, C['DISK0_BASE'], 1024)
    vm.screen_irq = vm.call('irqGrant', 1, 5)
    vm.disk_irq = vm.call('irqGrant', 2, 3)
    if start:
        for id in range(1, 4):
            vm.call('taskEnqueue', vm.call('taskGet', id))
        with unittest.TestCase().assertRaises(TaskEntered):
            vm.call('taskStart', 1000000)
    return vm


class ScreenSecurityTests(unittest.TestCase):
    def test_resource_grants_are_exclusive_nx_bounded_and_not_ipc_buffers(self):
        vm = kernel_fixture()
        root = vm.field('directory')
        grants = [(C['SCREEN_VRAM_VA'], C['VRAM_BASE'], C['SCREEN_VRAM_BYTES'], 23),
                  (C['SCREEN_VIDEO_VA'], C['VIDEO_BASE'], 4096, 19),
                  (C['SCREEN_FONT_VA'], 0x16000, 4096, 19)]
        for virtual, physical, size, perms in grants:
            before = dict(vm.memory)
            for args in [(virtual, physical, size, perms | 8), (virtual, physical, size + 4096, perms),
                         (virtual + 4096, physical, size, perms), (virtual, physical + 4096, size, perms),
                         (virtual, physical, size, perms ^ 4), (virtual, physical, 0xFFFFFFFF, perms)]:
                self.assertFalse(vm.call('mmuGrantResource', root, 1, *args))
                self.assertEqual(dict(vm.memory), before)
            self.assertTrue(vm.call('mmuGrantResource', root, 1, virtual, physical, size, perms))
            for offset in range(0, size, 4096):
                self.assertEqual(vm.leaf(virtual + offset, root), physical + offset | perms)
            self.assertEqual(vm.leaf(virtual + size, root), 0)
            self.assertFalse(vm.call('mmuUserBufferValid', root, 1, virtual, 1, 2))
            self.assertFalse(vm.call('setPagePermissions', root, 1, virtual, perms | 8))
            self.assertFalse(vm.call('unmapPage', root, 1, virtual))
            self.assertFalse(vm.call('mmuGrantResource', vm.field('directory', 2), 2, virtual, physical, size, perms))
        for device in range(C['IO_BASE'], C['IO_BASE'] + 17 * 4096, 4096):
            self.assertEqual(vm.leaf(device, root) & 24, 0)
        vm.call('mmuSealResources')
        self.assertFalse(vm.call('mmuGrantResource', root, 1, *grants[0]))

    def test_resource_rollback_preserves_shared_rodata_and_never_frees_vram(self):
        vm = kernel_fixture()
        free = vm.free_pages()
        root = vm.field('directory')
        self.assertTrue(vm.call('mmuGrantResource', root, 1, C['SCREEN_VRAM_VA'], C['VRAM_BASE'], C['SCREEN_VRAM_BYTES'], 23))
        self.assertTrue(vm.call('mmuGrantResource', root, 1, C['SCREEN_FONT_VA'], 0x16000, 4096, 19))
        self.assertTrue(vm.call('taskDiscardCreated', 1))
        self.assertFalse(vm.call('physicalPageAvailable', C['VRAM_BASE']))
        self.assertFalse(vm.call('physicalPageAvailable', 0x16000))
        self.assertGreaterEqual(len(vm.free_pages()), len(free))
        root2 = vm.field('directory', 3)
        self.assertTrue(vm.call('mmuGrantResource', root2, 3, C['SCREEN_VRAM_VA'], C['VRAM_BASE'], C['SCREEN_VRAM_BYTES'], 23))

    def test_irq_pending_before_wait_blocked_wake_and_level_rearm(self):
        vm = kernel_fixture(start=True)
        self.assertEqual(vm.call('irqComplete', 2, vm.screen_irq), error(1))
        self.assertEqual(vm.call('irqComplete', 1, vm.screen_irq), 0)
        vm.memory.lines |= 1 << 5
        self.assertTrue(vm.call('irqNotify', 5))
        self.assertEqual(vm.memory[C['PIC_ENABLE']] & (1 << 5), 0)
        frame = vm.field_address('context')
        epc = vm.memory[frame + C['TF_EPC']]
        self.assertEqual(vm.call('irqWait', frame, vm.screen_irq, 5), frame)
        self.assertEqual(vm.memory[frame + C['TF_R1']], 0)
        self.assertEqual(vm.memory[frame + C['TF_EPC']], epc)
        self.assertEqual(vm.call('irqComplete', 1, vm.screen_irq), error(16))
        vm.memory.lines &= ~(1 << 5)
        self.assertEqual(vm.call('irqComplete', 1, vm.screen_irq), 0)
        vm.call('irqWait', frame, vm.screen_irq, 5)
        self.assertEqual((vm.field('state'), vm.field('waitReason')), (4, 7))
        vm.memory.lines |= 1 << 5
        self.assertTrue(vm.call('irqNotify', 5))
        self.assertEqual((vm.field('state'), vm.field('waitReason')), (1, 0))
        self.assertEqual(vm.field('queued'), 1)
        self.assertTrue(vm.call('irqNotify', 5))
        self.assertEqual(vm.field('queued'), 1)
        self.assertEqual(vm.memory[C['PIC_ENABLE']] & 4, 4)

    def test_irq_timeout_foreign_token_unowned_mask_and_owner_death(self):
        vm = kernel_fixture(start=True)
        frame = vm.field_address('context')
        self.assertEqual(vm.call('irqComplete', 1, vm.screen_irq), 0)
        vm.call('irqWait', frame, vm.disk_irq, 5)
        self.assertEqual(vm.memory[frame + C['TF_R1']], error(1))
        vm.call('irqWait', frame, vm.screen_irq, 5)
        vm.memory.count = 5000000
        vm.call('irqTimerTick')
        self.assertEqual(vm.field('state'), 1)
        self.assertEqual(vm.memory[frame + C['TF_R1']], error(110))
        self.assertEqual(vm.memory[C['PIC_ENABLE']] & (1 << 5), 0)
        vm.memory[C['PIC_ENABLE']] |= 1 << 7
        self.assertFalse(vm.call('irqNotify', 7))
        self.assertEqual(vm.memory[C['PIC_ENABLE']] & (1 << 7), 0)
        vm.call('irqReleaseTask', 1)
        self.assertEqual(vm.call('irqComplete', 1, vm.screen_irq), error(1))

    def test_irq_notification_during_real_ipc_wait_remains_pending(self):
        vm = kernel_fixture()
        endpoint = vm.call('endpointBootstrapService', vm.field_address('handles', 2), 2)
        sender = vm.call('handleCopy', vm.field_address('handles', 2), endpoint,
                         vm.field_address('handles', 1), 2, 1, 1)
        for id in (1, 2, 3):
            vm.call('taskEnqueue', vm.call('taskGet', id))
        with self.assertRaises(TaskEntered):
            vm.call('taskStart', 1000000)
        frame = vm.field_address('context', 1)
        self.assertEqual(vm.call('irqComplete', 1, vm.screen_irq), 0)
        vm.call('ipcCall', frame, sender, USER_DATA, 0, USER_DATA, 0)
        self.assertEqual((vm.field('state'), vm.field('waitReason')), (4, 4))
        vm.memory.lines |= 1 << 5
        self.assertTrue(vm.call('irqNotify', 5))
        self.assertEqual((vm.field('state'), vm.field('waitReason')), (4, 4))
        self.assertNotEqual(vm.field('ipcEndpoint'), 0)
        receiver = vm.field_address('context', 2)
        vm.call('ipcAccept', receiver, endpoint, USER_DATA, 32)
        token = vm.memory[receiver + C['TF_R2']]
        vm.call('ipcReply', receiver, token, USER_DATA, 0)
        self.assertEqual(vm.field('state'), 1)
        vm.call('taskYield', receiver)
        vm.call('taskYield', vm.field_address('context', 3))
        self.assertEqual(vm.call('irqWait', frame, vm.screen_irq, 5), frame)
        self.assertEqual(vm.memory[frame + C['TF_R1']], 0)

    def test_screen_broker_has_no_drawing_or_physical_dma_operation(self):
        vm = kernel_fixture()
        # Reading/clearing status is the only available acknowledgement path;
        # an operation cannot encode COMMAND, ADDRESS or extra device flags.
        for operation in (0x40, 0x6C, 0x70, 0x100, 0xFFFFFFFF):
            self.assertEqual(vm.call('screenControl', 1, operation, 0), error(22))
        self.assertEqual(vm.call('screenControl', 2, 0, 10), error(1))
        self.assertEqual(vm.memory.commands, [])
        for offset in (0x40, 0x6C, 0x70):
            self.assertNotIn(C['VIDEO_BASE'] + offset, vm.memory)

    def test_dma_is_physical_pinned_bounded_and_copies_only_after_completion(self):
        vm = kernel_fixture()
        self.assertEqual(vm.call('deviceSubmit', 1, 0, 16, 1, 0), error(1))
        for offset, length in [(608, 16), (0xFFFFFFFF, 16), (0, 609), (0, 0xFFFFFFFF)]:
            self.assertEqual(vm.call('deviceSubmit', 2, offset, length, 1, 0), error(22))
        self.assertEqual(vm.memory.commands, [])
        self.assertGreater(vm.call('deviceSubmit', 2, 592, 16, 1, 0), 0)
        physical = vm.globals['deviceBounce']
        self.assertEqual(vm.memory.commands, [(1, physical, 3, 1)])
        self.assertEqual(physical & 4095, 0)
        self.assertEqual(vm.call('physicalPageReferences', physical), 1)
        self.assertFalse(vm.call('freePage', physical, 0xFFFFFFFE, 2))
        vm.memory.complete()
        self.assertEqual(vm.call('deviceFinish', 2, vm.operation_instance(), USER_DATA + 16), 16)
        target = vm.pages(2)[1] + 16
        self.assertEqual(bytes(vm.memory[target + i] for i in range(16)), bytes(range(80, 96)))
        self.assertNotEqual(physical, USER_DATA + 16)
        self.assertTrue(vm.call('physicalPageAvailable', physical))
        self.assertEqual(vm.memory.disk_state, C['DISK_PRESENT'])

    def test_dma_cancel_quarantines_until_late_completion_and_outlives_task(self):
        vm = kernel_fixture()
        self.assertGreater(vm.call('deviceSubmit', 2, 0, 16, 1, 0), 0)
        physical = vm.globals['deviceBounce']
        vm.call('deviceCancelOwner', 2)
        self.assertFalse(vm.call('physicalPageAvailable', physical))
        self.assertEqual(vm.call('physicalPageReferences', physical), 1)
        self.assertTrue(vm.call('taskDiscardCreated', 2))
        self.assertFalse(vm.call('physicalPageAvailable', physical))
        self.assertEqual(vm.call('deviceSubmit', 2, 0, 16, 1, 0), error(32))
        vm.memory.complete()
        vm.call('deviceReap')
        self.assertTrue(vm.call('physicalPageAvailable', physical))
        self.assertEqual(vm.globals['deviceBounce'], 0)
        self.assertEqual(len(vm.memory.commands), 1)

    def test_dma_error_media_change_and_invalid_destination_publish_no_data(self):
        for code, changed, destination, expected in [(4, False, USER_DATA, 5),
                (0, True, USER_DATA, 32), (0, False, 0, 14), (0, False, 0xFFFFFFFF, 14)]:
            with self.subTest(code=code, changed=changed, destination=destination):
                vm = kernel_fixture()
                target = vm.pages(2)[1]
                for i in range(16):
                    vm.memory[target + i] = 0xAA
                before = [vm.memory[target + i] for i in range(16)]
                self.assertGreater(vm.call('deviceSubmit', 2, 0, 16, 1, 0), 0)
                vm.memory.complete(code, changed)
                self.assertEqual(vm.call('deviceFinish', 2, vm.operation_instance(), destination), error(expected))
                self.assertEqual([vm.memory[target + i] for i in range(16)], before)
                self.assertEqual(vm.globals['deviceBounce'], 0)
                if changed:
                    self.assertEqual(vm.call('deviceValidate', 2), error(32))

    def test_embedded_program_constructor_permissions_zero_bss_and_invalid_ranges(self):
        vm = TaskM(ram=0x200000, root=LAIX / 'src/task/program.m')
        start = 0x20000
        header = (0x464C457F, 0x00010101, 0, 0, 0x08160002, 1,
                  C['SERVICE_IMAGE_BASE'], 52, 0, 0, 0x00200034, 3, 0)
        for i, word in enumerate(header):
            vm.memory[start + i * 4] = word
        for i, flags in enumerate((5, 4, 6)):
            words = (1, 256 + i * 16, C['SERVICE_IMAGE_BASE'] + i * 4096, 0, 16, 4096, flags, 4096)
            for j, word in enumerate(words):
                vm.memory[start + 52 + i * 32 + j * 4] = word
            for j in range(16):
                vm.memory[start + 256 + i * 16 + j] = 100 + j
        free = vm.free_pages()
        vm.memory[start + 52 + 24] = 7
        self.assertEqual(vm.call('taskCreateProgram', start, start + 304), 0)
        self.assertEqual(vm.free_pages(), free)
        vm.memory[start + 52 + 24] = 5
        self.assertEqual(vm.call('taskCreateProgram', start, start + 304), 1)
        root = vm.field('directory')
        for i, perms in enumerate((27, 19, 23)):
            leaf = vm.leaf(C['SERVICE_IMAGE_BASE'] + i * 4096, root)
            self.assertEqual(leaf & 31, perms)
            physical = leaf & ~4095
            self.assertEqual([vm.memory[physical + j] for j in range(16)], list(range(100, 116)))
            self.assertEqual(vm.memory[physical + 64], 0)
        self.assertEqual(vm.field('pages'), 0)
        self.assertTrue(vm.call('taskDiscardCreated', 1))
        self.assertEqual(vm.free_pages(), free)

    def test_full_screen_bootstrap_checks_startup_grants_and_rollback(self):
        def fixture(fail=None):
            vm = TaskM(ram=0x200000, root=LAIX / 'src/kernel/screen_main.m')
            vm.memory = Devices(vm)
            vm.addresses.update(fontData=0x16000, fontDataEnd=0x16000 + 184)
            for i, value in enumerate((0x3146414C, 1, 19, 32, 184, 16, 32, 0)):
                vm.memory[0x16000 + i * 4] = value
            for index, name in enumerate(('screenImage', 'storageImage', 'applicationImage')):
                start = 0x20000 + index * 4096
                vm.addresses[name], vm.addresses[name + 'End'] = start, start + 272
                values = (0x464C457F, 0x00010101, 0, 0, 0x08160002, 1,
                          C['SERVICE_IMAGE_BASE'], 52, 0, 0, 0x00200034, 1, 0)
                for i, value in enumerate(values):
                    vm.memory[start + i * 4] = value
                values = (1, 256, C['SERVICE_IMAGE_BASE'], 0, 16, 4096, 5, 4096)
                for i, value in enumerate(values):
                    vm.memory[start + 52 + i * 4] = value
                for i in range(16):
                    vm.memory[start + 256 + i] = 100 + i
            info = vm.addresses['kernelBootInfo']
            typ = vm.decls['kernelBootInfo'].sym.type
            vm.memory[info + typ.field('disk').offset] = C['DISK0_BASE']
            vm.memory[info + typ.field('imageSize').offset] = 1024
            vm.fail_mapping = fail
            return vm
        vm = fixture()
        self.assertTrue(vm.call('bootstrapScreenInit'))
        for id, role, devices, rights in ((1, 1, 2, 2), (2, 3, 4, 2), (3, 2, 0, 1)):
            start = vm.field('bootPage', id)
            self.assertEqual([vm.memory[start + 4 * i] for i in range(3)], [C['START_MAGIC'], 2, 64])
            self.assertEqual(vm.memory[start + 12], role)
            self.assertEqual(vm.memory[start + 24], rights)
            self.assertEqual(vm.memory[start + 28], devices)
            self.assertEqual(vm.field('deviceRights', id), devices)
            self.assertEqual(vm.field('state', id), 1)
        root = vm.field('directory')
        self.assertTrue(vm.call('mmuResourcesValid', root, 1, 1))
        self.assertFalse(vm.call('mmuResourcesValid', root, 1, 3))  # role without rows holds none
        self.assertFalse(vm.call('mmuResourcesValid', vm.field('directory', 2), 2, 1))
        for fail in range(1, 16):
            vm = fixture(fail)
            free = vm.free_pages()
            self.assertFalse(vm.call('bootstrapScreenInit'))
            self.assertEqual(vm.free_pages(), free)
            self.assertTrue(vm.call('taskInitAvailable'))
            self.assertEqual(vm.memory[C['PIC_ENABLE']], 0)
            self.assertEqual(vm.globals['deviceBounce'], 0)


class UserComponentTests(unittest.TestCase):
    def test_cpu_rendering_math_upload_fence_and_framebuffer_bounds(self):
        class VideoVM(SourceM):
            def __init__(self):
                super().__init__(LAIX / 'user/screen/video.m')
                self.memory.update({C['SCREEN_VRAM_VA'] + i: 0xEE for i in range(C['SCREEN_VRAM_BYTES'])})
            def trap(self, cause, args=()):
                raise AssertionError('rendering must not invoke a device syscall')
        vm = VideoVM()
        bitmap = 0x1000000
        for i in range(32):
            vm.memory[bitmap + i] = 0x81 if i % 2 == 0 else 0x42
        self.assertEqual(vm.call('videoUpload', 0, bitmap), 0)
        self.assertEqual(vm.call('videoGlyph', 0, 0, 0, 16), 0)
        for row in range(16):
            expected = [1, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 1, 0]
            self.assertEqual([vm.memory[C['SCREEN_VRAM_VA'] + row * 640 + i] for i in range(16)], expected)
            self.assertEqual(vm.memory[C['SCREEN_VRAM_VA'] + row * 640 + 16], 0xEE)
        before = dict(vm.memory)
        for args in ((625, 0, 0, 16), (0, 465, 0, 16), (0, 0, 256, 16), (0, 0, 0, 9), (0xFFFFFFFF, 0, 0, 8)):
            self.assertEqual(vm.call('videoGlyph', *args), error(22))
        self.assertEqual(dict(vm.memory), before)
        self.assertGreaterEqual(sum(name == 'fence' for name, _ in vm.events), 2)

    def test_user_module_closures_and_entries_have_no_kernel_hardware_dependencies(self):
        for name in ('server', 'storage', 'application'):
            root = LAIX / 'user/screen' / (name + '.m')
            modules = check_m(root)
            for module in modules:
                path = Path(module.path)
                self.assertTrue(path.is_relative_to(LAIX / 'user') or path == LAIX / 'src/task/service_start.m' or
                                path in (LAIX / 'src/arch/wrm081632/defs.m', LAIX / 'src/task/runtime_start.m', LAIX / 'src/task/recovery_start.m'), str(path))
            parser = parse_asm(root.with_suffix('.asm'))
            self.assertFalse(any(st.op in ('mtcr', 'iret', 'wfi') for st in parser.stmts))

    def test_unicode_bounds_full_scalars_and_no_partial_prefix(self):
        vm = SourceM(LAIX / 'user/screen/unicode.m')
        pointer = 0x1000000
        for text in (b'', b'ASCII\t\r\n', 'Я日本😀'.encode(), '\U0010ffff'.encode()):
            for i, byte in enumerate(text):
                vm.memory[pointer + i] = byte
            self.assertEqual(vm.call('validateText', pointer, len(text)), 0)
        for text in (b'prefix\0', b'prefix\x1b', b'\x7f', b'\xc0\x80', b'\xc2',
                     b'\xe0\x80\x80', b'\xed\xa0\x80', b'\xf4\x90\x80\x80',
                     b'\xf0\x9f\x98', b'\xf5\x80\x80\x80', b'\xc2\x80'):
            for i, byte in enumerate(text):
                vm.memory[pointer + i] = byte
            self.assertEqual(vm.call('validateText', pointer, len(text)), error(22))
        # Strict memory catches reading a continuation outside delivery.
        vm.memory = {pointer: 0xF0}
        self.assertEqual(vm.call('validateText', pointer, 1), error(22))

    def test_screen_validation_precedes_render_and_reports_complete_utf8_bytes(self):
        class ScreenVM(SourceM):
            def __init__(self):
                super().__init__(LAIX / 'user/screen/server.m')
                self.rendered = []
            def call(self, name, *args):
                if name == 'screenCode':
                    self.rendered.append(args[0])
                    return 0
                if name == 'videoFrame':
                    return 0
                return super().call(name, *args)
        pointer, response = 0x1000000, 0x1001000
        vm = ScreenVM()
        for text in (b'prefix\x1b', b'prefix\xf0', b'prefix\0'):
            message = bytes([2, 1, len(text), 0]) + text
            for i, byte in enumerate(message):
                vm.memory[pointer + i] = byte
            vm.call('screenHandle', pointer, len(message), response)
            self.assertEqual(vm.memory[response + 4], error(22))
            self.assertEqual(vm.rendered, [])
        text = 'Я日😀\n'.encode()
        message = bytes([2, 1, len(text), 0]) + text
        for i, byte in enumerate(message):
            vm.memory[pointer + i] = byte
        vm.call('screenHandle', pointer, len(message), response)
        self.assertEqual(vm.rendered, [ord(c) for c in 'Я日😀\n'])
        self.assertEqual([vm.memory[response + i * 4] for i in range(3)], [C['SCREEN_RESPONSE_HEADER'], 0, len(text)])
        vm.memory = {pointer: 2, pointer + 1: 1}
        vm.call('screenHandle', pointer, 2, response)
        self.assertEqual(vm.memory[response + 4], error(22))

    def test_cache_checks_both_halves_generation_errors_and_cache_hit_liveness(self):
        class CacheVM(SourceM):
            def __init__(self):
                super().__init__(LAIX / 'user/screen/cache.m')
                self.calls, self.uploads = [], []
                self.bad_second = False
                self.dead = False
            def call(self, name, *args):
                if name == 'call':
                    _, req, length, reply, cap = args
                    words = [self.memory[req + i * 4] for i in range(4)]
                    self.calls.append(words)
                    validate = words[0] == C['FONT_VALIDATE_HEADER']
                    status = error(32) if self.dead else 0
                    generation = 2 if self.bad_second and words[3] == 1 else 1
                    result = [C['FONT_RESPONSE_HEADER'], status, generation,
                              0 if validate or status else 16] + ([0] * 4 if validate or status else [words[3] * 4 + i for i in range(4)])
                    for i, word in enumerate(result):
                        self.memory[reply + i * 4] = word
                    return 32
                if name == 'videoUpload':
                    self.uploads.append((args[0], [self.memory[args[1] + i * 4] for i in range(8)]))
                    return 0
                return super().call(name, *args)
        vm = CacheVM()
        vm.call('cacheInit', 0x101)
        vm.bad_second = True
        self.assertEqual(vm.call('cacheGlyph', 7), error(71))
        self.assertEqual(vm.uploads, [])
        vm.bad_second = False
        self.assertEqual(vm.call('cacheGlyph', 7), 0)
        self.assertEqual(vm.uploads, [(0, list(range(8)))])
        self.assertEqual(vm.call('cacheGlyph', 7), 0)
        self.assertEqual(vm.calls[-1], [C['FONT_VALIDATE_HEADER'], 1, 0, 0])
        vm.dead = True
        self.assertEqual(vm.call('cacheGlyph', 7), error(32))
        self.assertEqual(len(vm.uploads), 1)

    def test_storage_rejects_unbounded_and_bad_generation_before_any_dma(self):
        class StorageVM(SourceM):
            def __init__(self):
                super().__init__(LAIX / 'user/screen/storage.m')
                self.operations = []
            def trap(self, cause, args=()):
                self.operations.append(tuple(args))
                return 0
        vm = StorageVM()
        req, res = 0x1000000, 0x1001000
        for words, size, errno in [([0, 1, 0, 0], 16, 22), ([C['FONT_REQUEST_HEADER'], 2, 0, 0], 16, 32),
                                  ([C['FONT_VALIDATE_HEADER'], 1, 1, 0], 16, 22), ([C['FONT_REQUEST_HEADER'], 1, 0, 0], 12, 22)]:
            for i, word in enumerate(words):
                vm.memory[req + 4 * i] = word
            vm.call('storageHandle', req, size, res, 0x104)
            self.assertEqual(vm.operations, [])
            self.assertEqual(vm.memory[res + 4], error(errno))
            self.assertEqual([vm.memory[res + 4 * i] for i in range(3, 8)], [0] * 5)


if __name__ == '__main__':
    unittest.main()
