"""G2: device resources, IRQ lines and display registers come from table rows.

The rows are data. These checks change rows in memory, never kernel code, and
confirm the mechanism follows them while its hardware-class invariants hold.
"""
import unittest

from source_m import LAYOUT as C
from test_ipc_handles import error
from test_kernel import LAIX, check_m
from test_screen_services import kernel_fixture
from test_task import USER_DATA

ROW_WORDS = 6
KIND_VRAM, KIND_MMIO, KIND_BLOB, KIND_IRQ, KIND_DISK = 1, 2, 3, 4, 5
ROLE_SCREEN, ROLE_INPUT, ROLE_STORAGE = 1, 2, 3
PAGE = 4096
RW_U, RO_U = 23, 19


def row(vm, index, column, value=None):
    address = vm.addresses['deviceRows'] + 4 * (index * ROW_WORDS + column)
    if value is not None:
        vm.memory[address] = value
    return vm.memory[address]


def rule(vm, index, column, value=None):
    address = vm.addresses['registerRules'] + 4 * (index * 4 + column)
    if value is not None:
        vm.memory[address] = value
    return vm.memory[address]


def grants(vm, role):
    results = []
    for index in range(vm.call('deviceRoleMappingCount', role)):
        item = vm.call('deviceRoleMapping', role, index)
        results.append(tuple(vm.call(name, item) for name in
                             ('deviceRowVirtual', 'deviceRowPhysical', 'deviceRowBytes', 'deviceRowPermissions')))
    return results


