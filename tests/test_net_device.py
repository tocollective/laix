"""Ethernet card broker (src/drivers/net_device.m) against a model of the card."""
import unittest

from source_m import LAYOUT as C
from test_ipc_handles import error
from test_kernel import LAIX
from test_screen_services import Devices, kernel_fixture
from test_task import USER_DATA

ETH = C['ETH_BASE']
EPERM, EFAULT, EBUSY, EINVAL, EIO, EAGAIN = 1, 14, 16, 22, 5, 11
OWN, ERROR = 0x80000000, 0x40000000
NOT_MAPPED = 0x61000000


class NetDevices(Devices):
    """The card as docs/SPECIFICATION.md describes it, DMA included."""

    def __init__(self, vm):
        super().__init__(vm)
        self.control = self.pending = 0
        self.regs = {}
        self.rx_next = self.tx_next = 0
        self.sent, self.waiting = [], []
        self.lost = 0
        self.link = 1
        self.on_pending_clear = None  # a frame may arrive just as software clears PENDING
        self.faulty_tx = False

    def line(self):
        enabled = 0
        if self.control & 2:
            enabled |= 5
        if self.control & 4:
            enabled |= 2
        if self.pending & 8 and self.control & 6:
            enabled |= 8
        if self.pending & enabled:
            self.lines |= 1 << 8
        else:
            self.lines &= ~(1 << 8)

    def __getitem__(self, address):
        if ETH <= address < ETH + 0x30:
            return {0: self.link, 4: self.control, 8: self.pending, 12: 0x12005452, 16: 0x5634,
                    0x1C: self.rx_next, 0x28: self.tx_next}.get(address - ETH, self.regs.get(address - ETH, 0))
        return super().__getitem__(address)

    def __setitem__(self, address, value):
        if ETH <= address < ETH + 0x30:
            offset = address - ETH
            if offset == 4:
                if value & 1 and not self.control & 1:
                    self.rx_next = self.tx_next = 0
                self.control = value & 7
                self.line()
            elif offset == 8:
                self.pending &= ~value
                self.line()
                if self.on_pending_clear:
                    hook, self.on_pending_clear = self.on_pending_clear, None
                    hook()
            elif offset in (0x14, 0x18, 0x20, 0x24):
                if not self.control & 1:
                    self.regs[offset] = value
            elif offset == 0x2C and self.control & 1:
                self.kick()
            return
        super().__setitem__(address, value)

    def descriptor(self, base, index):
        return base + 8 * index

    def kick(self):
        ring, size = self.regs.get(0x20, 0), self.regs.get(0x24, 0)
        while True:
            d = self.descriptor(ring, self.tx_next)
            control = dict.__getitem__(self, d + 4)
            if not control & OWN:
                break
            length, buffer = control & 0xFFFF, dict.__getitem__(self, d)
            if self.faulty_tx:
                self.pending |= 8
                self.control = 0
                self.line()
                return
            if length < 14 or length > 1514:
                dict.__setitem__(self, d + 4, length | ERROR)
            else:
                self.sent.append(bytes(dict.get(self, buffer + i, 0) for i in range(length)))
                dict.__setitem__(self, d + 4, length)
            self.pending |= 2
            self.tx_next = (self.tx_next + 1) % size
        self.line()

    def inject(self, frame, error_flag=False):
        if not self.control & 1:
            return
        ring, size = self.regs.get(0x14, 0), self.regs.get(0x18, 0)
        d = self.descriptor(ring, self.rx_next)
        control = dict.__getitem__(self, d + 4)
        if not control & OWN:
            if len(self.waiting) >= 64:
                self.lost += 1
            else:
                self.waiting.append(frame)
            self.pending |= 4
            self.line()
            return
        buffer, capacity = dict.__getitem__(self, d), control & 0xFFFF
        stored = frame[:capacity]
        for i, byte in enumerate(stored):
            dict.__setitem__(self, buffer + i, byte)
        flags = ERROR if error_flag or len(frame) > capacity else 0
        dict.__setitem__(self, d + 4, len(stored) | flags)
        self.rx_next = (self.rx_next + 1) % size
        self.pending |= 1
        self.line()


def fixture():
    vm = kernel_fixture()
    vm.memory = NetDevices(vm)
    vm.net_irq = vm.call('irqGrant', 2, 8)
    return vm


def started(owner=2):
    vm = fixture()
    assert vm.call('netDevicesInit', owner, vm.net_irq)
    # The owner's first arm, as its service does at start.
    assert vm.call('irqComplete', owner, vm.net_irq) == 0
    return vm


def put(vm, data, task=2):
    page = vm.pages(task)[1]
    for i, byte in enumerate(data):
        vm.memory[page + i] = byte


