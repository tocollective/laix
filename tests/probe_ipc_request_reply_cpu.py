#!/usr/bin/env python3
"""Accept request/reply on ready executable bytes; never build or emit code.

The fixture changes saved contexts and data in a temporary machine. Each IPC
operation executes the unchanged user blob's final SYSCALL and returns to its
existing self-branch. Trusted bootstrap and termination/MMU fixtures call the
image's existing kernel APIs. IRQs, copies, scheduling and teardown run on CPU.
"""

import argparse
import hashlib
import json
from pathlib import Path
import socket
import subprocess

from probe_boot import ready_monitor, require
from probe_mmu_cpu import symbols_from_map
from probe_scheduler_cpu import SchedulerProbe, preflight as scheduler_preflight, uart_text, CODE, DATA, PAGE, C
from test_kernel import LAIX, check_m
from run_ready import ROOT

CASES = ("exchange", "accept_first", "buffers", "destroy", "last_receive",
         "exit", "fault", "blocked_service", "client_death", "stress")
CALL, ACCEPT, REPLY = 21, 22, 23


def negative(errno):
    return -errno & 0xFFFFFFFF


def preflight(data, symbols):
    required = {"ipcCall", "ipcAccept", "ipcReply", "endpointBootstrapService",
                "endpointBootstrap", "handleCopy", "taskAbortBlocked", "setPagePermissions",
                "objects__endpoints", "objects__bootstrapSealed"}
    require(required <= symbols.keys(), "image lacks request/reply symbols: " +
            ", ".join(sorted(required - symbols.keys())))
    layout = scheduler_preflight(data, symbols)
    module = check_m(LAIX / "src/ipc/objects.m")[0]
    endpoint = module.scope["Endpoint"].type
    require(symbols["objects__bootstrapSealed"] - symbols["objects__endpoints"] == 16 * endpoint.size,
            "ready endpoint array does not match source ABI")
    require(layout[3] == layout[2][-1] + 4, "last existing SYSCALL must return into the self-branch")
    return layout, {field.name: field.offset for field in endpoint.fields}


