"""The shell (user/apps/shell.m) with scripted keystrokes, the real filesystem service and a scripted Exec."""
import struct
import sys
import unittest

from source_m import SourceM, LAYOUT as C
from test_fs import FsVM, DISK, content, wfs
from test_ipc_handles import error
from test_kernel import LAIX
from mlang import syntax

FS, EXEC, CONSOLE = 0x31, 0x32, 0x33
EVENTS = 0x2000000
STRINGS = 0x2200000


class Finished(Exception):
    pass


def hid_table():
    """US layout, from the USB HID usage tables: character -> (usage, shifted)."""
    table = {}
    for i, letter in enumerate('abcdefghijklmnopqrstuvwxyz'):
        table[letter] = (4 + i, False)
        table[letter.upper()] = (4 + i, True)
    for i, digit in enumerate('1234567890'):
        table[digit] = (30 + i, False)
    for i, symbol in enumerate('!@#$%^&*()'):
        table[symbol] = (30 + i, True)
    plain, shifted = "-=[]\\#;'`,./", '_+{}|~:"~<>?'
    for i, (a, b) in enumerate(zip(plain, shifted)):
        table.setdefault(a, (45 + i, False))
        table.setdefault(b, (45 + i, True))
    table[' '] = (44, False)
    table['\n'] = (40, False)
    table['\b'] = (42, False)
    return table


HID = hid_table()


def keystrokes(text):
    events = []
    for char in text:
        usage, shifted = HID[char]
        if shifted:
            events.append(0xE1)
        events += [usage, usage | 0x80000000]
        if shifted:
            events.append(0xE1 | 0x80000000)
    return events


