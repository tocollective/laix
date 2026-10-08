"""Reject false CPU acceptance from incompatible images or corrupt IPC fixtures."""

import unittest
from unittest.mock import patch

import probe_ipc_request_reply_cpu as probe
from test_kernel import LAIX, check_m
import test_ipc_request_reply as source_fixture
from test_scheduler_cpu_probe import MemoryMonitor


class CPUProbeTests(unittest.TestCase):
    def fixture(self):
        vm, tokens, endpoint = source_fixture.RequestReplyTests().fixture(4)
        p = object.__new__(probe.RequestReplyProbe)
        p.m = MemoryMonitor(vm)
        p.size = vm.task_type.size
        p.offsets = {field.name: field.offset for field in vm.task_type.fields}
        p.count, p.endpoint = 4, endpoint
        p.s = dict(tasks=vm.table_base("tasks"), idleTask=vm.addresses["idleTask"],
                   currentTask=0xE000000, task__readyHead=0xE000004,
                   task__readyCount=0xE000008, taskCapacity=0xE00000C,
                   task__readyQueue=vm.table_base("readyQueue"))
        vm.memory[p.s["taskCapacity"]] = vm.globals["taskCapacity"]
        return vm, tokens, p

    def test_preflight_rejects_missing_service_symbols_wrong_endpoint_pool_and_wrong_resume(self):
        endpoint = check_m(LAIX / "src/ipc/objects.m")[0].scope["Endpoint"].type
        names = ("ipcCall", "ipcAccept", "ipcReply", "endpointBootstrapService", "endpointBootstrap",
                 "handleCopy", "taskAbortBlocked", "setPagePermissions", "objects__endpoints", "objects__bootstrapSealed")
        symbols = dict.fromkeys(names, 0x14000)
        symbols["objects__bootstrapSealed"] = symbols["objects__endpoints"] + 16 * endpoint.size
        layout = (560, {}, [probe.CODE + 4], probe.CODE + 8)
        with patch.object(probe, "scheduler_preflight", return_value=layout):
            self.assertEqual(probe.preflight(b"", symbols)[0], layout)
            for name in ("ipcCall", "ipcAccept", "ipcReply", "endpointBootstrapService"):
                missing = dict(symbols)
                del missing[name]
                with self.subTest(missing=name), self.assertRaisesRegex(ValueError, "request/reply symbols"):
                    probe.preflight(b"", missing)
            wrong = dict(symbols)
            wrong["objects__bootstrapSealed"] += 4
            with self.assertRaisesRegex(ValueError, "endpoint array"):
                probe.preflight(b"", wrong)
        with patch.object(probe, "scheduler_preflight", return_value=(560, {}, [probe.CODE + 4], probe.CODE + 12)):
            with self.assertRaisesRegex(ValueError, "self-branch"):
                probe.preflight(b"", symbols)

    def test_four_task_ready_queue_rejects_duplicates_missing_peers_and_early_wakes(self):
        vm, _, p = self.fixture()
        vm.memory[p.s["task__readyHead"]] = vm.globals["readyHead"]
        vm.memory[p.s["task__readyCount"]] = vm.globals["readyCount"]
        p.check_queue(1)
        first = p.s["task__readyQueue"] + 4 * vm.globals["readyHead"]
        vm.memory[first] = 4
        with self.assertRaisesRegex(ValueError, "duplicate"):
            p.check_queue(1)
        vm.memory[first] = 2
        vm.memory[p.address(4, "state")] = 4
        with self.assertRaisesRegex(ValueError, "queue and TCB"):
            p.check_queue(1)
        vm.memory[p.address(4, "state")] = 1
        vm.memory[p.s["task__readyCount"]] = 2
        with self.assertRaisesRegex(ValueError, "queue and TCB"):
            p.check_queue(1)

    def test_wait_guard_rejects_early_wakeup_wrong_kind_endpoint_and_wait_reason(self):
        vm, tokens, p = self.fixture()
        vm.run(2)
        vm.request(tokens[2])
        p.waiting(2, 4)
        vm.run(1)
        vm.accept(tokens[1])
        p.waiting(2, 6)
        for name, value in (("state", 1), ("ipcKind", 4), ("waitReason", 4), ("ipcEndpoint", 0)):
            address = p.address(2, name)
            previous = vm.memory[address]
            vm.memory[address] = value
            with self.subTest(field=name), self.assertRaisesRegex(ValueError, "woken early"):
                p.waiting(2, 6)
            vm.memory[address] = previous

    def test_completion_guard_rejects_pins_metadata_and_snapshots_after_reply(self):
        vm, tokens, p = self.fixture()
        vm.run(2)
        vm.request(tokens[2])
        frame = p.context(2)
        p.busy = frame[probe.C["TF_EPC"] // 4]
        p.pending_contexts = {2: dict({f"r{i}": frame[i] for i in range(32)},
                                      fcsr=frame[probe.C["TF_FCSR"] // 4])}
        p.check_pending_contexts()
        self.assertIn(2, p.pending_contexts)
        vm.run(1)
        token = vm.accept(tokens[1])
        vm.response(token)
        address = p.address(2, "context") + probe.C["TF_R3"]
        preserved = vm.memory[address]
        vm.memory[address] += 1
        with self.assertRaisesRegex(ValueError, "preserved context"):
            p.check_pending_contexts()
        vm.memory[address] = preserved
        p.check_pending_contexts()
        self.assertEqual(p.pending_contexts, {})
        p.cleared(2)
        for name in ("ipcEndpoint", "ipcKind", "ipcReplyOwner", "ipcReplyBuffer", "ipcObjectGeneration", "waitReason", "ipcMessage"):
            address = p.address(2, name)
            previous = vm.memory[address]
            vm.memory[address] = 1
            with self.subTest(field=name), self.assertRaisesRegex(ValueError, "retained"):
                p.cleared(2)
            vm.memory[address] = previous


if __name__ == "__main__":
    unittest.main()
