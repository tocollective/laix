"""Trusted init, resource isolation and rollback from checked sources.

Image words and addresses are fixtures, not generated instructions. Execution
of the embedded user images is verified separately by the CPU boot probe.
"""

import re
import unittest

from test_kernel import LAIX, check_m
from test_task import TaskEntered, PAGE, USER_DATA, USER_CODE
from test_ipc_request_reply import ServiceM
from test_ipc_handles import error
from source_m import LAYOUT

START = 0x40002000


class InitM(ServiceM):
    def __init__(self):
        super().__init__(root=LAIX / "src/kernel/main.m")
        self.addresses.update(bootstrapServerStart=0x14200, bootstrapServerEnd=0x14210,
                              bootstrapClientStart=0x14300, bootstrapClientEnd=0x1430C)
        self.server_blob, self.client_blob = [0x1100 + i for i in range(4)], [0x2200 + i for i in range(3)]
        for name, blob in (("bootstrapServerStart", self.server_blob), ("bootstrapClientStart", self.client_blob)):
            for i, word in enumerate(blob):
                self.memory[self.addresses[name] + 4 * i] = word

    def start_record(self, id):
        return self.field("bootPage", id)

    def token(self, id):
        return self.memory[self.start_record(id) + 20]

    def start(self):
        assert self.call("bootstrapInit")
        with unittest.TestCase().assertRaises(TaskEntered):
            self.call("taskStart", 1000000)


