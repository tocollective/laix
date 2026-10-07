"""The IPv4 service (user/services/ip.m) over the real driver service and a model of the emulated network."""
import struct
import unittest

from source_m import SourceM, LAYOUT as C
from test_ipc_handles import error
from test_kernel import LAIX
from test_netdrv import DriverVM

HANDLE = 0x41
INFO, PING, NAME, RESOLVE = (C['IP_' + n + '_HEADER'] for n in ('INFO', 'PING', 'NAME', 'RESOLVE'))
ETIMEDOUT, EHOSTUNREACH, ENOENT, EINVAL, EPIPE, EIO, EPROTO, ENETDOWN = 110, 113, 2, 22, 32, 5, 71, 100
GUEST_MAC = bytes([0x52, 0x54, 0x00, 0x12, 0x34, 0x56])
GATEWAY_MAC = bytes([0x52, 0x55, 0x0A, 0x00, 0x02, 0x02])
GUEST, GATEWAY, DNS = 0x0A00020F, 0x0A000202, 0x0A000203


def ip_bytes(address):
    return struct.pack('>I', address)


def checksum(data, start=0):
    if len(data) % 2:
        data += b'\0'
    total = start + sum(struct.unpack('>%dH' % (len(data) // 2), data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def ipv4(source, destination, protocol, payload, ttl=64, ident=1):
    header = struct.pack('>BBHHHBBH4s4s', 0x45, 0, 20 + len(payload), ident, 0x4000, ttl, protocol, 0,
                         ip_bytes(source), ip_bytes(destination))
    header = header[:10] + struct.pack('>H', checksum(header)) + header[12:]
    return header + payload


def ethernet(destination, source, ethertype, payload):
    frame = destination + source + struct.pack('>H', ethertype) + payload
    return frame.ljust(60, b'\0')


def udp(source, destination, sport, dport, payload, with_checksum=True):
    length = 8 + len(payload)
    body = struct.pack('>HHHH', sport, dport, length, 0) + payload
    if with_checksum:
        pseudo = ip_bytes(source) + ip_bytes(destination) + struct.pack('>BBH', 0, 17, length)
        value = checksum(pseudo + body) or 0xFFFF
        body = body[:6] + struct.pack('>H', value) + body[8:]
    return body


def dns_name(name):
    out = b''
    for label in name.split('.'):
        out += bytes([len(label)]) + label.encode()
    return out + b'\0'


class Network:
    """The emulated gateway: ARP for itself and the DNS server, ping, and DNS answers."""

    def __init__(self, driver):
        self.driver = driver
        driver.on_send = self.receive
        driver.wait_result = error(ETIMEDOUT)  # an idle second passes when nothing arrives
        self.records = {'localhost': [0x7F000001], 'example.test': [0x5DB8D822, 0x5DB8D823]}
        self.frames = []
        self.problems = []
        self.silent = set()  # what the gateway ignores: 'arp', 'icmp', 'dns'
        self.compress = True

    def deliver(self, frame):
        self.driver.queue.append(frame)

    def receive(self, frame):
        self.frames.append(frame)
        if len(frame) < 60:
            self.problems.append('runt frame on the wire')
        ethertype = struct.unpack('>H', frame[12:14])[0]
        if ethertype == 0x0806:
            self.arp(frame)
        elif ethertype == 0x0800:
            self.ip(frame)
        else:
            self.problems.append(f'unknown ethertype {ethertype:x}')

    def arp(self, frame):
        hardware, protocol, hlen, plen, operation = struct.unpack('>HHBBH', frame[14:22])
        if (hardware, protocol, hlen, plen) != (1, 0x0800, 6, 4):
            self.problems.append('bad ARP header')
        sender_mac, sender_ip, target_mac, target_ip = frame[22:28], frame[28:32], frame[32:38], frame[38:42]
        if sender_mac != GUEST_MAC or frame[6:12] != GUEST_MAC:
            self.problems.append('ARP from the wrong MAC')
        if operation == 1 and 'arp' not in self.silent and struct.unpack('>I', target_ip)[0] in (GATEWAY, DNS):
            reply = struct.pack('>HHBBH', 1, 0x0800, 6, 4, 2) + GATEWAY_MAC + target_ip + sender_mac + sender_ip
            self.deliver(ethernet(sender_mac, GATEWAY_MAC, 0x0806, reply))

    def ip(self, frame):
        packet = frame[14:]
        if packet[0] != 0x45 or checksum(packet[:20]) != 0:
            self.problems.append('bad IP header')
        total = struct.unpack('>H', packet[2:4])[0]
        if total > len(packet):
            self.problems.append('IP length beyond the frame')
        if frame[:6] != GATEWAY_MAC:
            self.problems.append('IP frame not addressed to the gateway MAC')
        if packet[6] & 0x20 or struct.unpack('>H', packet[6:8])[0] & 0x1FFF:
            self.problems.append('fragment')
        protocol, source, destination = packet[9], struct.unpack('>I', packet[12:16])[0], struct.unpack('>I', packet[16:20])[0]
        if source != GUEST:
            self.problems.append('wrong source address')
        body = packet[20:total]
        if protocol == 1:
            self.icmp(destination, body)
        elif protocol == 17:
            self.udp(destination, body)

    def icmp(self, destination, body):
        if checksum(body) != 0:
            self.problems.append('bad ICMP checksum')
        if body[0] == 8 and 'icmp' not in self.silent and destination == GATEWAY:
            reply = b'\0\0\0\0' + body[4:]
            reply = reply[:2] + struct.pack('>H', checksum(reply)) + reply[4:]
            self.deliver(ethernet(GUEST_MAC, GATEWAY_MAC, 0x0800, ipv4(GATEWAY, GUEST, 1, reply, ttl=63)))

    def udp(self, destination, body):
        sport, dport, length, check = struct.unpack('>HHHH', body[:8])
        if check:
            pseudo = ip_bytes(GUEST) + ip_bytes(destination) + struct.pack('>BBH', 0, 17, length)
            if checksum(pseudo + body[:length]) != 0:
                self.problems.append('bad UDP checksum')
        if destination != DNS or dport != 53 or 'dns' in self.silent:
            return
        query = body[8:length]
        ident, flags, qd, an = struct.unpack('>HHHH', query[:8])
        question_end = 12
        labels = []
        while query[question_end]:
            size = query[question_end]
            labels.append(query[question_end + 1:question_end + 1 + size].decode())
            question_end += size + 1
        question_end += 1
        qtype, qclass = struct.unpack('>HH', query[question_end:question_end + 4])
        name = '.'.join(labels)
        question = query[12:question_end + 4]
        if qtype != 1 or qclass != 1 or flags != 0x0100 or qd != 1:
            self.problems.append('odd DNS query')
        addresses = self.records.get(name)
        rcode = 0 if addresses else 3
        answers = b''
        for address in addresses or []:
            pointer = b'\xc0\x0c' if self.compress else dns_name(name)
            answers += pointer + struct.pack('>HHIH', 1, 1, 300, 4) + ip_bytes(address)
        response = struct.pack('>HHHHHH', ident, 0x8180 | rcode, 1, len(addresses or []), 0, 0) + question + answers
        datagram = udp(DNS, GUEST, 53, sport, response)
        self.deliver(ethernet(GUEST_MAC, GATEWAY_MAC, 0x0800, ipv4(DNS, GUEST, 17, datagram)))


class IpVM(SourceM):
    """ip.m talking to the real netdrv.m, which talks to the modelled network."""

    def __init__(self, link=True):
        super().__init__(LAIX / 'user/services/ip.m')
        self.driver = DriverVM()
        self.network = Network(self.driver)
        self.globals['ipDriver'] = HANDLE
        self.driver_calls = 0
        # What the service does at start: ask the driver for the MAC and link.
        status, out = self.driver.request(C['NETDRV_INFO_HEADER'])
        assert status == 0
        self.memory[self.addresses['ipMac']] = out[3]
        self.memory[self.addresses['ipMac'] + 4] = out[4] & 0xFFFF
        self.globals['ipLink'] = int(link)

    def call(self, name, *args):
        if name == 'call':
            handle, request, size, response, capacity = args
            assert handle == HANDLE and capacity == 32
            self.driver_calls += 1
            words = [self.memory[request + 4 * i] for i in range(8 if size > 16 else (size + 3) // 4)]
            status, out = self.driver.send(words, size)
            for i, word in enumerate(out):
                self.memory[response + 4 * i] = word
            return 32
        return super().call(name, *args)

    def ask(self, header, a=0, b=0, extra=(), size=16):
        words = [header, 1, a, b, *extra]
        for i, word in enumerate(words):
            self.memory[0x1000000 + 4 * i] = word
        self.call('ipHandle', 0x1000000, size, 0x1001000)
        out = [self.memory[0x1001000 + 4 * i] for i in range(8)]
        status = out[1] - (1 << 32) if out[1] >= 1 << 31 else out[1]
        return status, out

    def ping(self, address, seq=1, seconds=2):
        return self.ask(PING, address, (seconds << 16) | seq)

    def stage(self, name):
        raw = name.encode().ljust(64, b'\0')
        for offset in range(0, 64, 16):
            status, _ = self.ask(NAME, offset, 0, struct.unpack('<4I', raw[offset:offset + 16]), 32)
            assert status == 0

    def resolve(self, name, seconds=3):
        self.stage(name)
        status, out = self.ask(RESOLVE, seconds)
        return status, out[3]


class IpTests(unittest.TestCase):
    def test_info_reports_the_fixed_address_gateway_and_mac(self):
        vm = IpVM()
        status, out = vm.ask(INFO)
        self.assertEqual((status, out[3], out[4]), (0, GUEST, GATEWAY))
        self.assertEqual(struct.pack('<I', out[5]) + struct.pack('<H', out[6] & 0xFFFF), GUEST_MAC)
        self.assertEqual(out[6] >> 16, 1)

    def test_ping_the_gateway_asks_arp_first_and_returns_the_ttl(self):
        vm = IpVM()
        status, out = vm.ping(GATEWAY, seq=7)
        self.assertEqual((status, out[3]), (0, 63))
        kinds = [struct.unpack('>H', f[12:14])[0] for f in vm.network.frames]
        self.assertEqual(kinds, [0x0806, 0x0800])
        self.assertEqual(vm.network.problems, [])
        # The second ping needs no ARP.
        vm.network.frames.clear()
        self.assertEqual(vm.ping(GATEWAY, seq=8)[0], 0)
        self.assertEqual([struct.unpack('>H', f[12:14])[0] for f in vm.network.frames], [0x0800])
        echo = vm.network.frames[0][34:]
        self.assertEqual(struct.unpack('>BBHHH', echo[:8])[0:1] + struct.unpack('>BBHHH', echo[:8])[3:], (8, 0x4C58, 8))
        self.assertEqual(len(echo), 40)  # an eight-byte header and 32 bytes of data
        self.assertEqual(vm.network.problems, [])

    def test_ping_times_out_without_an_answer_and_fails_without_arp(self):
        vm = IpVM()
        vm.network.silent.add('icmp')
        status, _ = vm.ping(GATEWAY, seconds=2)
        self.assertEqual(status, -ETIMEDOUT)
        self.assertEqual(vm.ping(0x0A000299, seconds=1)[0], -EHOSTUNREACH)  # nobody answers ARP for it
        vm2 = IpVM()
        vm2.network.silent.add('arp')
        self.assertEqual(vm2.ping(GATEWAY)[0], -EHOSTUNREACH)
        self.assertEqual(vm2.ping(0x08080808)[0], -EHOSTUNREACH)  # off-net goes via the gateway, same ARP

    def test_a_stale_or_foreign_echo_reply_is_not_taken_for_the_answer(self):
        vm = IpVM()
        vm.ping(GATEWAY, seq=1)  # learn the gateway
        vm.network.silent.add('icmp')
        # A reply to another sequence number, then one from another host, then none.
        for seq, source in ((9, GATEWAY), (2, 0x0A000299)):
            body = struct.pack('>BBHHH', 0, 0, 0, 0x4C58, seq) + bytes(32)
            body = body[:2] + struct.pack('>H', checksum(body)) + body[4:]
            vm.network.deliver(ethernet(GUEST_MAC, GATEWAY_MAC, 0x0800, ipv4(source, GUEST, 1, body)))
        self.assertEqual(vm.ping(GATEWAY, seq=2, seconds=2)[0], -ETIMEDOUT)

    def test_the_stack_answers_arp_and_ping_for_others(self):
        vm = IpVM()
        vm.ping(GATEWAY)  # any operation pumps frames; the gateway pings us while we wait
        vm.network.frames.clear()
        request = struct.pack('>HHBBH', 1, 0x0800, 6, 4, 1) + GATEWAY_MAC + ip_bytes(GATEWAY) + bytes(6) + ip_bytes(GUEST)
        vm.network.deliver(ethernet(b'\xff' * 6, GATEWAY_MAC, 0x0806, request))
        body = struct.pack('>BBHHH', 8, 0, 0, 0x1234, 5) + b'payload!'
        body = body[:2] + struct.pack('>H', checksum(body)) + body[4:]
        vm.network.deliver(ethernet(GUEST_MAC, GATEWAY_MAC, 0x0800, ipv4(GATEWAY, GUEST, 1, body)))
        vm.network.silent.add('icmp')
        vm.ping(GATEWAY, seconds=2)  # waits two seconds; handles both frames meanwhile
        replies = [f for f in vm.network.frames if f[12:14] in (b'\x08\x06',)]
        arp = [f for f in replies if f[20:22] == b'\x00\x02']
        self.assertEqual(len(arp), 1)
        self.assertEqual((arp[0][:6], arp[0][22:28], arp[0][28:32]), (GATEWAY_MAC, GUEST_MAC, ip_bytes(GUEST)))
        echoes = [f for f in vm.network.frames if f[12:14] == b'\x08\x00' and f[34] == 0]
        self.assertEqual(len(echoes), 1)
        self.assertEqual(echoes[0][38:42], struct.pack('>HH', 0x1234, 5))
        self.assertEqual(echoes[0][42:50], b'payload!')
        self.assertEqual(checksum(echoes[0][34:34 + 16]), 0)
        self.assertEqual(vm.network.problems, [])

    def test_names_resolve_through_the_dns_server(self):
        vm = IpVM()
        status, address = vm.resolve('localhost')
        self.assertEqual((status, address), (0, 0x7F000001))
        self.assertEqual(vm.network.problems, [])
        udp_frames = [f for f in vm.network.frames if f[12:14] == b'\x08\x00']
        self.assertEqual(ip_bytes(DNS), udp_frames[0][30:34])
        self.assertEqual(vm.resolve('example.test')[1], 0x5DB8D822)  # the first A record
        vm.network.compress = False
        self.assertEqual(vm.resolve('Example.TEST'.lower())[0], 0)
        self.assertEqual(vm.resolve('nothing.test')[0], -ENOENT)
        self.assertEqual(vm.network.problems, [])

    def test_resolve_rejects_bad_names_before_sending_anything(self):
        vm = IpVM()
        for name in ('', 'a..b', '.a', 'bad name', 'under_score', 'x' * 64, 'a' + '.b' * 40):
            if not name:
                vm.stage('')
                status, _ = vm.ask(RESOLVE, 3)
            else:
                status, _ = vm.resolve(name)
            self.assertEqual(status, -EINVAL, name)
        self.assertEqual(vm.network.frames, [])

    def test_resolve_times_out_and_ignores_unrelated_datagrams(self):
        vm = IpVM()
        vm.network.silent.add('dns')
        self.assertEqual(vm.resolve('localhost', seconds=2)[0], -ETIMEDOUT)
        vm.network.silent.discard('dns')
        # A datagram with the wrong id or port sits ahead of the real answer.
        junk = udp(DNS, GUEST, 53, 40000, bytes(40))
        vm.network.deliver(ethernet(GUEST_MAC, GATEWAY_MAC, 0x0800, ipv4(DNS, GUEST, 17, junk)))
        self.assertEqual(vm.resolve('localhost')[1], 0x7F000001)

    def test_damaged_dns_answers_are_refused_not_followed(self):
        vm = IpVM()
        original = vm.network.udp

        def mangled(destination, body, damage):
            vm.network.udp = original
            before = len(vm.driver.queue)
            original(destination, body)
            frame = vm.driver.queue.pop()
            packet = frame[14:]
            payload = bytearray(packet[28:])
            damage(payload)
            datagram = udp(DNS, GUEST, 53, struct.unpack('>H', body[:2])[0], bytes(payload))
            vm.driver.queue.append(ethernet(GUEST_MAC, GATEWAY_MAC, 0x0800, ipv4(DNS, GUEST, 17, datagram)))

        def run(damage):
            vm.network.udp = lambda d, b: mangled(d, b, damage)
            return vm.resolve('localhost', seconds=2)[0]

        # An answer whose record length runs past the datagram, or a name that overruns.
        self.assertEqual(run(lambda p: p.__setitem__(slice(len(p) - 6, len(p) - 4), b'\xff\xff')), -EPROTO)
        self.assertEqual(run(lambda p: p.__setitem__(12, 0xFF)), -EPROTO)
        self.assertEqual(run(lambda p: p.__setitem__(slice(0, 2), b'\0\0')), -ETIMEDOUT)  # wrong id: ignored
        self.assertEqual(vm.network.problems, [])

    def test_frames_that_are_not_for_us_or_damaged_are_dropped(self):
        vm = IpVM()
        vm.ping(GATEWAY)
        for frame in (
                ethernet(GUEST_MAC, GATEWAY_MAC, 0x86DD, bytes(40)),                       # IPv6
                ethernet(GUEST_MAC, GATEWAY_MAC, 0x0800, ipv4(GATEWAY, 0x0A000210, 1, bytes(16))),  # another host
                ethernet(GUEST_MAC, GATEWAY_MAC, 0x0800, ipv4(GATEWAY, GUEST, 6, bytes(20))),       # TCP
                ethernet(GUEST_MAC, GATEWAY_MAC, 0x0800, b'\x46' + bytes(40)),             # options
                ethernet(GUEST_MAC, GATEWAY_MAC, 0x0800, bytes(14) + bytes(10)),           # truncated
        ):
            vm.network.deliver(frame)
        bad_checksum = bytearray(ipv4(GATEWAY, GUEST, 1, struct.pack('>BBHHH', 8, 0, 0, 0x4C58, 3) + bytes(8)))
        bad_checksum[10] ^= 1
        vm.network.deliver(ethernet(GUEST_MAC, GATEWAY_MAC, 0x0800, bytes(bad_checksum)))
        fragment = bytearray(ipv4(GATEWAY, GUEST, 1, bytes(16)))
        fragment[6:8] = b'\x20\x00'
        vm.network.deliver(ethernet(GUEST_MAC, GATEWAY_MAC, 0x0800, bytes(fragment)))
        vm.network.frames.clear()
        vm.network.silent.add('icmp')
        self.assertEqual(vm.ping(GATEWAY, seconds=1)[0], -ETIMEDOUT)
        self.assertEqual([f[12:14] for f in vm.network.frames], [b'\x08\x00'])  # only our own echo request went out

    def test_a_busy_wire_cannot_keep_a_request_waiting_forever(self):
        vm = IpVM()
        vm.network.silent.add('icmp')
        vm.ping(GATEWAY, seconds=1)  # fails; ARP learnt
        for _ in range(200):
            vm.network.deliver(ethernet(GUEST_MAC, GATEWAY_MAC, 0x86DD, bytes(40)))
        self.assertEqual(vm.ping(GATEWAY, seconds=30)[0], -ETIMEDOUT)
        self.assertGreater(len(vm.driver.queue), 100)  # it gave up after a bounded number of frames

    def test_link_down_and_bad_requests(self):
        vm = IpVM(link=False)
        self.assertEqual(vm.ping(GATEWAY)[0], -ENETDOWN)
        self.assertEqual(vm.ask(RESOLVE, 3)[0], -ENETDOWN)
        self.assertEqual(vm.network.frames, [])
        vm = IpVM()
        for header, size in ((INFO, 12), (PING, 32), (NAME, 16), (RESOLVE, 20), (0, 16)):
            self.assertEqual(vm.ask(header, 0, 0, (0, 0, 0, 0), size)[0], -EINVAL)
        self.assertEqual(vm.ask(PING, GATEWAY, 1)[0], -EINVAL)  # zero seconds
        self.assertEqual(vm.ask(PING, GATEWAY, (31 << 16) | 1)[0], -EINVAL)
        self.assertEqual(vm.ask(RESOLVE, 0)[0], -EINVAL)
        self.assertEqual(vm.ask(RESOLVE, 31)[0], -EINVAL)
        self.assertEqual(vm.ask(NAME, 64, 0, (0, 0, 0, 0), 32)[0], -EINVAL)
        self.assertEqual(vm.ask(NAME, 8, 0, (0, 0, 0, 0), 32)[0], -EINVAL)
        words = [INFO, 2, 0, 0]
        for i, word in enumerate(words):
            vm.memory[0x1000000 + 4 * i] = word
        vm.call('ipHandle', 0x1000000, 16, 0x1001000)
        self.assertEqual(vm.memory[0x1001004], error(EPIPE))
        self.assertEqual(vm.network.frames, [])


if __name__ == '__main__':
    unittest.main()