class ShellVM(SourceM):
    def __init__(self, image, writable=True, programs=None, exec_script=None):
        super().__init__(LAIX / 'user/apps/shell.m')
        self.service = FsVM(image, writable=writable)
        self.keys = []
        self.output = []
        self.console_writes = []
        self.exec_calls = []
        self.programs = programs or {}
        self.strings = {}
        self.next_string = STRINGS
        self.globals['shellFs'] = FS
        self.globals['shellExec'] = EXEC
        self.call('textStart', CONSOLE)

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
        if name == 'inputRead':
            destination, capacity = args
            batch, self.keys = self.keys[:32], self.keys[32:]
            self.memory[destination] = len(batch)
            self.memory[destination + 4] = 0
            for i, event in enumerate(batch):
                self.memory[destination + 8 + 4 * i] = event
            return 136
        if name == 'yield':
            if not self.keys:
                raise Finished()
            return 0
        if name == 'consoleWrite':
            handle, text, length = args
            assert handle == CONSOLE and 0 < length <= 28
            data = bytes(self.memory[text + i] for i in range(length))
            self.console_writes.append(data)
            self.output.append(data)
            return length
        if name == 'call':
            handle, request, size, response, capacity = args
            if handle == FS:
                for i in range((size + 3) // 4):
                    self.service.memory[0x1000000 + 4 * i] = self.memory[request + 4 * i]
                self.service.call('fsHandle', 0x1000000, size, 0x1001000, DISK)
                for i in range(8):
                    self.memory[response + 4 * i] = self.service.memory[0x1001000 + 4 * i]
                return 32
            assert handle == EXEC and size == 32
            words = [self.memory[request + 4 * i] for i in range(8)]
            name_bytes = struct.pack('<4I', *words[4:8]).rstrip(b'\0').decode()
            self.exec_calls.append((name_bytes, words[2], words[3]))
            status, code, faulted, cause = self.programs.get(name_bytes, (error(2), 0, 0, 0))
            out = [C['EXEC_RESPONSE_HEADER'], status & 0xFFFFFFFF, 1, code & 0xFFFFFFFF, faulted, cause, 0, 0]
            if status != 0:
                out[3] = out[4] = out[5] = 0
            for i, word in enumerate(out):
                self.memory[response + 4 * i] = word
            return 32
        return super().call(name, *args)

    def type(self, text):
        self.keys += keystrokes(text)
        try:
            self.call('shellLoop')
        except Finished:
            pass

    def text(self):
        return b''.join(self.output).decode()


def volume(**files):
    return wfs.mkfs(40, files)


def shell(**files):
    return ShellVM(volume(**files))


class ShellTests(unittest.TestCase):
    def test_help_lists_the_commands(self):
        vm = shell()
        vm.type('help\n')
        text = vm.text()
        self.assertTrue(text.startswith('$ '))
        for word in ('ls', 'cat NAME', 'write NAME TEXT', 'cp FROM TO', 'mv FROM TO', 'rm NAME', 'df', 'sync', 'echo TEXT', 'run NAME'):
            self.assertIn(word, text)

    def test_ls_and_cat(self):
        vm = shell(motd=b'hello\n', blob=bytes([65, 0, 66, 200, 10, 67]), tail=b'no newline')
        vm.type('ls\ncat motd\ncat blob\ncat tail\ncat missing\n')
        out = vm.text()
        self.assertIn('       6  motd\n', out)
        self.assertIn('       6  blob\n', out)
        self.assertIn('      10  tail\n', out)
        self.assertIn('hello\n', out)
        self.assertIn('A.B.\nC\n', out)  # bytes that cannot be shown become dots
        self.assertIn('no newline\n', out)
        self.assertIn('error: no such file\n', out)

    def test_write_cat_cp_mv_rm_reach_the_medium_atomically(self):
        vm = shell(old=b'old\n')
        vm.type('write note two words here\n')
        self.assertEqual(wfs.files(vm.service.disk.durable), {'old': b'old\n', 'note': b'two words here\n'})
        vm.type('cp note copy\n')
        self.assertEqual(wfs.files(vm.service.disk.durable)['copy'], b'two words here\n')
        vm.type('mv copy moved\n')
        self.assertEqual(set(wfs.files(vm.service.disk.durable)), {'old', 'note', 'moved'})
        # The move was one commit: the medium never showed both names or neither.
        vm.type('rm note\n')
        self.assertEqual(set(wfs.files(vm.service.disk.durable)), {'old', 'moved'})
        vm.type('write moved replaced\ncat moved\n')
        self.assertEqual(wfs.files(vm.service.disk.durable)['moved'], b'replaced\n')
        self.assertTrue(vm.text().rstrip().endswith('replaced\n$'))
        self.assertEqual(vm.service.disk.pending, [])

    def test_a_move_is_a_single_commit(self):
        vm = shell(a=b'data')
        before = vm.service.disk.events
        generation = wfs.current(bytes(vm.service.disk.durable))[1]['generation']
        vm.type('mv a b\n')
        self.assertEqual(wfs.current(bytes(vm.service.disk.durable))[1]['generation'], generation + 1)
        self.assertEqual(wfs.files(vm.service.disk.durable), {'b': b'data'})

    def test_same_file_and_missing_source_are_refused(self):
        vm = shell(a=b'data')
        vm.type('mv a a\ncp a a\ncp nothing x\nmv nothing x\n')
        self.assertEqual(vm.text().count('error: same file'), 2)
        self.assertEqual(vm.text().count('error: no such file'), 2)
        self.assertEqual(wfs.files(vm.service.disk.durable), {'a': b'data'})

    def test_df_echo_and_sync(self):
        vm = shell(a=b'x')
        vm.type('df\necho  hello   there\necho\nsync\n')
        out = vm.text()
        self.assertIn('sectors 36  free 35  generation 1\n', out)
        self.assertIn('hello there\n', out)
        vm.type('write b data\ndf\n')
        self.assertIn('generation 2', vm.text())

    def test_line_editing_and_shift(self):
        vm = shell(Motd=b'x\n')
        vm.type('lx\bs\n')
        self.assertIn('Motd', vm.text())
        self.assertIn('\r$ l \r$ l', vm.text())  # the redraw after the erase
        vm.type('ca\b\b\bcat Motd\n')
        self.assertIn('x\n', vm.text())
        vm.type('\b\b\bls\n')  # erasing an empty line does nothing
        vm = shell()
        vm.type('write F1 A!@#$%^&*()_+{}|:"<>?~\ncat F1\n')
        self.assertIn('A!@#$%^&*()_+{}|:"<>?~', vm.text())

    def test_programs_run_through_exec(self):
        vm = ShellVM(volume(), programs={'hello': (0, 7, 0, 0), 'crash': (0, 0, 1, 3), 'slow': (error(110), 0, 0, 0),
                                         'big': (error(23), 0, 0, 0)})
        vm.type('run hello\nhello 41\nrun crash\nrun slow\nbig\nnothing\nrun nothing 3\n')
        self.assertEqual(vm.exec_calls, [('hello', 0, 60), ('hello', 41, 60), ('crash', 0, 60), ('slow', 0, 60),
                                         ('big', 0, 60), ('nothing', 0, 60), ('nothing', 3, 60)])
        out = vm.text()
        self.assertEqual(out.count('exit 7\n'), 2)
        self.assertIn('fault, cause 3\n', out)
        self.assertIn('error: timed out\n', out)
        self.assertIn('error: no room to run\n', out)
        self.assertEqual(out.count('nothing: not found\n'), 2)

    def test_negative_exit_codes_are_shown_signed(self):
        vm = ShellVM(volume(), programs={'neg': (0, -9, 0, 0)})
        vm.type('neg\n')
        self.assertIn('exit -9\n', vm.text())

    def test_bad_arguments_get_a_usage_line_and_nothing_runs(self):
        vm = shell(a=b'x')
        vm.type('cat\ncat a b\nwrite a\nwrite\nrm\nrm a b\ncp a\nmv\nrun\nprog xyz\nprog 12345678901\n')
        out = vm.text()
        for usage in ('cat NAME', 'write NAME TEXT', 'rm NAME', 'cp FROM TO', 'mv FROM TO', 'run NAME [N]', 'NAME [N]'):
            self.assertIn('usage: ' + usage, out)
        self.assertEqual(vm.exec_calls, [])
        self.assertEqual(wfs.files(vm.service.disk.durable), {'a': b'x'})

    def test_a_read_only_volume_is_reported_not_changed(self):
        image = volume(a=b'x')
        vm = ShellVM(image, writable=False)
        vm.type('write b hello\nrm a\ncp a c\ndf\ncat a\n')
        out = vm.text()
        self.assertEqual(out.count('error: read-only'), 3)
        self.assertIn('read-only\n', out)
        self.assertEqual(vm.service.disk.events, 0)

    def test_no_filesystem_is_reported(self):
        vm = ShellVM(bytes(40 * 512))
        vm.type('ls\ndf\n')
        self.assertEqual(vm.text().count('error: no filesystem'), 2)

    def test_console_traffic_is_legal_and_in_small_pieces(self):
        vm = shell(a=b'x' * 100)
        vm.type('help\nls\ncat a\n' + 'z' * 99 + '\n')
        self.assertTrue(vm.console_writes)
        for piece in vm.console_writes:
            self.assertLessEqual(len(piece), 28)
            self.assertTrue(all(b in (9, 10, 13) or 32 <= b < 127 for b in piece), piece)

    def test_a_long_line_is_capped_and_overflow_keys_are_dropped(self):
        vm = shell()
        vm.type('echo ' + 'q' * 200 + '\n')
        line = [l for l in vm.text().split('\n') if l.startswith('q')]
        self.assertEqual(len(line[0]), 94)  # 99 characters fit on the line, five are "echo "

    def test_key_events_are_batched_and_releases_ignored(self):
        vm = shell(a=b'x\n')
        # 33 events in one go: more than one batch of 32.
        vm.type('cat a\n' * 3)
        self.assertEqual(vm.text().count('x\n'), 3)


if __name__ == '__main__':
    unittest.main()
