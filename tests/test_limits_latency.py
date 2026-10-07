"""A8 logical limits and retirement checks; these do not measure CPU timing."""
import unittest

from source_m import LAYOUT as C
from test_ipc_handles import error
import test_ipc_request_reply as rpc
from test_ipc_request_reply import CALL, ACCEPT, REPLY, GEN_MAX
import test_ipc_transport as raw
from test_runtime_tasks import fixture, create
from test_runtime_memory import space, allocate, invoke, frames


class LimitsLatencyTests(unittest.TestCase):
    def budget(self, vm, owner=1):
        typ = vm.decls['spaceBudgets'].sym.type.elem
        return vm.call('mmuBudget', vm.field('directory', owner), owner), typ

    def test_oversized_ipc_capacities_do_not_walk_user_pages_or_spend_generation(self):
        vm, tokens, endpoint = rpc.RequestReplyTests().fixture(2)
        original = vm.call
        walks = []
        def counted(name, *args):
            if name in ('mmuUserBufferValid', 'copyFromUser', 'copyToUser'):
                walks.append((name, args))
            return original(name, *args)
        vm.call = counted
        for capacity in (33, 4096, 0x7FFFFFFF, 0xFFFFFFFF):
            walks.clear()
            vm.invoke(ACCEPT, tokens[1], 0xFFFFFFFF, capacity)
            self.assertEqual(vm.result(1), (error(90), 0))
            self.assertEqual(walks, [])
            vm.run(2)
            walks.clear()
            vm.invoke(CALL, tokens[2], 0xFFFFFFFF, 0, 0xFFFFFFFF, capacity)
            self.assertEqual(vm.result(2), (error(90), 0))
            self.assertEqual(walks, [])
            self.assertEqual(vm.field('ipcCallGeneration', 2), 0)
            self.assertEqual(vm.queue(endpoint), [])
            vm.run(1)
        vm, tokens, endpoint = raw.TransportTests().fixture()
        for capacity in (33, 4096, 0xFFFFFFFF):
            vm.syscall(20, tokens[1], 0xFFFFFFFF, capacity)
            self.assertEqual(vm.result(1), (error(90), 0))
            self.assertEqual(vm.queue(endpoint, False), [])

    def test_last_reply_generation_retires_slot_and_replacement_rejects_stale_right(self):
        vm = fixture(1, endpoint=True)
        child = create(vm, token=vm.root_handle, rights=1)
        token = vm.memory[vm.field('bootPage', child) + 28]
        vm.memory[vm.field_address('ipcCallGeneration', child)] = GEN_MAX - 1
        vm.run(child)
        vm.request(token)
        vm.run(1)
        last = vm.accept(vm.root_handle)
        self.assertEqual(last, GEN_MAX << 8 | child)
        vm.response(last)
        vm.run(child)
        vm.request(token)
        self.assertEqual(vm.result(child), (error(75), 0))
        vm.invoke(C['SYS_EXIT'], 0)
        vm.run(1)
        vm.reap()
        vm.invoke(C['SYS_TASK_COLLECT'], child, 0x40001000)
        self.assertEqual(vm.result(1)[0], 0)
        replacement = create(vm, token=vm.root_handle, rights=1)
        self.assertNotEqual(replacement & 255, child & 255)
        self.assertEqual(vm.field('ipcCallGeneration', child), GEN_MAX)
        vm.run(replacement)
        fresh_handle = vm.memory[vm.field('bootPage', replacement) + 28]
        vm.request(fresh_handle)
        vm.run(1)
        fresh = vm.accept(vm.root_handle)
        vm.response(last)
        self.assertEqual(vm.result(1), (error(9), 0))
        self.assertEqual(vm.field('state', replacement), 4)
        vm.response(fresh)
        self.assertEqual(vm.result(replacement), (8, 8))

    def test_all_retired_reply_namespaces_fail_creation_without_allocating(self):
        vm = fixture(1)
        for slot in range(2, 9):
            vm.memory[vm.field_address('ipcCallGeneration', slot)] = GEN_MAX
        baseline = vm.free_pages(), vm.control_count()
        vm.invoke(C['SYS_TASK_CREATE'], 1)
        self.assertEqual(vm.result(1)[0], error(23))
        self.assertEqual((vm.free_pages(), vm.control_count()), baseline)
        for slot in range(2, 9):
            self.assertEqual(vm.field('ipcCallGeneration', slot), GEN_MAX)

    def test_mapping_quota_counts_aliases_and_recovers_on_unmap(self):
        vm = fixture(2)
        cap, region = space(vm), None
        region = allocate(vm, cap, 16)
        address, typ = self.budget(vm)
        mapped = address + typ.field('mappings').offset
        tables = address + typ.field('tables').offset
        base = C['MEM_VA_START']
        initial = vm.memory[mapped]
        count = 128 - initial
        # Repeated aliases of the same sixteen frames spend mapping capacity.
        done = 0
        while done < count:
            part = min(16, count - done)
            self.assertEqual(invoke(vm, 'SYS_MEM_MAP', cap, region, base + done * 4096, 0, part, 23), 0)
            done += part
        baseline = vm.free_pages(), vm.memory[tables], [vm.call('physicalPageReferences', p) for p in frames(vm, region)]
        self.assertEqual(invoke(vm, 'SYS_MEM_MAP', cap, region, base + count * 4096, 0, 1, 23), -12)
        self.assertEqual(vm.memory[mapped], 128)
        self.assertEqual((vm.free_pages(), vm.memory[tables], [vm.call('physicalPageReferences', p) for p in frames(vm, region)]), baseline)
        vm.run(2)
        peer = space(vm)
        self.assertGreater(invoke(vm, 'SYS_MEM_ALLOC', peer, 1), 0)
        vm.run(1)
        self.assertEqual(invoke(vm, 'SYS_MEM_UNMAP', cap, base, 1), 0)
        self.assertEqual(vm.memory[mapped], 127)
        self.assertEqual(invoke(vm, 'SYS_MEM_MAP', cap, region, base, 0, 1, 23), 0)
        self.assertEqual(vm.memory[mapped], 128)

    def test_table_quota_rejects_cross_boundary_batch_without_partial_publication(self):
        vm = fixture(1)
        cap = space(vm)
        region = allocate(vm, cap, 2)
        address, typ = self.budget(vm)
        tables = address + typ.field('tables').offset
        base = C['MEM_VA_START']
        installed = []
        while vm.memory[tables] < 7:
            va = base + len(installed) * 0x400000
            self.assertEqual(invoke(vm, 'SYS_MEM_MAP', cap, region, va, 0, 1, 23), 0)
            installed.append(va)
        cross = base + len(installed) * 0x400000 + 0x3FF000
        baseline = vm.free_pages(), vm.memory[address + typ.field('mappings').offset]
        self.assertEqual(invoke(vm, 'SYS_MEM_MAP', cap, region, cross, 0, 2, 23), -12)
        self.assertEqual(vm.memory[tables], 7)
        self.assertEqual((vm.free_pages(), vm.memory[address + typ.field('mappings').offset]), baseline)
        self.assertEqual(vm.call('mmuUserLeaf', vm.field('directory'), 1, cross), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_MAP', cap, region, cross, 0, 1, 23), 0)
        self.assertEqual(vm.memory[tables], 8)
        self.assertEqual(invoke(vm, 'SYS_MEM_MAP', cap, region, cross + 4096, 1, 1, 23), -12)
        self.assertEqual(invoke(vm, 'SYS_MEM_UNMAP', cap, cross, 1), 0)
        self.assertEqual(vm.memory[tables], 7)

    def test_memory_row_last_generation_is_issued_once_then_retires(self):
        vm = fixture(2)
        for variable, count in (('memorySpaces', 32), ('memoryRegions', 64), ('memoryGrants', 64)):
            typ = vm.decls[variable].sym.type.elem
            for i in range(count):
                vm.memory[vm.addresses[variable] + i * typ.size + typ.field('generation').offset] = GEN_MAX
            vm.memory[vm.addresses[variable] + typ.field('generation').offset] = GEN_MAX - 1
        cap = space(vm, rights=47)
        self.assertEqual(cap, GEN_MAX << 8 | 1)
        region = allocate(vm, cap, 1)
        self.assertEqual(region, GEN_MAX << 8 | 1)
        grant = invoke(vm, 'SYS_MEM_GRANT', cap, region, 2, 23)
        self.assertEqual(grant, GEN_MAX << 8 | 1)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT_CLOSE', grant), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_GRANT', cap, region, 2, 23), -23)
        self.assertEqual(invoke(vm, 'SYS_MEM_RELEASE', cap, region), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_ALLOC', cap, 1), -23)
        self.assertEqual(invoke(vm, 'SYS_MEM_CLOSE', cap), 0)
        self.assertEqual(invoke(vm, 'SYS_MEM_SPACE', 0, 15), -23)
        for variable in ('memorySpaces', 'memoryRegions', 'memoryGrants'):
            typ = vm.decls[variable].sym.type.elem
            self.assertEqual(vm.memory[vm.addresses[variable] + typ.field('generation').offset], GEN_MAX)

    def test_device_operation_and_extent_last_generation_complete_without_wrap(self):
        from test_screen_services import kernel_fixture
        from test_device_boundary import record
        from test_service_recovery import fixture as manager_fixture
        vm = kernel_fixture()
        record(vm, 'deviceOperation', 'instance', 0x7FFFFFFE)
        token = vm.call('deviceSubmit', 2, 0, 16, 1, 0)
        self.assertEqual(token, 0x7FFFFFFF)
        vm.memory.complete()
        self.assertEqual(vm.call('deviceFinish', 2, token, 0x40001000), 16)
        self.assertEqual(vm.call('deviceSubmit', 2, 0, 16, 1, 0), error(75))
        self.assertEqual(vm.call('deviceFinish', 2, token, 0x40001000), error(22))
        self.assertEqual(record(vm, 'deviceOperation', 'instance'), 0x7FFFFFFF)
        self.assertEqual(vm.globals['deviceBounce'], 0)
        vm = manager_fixture(devices=True)
        child = create(vm, configure=False, publish=False)
        self.assertGreater(vm.call('taskRuntimeDevices', child, C['DEVICE_DISK']), 0)
        record(vm, 'deviceExtent', 'generation', 0x7FFFFFFE)
        self.assertEqual(vm.call('taskRuntimeExtent', child, 512, 96, 0), 0)
        self.assertEqual(vm.call('taskRuntimeExtent', child, 0, 16, 0), error(75))
        self.assertEqual(record(vm, 'deviceExtent', 'generation'), 0x7FFFFFFF)
        self.assertEqual(vm.call('diskInfo', child), 96)

    def test_seven_callers_fifo_and_unresponsive_client_do_not_block_server(self):
        vm, tokens, endpoint = rpc.RequestReplyTests().fixture(8)
        for caller in range(2, 9):
            vm.run(caller)
            vm.request(tokens[caller], bytes([caller]) * 32)
        self.assertEqual(vm.queue(endpoint), list(range(2, 9)))
        vm.run(1)
        held = vm.accept(tokens[1])
        self.assertEqual(held & 255, 2)
        for caller in range(3, 9):
            reply = vm.accept(tokens[1])
            self.assertEqual(reply & 255, caller)
            vm.response(reply, bytes([caller]) * 32)
            self.assertEqual(vm.result(caller), (32, 32))
        self.assertEqual(vm.field('state', 2), 4)
        self.assertEqual(vm.queue(endpoint), [])
        vm.response(held, b'done')
        self.assertEqual(vm.object_value(endpoint, 'references'), 8)


if __name__ == '__main__':
    unittest.main()
