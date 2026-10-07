"""The IP client library and the network acceptance client against the real services and a modelled network."""
import struct
import unittest

from source_m import SourceM, LAYOUT as C
from test_ipc_handles import error
from test_kernel import LAIX
from mlang import syntax
from test_ip import IpVM, GATEWAY, GUEST, ETIMEDOUT, ENOENT, EINVAL, EHOSTUNREACH, ENETDOWN, GUEST_MAC

IP = 0x51
CONSOLE = 0x52
BUFFER, RESULT, STRINGS = 0x2000000, 0x2100000, 0x2200000


class Program(SourceM):
    """A client program; each call to the IP service runs the real ip.m."""

    def __init__(self, root, link=True):
        super().__init__(LAIX / root)
        self.ip = IpVM(link=link)
        self.output = []
        if 'textStart' in self.decls:
            self.call('textStart', CONSOLE)
        self.strings = {}
        self.next_string = STRINGS

    def expr(self, node, local):
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
            assert handle == IP
            for i in range(8 if size > 16 else (size + 3) // 4):
                self.ip.memory[0x1000000 + 4 * i] = self.memory[request + 4 * i]
            self.ip.call('ipHandle', 0x1000000, size, 0x1001000)
            for i in range(8):
                self.memory[response + 4 * i] = self.ip.memory[0x1001000 + 4 * i]
            return 32
        if name == 'consoleWrite':
            handle, text, length = args
            assert handle == CONSOLE and 0 < length <= 28
            self.output.append(bytes(self.memory[text + i] for i in range(length)))
            return length
        return super().call(name, *args)

    def cstring(self, text, address=BUFFER):
        for i, byte in enumerate(text.encode() + b'\0'):
            self.memory[address + i] = byte
        return address


class ClientLibraryTests(unittest.TestCase):
    def test_info_ping_and_resolve(self):
        vm = Program('user/services/ipclient.m')
        self.assertEqual(vm.call('ipInfo', IP, RESULT), 0)
        self.assertEqual([vm.memory[RESULT + 4 * i] for i in range(2)], [GUEST, GATEWAY])
        self.assertEqual(vm.memory[RESULT + 8], struct.unpack('<I', GUEST_MAC[:4])[0])
        self.assertEqual(vm.call('ipPing', IP, GATEWAY, 1, 2), 63)
        self.assertEqual(vm.call('ipResolve', IP, vm.cstring('localhost'), 3, RESULT), 0)
        self.assertEqual(vm.memory[RESULT], 0x7F000001)
        name = 'a' * 14 + '.' + 'b' * 16 + '.example.test'  # spans three staging chunks
        vm.ip.network.records[name] = [0x01020304]
        self.assertEqual(vm.call('ipResolve', IP, vm.cstring(name), 3, RESULT), 0)
        self.assertEqual(vm.memory[RESULT], 0x01020304)
        # A name that is an exact multiple of the chunk size still ends with a zero byte.
        exact = 'abcdefgh.ijklmno.p'.ljust(32, 'q')
        vm.ip.network.records[exact] = [0x05060708]
        self.assertEqual(vm.call('ipResolve', IP, vm.cstring(exact), 3, RESULT), 0)

    def test_errors_come_back_as_errnos(self):
        vm = Program('user/services/ipclient.m')
        self.assertEqual(vm.call('ipResolve', IP, vm.cstring('missing.test'), 3, RESULT), error(ENOENT))
        vm.ip.network.silent.add('icmp')
        self.assertEqual(vm.call('ipPing', IP, GATEWAY, 1, 1), error(ETIMEDOUT))
        for name in ('', 'x' * 64, 'bad name'):
            self.assertEqual(vm.call('ipResolve', IP, vm.cstring(name), 3, RESULT), error(EINVAL), name)
        self.assertEqual(vm.call('ipResolve', IP, 0, 3, RESULT), error(EINVAL))
        self.assertEqual(vm.call('ipInfo', IP, 0), error(EINVAL))
        down = Program('user/services/ipclient.m', link=False)
        self.assertEqual(down.call('ipPing', IP, GATEWAY, 1, 1), error(ENETDOWN))


class AcceptanceClientTests(unittest.TestCase):
    def test_the_acceptance_client_passes_and_prints_what_the_probe_looks_for(self):
        vm = Program('tests/programs/net/client.m')
        self.assertEqual(vm.call('ncMain', IP), 0)
        text = b''.join(vm.output).decode()
        self.assertEqual(text, 'address 10.0.2.15 gateway 10.0.2.2\n'
                               'ping 10.0.2.2 seq 1 ttl 63\n'
                               'ping 10.0.2.2 seq 2 ttl 63\n'
                               'resolve localhost: 127.0.0.1\n'
                               'resolve no-such-host.invalid: no such name\n'
                               'network ok\n')
        self.assertEqual(vm.ip.network.problems, [])

    def test_it_names_the_step_that_failed(self):
        vm = Program('tests/programs/net/client.m', link=False)
        self.assertEqual(vm.call('ncMain', IP), 3)
        vm = Program('tests/programs/net/client.m')
        vm.ip.network.silent.add('icmp')
        self.assertEqual(vm.call('ncMain', IP), 11)
        vm = Program('tests/programs/net/client.m')
        vm.ip.network.silent.add('dns')
        self.assertEqual(vm.call('ncMain', IP), 20)


if __name__ == '__main__':
    unittest.main()