def frame(size, tag=0):
    return bytes((tag + i * 7 + i // 5) & 255 for i in range(size))


def send(vm, data):
    put(vm, data)
    return vm.call('netSend', 2, USER_DATA, len(data))


def receive(vm, capacity=1514):
    return vm.call('netRecv', 2, USER_DATA, capacity)


def received(vm, length):
    page = vm.pages(2)[1]
    return bytes(vm.memory[page + i] for i in range(length))


class NetDeviceTests(unittest.TestCase):
    def test_init_builds_rings_of_owned_buffers_and_enables_the_card(self):
        vm = fixture()
        free = len(vm.free_pages())
        self.assertTrue(vm.call('netDevicesInit', 2, vm.net_irq))
        card = vm.memory
        self.assertEqual(len(vm.free_pages()), free - 11)
        self.assertEqual(card.control, 3)  # on, interrupt on RX and LOST, not on TX
        rx, tx = card.regs[0x14], card.regs[0x20]
        self.assertEqual((card.regs[0x18], card.regs[0x24]), (16, 4))
        self.assertEqual((rx % 8, tx % 8, tx - rx), (0, 0, 256))
        buffers = set()
        for i in range(16):
            address, control = dict.__getitem__(card, rx + 8 * i), dict.__getitem__(card, rx + 8 * i + 4)
            self.assertEqual(control, 2048 | OWN)
            self.assertEqual(address % 2048, 0)
            buffers.add(address)
        for i in range(4):
            address, control = dict.__getitem__(card, tx + 8 * i), dict.__getitem__(card, tx + 8 * i + 4)
            self.assertEqual(control, 0)
            buffers.add(address)
        self.assertEqual(len(buffers), 20)  # every slot has its own buffer, none shared
        pages = {a & ~4095 for a in buffers}
        self.assertEqual(len(pages), 10)
        self.assertNotIn(rx & ~4095, pages)
        for page in pages | {rx & ~4095}:
            self.assertFalse(vm.call('physicalPageAvailable', page))
            self.assertEqual(vm.call('physicalPageReferences', page), 1)

    def test_only_an_unpublished_task_with_the_net_token_can_own_the_card(self):
        vm = fixture()
        free = len(vm.free_pages())
        self.assertFalse(vm.call('netDevicesInit', 3, vm.net_irq))  # not the token's owner
        self.assertFalse(vm.call('netDevicesInit', 2, vm.net_irq + 256))
        other = vm.call('irqGrant', 3, 0)  # a keyboard token is not a NET token
        self.assertFalse(vm.call('netDevicesInit', 3, other))
        self.assertEqual(len(vm.free_pages()), free)
        self.assertEqual(vm.memory.control, 0)
        self.assertTrue(vm.call('netDevicesInit', 2, vm.net_irq))
        self.assertFalse(vm.call('netDevicesInit', 2, vm.net_irq))  # once
        self.assertEqual(vm.call('netSend', 3, USER_DATA, 60), error(EPERM))
        self.assertEqual(vm.call('netRecv', 1, USER_DATA, 1514), error(EPERM))
        self.assertEqual(vm.call('netInfo', 0, USER_DATA), error(EPERM))

    def test_no_memory_means_no_card(self):
        vm = fixture()
        free = len(vm.free_pages())
        for _ in range(free - 10):
            self.assertNotEqual(vm.call('allocPage', 0xFFFFFFF0, 2), 0)
        free = 10
        self.assertEqual(len(vm.free_pages()), free)
        self.assertFalse(vm.call('netDevicesInit', 2, vm.net_irq))
        self.assertEqual(len(vm.free_pages()), free)
        self.assertEqual(vm.memory.control, 0)

    def test_send_copies_whole_frames_through_the_ring(self):
        vm = started()
        sizes = [14, 60, 98, 1514, 15, 64, 1000, 14]
        for n, size in enumerate(sizes):  # more than the four slots: the ring wraps
            data = frame(size, n)
            self.assertEqual(send(vm, data), size)
            self.assertEqual(vm.memory.sent[-1], data)
        self.assertEqual(len(vm.memory.sent), len(sizes))
        self.assertEqual(vm.memory.tx_next, 0)  # two trips round four slots
        self.assertEqual(vm.memory.pending & 2, 0)  # the TX cause is acknowledged

    def test_send_refuses_bad_lengths_and_buffers_without_touching_the_card(self):
        vm = started()
        put(vm, frame(2000))
        for length in (0, 13, 1515, 0xFFFFFFFF):
            self.assertEqual(vm.call('netSend', 2, USER_DATA, length), error(EINVAL))
        for source in (NOT_MAPPED, USER_DATA + 4096 - 20, 0xFFFFFFF0):
            self.assertEqual(vm.call('netSend', 2, source, 60), error(EFAULT))
        self.assertEqual(vm.memory.sent, [])
        self.assertEqual(vm.memory.tx_next, 0)
        self.assertEqual(send(vm, frame(60)), 60)  # the slot that was spared is the next one
        self.assertEqual(len(vm.memory.sent), 1)

    def test_a_card_fault_is_reported_and_the_broker_stops_serving(self):
        vm = started()
        vm.memory.faulty_tx = True
        self.assertEqual(send(vm, frame(60)), error(EIO))
        vm.memory.faulty_tx = False
        for call in (lambda: send(vm, frame(60)), lambda: receive(vm), lambda: vm.call('netInfo', 2, USER_DATA)):
            self.assertEqual(call(), error(EIO))
        free = len(vm.free_pages())
        vm.call('netReleaseOwner', 2)
        self.assertEqual(len(vm.free_pages()), free + 11)
        self.assertEqual(vm.memory.control, 0)

    def test_receive_returns_frames_in_order_and_recycles_buffers(self):
        vm = started()
        frames = [frame(14 + 97 * i, i) for i in range(1, 17)] + [frame(1514, 9)]
        # Fewer than 16 at a time, but more than the ring holds over the run.
        for batch in (frames[:10], frames[10:]):
            for data in batch:
                vm.memory.inject(data)
            for data in batch:
                length = receive(vm)
                self.assertEqual(length, len(data))
                self.assertEqual(received(vm, length), data)
        self.assertEqual(receive(vm), error(EAGAIN))

    def test_an_empty_ring_arms_the_interrupt_and_a_frame_wakes_it(self):
        vm = started()
        self.assertEqual(receive(vm), error(EAGAIN))
        typ = vm.decls['irqGrants'].sym.type
        line8 = vm.addresses['irqGrants'] + typ.elem.size * 8
        in_service = line8 + typ.elem.field('inService').offset
        self.assertEqual(vm.memory[in_service], 0)  # armed
        self.assertNotEqual(vm.memory[C['PIC_ENABLE']] & (1 << 8), 0)
        vm.memory.inject(frame(70))
        self.assertTrue(vm.memory.lines & (1 << 8))  # the level the PIC will deliver
        self.assertEqual(vm.call('irqNotify', 8), True)
        self.assertEqual(receive(vm), 70)
        self.assertEqual(received(vm, 70), frame(70))

    def test_a_frame_arriving_while_arming_is_not_missed(self):
        vm = started()
        # Just as software acknowledges the causes of an empty ring, a frame lands.
        vm.memory.on_pending_clear = lambda: vm.memory.inject(frame(80))
        self.assertEqual(receive(vm), 80)
        # And one that lands between the second look and the arming.
        real = vm.call

        def call(name, *args):
            if name == 'irqPollComplete':
                vm.memory.inject(frame(90))
            return real(name, *args)

        vm.call = call
        self.assertEqual(receive(vm), 90)

    def test_damaged_or_unreadable_frames_are_dropped_and_the_slot_is_returned(self):
        vm = started()
        vm.memory.inject(frame(100), error_flag=True)  # cut short
        vm.memory.inject(frame(5))  # a runt
        vm.memory.inject(frame(120))
        self.assertEqual(receive(vm), 120)  # the two bad ones are skipped
        self.assertEqual(received(vm, 120), frame(120))
        vm.memory.inject(frame(2500))  # longer than a buffer: the card cuts it and flags it
        self.assertEqual(receive(vm), error(EAGAIN))
        # A destination that cannot be written costs that frame, not the ring.
        vm.memory.inject(frame(60))
        self.assertEqual(vm.call('netRecv', 2, NOT_MAPPED, 1514), error(EFAULT))
        vm.memory.inject(frame(61))
        self.assertEqual(receive(vm), 61)
        for capacity in (0, 1513):
            self.assertEqual(receive(vm, capacity), error(EINVAL))
        info = vm.call('netInfo', 2, USER_DATA)
        self.assertEqual(info, 16)
        self.assertEqual(int.from_bytes(received(vm, 16)[12:16], 'little'), 4)  # the error, the runt, the cut one, the unreadable

    def test_info_reports_the_mac_and_link(self):
        vm = started()
        self.assertEqual(vm.call('netInfo', 2, USER_DATA), 16)
        data = received(vm, 16)
        self.assertEqual(data[:6], bytes([0x52, 0x54, 0x00, 0x12, 0x34, 0x56]))
        self.assertEqual(data[6], 1)
        vm.memory.link = 0
        vm.call('netInfo', 2, USER_DATA)
        self.assertEqual(received(vm, 16)[6], 0)
        self.assertEqual(vm.call('netInfo', 2, NOT_MAPPED), error(EFAULT))

    def test_the_owner_going_away_stops_the_card_and_frees_everything(self):
        vm = started()
        free = len(vm.free_pages())
        vm.memory.inject(frame(60))
        vm.call('netReleaseOwner', 3)  # someone else: no effect
        self.assertEqual(vm.memory.control, 3)
        vm.call('netReleaseOwner', 2)
        self.assertEqual(vm.memory.control, 0)
        self.assertEqual(vm.memory.pending, 0)
        self.assertEqual(len(vm.free_pages()), free + 11)
        self.assertEqual(send(vm, frame(60)), error(EPERM))
        vm.memory.inject(frame(60))  # a card that is off drops frames
        self.assertEqual(vm.memory.waiting, [])

    def test_the_system_call_needs_the_net_right(self):
        from test_service_recovery import fixture as running
        vm = running()
        for number, args in ((C['SYS_NET_INFO'], (USER_DATA,)), (C['SYS_NET_SEND'], (USER_DATA, 60)),
                             (C['SYS_NET_RECV'], (USER_DATA, 1514))):
            vm.invoke(number, *args)
            self.assertEqual(vm.result(vm.current())[0], error(EPERM))


if __name__ == '__main__':
    unittest.main()
