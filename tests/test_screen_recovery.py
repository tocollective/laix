"""G3: runtime Screen/font-extent regrant, replacement and supervised policy.

Executes the checked kernel and user sources. The CPU evidence is
probe_screen_recovery_cpu.py; these cases cover the preflights and failure
branches that the CPU run cannot inject.
"""
import unittest

from source_m import LAYOUT as C
from test_ipc_handles import error
from test_kernel import LAIX, check_m
from test_runtime_tasks import create
from test_service_recovery import fixture, replacement, retire, client, resolve
from test_task import USER_DATA

SCREEN, FONT = C['DEVICE_SCREEN'], C['DEVICE_FONT']
VIDEO_STATUS = C['VIDEO_BASE']
VIDEO_CONTROL = C['VIDEO_BASE'] + 4


def display_fixture():
    vm = fixture(True)
    address = vm.field_address('deviceFactory')
    vm.memory[address] = vm.memory[address] | SCREEN | FONT
    for offset in range(0, 0x100, 4):
        dict.__setitem__(vm.memory, C['VIDEO_BASE'] + offset, 0)
    return vm


def grants_in_use(vm):
    typ = vm.decls['resourceGrants'].sym.type.elem
    base = vm.addresses['resourceGrants']
    return sum(bool(vm.memory[base + i * typ.size + typ.field('directory').offset]) for i in range(8))


def irq_owners(vm):
    typ = vm.decls['irqGrants'].sym.type.elem
    base = vm.addresses['irqGrants']
    return sum(bool(vm.memory[base + i * typ.size + typ.field('owner').offset]) for i in range(32))


class DisplayHandoverTests(unittest.TestCase):
    def test_grant_installs_exactly_the_role_rows_and_makes_the_child_the_display_owner(self):
        vm = display_fixture()
        child = create(vm, configure=False, publish=False)
        token = vm.call('taskRuntimeDevices', child, SCREEN)
        self.assertTrue(0 < token < 0x80000000)
        self.assertEqual(vm.field('deviceRights', child), SCREEN)
        self.assertTrue(vm.call('irqTokenValid', child, token, C['VIDEO_IRQ']))
        root = vm.field('directory', child)
        for virtual, physical, size, perms in [(C['SCREEN_VRAM_VA'], C['VRAM_BASE'], C['SCREEN_VRAM_BYTES'], 23),
                                               (C['SCREEN_VIDEO_VA'], C['VIDEO_BASE'], 4096, 19),
                                               (C['SCREEN_FONT_VA'], vm.addresses['fontData'], 4096, 19)]:
            for offset in range(0, size, 4096):
                self.assertEqual(vm.leaf(virtual + offset, root), physical + offset | perms)
        # Only the owner programs the display; nobody else gains the register broker.
        self.assertEqual(vm.call('screenControl', child, 4, 0), 0)
        self.assertEqual(vm.call('screenControl', 1, 4, 0), error(1))
        self.assertEqual(vm.call('screenControl', child, 0x40, 0), error(22))

    def test_second_display_is_refused_while_the_owner_lives_and_until_it_is_reclaimed(self):
        vm = display_fixture()
        first, _, _, old = replacement(vm, 1, devices=SCREEN)
        second = create(vm, configure=False, publish=False)
        before = irq_owners(vm)
        self.assertEqual(vm.call('taskRuntimeDevices', second, SCREEN), error(16))
        self.assertEqual((vm.field('deviceRights', second), irq_owners(vm)), (0, before))
        vm.run(1)
        vm.invoke(C['SYS_TASK_TERMINATE'], first, 0)
        # Dead, not yet reclaimed: its directory still holds the ledger ranges.
        for _ in range(3):
            self.assertEqual(vm.call('taskRuntimeDevices', second, SCREEN), error(16))
            self.assertEqual(grants_in_use(vm), 3)
        self.assertEqual((vm.field('deviceRights', second), irq_owners(vm)), (0, 0))
        vm.reap()
        self.assertEqual(grants_in_use(vm), 0)
        fresh = vm.call('taskRuntimeDevices', second, SCREEN)
        self.assertTrue(0 < fresh < 0x80000000)
        self.assertNotEqual(fresh, old)
        self.assertFalse(vm.call('irqTokenValid', second, old, C['VIDEO_IRQ']))
        # The dead owner's register authority did not carry over.
        self.assertEqual(vm.call('screenControl', first, 4, 0), error(1))
        self.assertEqual(vm.call('screenControl', second, 4, 0), 0)

    def test_death_stops_scanout_and_a_busy_engine_refuses_handover(self):
        vm = display_fixture()
        first = create(vm, configure=False, publish=False)
        vm.call('taskRuntimeDevices', first, SCREEN)
        vm.memory[VIDEO_CONTROL] = 5
        vm.run(1)
        vm.invoke(C['SYS_TASK_TERMINATE'], first, 0)
        vm.reap()
        self.assertEqual(vm.memory[VIDEO_CONTROL], 0)
        second = create(vm, configure=False, publish=False)
        vm.memory[VIDEO_STATUS] = C['VIDEO_BUSY']
        self.assertEqual(vm.call('taskRuntimeDevices', second, SCREEN), error(16))
        self.assertEqual((vm.field('deviceRights', second), irq_owners(vm), grants_in_use(vm)), (0, 0, 0))
        vm.memory[VIDEO_STATUS] = 0
        self.assertGreater(vm.call('taskRuntimeDevices', second, SCREEN), 0)

    def test_grants_are_exclusive_per_class_and_gated_by_the_factory_mask(self):
        vm = display_fixture()
        child = create(vm, configure=False, publish=False)
        for devices in (SCREEN | FONT, SCREEN | C['DEVICE_INPUT'], FONT | C['DEVICE_DISK'], SCREEN | C['DEVICE_DISK'],
                        SCREEN | 0x100):
            self.assertEqual(vm.call('taskRuntimeDevices', child, devices), error(1), devices)
        self.assertEqual((vm.field('deviceRights', child), irq_owners(vm), grants_in_use(vm)), (0, 0, 0))
        vm.memory[vm.field_address('deviceFactory')] = C['DEVICE_DISK'] | C['DEVICE_INPUT']
        self.assertEqual(vm.call('taskRuntimeDevices', child, SCREEN), error(1))
        self.assertEqual(vm.call('taskRuntimeDevices', child, FONT), error(1))

    def test_the_fixed_bootstrap_display_is_never_regranted(self):
        from test_screen_services import kernel_fixture
        vm = kernel_fixture()
        self.assertEqual(vm.call('screenDevicesCheck'), error(1))

    def test_mapping_exhaustion_leaves_no_owner_irq_or_rights(self):
        vm = display_fixture()
        child = create(vm, configure=False, publish=False)
        vm.fail_allocation = vm.allocation_count + 2
        self.assertEqual(vm.call('taskRuntimeDevices', child, SCREEN), error(23))
        self.assertEqual((vm.field('deviceRights', child), irq_owners(vm)), (0, 0))
        self.assertEqual(vm.call('screenControl', child, 4, 0), error(1))
        # The partly constructed child is discarded by its supervisor; the display is free again.
        baseline = vm.free_pages()
        vm.run(1)
        vm.invoke(C['SYS_TASK_TERMINATE'], child, 0)
        self.assertEqual(grants_in_use(vm), 0)
        fresh = create(vm, configure=False, publish=False)
        self.assertGreater(vm.call('taskRuntimeDevices', fresh, SCREEN), 0)


