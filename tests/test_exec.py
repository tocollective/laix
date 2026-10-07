"""Exec (user/services/exec.m) against the real filesystem service and a fake kernel."""
import struct
import sys
import unittest

from source_m import SourceM, LAYOUT as C
from test_fs import FsVM, DISK, content, wfs
from test_ipc_handles import error
from test_kernel import LAIX

REQ, RES, IMAGE, EVENT = 0x1000000, 0x1001000, 0x3000000, 0x3100000
FS, CONSOLE = 0x31, 0x32
HEADER = C['EXEC_RUN_HEADER']
ENOENT, EINVAL, EPIPE, ETIMEDOUT, ENFILE, EAGAIN = 2, 22, 32, 110, 23, 11
TASK_EVENT_FAULT = 1


class ExecVM(SourceM):
    """exec.m; the filesystem calls reach a real fs.m, kernel calls are scripted."""

    def __init__(self, image, polls=2, code=7, flags=0, cause=0, load=None, configure=0, publish=0):
        super().__init__(LAIX / 'user/services/exec.m')
        self.service = FsVM(image)
        self.polls, self.code, self.flags, self.cause = polls, code, flags, cause
        self.load, self.configure, self.publish = load, configure, publish
        self.log = []
        self.remaining = polls
        self.globals['execImage'] = IMAGE
        self.globals['execFs'] = FS
        self.globals['execConsole'] = CONSOLE

    def call(self, name, *args):
        if name == 'call':
            handle, request, size, response, capacity = args
            assert handle == FS and capacity == 32
            for i in range((size + 3) // 4):
                self.service.memory[0x1000000 + 4 * i] = self.memory[request + 4 * i]
            self.service.call('fsHandle', 0x1000000, size, 0x1001000, DISK)
            for i in range(8):
                self.memory[response + 4 * i] = self.service.memory[0x1001000 + 4 * i]
            return 32
        if name == 'loadTask':
            image, size = args
            self.log.append(('load', size, bytes(self.memory.get(image + i, 0) for i in range(size))))
            return 0x105 if self.load is None else self.load
        if name == 'configureTask':
            self.log.append(('configure',) + args)
            return self.configure
        if name == 'publishTask':
            self.log.append(('publish',) + args)
            return self.publish
        if name == 'collectTask':
            reference, event = args
            self.log.append(('collect', reference))
            if self.remaining:
                self.remaining -= 1
                return error(EAGAIN)
            for i, word in enumerate([reference, 5, 3, self.code & 0xFFFFFFFF, self.flags, self.cause, 0, 0, 0, 0, 0]):
                self.memory[event + 4 * i] = word
            return 0
        if name == 'terminateTask':
            self.log.append(('terminate',) + args)
            self.remaining = 0
            return 0
        if name in ('yield', 'sleep'):
            self.log.append((name,) + args)
            return 0
        return super().call(name, *args)

    def run(self, name, argument=0, limit=0, header=HEADER, generation=1, size=32):
        raw = name.encode().ljust(16, b'\0')
        words = [header, generation, argument, limit, *struct.unpack('<4I', raw)]
        for i, word in enumerate(words):
            self.memory[REQ + 4 * i] = word
        self.call('execHandle', REQ, size, RES)
        out = [self.memory[RES + 4 * i] for i in range(8)]
        status = out[1] - (1 << 32) if out[1] >= 1 << 31 else out[1]
        return status, out

    def kinds(self):
        return [entry[0] for entry in self.log]


def volume(**files):
    return wfs.mkfs(24, files)


class ExecTests(unittest.TestCase):
    def test_a_program_is_read_loaded_configured_published_and_waited_for(self):
        program = content(5, 1300)
        vm = ExecVM(volume(prog=program))
        status, out = vm.run('prog', argument=0xBEEF)
        self.assertEqual((status, out[3], out[4]), (0, 7, 0))
        self.assertEqual(vm.log[0], ('load', 1300, program))
        self.assertEqual(vm.log[1], ('configure', 0x105, CONSOLE, C['RIGHT_SEND'], 0xBEEF))
        self.assertEqual(vm.log[2], ('publish', 0x105))
        self.assertEqual(vm.kinds().count('collect'), 3)  # two polls, then the result
        self.assertEqual(vm.kinds().count('yield'), 2)
        self.assertEqual(out[0], C['EXEC_RESPONSE_HEADER'])

    def test_a_fault_is_reported_with_its_cause(self):
        vm = ExecVM(volume(prog=content(1, 100)), flags=TASK_EVENT_FAULT, cause=3, code=0)
        status, out = vm.run('prog')
        self.assertEqual((status, out[3], out[4], out[5]), (0, 0, 1, 3))

    def test_negative_exit_codes_pass_through(self):
        vm = ExecVM(volume(prog=content(1, 100)), code=-9)
        status, out = vm.run('prog')
        self.assertEqual((status, out[3]), (0, 0xFFFFFFF7))

    def test_missing_file_or_bad_name_loads_nothing(self):
        vm = ExecVM(volume(prog=content(1, 100)))
        self.assertEqual(vm.run('absent')[0], -ENOENT)
        self.assertEqual(vm.run('')[0], -ENOENT if False else -EINVAL)
        self.assertEqual(vm.run('bad name')[0], -EINVAL)
        self.assertEqual(vm.log, [])

    def test_an_image_over_the_load_limit_or_empty_is_refused_before_loading(self):
        vm = ExecVM(wfs.mkfs(300, {'big': (bytes(65537), 0), 'empty': (b'', 1)}))
        self.assertEqual(vm.run('big')[0], -EINVAL)
        self.assertEqual(vm.run('empty')[0], -EINVAL)
        self.assertEqual(vm.log, [])

    def test_kernel_refusals_are_reported_and_leave_nothing_running(self):
        vm = ExecVM(volume(prog=content(1, 100)), load=error(ENFILE))
        self.assertEqual(vm.run('prog')[0], -ENFILE)
        self.assertEqual(vm.kinds(), ['load'])
        vm = ExecVM(volume(prog=content(1, 100)), load=error(EINVAL))
        self.assertEqual(vm.run('prog')[0], -EINVAL)
        for failing in ('configure', 'publish'):
            vm = ExecVM(volume(prog=content(1, 100)), **{failing: error(EINVAL)})
            self.assertEqual(vm.run('prog')[0], -EINVAL)
            self.assertIn('terminate', vm.kinds())
            self.assertEqual(vm.kinds()[-1], 'collect')  # the quota row is released
            self.assertNotIn('yield', vm.kinds()[:vm.kinds().index('terminate')])

    def test_a_program_that_runs_too_long_is_terminated(self):
        vm = ExecVM(volume(prog=content(1, 100)), polls=10 ** 6)
        vm.remaining = 3000  # past the quick polls, then two seconds of sleeping
        status, out = vm.run('prog', limit=2)
        self.assertEqual(status, -ETIMEDOUT)
        self.assertEqual(out[3], 0)
        self.assertEqual(vm.kinds().count('yield'), 2001)  # the quick polls and one after the kill
        self.assertEqual(vm.kinds().count('sleep'), 2)
        self.assertEqual(vm.kinds().count('terminate'), 1)
        self.assertEqual(vm.log[vm.kinds().index('terminate')][2], 0xFFFFFFFF)

    def test_no_limit_means_it_keeps_waiting(self):
        vm = ExecVM(volume(prog=content(1, 100)))
        vm.remaining = 2600
        status, out = vm.run('prog', limit=0)
        self.assertEqual((status, out[3]), (0, 7))
        self.assertNotIn('terminate', vm.kinds())
        self.assertEqual(vm.kinds().count('sleep'), 600)

    def test_malformed_and_stale_requests_never_touch_the_filesystem(self):
        vm = ExecVM(volume(prog=content(1, 100)))
        for size, header, generation, expected in ((16, HEADER, 1, EINVAL), (31, HEADER, 1, EINVAL),
                                                   (32, 0, 1, EINVAL), (32, HEADER, 2, EPIPE)):
            self.assertEqual(vm.run('prog', header=header, generation=generation, size=size)[0], -expected)
        self.assertEqual(vm.log, [])
        self.assertEqual(vm.service.disk.counts, {})

    def test_the_client_library_round_trips_against_exec(self):
        client = SourceM(LAIX / 'user/services/execclient.m')
        vm = ExecVM(volume(prog=content(1, 100)))

        def call(name, *args):
            if name == 'call':
                handle, request, size, response, capacity = args
                for i in range(8):
                    vm.memory[REQ + 4 * i] = client.memory[request + 4 * i]
                vm.call('execHandle', REQ, size, RES)
                for i in range(8):
                    client.memory[response + 4 * i] = vm.memory[RES + 4 * i]
                return 32
            return SourceM.call(client, name, *args)

        client.call = call
        for i, byte in enumerate(b'prog\0'):
            client.memory[0x2000000 + i] = byte
        self.assertEqual(client.call('execRun', 9, 0x2000000, 42, 5, 0x2100000), 0)
        self.assertEqual((client.memory[0x2100000], client.memory[0x2100004], client.memory[0x2100008]), (7, 0, 0))
        self.assertEqual(vm.log[1][4], 42)
        for i, byte in enumerate(b'missing\0'):
            client.memory[0x2000000 + i] = byte
        self.assertEqual(client.call('execRun', 9, 0x2000000, 0, 0, 0x2100000), error(ENOENT))
        for i, byte in enumerate(b'x' * 17 + b'\0'):
            client.memory[0x2000000 + i] = byte
        self.assertEqual(client.call('execRun', 9, 0x2000000, 0, 0, 0x2100000), error(EINVAL))
        self.assertEqual(client.call('execRun', 9, 0, 0, 0, 0x2100000), error(EINVAL))


if __name__ == '__main__':
    unittest.main()
