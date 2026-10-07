"""LAIX_CONSOLE=net boot policy: only the driver owns the card; each task reaches only its neighbour."""
import unittest

from source_m import LAYOUT as C
from test_ipc_handles import error
from test_kernel import LAIX, check_m
from test_net_device import NetDevices
from test_shell_profile import handle_list, points_at
from test_task import TaskM

RIGHT_SEND, RIGHT_RECEIVE = C['RIGHT_SEND'], C['RIGHT_RECEIVE']
CONSOLE, DRIVER, IP, CLIENT = 1, 2, 3, 4


def fixture(fail=None):
    vm = TaskM(ram=0x200000, root=LAIX / 'src/kernel/net_main.m')
    vm.memory = NetDevices(vm)
    vm.addresses.update(fontData=0x16000, fontDataEnd=0x16000 + 184,
                        bootstrapServerStart=0x14000, bootstrapServerEnd=0x1401C)
    for index, name in enumerate(('netdrvImage', 'ipImage', 'netClientImage')):
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
    vm.fail_mapping = fail
    return vm


class NetProfileTests(unittest.TestCase):
    def test_sources_check(self):
        for path in ('src/kernel/net_main.m', 'src/kernel/net_bootstrap.m', 'user/services/netdrv.m',
                     'user/services/ip.m', 'user/services/ipclient.m', 'tests/programs/net/client.m'):
            check_m(LAIX / path)

    def test_authority_and_devices(self):
        vm = fixture()
        self.assertTrue(vm.call('bootstrapNetInit'))
        for task in range(1, 5):
            self.assertEqual(vm.field('state', task), 1)
        self.assertEqual([vm.field('deviceRights', t) for t in range(1, 5)], [C['DEVICE_UART_TX'], C['DEVICE_NET'], 0, 0])
        self.assertEqual([vm.field('createImages', t) for t in range(1, 5)], [0, 0, 0, 0])
        self.assertEqual(vm.globals['netOwner'], vm.field('id', DRIVER))
        self.assertEqual(vm.memory.control, 3)
        # The ring buffers exist only for the driver's card; nobody else may use the broker.
        self.assertEqual(vm.call('netSend', vm.field('id', IP), 0x40001000, 60), error(1))
        self.assertEqual(vm.call('netRecv', vm.field('id', CLIENT), 0x40001000, 1514), error(1))

    def test_each_handle_reaches_only_its_neighbour(self):
        vm = fixture()
        self.assertTrue(vm.call('bootstrapNetInit'))
        token = vm.globals['netToken']
        driver, _ = handle_list(vm, DRIVER)
        self.assertEqual(driver, [token])
        self.assertTrue(vm.call('irqTokenValid', vm.field('id', DRIVER), token, C['ETH_IRQ']))
        ip, _ = handle_list(vm, IP)
        self.assertEqual(len(ip), 1)
        self.assertTrue(points_at(vm, IP, ip[0], RIGHT_SEND, DRIVER))
        client, unused = handle_list(vm, CLIENT)
        self.assertEqual((len(client), unused), (2, [0, 0, 0, 0]))
        self.assertTrue(points_at(vm, CLIENT, client[0], RIGHT_SEND, IP))
        self.assertTrue(points_at(vm, CLIENT, client[1], RIGHT_SEND, CONSOLE))
        self.assertFalse(points_at(vm, CLIENT, client[0], RIGHT_SEND, DRIVER))  # no way round the stack
        self.assertFalse(points_at(vm, IP, ip[0], RIGHT_SEND, CONSOLE))
        for task in (DRIVER, IP):
            block = vm.field('bootPage', task)
            self.assertEqual(vm.memory[block], C['RUNTIME_START_MAGIC'])
            self.assertNotEqual(vm.memory[block + 4 * 7], 0)       # its receive handle
            self.assertEqual(vm.memory[block + 4 * 8], RIGHT_RECEIVE)
        self.assertEqual(vm.memory[vm.field('bootPage', CLIENT) + 4 * 7], 0)

    def test_every_construction_failure_rolls_back_and_the_card_is_left_off(self):
        for fail in range(1, 30):
            vm = fixture(fail)
            free = vm.free_pages()
            if vm.call('bootstrapNetInit'):
                self.assertGreater(fail, 8)
                continue
            self.assertEqual(len(vm.free_pages()), len(free), fail)
            self.assertTrue(vm.call('taskInitAvailable'))
            self.assertEqual(vm.globals['netOwner'], 0)
            self.assertEqual(vm.memory.control, 0)
            vm.fail_mapping = None
            self.assertTrue(vm.call('bootstrapNetInit'), fail)


if __name__ == '__main__':
    unittest.main()