class DisplayReplacementTests(unittest.TestCase):
    def test_repeated_replacement_returns_to_the_resource_baseline(self):
        vm = display_fixture()
        caller = client(vm, 1)
        baseline = vm.free_pages()
        old_ref = old_irq = 0
        for generation in range(1, 9):
            vm.run(1)
            ref, root, rx, irq = replacement(vm, generation, devices=SCREEN)
            self.assertNotEqual(ref, old_ref)
            self.assertFalse(vm.call('irqTokenValid', ref, old_irq, C['VIDEO_IRQ']))
            start = vm.field('bootPage', ref)
            # Kernel-announced font mapping, taken from the device table (words 14 and 15).
            self.assertEqual((vm.memory[start + 56], vm.memory[start + 60]),
                             (C['SCREEN_FONT_VA'], vm.addresses['fontDataEnd'] - vm.addresses['fontData']))
            self.assertEqual(vm.memory[start + 8], C['RECOVERY_START_BYTES'])
            vm.run(caller)
            self.assertEqual(resolve(vm)[1:3], (ref, generation))
            vm.run(1)
            retire(vm, ref, root)
            self.assertEqual(vm.free_pages(), baseline)
            self.assertEqual((grants_in_use(vm), irq_owners(vm), vm.control_count()), (0, 0, 2))
            self.assertEqual(vm.memory[VIDEO_CONTROL], 0)
            old_ref, old_irq = ref, irq
        vm.run(caller)

    def test_configure_requires_the_childs_own_line_and_complete_mappings(self):
        vm = display_fixture()
        child = create(vm, configure=False, publish=False)
        root = vm.factory(1, child)
        token = vm.call('taskRuntimeDevices', child, SCREEN)
        self.assertEqual(vm.call('serviceConfigure', child, root, 0, 1, 0), error(22))
        self.assertEqual(vm.call('serviceConfigure', child, root, 0, 1, token + 1), error(22))
        typ = vm.decls['resourceGrants'].sym.type.elem
        base = vm.addresses['resourceGrants']
        slots = [i for i in range(8) if vm.memory[base + i * typ.size + typ.field('directory').offset]]
        saved = vm.memory[base + slots[0] * typ.size + typ.field('directory').offset]
        vm.memory[base + slots[0] * typ.size + typ.field('directory').offset] = 0
        self.assertEqual(vm.call('serviceConfigure', child, root, 0, 1, token), error(22))
        vm.memory[base + slots[0] * typ.size + typ.field('directory').offset] = saved
        self.assertEqual(vm.call('serviceConfigure', child, root, 0, 1, token), 0)
        # Only a display child is announced a blob; other roles keep zeros there.
        plain = create(vm, configure=False, publish=False)
        own = vm.factory(1, plain)
        self.assertEqual(vm.call('serviceConfigure', plain, own, 0, 1, 0), 0)
        start = vm.field('bootPage', plain)
        self.assertEqual((vm.memory[start + 56], vm.memory[start + 60]), (0, 0))

    def test_font_extent_service_is_replaceable_while_the_display_lives(self):
        vm = display_fixture()
        screen, screen_root, _, _ = replacement(vm, 1, name=1, devices=SCREEN)
        font, font_root, _, font_irq = replacement(vm, 1, name=2, devices=FONT)
        self.assertEqual(vm.field('deviceRights', font), FONT)
        self.assertEqual(vm.call('diskInfo', font), 608)
        self.assertTrue(vm.call('irqTokenValid', font, font_irq, 3))
        for generation in range(2, 5):
            vm.run(1)
            retire(vm, font, font_root)
            fresh, fresh_root, _, fresh_irq = replacement(vm, generation, name=2, devices=FONT)
            self.assertNotEqual(fresh_irq, font_irq)
            self.assertFalse(vm.call('irqTokenValid', fresh, font_irq, 3))
            self.assertEqual(vm.call('diskInfo', fresh), 608)
            self.assertEqual(vm.call('diskInfo', font), error(1))
            self.assertEqual(vm.call('screenControl', screen, 4, 0), 0)
            font, font_root, font_irq = fresh, fresh_root, fresh_irq

    def test_font_extent_is_refused_while_its_owner_has_a_dma_pin(self):
        vm = display_fixture()
        font, root, _, _ = replacement(vm, 1, name=2, devices=FONT)
        vm.run(font)
        self.assertGreater(vm.call('deviceSubmit', font, 0, 16, 1, 0), 0)
        vm.run(1)
        vm.invoke(C['SYS_TASK_TERMINATE'], font, 0)
        child = create(vm, configure=False, publish=False)
        for _ in range(4):
            self.assertEqual(vm.call('taskRuntimeDevices', child, FONT), error(16))
            vm.reap()
            vm.invoke(C['SYS_YIELD'])
            vm.run(1)
        vm.memory.complete()
        vm.reap()
        self.assertGreater(vm.call('taskRuntimeDevices', child, FONT), 0)