class BootstrapTests(unittest.TestCase):
    def test_start_record_field_offsets_and_size_match_assembly_abi(self):
        typ = check_m(LAIX / "src/task/start.m")[0].scope["TaskStart"].type
        self.assertEqual(typ.size, LAYOUT["START_BLOCK_BYTES"])
        for field in typ.fields:
            # M uses camelCase, the assembly constants use uppercase words.
            name = re.sub(r"([a-z])([A-Z])", r"\1_\2", field.name).upper()
            self.assertEqual(field.offset, LAYOUT["START_FIELD_" + name])

    def test_distinct_images_ro_start_blocks_and_exact_resource_grants(self):
        vm = InitM()
        self.assertTrue(vm.call("bootstrapInit"))
        roots, resources = [], []
        for id, blob, rights, devices, role in ((1, vm.server_blob, 2, 1, 1), (2, vm.client_blob, 1, 0, 2)):
            root = vm.field("directory", id)
            roots.append(root)
            code, data, stack = vm.pages(id)
            boot = vm.start_record(id)
            self.assertEqual([vm.memory[code + 4 * i] for i in range(len(blob))], blob)
            self.assertEqual(vm.leaf(USER_CODE, root) & 31, 27)
            self.assertEqual(vm.leaf(USER_DATA, root) & 31, 23)
            self.assertEqual(vm.leaf(START, root), boot | 19)  # RO, U, NX
            self.assertFalse(vm.call("mmuUserBufferValid", root, id, START, 48, 4))
            self.assertTrue(vm.call("mmuUserBufferValid", root, id, START, 48, 2))
            self.assertEqual(vm.leaf(LAYOUT["UART_BASE"], root) & 16, 0)
            resources.extend([root, code, data, stack, boot, vm.field("kernelStackBottom", id)])
            self.assertEqual(vm.field("deviceRights", id), devices)
            token = vm.token(id)
            entry = vm.call("handleEntry", vm.field_address("handles", id), token)
            handle_type = vm.decls["tasks"].sym.type.elem.field("handles").type.field("entries").type.elem
            self.assertEqual(vm.memory[entry + handle_type.field("rights").offset], rights)
            for forbidden in (7, 4, 1 if id == 1 else 2):
                self.assertEqual(vm.call("handleLookup", vm.field_address("handles", id), token, forbidden), 0)
            self.assertEqual([vm.memory[boot + 4 * i] for i in range(12)],
                             [LAYOUT["START_MAGIC"], 1, 48, role, id, token, rights, devices,
                              USER_DATA, PAGE, 32, LAYOUT["START_PROTOCOL_CONSOLE"]])
            context = vm.field_address("context", id)
            self.assertEqual([vm.memory[context + 4 * i] for i in (1, 2, 3, 4)], [START, 48, 0, 0])
            self.assertEqual(context % 8, 0)
            self.assertEqual(vm.field("state", id), 1)
        self.assertEqual(len(set(resources)), len(resources))
        self.assertNotEqual(*roots)
        endpoint = vm.endpoint(vm.token(1))
        self.assertEqual(vm.object_value(endpoint, "mode"), 1)
        self.assertEqual(vm.object_value(endpoint, "references"), 2)
        self.assertEqual(vm.object_value(endpoint, "receiveReferences"), 1)
        self.assertFalse(vm.call("bootstrapInit"))

    def test_receive_only_server_blocks_client_calls_and_uart_is_denied_to_client(self):
        vm = InitM()
        vm.start()
        server, client = vm.token(1), vm.token(2)
        vm.invoke(22, server, USER_DATA, 32)
        self.assertEqual(vm.current(), 2)
        self.assertEqual(vm.field("state", 1), 4)
        vm.memory[LAYOUT["UART_BASE"]] = 0x99
        vm.invoke(0, 65)
        self.assertEqual(vm.result(2)[0], error(1))
        self.assertEqual(vm.memory[LAYOUT["UART_BASE"]], 0x99)
        vm.invoke(22, client, USER_DATA, 32)
        self.assertEqual(vm.result(2)[0], error(1))
        vm.request(client, b"B", response=USER_DATA + 32, capacity=4)
        self.assertEqual(vm.current(), 1)
        self.assertEqual(vm.field("state", 2), 4)
        reply_token = vm.result(1)[1]
        self.assertEqual(vm.read_bytes(vm.pages(1)[1], 1), b"B")
        vm.invoke(0, 66)
        self.assertEqual(vm.result(1)[0], 0)
        self.assertEqual(vm.memory[LAYOUT["UART_BASE"]], 66)
        vm.invoke(18, server)
        self.assertEqual(vm.result(1)[0], error(1))
        vm.response(reply_token, b"\0\0\0\0")
        self.assertEqual(vm.result(2)[0], 4)
        self.assertEqual(vm.read_bytes(vm.pages(2)[1] + 32, 4), b"\0\0\0\0")
        # Receiver lifetime is enough to revoke the endpoint; no manage right.
        vm.invoke(16, server)
        vm.run(2)
        vm.request(client)
        self.assertEqual(vm.result(2)[0], error(32))

    def test_bad_start_fields_stale_tokens_and_excess_rights_never_allocate(self):
        invalid = [("magic", 0), ("version", 2), ("bytes", 44), ("role", 3), ("taskId", 2),
                   ("endpoint", 0), ("endpoint", 0x7FFF01), ("rights", 7), ("rights", 1),
                   ("devices", 0), ("devices", 2), ("data", USER_DATA + 4),
                   ("dataBytes", PAGE + 1), ("ipcLimit", 33), ("protocol", 3)]
        for name, value in invalid:
            with self.subTest(field=name, value=value):
                vm = InitM()
                self.assertEqual(vm.call("taskCreateImage", 0x14200, 0x14210, 0), 1)
                root = vm.call("endpointBootstrapService", vm.field_address("handles"), 1)
                token = vm.call("handleCopy", vm.field_address("handles"), root,
                                vm.field_address("handles"), 1, 1, 2)
                # Build a valid template using init itself, then discard its
                # page and validate the independent local record fixture.
                self.assertTrue(vm.call("bootstrapInstall", 1, 0, token))
                typ = vm.decls["taskStartBlockValid"].sym.type.params[0].target
                block = 0x0E000000
                for i in range(0, 48, 4):
                    vm.memory[block + i] = vm.memory[vm.start_record(1) + i]
                self.assertTrue(vm.call("unmapPage", vm.field("directory"), 1, START))
                self.assertTrue(vm.call("freePage", vm.start_record(1), 1, 5))
                vm.memory[vm.field_address("bootPage")] = 0
                vm.memory[vm.field_address("deviceRights")] = 0
                vm.memory[block + typ.field(name).offset] = value
                before = vm.free_pages()
                self.assertFalse(vm.call("taskInstallStart", 1, block))
                self.assertEqual(vm.free_pages(), before)
                self.assertEqual(vm.field("bootPage"), 0)
                self.assertEqual(vm.field("deviceRights"), 0)
        # A valid receive block may not conceal send/manage in its handle.
        vm = InitM()
        vm.call("taskCreateImage", 0x14200, 0x14210, 0)
        token = vm.call("endpointBootstrapService", vm.field_address("handles"), 1)
        self.assertFalse(vm.call("bootstrapInstall", 1, 0, token))

    def test_every_bootstrap_mapping_failure_rolls_back_both_tasks_and_can_retry(self):
        for failure in range(1, 9):
            with self.subTest(mapping=failure):
                vm = InitM()
                before = vm.free_pages()
                vm.fail_mapping = failure
                self.assertFalse(vm.call("bootstrapInit"))
                self.assertEqual(vm.free_pages(), before)
                self.assertEqual(vm.globals["readyCount"], 0)
                for id in (1, 2):
                    for field in ("state", "directory", "kernelStackBottom", "bootPage", "deviceRights"):
                        self.assertEqual(vm.field(field, id), 0, field)
                    self.assertEqual(vm.pages(id), [0, 0, 0])
                for address in before:
                    self.assertEqual(vm.leaf(address) & 31, 7)
                vm.fail_mapping = None
                self.assertTrue(vm.call("bootstrapInit"))

    def test_bad_embedded_image_and_enabled_irqs_leave_no_resources(self):
        for start, end, offset in ((0, 4, 0), (0x14200, 0x14200, 0), (0x14200, 0x14203, 0),
                                  (0x14200, 0x15204, 0), (0x14200, 0x14210, 16),
                                  (0x14200, 0x14210, 1), (0xFFFFFFFF, 4, 0)):
            vm = InitM()
            before = vm.free_pages()
            self.assertEqual(vm.call("taskCreateImage", start, end, offset), 0)
            self.assertEqual(vm.free_pages(), before)
        vm = InitM()
        vm.controls[0] = 1
        self.assertFalse(vm.call("bootstrapInit"))

    def test_every_init_oom_rolls_back_unpublished_tasks_including_start_pages(self):
        # Two roots, six image/data/stack pages, six kernel stack/guard pages,
        # four user tables and two start pages: twenty free pages for init.
        for count in range(20):
            with self.subTest(free_pages=count):
                vm = InitM()
                held = []
                while address := vm.call("allocPage", 9, 2):
                    held.append(address)
                for address in held[:count]:
                    self.assertTrue(vm.call("freePage", address, 9, 2))
                before = vm.free_pages()
                self.assertFalse(vm.call("bootstrapInit"))
                self.assertEqual(vm.free_pages(), before)
                self.assertEqual(vm.globals["readyCount"], 0)
                self.assertTrue(vm.call("taskInitAvailable"))
                for id in (1, 2):
                    self.assertEqual(vm.field("bootPage", id), 0)
                    self.assertEqual(vm.field("deviceRights", id), 0)
                for address in held[count:]:
                    self.assertTrue(vm.call("freePage", address, 9, 2))
                self.assertTrue(vm.call("bootstrapInit"))

    def test_endpoint_and_handle_issuance_failure_releases_all_resources(self):
        for failure in ("endpointBootstrapService", 1, 2):
            with self.subTest(grant=failure):
                vm = InitM()
                before = vm.free_pages()
                original = vm.call
                copies = 0
                def failed_grant(name, *args):
                    nonlocal copies
                    if name == "handleCopy":
                        copies += 1
                        if copies == failure:
                            return error(24)
                    if name == failure:
                        return error(23)
                    return original(name, *args)
                vm.call = failed_grant
                self.assertFalse(vm.call("bootstrapInit"))
                self.assertEqual(vm.free_pages(), before)
                self.assertTrue(vm.call("taskInitAvailable"))
                endpoint_type = vm.decls["endpoints"].sym.type.elem
                for i in range(16):
                    endpoint = vm.addresses["endpoints"] + i * endpoint_type.size
                    self.assertEqual(vm.object_value(endpoint, "references"), 0)
                vm.call = original
                self.assertTrue(vm.call("bootstrapInit"))

    def test_created_tasks_cannot_run_and_user_entry_seals_task_memory_authority(self):
        vm = InitM()
        self.assertEqual(vm.call("taskCreateImage", 0x14200, 0x14210, 4), 1)
        self.assertEqual(vm.field("state"), 5)
        self.assertEqual(vm.globals["readyCount"], 0)
        self.assertEqual(vm.memory[vm.field_address("context") + LAYOUT["TF_EPC"]], USER_CODE + 4)
        self.assertFalse(vm.call("taskPublish", 1))
        self.assertTrue(vm.call("taskDiscardCreated", 1))
        vm.start()
        before = vm.free_pages()
        self.assertEqual(vm.call("taskCreateImage", 0x14200, 0x14210, 0), 0)
        self.assertEqual(vm.call("taskCreate"), 0)
        self.assertFalse(vm.call("taskDiscardCreated", 2))
        self.assertFalse(vm.call("bootstrapInit"))
        self.assertEqual(vm.free_pages(), before)
        # No task-create/memory-control number is installed in the syscall ABI.
        vm.invoke(63, 2, 7, 0xDEADBEEF)
        self.assertEqual(vm.result(1)[0], error(38))


if __name__ == "__main__":
    unittest.main()
