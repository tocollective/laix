"""Execute checked lifecycle and simple service source with device fixtures."""
import struct
import unittest
from pathlib import Path

from test_kernel import LAIX, check_m, parse_asm
from source_m import SourceM, LAYOUT as C
from test_task import TaskM, TaskEntered, USER_DATA
from test_screen_services import Devices, kernel_fixture
from test_ipc_handles import error


class KeyboardDevices(Devices):
    def __init__(self, vm):
        super().__init__(vm)
        self.keys = []
        self.overflow = False
        self.status_reads = 0

    def inject(self, keys, overflow=False):
        self.keys.extend(keys)
        self.overflow |= overflow
        if self.keys:
            self.lines |= 1

    def __getitem__(self, address):
        if address == C['KEYBOARD_BASE']:
            self.status_reads += 1
            result = int(bool(self.keys)) | (2 if self.overflow else 0)
            self.overflow = False
            return result
        if address == C['KEYBOARD_BASE'] + 4:
            result = self.keys.pop(0) if self.keys else 0
            if not self.keys:
                self.lines &= ~1
            return result
        return super().__getitem__(address)

    def __setitem__(self, address, value):
        if address == C['KEYBOARD_BASE'] + 8:
            if value & 1:
                self.keys.clear()
                self.lines &= ~1
            return
        super().__setitem__(address, value)


def simple_fixture(fail=None):
    vm = TaskM(ram=0x200000, root=LAIX / 'src/kernel/simple_main.m')
    vm.memory = KeyboardDevices(vm)
    vm.addresses.update(fontData=0x16000, fontDataEnd=0x16000 + 184)
    for i, word in enumerate((0x3146414C, 1, 19, 32, 184, 16, 32, 0)):
        vm.memory[0x16000 + 4 * i] = word
    for index, name in enumerate(('inputImage', 'diskImage', 'filesImage', 'simpleApplicationImage')):
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