class DisplayPolicyTests(unittest.TestCase):
    def test_modules_type_check(self):
        for name in ('user/recovery/screen.m', 'user/recovery/bitmap.m', 'user/recovery/screen_supervisor.m',
                     'src/kernel/screen_recovery_main.m', 'tests/programs/screenrecovery/screen.m',
                     'tests/programs/screenrecovery/scenario.m'):
            check_m(LAIX/name)

    def test_chain_recovery_retires_the_display_first_and_launches_storage_first(self):
        from test_recovery_policy import PolicyM
        vm = PolicyM()
        bitmap, _ = vm.service(0x1000000, 1)
        screen, _ = vm.service(0x1000100, 2)
        self.assertEqual(vm.call('recoverScreenBitmap', bitmap, screen), 0)
        self.assertEqual([args[0] for name, args in vm.operations if name == 'terminateTask'], [2, 1])
        self.assertEqual([args[0] for name, args in vm.operations if name == 'createTask'], [3, 4])
        self.assertEqual([args[1] for name, args in vm.operations if name == 'grantTaskDevices'], [FONT, SCREEN])
        configured = [args for name, args in vm.operations if name == 'configureService']
        self.assertEqual(configured[0][2:4], (0, 2))
        self.assertEqual(configured[1][2:4], (0x101, 2))
        self.assertEqual([args[0] for name, args in vm.operations if name == 'publishService'], [2, 1])

    def test_exhausted_chain_marks_both_unavailable_without_launching(self):
        from test_recovery_policy import PolicyM
        vm = PolicyM()
        bitmap, typ = vm.service(0x1000000, 1, attempts=5)
        screen, _ = vm.service(0x1000100, 2, attempts=3)
        self.assertEqual(vm.call('recoverScreenBitmap', bitmap, screen), error(32))
        self.assertFalse(any(name in ('createTask', 'grantTaskDevices') for name, _ in vm.operations))
        self.assertEqual(vm.memory[bitmap + typ.field('unavailable').offset], True)
        self.assertEqual(vm.memory[screen + typ.field('unavailable').offset], True)
        self.assertEqual([args for name, args in vm.operations if name == 'withdrawService'][-2:],
                         [(2, error(32)), (1, error(32))])


if __name__ == '__main__':
    unittest.main()
