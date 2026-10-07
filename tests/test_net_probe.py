"""The network CPU probe's UART patterns against the acceptance client's modelled output."""
import unittest

import probe_net_cpu as probe
from test_netclient import Program, IP


class NetProbeTests(unittest.TestCase):
    def test_the_modelled_client_output_satisfies_the_patterns(self):
        vm = Program('tests/programs/net/client.m')
        self.assertEqual(vm.call('ncMain', IP), 0)
        text = b''.join(vm.output).decode()
        self.assertGreater(probe.check(text, probe.LINK_UP, 'up'), 0)
        down = Program('tests/programs/net/client.m', link=False)
        self.assertEqual(down.call('ncMain', IP), 3)
        self.assertGreater(probe.check(b''.join(down.output).decode(), probe.LINK_DOWN, 'down'), 0)

    def test_failures_are_not_accepted(self):
        good = 'address 10.0.2.15 gateway 10.0.2.2\nping 10.0.2.2 seq 1 ttl 63\nping 10.0.2.2 seq 2 ttl 63\n' \
               'resolve localhost: 127.0.0.1\nresolve no-such-host.invalid: no such name\nnetwork ok\n'
        probe.check(good, probe.LINK_UP, 'good')
        for broken in (good.replace('ttl 63', 'failed -110', 1), good.replace('network ok\n', ''),
                       good.replace('resolve localhost: 127.0.0.1', 'resolve localhost: failed -110'),
                       good + 'PANIC', good.replace('seq 2', 'seq 3')):
            with self.assertRaises(ValueError):
                probe.check(broken, probe.LINK_UP, 'broken')


if __name__ == '__main__':
    unittest.main()
