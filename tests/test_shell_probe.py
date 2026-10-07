"""The shell CPU probe's script, transcript patterns and volume judgement, on a modelled run."""
import sys
import unittest

from test_kernel import LAIX
from test_shell import ShellVM, error
import probe_shell_cpu as probe

sys.path.insert(0, str(LAIX / 'tools'))
import build_shell_volume  # noqa: E402
import wfs  # noqa: E402

ELFS = {name: bytes([0x7F, 0x45, 0x4C, 0x46]) + name.encode() * 700 for name in build_shell_volume.PROGRAMS}


def modelled_run():
    original = build_shell_volume.volume(ELFS)
    outputs = {'hello': lambda n: f'Hello from a loaded program, argument {n}\n',
               'count': lambda n: ''.join(f'{i:3d}\n' for i in range(1, n + 1))}
    vm = ShellVM(original, programs={'hello': (0, 5, 0, 0), 'count': (0, 3, 0, 0)})
    real_call = vm.call

    def call(name, *args):
        if name == 'call' and args[0] == 0x32:
            result = real_call(name, *args)
            program, argument, _ = vm.exec_calls[-1]
            vm.output.append(outputs[program](argument).encode())
            return result
        return real_call(name, *args)

    vm.call = call
    vm.programs['hello'] = (0, 5, 0, 0)
    vm.programs['count'] = (0, 3, 0, 0)
    vm.output.append(b'LA/IX shell. Type help.\n')
    for command, _ in probe.COMMANDS:
        vm.type(command + '\n')
    return vm, original


class ProbeTests(unittest.TestCase):
    def test_the_script_types_every_command_and_waits_for_the_slow_ones(self):
        script, end = probe.input_script()
        events = [line.split() for line in script.splitlines()]
        ticks = [int(e[0]) for e in events]
        self.assertEqual(ticks, sorted(ticks))
        self.assertGreaterEqual(ticks[0], probe.BOOT_SECONDS * probe.CLOCK)
        downs = sum(1 for e in events if e[3] == 'down')
        self.assertEqual(downs, sum(len(c) + 1 for c, _ in probe.COMMANDS))  # every character and the Enter
        self.assertTrue(all(e[1] == 'key' and e[3] in ('down', 'up') for e in events))
        # Never more than 32 events within the time one command needs to be consumed.
        gap = probe.KEY_GAP
        self.assertTrue(all(b - a >= gap for a, b in zip(ticks, ticks[1:])))
        self.assertGreater(end, ticks[-1])

    def test_a_modelled_transcript_passes_and_the_volume_is_judged(self):
        vm, original = modelled_run()
        text = vm.text()
        position = probe.check_transcript(text)
        self.assertGreater(position, 0)
        generation = probe.judge_volume(bytes(vm.service.disk.durable), original)
        self.assertGreater(generation, 1)
        self.assertEqual(vm.exec_calls, [('hello', 5, 60), ('count', 3, 60)])

    def test_missing_or_misordered_output_and_errors_fail(self):
        vm, original = modelled_run()
        text = vm.text()
        for broken in (text.replace('exit 5', 'exit 6'), text.replace('hello disk\n', ''),
                       text + 'error: x', text.replace('LA/IX shell', 'PANIC'),
                       text.replace('Hello from a loaded program, argument 5\n', '') + 'Hello from a loaded program, argument 5\n'):
            with self.assertRaises(ValueError):
                probe.check_transcript(broken)
        durable = bytearray(vm.service.disk.durable)
        file_start = wfs.current(bytes(durable))[1]['entries']
        with self.assertRaises((ValueError, wfs.FormatError)):
            probe.judge_volume(bytes(durable[:1024]) + bytes(len(durable) - 1024), original)


class VolumeTests(unittest.TestCase):
    def test_the_shell_volume_holds_the_programs_and_room_to_write(self):
        image = build_shell_volume.volume(ELFS)
        table = wfs.current(image)[1]
        wfs.check_table(table)
        self.assertEqual(set(wfs.files(image)), {'motd', *build_shell_volume.PROGRAMS})
        self.assertEqual(wfs.files(image)['hello'], ELFS['hello'])
        used = sum(e['capacity'] for e in table['entries'] if e)
        self.assertEqual(table['sectors'] - wfs.DATA_START - used, build_shell_volume.SPARE_SECTORS)
        self.assertEqual(len(image) % 512, 0)


if __name__ == '__main__':
    unittest.main()
