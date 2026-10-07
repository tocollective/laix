"""The Ethernet driver service (user/services/netdrv.m) with a scripted broker."""
import struct
import unittest

from source_m import SourceM, LAYOUT as C
from test_ipc_handles import error
from test_kernel import LAIX

REQ, RES = 0x1000000, 0x1001000
INFO, PUT, SEND, POLL, GET, DONE = (C['NETDRV_' + n + '_HEADER'] for n in ('INFO', 'PUT', 'SEND', 'POLL', 'GET', 'DONE'))
EAGAIN, EIO, EINVAL, EPIPE, ETIMEDOUT = 11, 5, 22, 32, 110
IRQ = 0x108


def frame(size, tag=0):
    return bytes((tag + i * 5 + i // 3) & 255 for i in range(size))


class DriverVM(SourceM):
    """netdrv.m; the broker's three calls and the interrupt wait are scripted."""

    def __init__(self):
        super().__init__(LAIX / 'user/services/netdrv.m')
        self.sent, self.queue, self.waits = [], [], []
        self.send_result = None
        self.arrive_on_wait = []
        self.wait_result = 0
        self.dropped = 3
        self.receive_error = None
        self.on_send = None

    def call(self, name, *args):
        if name == 'netDeviceInfo':
            (destination,) = args
            mac = bytes([0x52, 0x54, 0x00, 0x12, 0x34, 0x56])
            words = [struct.unpack('<I', mac[:4])[0], mac[4] | mac[5] << 8 | 1 << 16, 0, self.dropped]
            for i, word in enumerate(words):
                self.memory[destination + 4 * i] = word
            return 16
        if name == 'netDeviceSend':
            source, length = args
            data = b''.join(struct.pack('<I', self.memory[source + 4 * w]) for w in range((length + 3) // 4))[:length]
            self.sent.append(data)
            if self.on_send:
                self.on_send(data)
            return length if self.send_result is None else self.send_result
        if name == 'netDeviceReceive':
            destination, capacity = args
            assert capacity >= 1514
            if self.receive_error:
                return self.receive_error
            if not self.queue:
                return error(EAGAIN)
            data = self.queue.pop(0)
            padded = data.ljust((len(data) + 3) // 4 * 4, b'\0')
            for w in range(len(padded) // 4):
                self.memory[destination + 4 * w] = struct.unpack_from('<I', padded, 4 * w)[0]
            return len(data)
        if name == 'irqWait':
            self.waits.append(args)
            if self.wait_result == 0:
                self.queue += self.arrive_on_wait
                self.arrive_on_wait = []
            return self.wait_result
        return super().call(name, *args)

    def send(self, words, size=16):
        for i, word in enumerate(words):
            self.memory[REQ + 4 * i] = word
        self.call('netdrvHandle', REQ, size, RES, IRQ)
        out = [self.memory[RES + 4 * i] for i in range(8)]
        status = out[1] - (1 << 32) if out[1] >= 1 << 31 else out[1]
        return status, out

    def request(self, header, a=0, b=0):
        return self.send([header, 1, a, b])

    def upload(self, data):
        data = data.ljust((len(data) + 15) // 16 * 16, b'\0')
        for offset in range(0, len(data), 16):
            status, _ = self.send([PUT, 1, offset, 0, *struct.unpack('<4I', data[offset:offset + 16])], 32)
            assert status == 0
        return len(data)

    def download(self, length):
        data = b''
        for offset in range(0, length, 16):
            status, out = self.request(GET, offset)
            assert status == 0
            data += struct.pack('<4I', *out[4:8])[:out[3]]
        return data


class NetDriverTests(unittest.TestCase):
    def test_info_reports_mac_link_and_discards(self):
        vm = DriverVM()
        status, out = vm.request(INFO)
        self.assertEqual(status, 0)
        self.assertEqual(struct.pack('<I', out[3]) + struct.pack('<I', out[4] & 0xFFFF)[:2], bytes([0x52, 0x54, 0x00, 0x12, 0x34, 0x56]))
        self.assertEqual((out[4] >> 16, out[5]), (1, 3))

    def test_a_frame_goes_out_exactly_as_it_was_put_together(self):
        vm = DriverVM()
        for size in (14, 60, 98, 1514):
            data = frame(size, size)
            vm.upload(data)
            status, out = vm.request(SEND, size)
            self.assertEqual((status, out[3]), (0, size))
            self.assertEqual(vm.sent[-1], data)

    def test_send_and_put_are_bounded(self):
        vm = DriverVM()
        for length in (0, 13, 1515, 0xFFFFFFFF):
            self.assertEqual(vm.request(SEND, length)[0], -EINVAL)
        for offset in (1, 8, 1505, 1520, 0xFFFFFFF0):
            self.assertEqual(vm.send([PUT, 1, offset, 0, 1, 2, 3, 4], 32)[0], -EINVAL, offset)
        self.assertEqual(vm.send([PUT, 1, 1504, 0, 1, 2, 3, 4], 32)[0], 0)
        self.assertEqual(vm.send([PUT, 1, 16, 1, 1, 2, 3, 4], 32)[0], -EINVAL)
        self.assertEqual(vm.sent, [])

    def test_a_broker_error_comes_back_and_a_dead_card_ends_the_service(self):
        vm = DriverVM()
        vm.upload(frame(60))
        vm.send_result = error(16)
        self.assertEqual(vm.request(SEND, 60)[0], -16)
        self.assertFalse(vm.globals['netdrvFailed'])
        vm.send_result = error(EIO)
        self.assertEqual(vm.request(SEND, 60)[0], -EIO)
        self.assertTrue(vm.globals['netdrvFailed'])

    def test_poll_get_done_walk_the_received_frames_in_order(self):
        vm = DriverVM()
        frames = [frame(42, 1), frame(98, 2), frame(1514, 3), frame(16, 4)]
        vm.queue = list(frames)
        for data in frames:
            status, out = vm.request(POLL, 0)
            self.assertEqual((status, out[3]), (0, len(data)))
            self.assertEqual(vm.request(POLL, 0)[1][3], len(data))  # held until DONE
            self.assertEqual(vm.download(len(data)), data)
            self.assertEqual(vm.request(DONE)[0], 0)
        self.assertEqual(vm.request(POLL, 0)[1][3], 0)
        self.assertEqual(vm.waits, [])  # seconds 0 never waits

    def test_poll_waits_for_the_interrupt_and_reports_a_timeout_as_no_frame(self):
        vm = DriverVM()
        vm.arrive_on_wait = [frame(70)]
        status, out = vm.request(POLL, 3)
        self.assertEqual((status, out[3]), (0, 70))
        self.assertEqual(vm.waits, [(IRQ, 3)])
        vm.request(DONE)
        vm.wait_result = error(ETIMEDOUT)
        status, out = vm.request(POLL, 2)
        self.assertEqual((status, out[3]), (0, 0))
        self.assertEqual(len(vm.waits), 2)
        vm.wait_result = error(EINVAL)  # a real wait failure is passed on
        self.assertEqual(vm.request(POLL, 2)[0], -EINVAL)

    def test_get_and_poll_arguments_are_checked(self):
        vm = DriverVM()
        vm.queue = [frame(40)]
        self.assertEqual(vm.request(GET, 0)[0], -EINVAL)  # nothing received yet
        vm.request(POLL, 0)
        for offset in (1, 8, 48, 0xFFFFFFF0):
            self.assertEqual(vm.request(GET, offset)[0], -EINVAL, offset)
        self.assertEqual(vm.request(GET, 32)[1][3], 8)  # the last chunk is short
        self.assertEqual(vm.request(POLL, 61)[0], -EINVAL)
        self.assertEqual(vm.request(POLL, 1, 1)[0], -EINVAL)

    def test_a_dead_card_while_receiving_ends_the_service(self):
        vm = DriverVM()
        vm.receive_error = error(EIO)
        self.assertEqual(vm.request(POLL, 0)[0], -EIO)
        self.assertTrue(vm.globals['netdrvFailed'])

    def test_malformed_and_stale_messages_are_refused(self):
        vm = DriverVM()
        for header, size in ((PUT, 16), (SEND, 32), (INFO, 12), (POLL, 20), (0, 16), (C['DISK_LOAD_HEADER'], 16)):
            self.assertEqual(vm.send([header, 1, 0, 0, 0, 0, 0, 0], size)[0], -EINVAL, hex(header))
        for header in (INFO, SEND, POLL, GET, DONE):
            self.assertEqual(vm.send([header, 2, 0, 0])[0], -EPIPE)
        self.assertEqual(vm.send([INFO, 1, 1, 0])[0], -EINVAL)
        self.assertEqual(vm.sent, [])


if __name__ == '__main__':
    unittest.main()