class RequestReplyProbe(SchedulerProbe):
    def __init__(self, monitor, data, symbols, timeout, layout):
        super().__init__(monitor, data, symbols, timeout, layout[0])
        self.endpoint_offsets = layout[1]
        self.count = 4
        self.ipc_calls = 0
        self.timer_switches = 0
        self.additional_pages = {}
        self.pending_contexts = {}

    def running(self):
        pointer = self.m.words(self.s["currentTask"], 1)[0]
        require(pointer != self.s["idleTask"], "unexpected idle selection")
        id = (pointer - self.s["tasks"]) // self.size + 1
        require(1 <= id <= self.count and pointer == self.address(id, "id"), "invalid current TCB")
        return id

    def check_queue(self, running):
        head, count = self.m.words(self.s["task__readyHead"], 2)
        ring = self.m.words(self.s["task__readyQueue"], 8)
        require(head < 8 and count <= 8, "invalid ready queue bounds")
        queue = [ring[(head + i) % 8] for i in range(count)]
        require(len(queue) == len(set(queue)) and all(1 <= id <= self.count for id in queue),
                "duplicate or invalid ready membership")
        for id in range(1, self.count + 1):
            state = self.field(id, "state")
            addr = self.address(id, "queued")
            queued = self.m.words(addr & ~3, 1)[0] >> ((addr & 3) * 8) & 255
            require(bool(queued) == (state == 1) and (id in queue) == (state == 1),
                    "ready queue and TCB state disagree")
            require((state == 2) == (id == running), "wrong Running task")

    def endpoint_field(self, name):
        return self.m.words(self.endpoint + self.endpoint_offsets[name], 1)[0]

    def bytes(self, id, offset, count):
        result = b""
        while count:
            base = self.data_page(id, offset // PAGE) + offset % PAGE
            chunk = min(count, PAGE - offset % PAGE)
            aligned = base & ~3
            words = self.m.words(aligned, ((base - aligned) + chunk + 3) // 4)
            data = b"".join(word.to_bytes(4, "little") for word in words)
            result += data[base - aligned:base - aligned + chunk]
            offset += chunk
            count -= chunk
        return result

    def data_page(self, id, page):
        return self.additional_pages[id, page] if page else self.m.words(self.address(id, "pages"), 3)[1]

    def put_bytes(self, id, offset, data):
        require(offset % 4 == 0, "fixture byte writes must start at a word boundary")
        padded = data + b"\0" * (-len(data) % 4)
        for i in range(0, len(padded), 4):
            base = self.data_page(id, (offset + i) // PAGE) + (offset + i) % PAGE
            self.write(base, int.from_bytes(padded[i:i + 4], "little"))

    def result(self, id):
        frame = self.context(id)
        return frame[1], frame[2]

    def cleared(self, id):
        for name in ("ipcEndpoint", "ipcKind", "ipcBuffer", "ipcSize", "ipcObjectGeneration",
                     "ipcReplyOwner", "ipcReplyBuffer", "ipcReplyCapacity", "waitReason"):
            require(self.field(id, name) == 0, f"terminal IPC retained {name}")
        require(self.m.words(self.address(id, "ipcMessage"), 8) == [0] * 8,
                "terminal IPC retained its snapshot")

    def waiting(self, id, kind):
        require(self.field(id, "state") == 4 and self.field(id, "ipcKind") == kind and
                self.field(id, "waitReason") == kind and self.field(id, "ipcEndpoint") == self.endpoint,
                "client was woken early or has a wrong wait identity")

    def check_pending_contexts(self):
        for id, before in list(self.pending_contexts.items()):
            saved = self.context(id)
            require(saved[3:32] == [before[f"r{i}"] for i in range(3, 32)] and
                    saved[C["TF_FCSR"] // 4] == before["fcsr"] and saved[C["TF_EPC"] // 4] == self.busy,
                    "blocked IPC completion corrupted preserved context")
            if self.field(id, "state") != 4:
                del self.pending_contexts[id]

    def start_service(self):
        for id in range(1, self.count + 1):
            require(self.call("taskCreate") == id, "could not create fixture tasks")
            self.seed(id, self.busy)
        owner = self.call("endpointBootstrapService", self.address(1, "handles"), 1)
        require(0 < owner < 0x80000000, "service bootstrap failed")
        self.tokens = {1: owner}
        self.raw = self.call("endpointBootstrap", self.address(1, "handles"), 1)
        self.foreign = self.call("endpointBootstrapService", self.address(4, "handles"), 4)
        require(self.raw > 0 and self.foreign > 0, "auxiliary bootstrap failed")
        require(self.call("handleCopy", self.address(1, "handles"), owner,
                          self.address(4, "handles"), 1, 4, 2) == negative(1),
                "service receive rights escaped the designated owner")
        for id in range(2, self.count + 1):
            token = self.call("handleCopy", self.address(1, "handles"), owner,
                              self.address(id, "handles"), 1, id, 1)
            require(0 < token < 0x80000000, "client bootstrap failed")
            self.tokens[id] = token
        self.endpoint = self.m.words(self.address(1, "handles") + ((owner & 255) - 1) * 16, 1)[0]
        require(self.endpoint_field("mode") == 1 and self.endpoint_field("manager") == 1 and
                self.endpoint_field("references") == 4, "wrong Service endpoint fixture")
        self.bridge()
        self.prepare(self.s["taskStart"], (1000000,))
        self.stop(self.busy)
        self.check_queue(1)

    def tick_return(self):
        before = self.regs()
        require(before["pc"] == self.busy, "timer fixture is not parked in the user self-branch")
        trapped = self.stop(self.s["trapEntry"])
        require(trapped["cause"] == 0 and trapped["epc"] == self.busy and trapped["status"] & 24 == 24,
                "fixture did not enter through a real user timer IRQ")
        require(all(trapped[f"r{i}"] == before[f"r{i}"] for i in range(32)) and
                trapped["fcsr"] == before["fcsr"], "hardware IRQ changed user registers")
        returned = self.stop(self.dispatch_return)
        self.timer_switches += 1
        self.check_queue(self.running())
        self.check_pending_contexts()
        return returned

    def resume_busy(self):
        regs = self.stop(self.busy)
        id = self.running()
        frame = self.context(id)
        require(regs["status"] & 21 == 5 and regs["ptbr"] == self.selected(id), "wrong user IRET/PTBR")
        require([regs[f"r{i}"] for i in range(32)] == frame[:32] and
                regs["fcsr"] == frame[C["TF_FCSR"] // 4], "IRET restored a corrupt context")
        self.check_queue(id)
        return regs

    def syscall(self, id, number, *args):
        require(self.field(id, "state") in (1, 2), "cannot issue a syscall from a blocked/dead task")
        for _ in range(8):
            returned = self.tick_return()
            if self.running() == id:
                break
            self.resume_busy()
        else:
            raise ValueError("caller did not become schedulable")
        values = dict(EPC=self.syscalls[-1], R9=number)
        values.update({f"R{i}": value for i, value in enumerate(args, 1)})
        self.set_frame(returned["r1"], values)
        before = self.stop(self.syscalls[-1])
        require(before["status"] & 21 == 5 and before["r9"] == number and
                all(before[f"r{i}"] == value for i, value in enumerate(args, 1)), "wrong syscall entry ABI")
        trapped = self.stop(self.s["trapEntry"])
        require(trapped["cause"] == 12 and trapped["epc"] == self.syscalls[-1] and
                trapped["status"] & 24 == 24, "IPC did not raise an actual user SYSCALL")
        require(all(trapped[f"r{i}"] == before[f"r{i}"] for i in range(32)), "SYSCALL changed entry registers")
        self.stop(self.dispatch_return)
        saved = self.context(id)
        require(saved[C["TF_EPC"] // 4] == self.busy, "syscall did not consume EPC exactly once")
        require(saved[3:32] == [before[f"r{i}"] for i in range(3, 32)] and
                saved[C["TF_FCSR"] // 4] == before["fcsr"], "IPC corrupted preserved registers")
        self.ipc_calls += number in (CALL, ACCEPT, REPLY)
        if self.field(id, "state") == 4:
            self.pending_contexts[id] = before
        self.check_pending_contexts()
        self.resume_busy()
        return self.result(id)

    def kernel_api(self, name, *args):
        returned = self.tick_return()
        id = self.running()
        original = self.context(id)
        self.inject_call(id, returned, name, args)
        self.stop(self.s[name])
        result = self.stop(self.s["trapEntry.restore"])["r1"]
        regs = self.stop(self.busy)
        # This controlled supervisor invocation borrowed a TCB return frame.
        self.m.commands([f"wp 0x{self.address(id, 'context') + i * 4:X} 0x{word:X}"
                         for i, word in enumerate(original)])
        require([regs[f"r{i}"] for i in range(32)] == original[:32], "kernel fixture corrupted user context")
        self.check_queue(self.running())
        self.check_pending_contexts()
        return result

    def request(self, id, data=b"request", capacity=32, response=DATA + 128):
        self.put_bytes(id, 0, data)
        self.syscall(id, CALL, self.tokens[id], DATA, len(data), response, capacity)

    def accept(self, capacity=32):
        length, token = self.syscall(1, ACCEPT, self.tokens[1], DATA, capacity)
        require(length <= 32 and 0 < token < 0x80000000, "accept did not grant a reply token")
        return token

    def reply(self, token, data=b"response"):
        self.put_bytes(1, 0, data)
        return self.syscall(1, REPLY, token, DATA, len(data))

    def exchange(self):
        require(self.syscall(1, CALL, self.tokens[1], 0xFFFFFFFF, 0, 0xFFFFFFFF, 0) == (negative(35), 0),
                "self-call did not reject before blocking")
        for number, token in ((19, self.tokens[1]), (20, self.tokens[1]), (CALL, self.raw), (ACCEPT, self.raw)):
            args = (token, DATA, 1, DATA + 128, 32) if number == CALL else (token, DATA, 32)
            require(self.syscall(1, number, *args) == (negative(22), 0), "Raw/Service modes were mixed")
        for id, message in ((2, b"client2"), (3, b"client3")):
            self.request(id, message)
            self.waiting(id, 4)
        require(self.endpoint_field("senderCount") == 2 and self.endpoint_field("references") == 6,
                "queued request pins are wrong")
        self.put_bytes(2, 0, b"changed")
        require(self.syscall(1, ACCEPT, self.tokens[1], DATA, 1) == (negative(90), 7), "small accept consumed request")
        require(self.syscall(1, ACCEPT, self.tokens[1], DATA, PAGE + 1) == (negative(14), 0), "accept skipped full-range checks")
        a = self.accept()
        require(a == 0x102 and self.bytes(1, 0, 7) == b"client2", "FIFO/snapshot identity is wrong")
        b = self.accept()
        require(b == 0x103 and self.bytes(1, 0, 7) == b"client3", "second request identity is wrong")
        for id in (2, 3):
            self.waiting(id, 6)
        before = self.bytes(2, 128, 32)
        for token in (a, b, 0, 1, a + 256, a | 0x80000000, 0x109, 0xFFFFFFFF):
            require(self.syscall(4, REPLY, token, DATA, 1) == (negative(9), 0), "foreign/malformed reply was accepted")
        require(self.bytes(2, 128, 32) == before, "foreign reply wrote client memory")
        for token, id, response in ((b, 3, b"reply3"), (a, 2, b"reply2")):
            require(self.reply(token, response) == (6, 6), "reply failed")
            require(self.result(id) == (6, 6) and self.bytes(id, 128, 6) == response, "reply reached wrong client")
            self.cleared(id)
            require(self.reply(token) == (negative(9), 0), "repeated reply succeeded")
        self.request(2)
        next_token = self.accept()
        require(next_token != a and self.reply(a) == (negative(9), 0), "old token completed a later call")
        self.waiting(2, 6)
        self.reply(next_token)
        self.write(self.address(2, "ipcCallGeneration"), 0x7FFFFE)
        self.request(2)
        last = self.accept()
        require(last == 0x7FFFFF02 and self.reply(last) == (8, 8), "last generation failed")
        require(self.syscall(2, CALL, self.tokens[2], DATA, 1, DATA + 128, 32) == (negative(75), 0),
                "reply generation wrapped")
        require(self.endpoint_field("references") == 4, "exchange leaked wait references")
        return dict(two_clients=True, reverse_replies=True, request_snapshot=True,
                    self_call=True, modes=True, foreign_stale_duplicate=True, generation_exhaustion=True)

    def accept_first(self):
        self.syscall(1, ACCEPT, self.tokens[1], DATA, 32)
        self.waiting(1, 5)
        self.request(2, b"hello")
        require(self.result(1) == (5, 0x102) and self.bytes(1, 0, 5) == b"hello", "blocked accept did not complete")
        self.waiting(2, 6)
        require(self.kernel_api("taskWake", 2) == 0, "generic wake bypassed AwaitReply")
        self.waiting(2, 6)
        self.reply(0x102, b"world")
        require(self.result(2) == (5, 5) and self.bytes(2, 128, 5) == b"world", "reply after blocked accept failed")
        self.cleared(2)
        return dict(accept_first=True, no_early_client_wake=True, generic_wake_rejected=True)

    def buffers(self):
        for source, length, response, capacity, errno in ((0, 1, DATA + 128, 32, 14),
                (DATA, 33, DATA + 128, 32, 90), (DATA + PAGE - 1, 2, DATA + 128, 32, 14),
                (DATA, 1, CODE, 32, 14), (DATA, 1, DATA, PAGE + 1, 14)):
            require(self.syscall(2, CALL, self.tokens[2], source, length, response, capacity) == (negative(errno), 0),
                    "invalid call buffer was admitted")
            self.cleared(2)
        self.syscall(1, ACCEPT, self.tokens[1], DATA, 1)
        self.request(2, b"abc", capacity=1)
        require(self.result(1) == (negative(90), 3), "small blocked accept did not return size")
        self.waiting(2, 4)
        token = self.accept()
        for source, length, errno in ((0, 1, 14), (DATA, 33, 90)):
            require(self.syscall(1, REPLY, token, source, length) == (negative(errno), 0), "invalid reply source succeeded")
            self.waiting(2, 6)
        before = self.bytes(2, 128, 32)
        require(self.reply(token, b"big") == (negative(90), 3) and self.result(2) == (negative(90), 3),
                "small response did not terminate both calls")
        require(self.bytes(2, 128, 32) == before, "small response partially wrote")
        self.cleared(2)
        self.syscall(1, ACCEPT, self.tokens[1], DATA, 32)
        require(self.kernel_api("setPagePermissions", self.field(1, "directory"), 1, DATA, 19) == 1,
                "could not remove service write permission")
        self.request(2, b"again")
        require(self.result(1) == (negative(14), 0), "blocked accept destination was not revalidated")
        self.waiting(2, 4)
        require(self.kernel_api("setPagePermissions", self.field(1, "directory"), 1, DATA, 23) == 1,
                "could not restore service write permission")
        token = self.accept()
        require(self.kernel_api("setPagePermissions", self.field(2, "directory"), 2, DATA, 19) == 1,
                "could not remove client write permission")
        require(self.reply(token) == (negative(14), 0) and self.result(2) == (negative(14), 0),
                "reply destination was not revalidated")
        self.cleared(2)
        require(self.reply(token) == (negative(9), 0), "destination failure preserved a used right")
        require(self.kernel_api("setPagePermissions", self.field(2, "directory"), 2, DATA, 23) == 1,
                "could not restore client write permission")
        for id in (1, 2):
            page = self.kernel_api("allocPage", id, 5)
            require(page != 0 and page != self.data_page(id, 0) + PAGE, "missing noncontiguous cross-page fixture")
            require(self.kernel_api("mapPage", self.field(id, "directory"), id, DATA + PAGE, page, 23) == 1,
                    "could not map second user data page")
            self.additional_pages[id, 1] = page
        message = bytes(range(32))
        self.put_bytes(2, PAGE - 16, message)
        self.syscall(2, CALL, self.tokens[2], DATA + PAGE - 16, 32, DATA + PAGE - 16, 32)
        length, token = self.syscall(1, ACCEPT, self.tokens[1], DATA + PAGE - 16, 32)
        require(length == 32 and self.bytes(1, PAGE - 16, 32) == message, "cross-page request copy failed")
        self.put_bytes(1, PAGE - 16, message[::-1])
        require(self.syscall(1, REPLY, token, DATA + PAGE - 16, 32) == (32, 32) and
                self.bytes(2, PAGE - 16, 32) == message[::-1], "cross-page overlapping response copy failed")
        self.request(2, b"x", capacity=PAGE + 32, response=DATA)
        token = self.accept()
        require(self.kernel_api("setPagePermissions", self.field(2, "directory"), 2, DATA + PAGE, 19) == 1,
                "could not revoke unused response capacity page")
        before = self.bytes(2, 0, 32)
        require(self.reply(token, b"z") == (negative(14), 0) and self.bytes(2, 0, 32) == before,
                "reply did not revalidate capacity beyond the bytes copied")
        self.cleared(2)
        require(self.endpoint_field("references") == 4, "buffer failures leaked wait pins")
        return dict(admission=True, small_accept_retry=True, source_retry=True,
                    blocked_accept_revalidation=True, reply_destination_errors=True, no_partial_writes=True,
                    cross_page_noncontiguous=True, full_response_capacity_revalidated=True)

    def lifecycle(self, event):
        self.request(2)
        token = self.accept()
        self.request(3)
        self.waiting(2, 6)
        self.waiting(3, 4)
        if event == "destroy":
            require(self.syscall(1, 18, self.tokens[1])[0] == 0, "destroy failed")
        elif event == "last_receive":
            duplicate = self.syscall(1, 17, self.tokens[1], 1, 2)[0]
            require(0 < duplicate < 0x80000000, "receive copy failed")
            require(self.syscall(1, 16, self.tokens[1])[0] == 0, "first receive close failed")
            self.waiting(2, 6)
            self.waiting(3, 4)
            require(self.syscall(1, 16, duplicate)[0] == 0, "last receive close failed")
        elif event in ("exit", "fault"):
            duplicate = self.syscall(1, 17, self.tokens[1], 1, 2)[0]
            require(0 < duplicate < 0x80000000, "receive copy failed")
            self.syscall(1, 16, self.tokens[1])
            if event == "exit":
                self.syscall(1, 1, 9)
            else:
                self.user_fault(1)
        elif event == "blocked_service":
            self.accept()  # accept client 3, then suspend the service itself
            self.syscall(1, ACCEPT, self.tokens[1], DATA, 32)
            require(self.kernel_api("taskAbortBlocked", 1, 9, 1) == 1, "blocked service termination failed")
        else:
            raise ValueError(event)
        require(self.endpoint_field("state") == 2, "service was not revoked")
        for id in (2, 3):
            require(self.result(id) == (negative(32), 0), "service loss did not cancel its client")
            self.cleared(id)
        require(self.endpoint_field("senderCount") == 0 and self.endpoint_field("receiverCount") == 0,
                "revocation retained queue entries")
        require(self.syscall(4, CALL, self.tokens[4], DATA, 1, DATA + 128, 32) == (negative(32), 0),
                "revoked service still accepted calls")
        require(self.syscall(4, REPLY, token, DATA, 1) == (negative(9), 0), "revoked reply right remained live")
        if event in ("exit", "fault", "blocked_service"):
            require(self.field(1, "state") == 3 and self.field(1, "directory") == 0 and
                    self.field(1, "kernelStackTop") == 0, "service pages/stack were not reaped")
        require(self.endpoint_field("references") == (4 if event == "destroy" else 3),
                "service lifecycle leaked references")
        return dict(event=event, queued_and_accepted_cancelled=True, survivor_timer=True,
                    stale_right_rejected=True, wait_pins_released=True)

    def user_fault(self, id):
        for _ in range(8):
            returned = self.tick_return()
            if self.running() == id:
                break
            self.resume_busy()
        else:
            raise ValueError("faulting task did not run")
        self.set_frame(returned["r1"], dict(EPC=DATA + PAGE))
        trapped = self.stop(self.s["trapEntry"])
        require(trapped["cause"] == 8 and trapped["epc"] == DATA + PAGE and trapped["status"] & 24 == 24,
                "service did not raise a real user instruction page fault")
        self.stop(self.dispatch_return)
        self.resume_busy()

    def client_death(self):
        self.request(2)
        token = self.accept()
        self.request(3)
        pages = self.m.words(self.address(2, "pages"), 3)
        directory = self.field(2, "directory")
        require(self.kernel_api("taskAbortBlocked", 2, 9, 1) == 1, "blocked client termination failed")
        self.cleared(2)
        self.waiting(3, 4)
        require(self.reply(token) == (negative(9), 0), "reply wrote a dead client's memory")
        require(self.field(2, "directory") == 0 and self.field(2, "kernelStackTop") == 0,
                "dead client retained its address space/stack")
        bitmap = self.bitmap()
        require(all(not bitmap[(page // PAGE) // 32] & (1 << ((page // PAGE) % 32))
                    for page in pages + [directory]), "dead client pages remained allocated")
        next_token = self.accept()
        require(self.reply(next_token) == (8, 8) and self.result(3) == (8, 8), "third task's request was lost")
        self.cleared(3)
        require(self.endpoint_field("references") == 3, "client death leaked a wait/handle reference")
        return dict(dead_client_right_revoked=True, pages_released=True, peer_fifo_preserved=True)

    def stress(self, exchanges):
        require(self.syscall(2, CALL, self.tokens[2], 0xFFFFFFFF, 0, 0xFFFFFFFF, 0)[0] == self.tokens[2],
                "zero call did not block")
        token = self.accept(capacity=0)
        require(self.syscall(1, REPLY, token, 0xFFFFFFFF, 0) == (0, 0) and self.result(2) == (0, 0),
                "zero message dereferenced its buffer or did not rendezvous")
        for index in range(exchanges):
            id = 2 + index % 2
            message = bytes((index + i) & 255 for i in range(32))
            self.request(id, message, response=DATA)  # overlap request and reply
            token = self.accept()
            require(self.bytes(1, 0, 32) == message, "preempted request was lost")
            self.waiting(id, 6)
            require(self.reply(token, message[::-1]) == (32, 32), "preempted reply failed")
            require(self.result(id) == (32, 32) and self.bytes(id, 0, 32) == message[::-1],
                    "preempted response was lost or reached wrong memory")
            self.cleared(id)
            require(self.endpoint_field("references") == 4, "ping-pong leaked wait references")
            if (index + 1) % 32 == 0:
                print(f"  stress: {index + 1} CPU request/reply exchanges", flush=True)
        return dict(exchanges=exchanges, maximal_messages=True, zero_message=True,
                    overlapping_buffers=True, timer_preemption=True, no_pin_leaks=True)


def run_case(case, data, symbols, emulator, rom, timeout, layout, exchanges, log_dir):
    with ready_monitor(data, emulator, rom, timeout) as opened:
        monitor, process, stdout, stderr = opened
        monitor.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        p = None
        try:
            p = RequestReplyProbe(monitor, data, symbols, timeout, layout)
            p.start_service()
            if case in ("destroy", "last_receive", "exit", "fault", "blocked_service"):
                result = p.lifecycle(case)
            elif case == "stress":
                result = p.stress(exchanges)
            else:
                result = getattr(p, case)()
            require("PANIC" not in uart_text(stdout), "kernel panicked during IPC acceptance")
            return dict(case=case, **result, ipc_syscalls=p.ipc_calls,
                        timer_irqs=p.timer_switches, all_preserved_gprs=True,
                        fcsr=True, syscall_epc_once=True, user_iret=True, unique_ready_queue=True)
        finally:
            if p is not None:
                (log_dir / (case + ".monitor.txt")).write_text("\n".join(p.log))
            (log_dir / (case + ".uart.txt")).write_text(uart_text(stdout))
            stderr.seek(0)
            (log_dir / (case + ".emulator.txt")).write_text(stderr.read())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("map", type=Path)
    parser.add_argument("--emulator", type=Path, default=ROOT / "bin/wrm081632")
    parser.add_argument("--rom", type=Path, default=ROOT / "bin/firmware.rom")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--exchanges", type=int, default=128)
    parser.add_argument("--case", action="append", choices=CASES)
    parser.add_argument("--log-dir", type=Path, default=LAIX / "build/acceptance/ipc_request_reply_cpu")
    args = parser.parse_args()
    require(args.timeout > 0 and args.exchanges > 0, "timeout and exchanges must be positive")
    paths = dict(image=args.image.resolve(), map=args.map.resolve(),
                 emulator=args.emulator.resolve(), rom=args.rom.resolve(), probe=Path(__file__).resolve())
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    report = dict(complete=False, acceptance_complete=False,
                  method="ready CPU image; existing user SYSCALL/IRET; no build",
                  artifacts={name: dict(path=str(paths[name]), sha256=value) for name, value in hashes.items()},
                  results=[])
    args.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        data = paths["image"].read_bytes()
        symbols = symbols_from_map(paths["map"])
        layout = preflight(data, symbols)
        for case in args.case or CASES:
            report["results"].append(run_case(case, data, symbols, paths["emulator"], paths["rom"],
                                              args.timeout, layout, args.exchanges, args.log_dir))
            print(f"PASS {case}", flush=True)
        require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name]
                    for name, path in paths.items()), "ready artifacts changed during acceptance")
        report["complete"] = True
        report["acceptance_complete"] = {result["case"] for result in report["results"]} == set(CASES)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        report["error"] = str(error)
        print(f"FAIL request/reply CPU probe: {error}", flush=True)
    (args.log_dir / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