class SimpleKernelTests(unittest.TestCase):
    def test_bootstrap_roles_rights_private_ro_start_and_no_mmio_grants(self):
        vm = simple_fixture()
        self.assertTrue(vm.call('bootstrapSimpleInit'))
        for id, role, protocol, devices, rights in ((1, 4, 5, 8, 2), (2, 5, 6, 16, 2),
                                                   (3, 6, 7, 0, 2), (4, 2, 7, 0, 1)):
            block = vm.field('bootPage', id)
            words = [vm.memory[block + i * 4] for i in range(16)]
            self.assertEqual(words[:5], [C['START_MAGIC'], 2, 64, role, id])
            self.assertEqual((words[6], words[7], words[11]), (rights, devices, protocol))
            self.assertEqual(vm.field('deviceRights', id), devices)
            self.assertEqual(vm.field('state', id), 1)
            root = vm.field('directory', id)
            self.assertEqual(vm.leaf(C['START_BLOCK_VA'], root) & 31, 19)
            for device in range(C['IO_BASE'], C['IO_BASE'] + 17 * 4096, 4096):
                self.assertEqual(vm.leaf(device, root) & 24, 0)
            self.assertEqual(words[13:15], [0, 0])
        self.assertEqual(vm.call('diskInfo', 2), 608)
        self.assertEqual(vm.call('diskInfo', 3), error(1))
        self.assertEqual(vm.call('inputRead', 4, USER_DATA), error(1))

    def test_bootstrap_rollback_restores_all_resources_and_can_retry(self):
        for fail in range(1, 21):
            vm = simple_fixture(fail)
            free = vm.free_pages()
            self.assertFalse(vm.call('bootstrapSimpleInit'), fail)
            self.assertEqual(vm.free_pages(), free)
            self.assertTrue(vm.call('taskInitAvailable'))
            self.assertEqual(vm.globals['inputOwner'], 0)
            self.assertEqual(vm.memory[C['PIC_ENABLE']], 0)
            vm.fail_mapping = None
            self.assertTrue(vm.call('bootstrapSimpleInit'))

    def input_vm(self):
        vm = kernel_fixture()
        vm.memory = KeyboardDevices(vm)
        token = vm.call('irqGrant', 3, C['KEYBOARD_IRQ'])
        self.assertTrue(vm.call('inputDevicesInit', 3, token))
        return vm, token

    def snapshot(self, vm):
        self.assertEqual(vm.call('inputRead', 3, USER_DATA), 24)
        address = vm.pages(3)[1]
        return struct.unpack('<6I', bytes(vm.memory[address + i] for i in range(24)))

    def test_input_fifo_release_bit_overflow_and_irq_coalescing(self):
        vm, token = self.input_vm()
        vm.memory.inject([4, 0x80000004, 5, 6, 7, 8], overflow=True)
        self.assertTrue(vm.call('irqNotify', 0))
        self.assertTrue(vm.call('irqNotify', 0))
        self.assertEqual(self.snapshot(vm), (4, 1, 4, 0x80000004, 5, 6))
        self.assertEqual(vm.memory[C['PIC_ENABLE']] & 1, 1)
        self.assertEqual(self.snapshot(vm), (2, 0, 7, 8, 0, 0))
        self.assertEqual(self.snapshot(vm), (0, 0, 0, 0, 0, 0))
        self.assertEqual(vm.call('irqComplete', 1, token), error(1))
        vm.call('irqReleaseTask', 3)
        vm.call('inputReleaseOwner', 3)
        self.assertEqual(vm.call('inputRead', 3, USER_DATA), error(1))
        self.assertEqual(vm.memory[C['PIC_ENABLE']] & 1, 0)

    def test_input_invalid_destination_does_not_pop_or_clear_overflow(self):
        vm, _ = self.input_vm()
        vm.memory.inject([4], overflow=True)
        reads = vm.memory.status_reads
        for pointer in (0, 0xFFFFFFFF, C['KEYBOARD_BASE'], USER_DATA + 4090):
            self.assertEqual(vm.call('inputRead', 3, pointer), error(14))
        self.assertEqual(vm.memory.status_reads, reads)
        self.assertEqual(vm.memory.keys, [4])
        self.assertTrue(vm.memory.overflow)

    def test_input_bounded_drain_keeps_remaining_level_masked_and_overflow_sticky(self):
        vm, _ = self.input_vm()
        # Continuous arrival can leave a level after the bounded hardware drain.
        vm.memory.inject(list(range(1, 41)))
        self.assertEqual(self.snapshot(vm), (4, 0, 1, 2, 3, 4))
        self.assertEqual(vm.memory[C['PIC_ENABLE']] & 1, 0)
        self.assertEqual(self.snapshot(vm), (4, 1, 5, 6, 7, 8))
        self.assertEqual(vm.memory[C['PIC_ENABLE']] & 1, 1)
        remaining = []
        while vm.globals['inputCount']:
            values = self.snapshot(vm)
            remaining.extend(values[2:2 + values[0]])
        self.assertEqual(remaining, list(range(9, 37)))

    def test_disk_extent_cross_sector_overflow_and_exact_copy_length(self):
        vm = kernel_fixture()
        for offset, count in ((608, 1), (0xFFFFFFFF, 1), (607, 2), (511, 2), (0, 0), (0, 17), (0, 0xFFFFFFFF)):
            self.assertEqual(vm.call('diskBegin', 2, offset, count), error(22))
        self.assertEqual(vm.memory.commands, [])
        self.assertEqual(vm.call('diskBegin', 2, 511, 1), 0)
        physical = vm.globals['fontBounce']
        self.assertEqual(vm.call('physicalPageReferences', physical), 1)
        vm.memory.complete()
        target = vm.pages(2)[1]
        vm.memory[target + 1] = 0xAB
        self.assertEqual(vm.call('fontFinish', 2, USER_DATA), 1)
        self.assertEqual((vm.memory[target], vm.memory[target + 1]), (255, 0xAB))
        self.assertTrue(vm.call('physicalPageAvailable', physical))

    def test_exit_fault_cancel_clients_then_defer_all_dma_owner_resources(self):
        for faulted in (False, True):
            vm = kernel_fixture()
            rx = vm.call('endpointBootstrapService', vm.field_address('handles', 2), 2)
            tx = {id: vm.call('handleCopy', vm.field_address('handles', 2), rx,
                             vm.field_address('handles', id), 2, id, 1) for id in (1, 3)}
            for id in (1, 2, 3):
                vm.call('taskEnqueue', vm.call('taskGet', id))
            with self.assertRaises(TaskEntered):
                vm.call('taskStart', 1000000)
            vm.call('ipcCall', vm.field_address('context', 1), tx[1], USER_DATA, 0, USER_DATA, 32)
            vm.call('ipcAccept', vm.field_address('context', 2), rx, USER_DATA, 32)
            vm.call('taskYield', vm.field_address('context', 2))
            vm.call('ipcCall', vm.field_address('context', 3), tx[3], USER_DATA, 0, USER_DATA, 32)
            self.assertEqual(vm.call('fontBegin', 2, 0, 0), 0)
            physical, directory = vm.globals['fontBounce'], vm.field('directory', 2)
            pages = vm.pages(2)
            vm.call('taskFinish', vm.field_address('context', 2), 9, faulted)
            for id in (1, 3):
                self.assertIn(vm.field('state', id), (1, 2))
                self.assertEqual(vm.memory[vm.field_address('context', id) + C['TF_R1']], error(32))
                self.assertEqual(vm.field('ipcEndpoint', id), 0)
            self.assertEqual(vm.call('irqComplete', 2, vm.disk_irq), error(1))
            self.assertEqual(vm.call('fontBegin', 2, 0, 0), error(32))
            current = (vm.globals['currentTask'] - vm.addresses['tasks']) // vm.task_type.size + 1
            vm.cpu_sp = vm.field('kernelStackTop', current) - 64
            vm.call('taskReap')
            self.assertFalse(vm.field('reaped', 2))
            self.assertEqual(vm.field('directory', 2), directory)
            self.assertTrue(all(not vm.call('physicalPageAvailable', p) for p in [physical, directory] + pages))
            vm.memory.complete()
            vm.call('taskReap')
            self.assertTrue(vm.field('reaped', 2))
            self.assertTrue(all(vm.call('physicalPageAvailable', p) for p in [physical, directory] + pages))
            self.assertEqual(len(vm.memory.commands), 1)
            self.assertEqual(vm.call('handleLookup', vm.field_address('handles', 1), tx[1], 1), 0)
            self.assertEqual(vm.call('endpointBootstrapService', vm.field_address('handles', 1), 1), error(1))