class DeviceTableTests(unittest.TestCase):
    def test_default_rows_describe_screen_input_and_storage(self):
        vm = kernel_fixture()
        self.assertEqual(grants(vm, ROLE_SCREEN), [
            (C['SCREEN_VRAM_VA'], C['VRAM_BASE'], C['SCREEN_VRAM_BYTES'], RW_U),
            (C['SCREEN_VIDEO_VA'], C['VIDEO_BASE'], PAGE, RO_U),
            (C['SCREEN_FONT_VA'], vm.addresses['fontData'], PAGE, RO_U)])
        self.assertEqual(vm.call('deviceRoleMappingCount', ROLE_SCREEN), 3)
        self.assertEqual(grants(vm, ROLE_INPUT), [])
        self.assertEqual(vm.call('deviceRoleIrq', ROLE_SCREEN), C['VIDEO_IRQ'])
        self.assertEqual(vm.call('deviceRoleIrq', ROLE_INPUT), C['KEYBOARD_IRQ'])
        self.assertEqual(vm.call('deviceRoleIrq', ROLE_STORAGE), 32)
        for base, line in ((C['DISK0_BASE'], 3), (C['DISK1_BASE'], 4), (C['FLOPPY_BASE'], 6)):
            self.assertEqual(vm.call('deviceDiskIrq', base), line)
        self.assertEqual(vm.call('deviceDiskIrq', C['VIDEO_BASE']), 32)
        self.assertEqual(vm.call('deviceRoleBlobBytes', ROLE_SCREEN), vm.addresses['fontDataEnd'] - vm.addresses['fontData'])
        for index in range(9):
            self.assertTrue(vm.call('deviceRowValid', index))
        self.assertFalse(vm.call('deviceRowValid', 9))
        ROLE_NET = 4
        self.assertEqual(vm.call('deviceRoleIrq', ROLE_NET), C['ETH_IRQ'])
        self.assertEqual(grants(vm, ROLE_NET), [])  # the card's page is never mapped to anyone

    def test_grant_must_equal_a_row_in_placement_size_and_permissions(self):
        vm = kernel_fixture()
        root = vm.field('directory')
        for args in ((C['SCREEN_VRAM_VA'] + PAGE, C['VRAM_BASE'], C['SCREEN_VRAM_BYTES'], RW_U),
                     (C['SCREEN_VRAM_VA'], C['VRAM_BASE'] + PAGE, C['SCREEN_VRAM_BYTES'], RW_U),
                     (C['SCREEN_VRAM_VA'], C['VRAM_BASE'], C['SCREEN_VRAM_BYTES'] + PAGE, RW_U),
                     (C['SCREEN_VRAM_VA'], C['VRAM_BASE'], C['SCREEN_VRAM_BYTES'], RW_U | 8),
                     (C['SCREEN_VIDEO_VA'], C['VIDEO_BASE'], PAGE, RW_U),
                     (C['SCREEN_VIDEO_VA'], C['TIMER_BASE'], PAGE, RO_U),
                     (0x80C00000, C['VIDEO_BASE'], PAGE, RO_U)):
            self.assertFalse(vm.call('deviceGrantAllowed', *args), args)
            self.assertFalse(vm.call('mmuGrantResource', root, 1, *args), args)

    def test_a_changed_row_moves_the_resource_without_code_changes(self):
        vm = kernel_fixture()
        root = vm.field('directory')
        moved = 0x80C00000
        row(vm, 1, 2, moved)  # screen MMIO appears at a different user address
        self.assertFalse(vm.call('mmuGrantResource', root, 1, C['SCREEN_VIDEO_VA'], C['VIDEO_BASE'], PAGE, RO_U))
        self.assertTrue(vm.call('mmuGrantResource', root, 1, moved, C['VIDEO_BASE'], PAGE, RO_U))
        self.assertEqual(vm.leaf(moved, root), C['VIDEO_BASE'] | RO_U)

    def test_rows_that_break_kernel_invariants_are_ignored(self):
        vm = kernel_fixture()
        io = C['IO_BASE']
        page = lambda number: io + number * PAGE
        # Screen MMIO row (index 1), column 3 = physical page.
        for value in (C['TIMER_BASE'], C['UART_BASE'], io, C['POWER_BASE'], C['RNG_BASE'],
                      C['DISK0_BASE'], page(1),  # page 1 is the keyboard: destructive reads
                      C['VIDEO_BASE'] + 16, io + 0x100000):
            vm = kernel_fixture()
            row(vm, 1, 3, value)
            self.assertFalse(vm.call('deviceRowValid', 1), value)
            self.assertEqual(vm.call('deviceRoleMappingCount', ROLE_SCREEN), 2, value)
            self.assertFalse(vm.call('deviceGrantAllowed', C['SCREEN_VIDEO_VA'], value, PAGE, RO_U), value)
            self.assertFalse(vm.call('mmuGrantResource', vm.field('directory'), 1,
                                     C['SCREEN_VIDEO_VA'], value, PAGE, RO_U), value)
        vm = kernel_fixture()
        row(vm, 0, 3, io)  # VRAM row aimed at the register window
        self.assertFalse(vm.call('deviceRowValid', 0))
        vm = kernel_fixture()
        row(vm, 0, 4, C['IO_BASE'] - C['VRAM_BASE'] + PAGE)  # VRAM window runs into the registers
        self.assertFalse(vm.call('deviceRowValid', 0))
        for column, value in ((4, 0), (2, 0x80000010)):
            vm = kernel_fixture()
            row(vm, 0, column, value)
            self.assertFalse(vm.call('deviceRowValid', 0))
        vm = kernel_fixture()
        row(vm, 2, 3, 1)  # no such blob
        self.assertFalse(vm.call('deviceRowValid', 2))
        self.assertEqual(vm.call('deviceRoleBlobBytes', ROLE_SCREEN), 0)
        vm = kernel_fixture()
        row(vm, 0, 0, 99)  # unknown kind
        self.assertFalse(vm.call('deviceRowValid', 0))

    def test_irq_lines_follow_rows_and_never_include_the_timer(self):
        vm = kernel_fixture()
        for line in (1, 7, 31, 32, 0xFFFFFFFF):
            self.assertFalse(vm.call('deviceIrqAllowed', line), line)
            self.assertEqual(vm.call('irqIssue', 1, line), 0)
        row(vm, 3, 5, 7)  # screen IRQ line becomes 7
        self.assertTrue(vm.call('deviceIrqAllowed', 7))
        self.assertEqual(vm.call('deviceRoleIrq', ROLE_SCREEN), 7)
        self.assertFalse(vm.call('deviceIrqAllowed', C['VIDEO_IRQ']))
        for tampered in (C['TIMER_IRQ'], 32, 0xFFFFFFFF):
            vm = kernel_fixture()
            row(vm, 3, 5, tampered)
            self.assertFalse(vm.call('deviceRowValid', 3))
            self.assertEqual(vm.call('deviceRoleIrq', ROLE_SCREEN), 32)
            self.assertFalse(vm.call('deviceIrqAllowed', tampered))
            self.assertEqual(vm.call('irqIssue', 3, tampered), 0)
        vm = kernel_fixture()
        row(vm, 5, 3, C['TIMER_BASE'])  # a disk row cannot name a kernel page
        self.assertFalse(vm.call('deviceRowValid', 5))
        self.assertEqual(vm.call('deviceDiskIrq', C['TIMER_BASE']), 32)
        row(vm, 6, 3, C['VIDEO_BASE'])  # nor a read-safe register page
        self.assertFalse(vm.call('deviceRowValid', 6))

    def test_register_policy_and_vram_bound_come_from_rows(self):
        vm = kernel_fixture()
        for offset in (0, 8, 32):
            vm.memory[C['VIDEO_BASE'] + offset] = 0
        self.assertEqual(vm.call('screenControl', 1, 8, 0x20), 0)
        self.assertEqual(vm.call('screenControl', 1, 40, 200), 0)
        rule(vm, 4, 1, 0x7F)  # palette index narrowed by data
        self.assertEqual(vm.call('screenControl', 1, 40, 200), error(22))
        self.assertEqual(vm.call('screenControl', 1, 40, 100), 0)
        vm = kernel_fixture()
        for offset in (0, 8, 32):
            vm.memory[C['VIDEO_BASE'] + offset] = 0
        row(vm, 0, 4, 2 * PAGE)  # the granted VRAM no longer holds a 640x480 frame
        self.assertEqual(vm.call('screenControl', 1, 8, 0x01), error(22))
        self.assertEqual(vm.call('screenControl', 1, 8, 0x20), error(22))
        row(vm, 0, 4, C['SCREEN_VRAM_BYTES'])
        self.assertEqual(vm.call('screenControl', 1, 8, 0x01), 0)
        vm = kernel_fixture()
        self.assertEqual(vm.call('deviceRegisterRule', 0x6C), 7)  # DMA address: no rule
        self.assertEqual(vm.call('screenControl', 1, 0x6C, USER_DATA), error(22))

    def test_kernel_modules_hold_no_screen_resource_constants(self):
        for name in ('src/mm/mmu.m', 'src/drivers/irq.m', 'src/drivers/service_devices.m',
                     'src/kernel/service_bootstrap.m'):
            source = (LAIX / name).read_text()
            for constant in ('SCREEN_VRAM_VA', 'SCREEN_VIDEO_VA', 'SCREEN_VRAM_BYTES',
                             'VIDEO_IRQ', 'KEYBOARD_IRQ', 'FLOPPY_BASE', 'DISK1_BASE'):
                self.assertNotIn(constant, source, (name, constant))
        check_m(LAIX / 'src/kernel/service_bootstrap.m')
        check_m(LAIX / 'src/kernel/simple_bootstrap.m')


if __name__ == '__main__':
    unittest.main()
