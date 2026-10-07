"""Reference run of the filesystem acceptance client against the service source.

Executes tests/programs/fs/client.m and user/services/fs.m in the source
evaluator and records every committed state of the volume. The CPU probe uses
the list to judge each medium snapshot it takes: whatever a power loss would
leave must be one of these states.
"""
import sys

from test_kernel import LAIX

sys.path.insert(0, str(LAIX / 'tools'))
import build_fs_volume  # noqa: E402
import wfs  # noqa: E402


def committed_states():
    """The successive committed file sets, from the initial volume to the final one."""
    from test_fsclient import ClientVM, HANDLE
    vm = ClientVM(build_fs_volume.volume(), root='tests/programs/fs/client.m')
    states = [wfs.files(build_fs_volume.volume())]
    disk = vm.service.disk
    handle = disk.handle

    def watching(words, size):
        result = handle(words, size)
        if words[0] == vm.service_constants['DISK_SYNC_HEADER'] and not disk.pending:
            files = wfs.files(bytes(disk.durable))
            if files != states[-1]:
                states.append(files)
        return result

    from source_m import LAYOUT as C
    vm.service_constants = C
    disk.handle = watching
    code = vm.call('fcMain', HANDLE)
    assert code == 0, f'acceptance client exited {code}'
    return states