class SimpleProtocolTests(unittest.TestCase):
    def put(self, vm, words, pointer=0x1000000):
        for i, word in enumerate(words):
            vm.memory[pointer + i * 4] = word
        return pointer

    def test_user_closures_and_entries_do_not_import_kernel_drivers(self):
        for name in ('input', 'disk', 'files', 'application'):
            root = LAIX / 'user/services' / (name + '.m')
            for module in check_m(root):
                path = Path(module.path)
                self.assertTrue(path.is_relative_to(LAIX / 'user') or
                                path in (LAIX / 'src/task/service_start.m', LAIX / 'src/arch/wrm081632/defs.m'))
            self.assertFalse(any(st.op in ('mtcr', 'iret', 'wfi') for st in parse_asm(root.with_suffix('.asm')).stmts))

    def test_input_malformed_size_or_header_never_reads_device(self):
        class InputVM(SourceM):
            def trap(self, cause, args=()):
                raise AssertionError('malformed request reached device')
        vm = InputVM(LAIX / 'user/services/input.m')
        req, res = 0x1000000, 0x1001000
        for header, size in ((0, 4), (C['INPUT_REQUEST_HEADER'], 0), (C['INPUT_REQUEST_HEADER'], 32)):
            vm.memory[req] = header
            vm.call('inputHandle', req, size, res)
            self.assertEqual(vm.memory[res + 4], error(22))
            self.assertEqual([vm.memory[res + i * 4] for i in range(2, 8)], [0] * 6)

    def test_disk_stale_or_malformed_request_is_not_a_terminal_device_failure(self):
        class DiskVM(SourceM):
            def trap(self, cause, args=()):
                raise AssertionError('malformed request reached broker')
        vm = DiskVM(LAIX / 'user/services/disk.m')
        for words, size, errno in (([0, 1, 0, 0], 16, 22),
                ([C['DISK_REQUEST_HEADER'], 2, 0, 16], 16, 32),
                ([C['DISK_STAT_HEADER'], 1, 0, 1], 16, 22),
                ([C['DISK_REQUEST_HEADER'], 1, 0, 16], 12, 22)):
            req = self.put(vm, words)
            vm.call('diskHandle', req, size, 0x1001000, 0x104)
            self.assertEqual(vm.memory[0x1001004], error(errno))
            self.assertFalse(vm.globals['diskFailed'])

    def file_vm(self, failure=None):
        class FileVM(SourceM):
            def __init__(self):
                super().__init__(LAIX / 'user/services/files.m')
                self.requests = []
            def call(self, name, *args):
                if name == 'call':
                    handle, req, size, res, cap = args
                    words = [self.memory[req + 4 * i] for i in range(4)]
                    self.requests.append(words)
                    stat = words[0] == C['DISK_STAT_HEADER']
                    values = [C['DISK_RESPONSE_HEADER'], 0, 1, 608 if stat else words[3]] + [0] * 4
                    if failure == 'dead':
                        return error(32)
                    if failure == 'generation':
                        values[2] = 2
                    if failure == 'size':
                        return 12
                    if failure == 'count' and not stat:
                        values[3] += 1
                    if failure == 'status':
                        values[1] = 1
                    if failure == 'extent' and stat:
                        values[3] = 0xFFFFFFFF
                    for i, word in enumerate(values):
                        self.memory[res + 4 * i] = word
                    if not stat:
                        for i in range(16):
                            self.memory[res + 16 + i] = (words[2] + i) & 255
                    return 32
                return super().call(name, *args)
        return FileVM()

    def test_file_stat_eof_short_reads_and_sector_boundary(self):
        for offset, count, expected in ((0, 16, 16), (510, 16, 2), (600, 16, 8), (608, 16, 0)):
            vm = self.file_vm()
            req = self.put(vm, [C['FILE_REQUEST_HEADER'], 1, 1, offset, count])
            vm.call('fileHandle', req, 20, 0x1001000, 0x102)
            self.assertEqual(vm.memory[0x1001004], 0)
            self.assertEqual(vm.memory[0x100100C], expected)
            self.assertEqual(len(vm.requests), 2 if expected else 1)
            for i in range(expected):
                self.assertEqual(vm.memory[0x1001010 + i], (offset + i) & 255)
        vm = self.file_vm()
        req = self.put(vm, [C['FILE_STAT_HEADER'], 1, 1, 0])
        vm.call('fileHandle', req, 16, 0x1001000, 0x102)
        self.assertEqual(vm.memory[0x100100C], 608)
        self.assertEqual(vm.requests, [[C['DISK_STAT_HEADER'], 1, 0, 0]])

    def test_file_invalid_and_stale_messages_do_not_forward_or_kill_service(self):
        for words, size, errno in (([C['FILE_REQUEST_HEADER'], 1, 1, 0, 17], 20, 22),
                ([C['FILE_REQUEST_HEADER'], 1, 1, 0, 0], 20, 22),
                ([C['FILE_STAT_HEADER'], 1, 1, 0], 20, 22),
                ([C['FILE_STAT_HEADER'], 1, 2, 0], 16, 2),
                ([C['FILE_STAT_HEADER'], 2, 1, 0], 16, 32),
                ([C['FILE_STAT_HEADER'], 1, 1, 1], 16, 22)):
            vm = self.file_vm()
            req = self.put(vm, words)
            vm.call('fileHandle', req, size, 0x1001000, 0x102)
            self.assertEqual(vm.memory[0x1001004], error(errno))
            self.assertEqual(vm.requests, [])
            self.assertFalse(vm.globals['filesFailed'])

    def test_disk_failure_or_bad_reply_is_terminal_and_publishes_no_file_data(self):
        for failure, errno in (('dead', 32), ('generation', 71), ('size', 71), ('count', 71), ('status', 71), ('extent', 71)):
            vm = self.file_vm(failure)
            req = self.put(vm, [C['FILE_REQUEST_HEADER'], 1, 1, 0, 16])
            vm.call('fileHandle', req, 20, 0x1001000, 0x102)
            self.assertEqual(vm.memory[0x1001004], error(errno))
            self.assertTrue(vm.globals['filesFailed'])
            self.assertEqual([vm.memory[0x1001000 + i * 4] for i in range(3, 8)], [0] * 5)

    def client_vm(self, response, returned=32):
        class ClientVM(SourceM):
            def __init__(self):
                super().__init__(LAIX / 'user/services/client.m')
            def call(self, name, *args):
                if name == 'call':
                    for i, value in enumerate(response):
                        self.memory[args[3] + 4 * i] = value
                    return returned
                return super().call(name, *args)
        return ClientVM()

    def test_input_client_rejects_bad_response_before_publishing_events(self):
        good = [C['INPUT_RESPONSE_HEADER'], 0, 1, 1, 0x80000004, 0, 0, 0]
        for index, value in ((0, 0), (1, 1), (2, 5), (3, 2), (4, 0x00010000), (5, 4)):
            values = good.copy()
            values[index] = value
            vm = self.client_vm(values)
            for offset in range(0, 20, 4):
                vm.memory[0x1000000 + offset] = 0xAA
            self.assertEqual(vm.call('inputEvents', 0x101, 0x1000000, 0x1000010), error(71))
            self.assertEqual([vm.memory[0x1000000 + i * 4] for i in range(5)], [0xAA] * 5)
        vm = self.client_vm(good)
        self.assertEqual(vm.call('inputEvents', 0x101, 0x1000000, 0x1000010), 1)
        self.assertEqual(vm.memory[0x1000000], 0x80000004)
        self.assertEqual(vm.memory[0x1000010], 1)

    def test_file_client_checks_generation_status_size_and_count_before_copy(self):
        good = [C['FILE_RESPONSE_HEADER'], 0, 1, 16, 0, 0, 0, 0]
        for index, value in ((0, 0), (1, 1), (2, 2), (3, 17)):
            values = good.copy()
            values[index] = value
            vm = self.client_vm(values)
            vm.memory[0x1000000] = 0xAA
            self.assertEqual(vm.call('fileRead', 0x101, 0, 0x1000000, 16), error(71))
            self.assertEqual(vm.memory[0x1000000], 0xAA)
        for returned in (12, error(32)):
            vm = self.client_vm(good, returned)
            self.assertEqual(vm.call('fileRead', 0x101, 0, 0x1000000, 16), error(71 if returned == 12 else 32))
        vm = self.client_vm([C['FILE_RESPONSE_HEADER'], 0, 1, 608, 0, 0, 0, 0])
        self.assertEqual(vm.call('fileSize', 0x101), 608)


if __name__ == '__main__':
    unittest.main()
