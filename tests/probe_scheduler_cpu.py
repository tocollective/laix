#!/usr/bin/env python3
"""Scheduler acceptance on an existing image; never compile or emit instructions.

Only saved contexts and data in temporary machines are edited. User PCs point
into the image's unchanged loader blob. The CPU runs its actual IRQ, syscall,
MMU and reaper paths. Source-derived offsets require explicit map preflight.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import socket
import struct
import subprocess
import time

from probe_boot import Monitor, ready_monitor, require, disassemble
from probe_mmu_cpu import CpuProbe, symbols_from_map
from probe_unexpected_traps import instruction, LAYOUT as C
from run_ready import ROOT, check_layout
from test_kernel import LAIX, check_m

CODE, DATA, PAGE = 0x40000000, 0x40001000, 4096


def task_layout():
    modules = check_m(LAIX / "src/task/task.m")
    task = modules[0].scope["Task"].type
    return task.size, {field.name: field.offset for field in task.fields}


def preflight(data, symbols):
    require(len(data) >= 16, "missing WRMB header")
    check_layout(symbols)
    size, offsets = task_layout()
    required = {"tasks", "idleTask", "currentTask", "taskStart", "taskCreate",
                "taskTick", "taskBlock", "taskWake", "taskIdlePoll", "timerInit",
                "task__readyQueue", "task__readyHead", "task__readyCount", "taskCapacity",
                "task__schedulerStarted", "trapEntry.restore", "trapRestoreFrame",
                "userCodeStart", "userCodeEnd", "taskKernelResume.idle", "trapEmergencyFrame",
                "kernelRamEnd", "memory__pageBitmap", "trapRegisterSelfTest", "taskKernelSp"}
    require(required <= symbols.keys(), "image lacks scheduler symbols: " +
            ", ".join(sorted(required - symbols.keys())))
    require(symbols["trapEmergencyFrame"] >= symbols["idleTask"] + size,
            "ready idle TCB does not match source ABI")
    magic, sectors, entry, reserved = struct.unpack_from("<4I", data)
    require(magic == 0x424D5257 and reserved == 0 and sectors > 0 and
            len(data) >= sectors * 512 and entry + 0x10000 == symbols["kernelStart"] and
            symbols["__image_end"] <= 0x10000 + sectors * 512 <= symbols["__bss_start"],
            "invalid ready image/map")
    blob = range(symbols["userCodeStart"], symbols["userCodeEnd"], 4)
    syscalls = [pc for pc in blob if instruction(data, pc) == 7]
    busy = symbols["userCodeEnd"] - 4
    require(len(syscalls) == 5 and
            disassemble(instruction(data, busy), busy) == f"jal r0, 0x{busy:08X}",
            "ready user blob lacks the expected syscalls and busy loop")
    return size, offsets, [CODE + pc - symbols["userCodeStart"] for pc in syscalls], \
        CODE + busy - symbols["userCodeStart"]


def uart_text(output):
    # debugPutChar accepts every byte including 0xFF, which is not UTF-8.
    output.seek(0)
    return output.buffer.read().decode("utf-8", errors="backslashreplace")


def memory_words(text):
    memory = {}
    for line in text.splitlines():
        parts = line.removeprefix("> ").split()
        if len(parts) >= 2 and all(re.fullmatch(r"[0-9A-F]{8}", part) for part in parts):
            address = int(parts[0], 16)
            memory.update({address + 4 * i: int(word, 16) for i, word in enumerate(parts[1:])})
    return memory


class SchedulerProbe(CpuProbe):
    def __init__(self, monitor, data, symbols, timeout, layout):
        super().__init__(monitor, data, symbols, timeout)
        self.size, self.offsets, self.syscalls, self.busy = layout
        self.expected = {}
        self.switches = 0

    def address(self, id, name):
        base = self.s["idleTask"] if id == 0 else self.s["tasks"] + (id - 1) * self.size
        return base + self.offsets[name]

    def field(self, id, name):
        return self.m.words(self.address(id, name), 1)[0]

    def context(self, id):
        return self.m.words(self.address(id, "context"), C["TF_SIZE"] // 4)

    def set_context(self, id, values):
        self.set_frame(self.address(id, "context"), values)

    def set_frame(self, frame, values):
        commands = [f"wp 0x{frame + C['TF_' + name]:X} 0x{value:X}"
                    for name, value in values.items()]
        response = self.m.commands(commands)
        require("no RAM" not in response and "usage:" not in response, "context write failed")

    def seed(self, id, pc, syscall=2):
        values = {f"R{i}": (0xA5100000 * id + i * 0x10203) & 0xFFFFFFFF
                  for i in range(1, 32)}
        values.update(R9=syscall, R28=DATA + id * 16, R30=0xC0000000,
                      EPC=pc, FCSR=0x21 if id == 1 else 0x61)
        self.set_context(id, values)
        self.expected[id] = self.context(id)
        pages = self.m.words(self.address(id, "pages"), 3)
        self.write(pages[1] + id * 16, id * 0x11111111)

    def stop(self, pc):
        self.cmd("del all")
        response = self.m.stop_at(pc)
        self.log.append(response)
        return Monitor.registers(response)

    def selected(self, id):
        fields = self.m.words(self.address(id, "id"), self.size // 4)
        value = lambda name: fields[self.offsets[name] // 4]
        require(self.m.words(self.s["currentTask"], 1) == [self.address(id, "id")],
                "current TCB does not match selected task")
        require(value("state") == 2, "selected task is not Running")
        require(self.m.words(C["KERNEL_SP"], 3) ==
                [value("kernelStackTop"), value("kernelStackBottom"), value("kernelStackTop")],
                "low entry words do not match selected kernel stack")
        return value("ptbr")

    def check_queue(self, running):
        head, count = self.m.words(self.s["task__readyHead"], 2)
        capacity = self.m.words(self.s["taskCapacity"], 1)[0]
        ring = self.m.words(self.s["task__readyQueue"], capacity)
        require(head < capacity and count <= capacity, "invalid queue bounds")
        queue = [ring[(head + i) % capacity] for i in range(count)]
        require(len(queue) == len(set(queue)) and all(id in (1, 2) for id in queue),
                "duplicate or uncreated ready task")
        for id in (1, 2):
            state = self.field(id, "state")
            address = self.address(id, "queued")
            queued = self.m.words(address & ~3, 1)[0] >> ((address & 3) * 8) & 255
            require(bool(queued) == (state == 1), "queued flag differs from Ready membership")
            require((id in queue) == (state == 1), "queue includes a non-Ready task")
            require((state == 2) == (id == running), "wrong Running task")

    def check_user(self, regs, id, pc=None):
        expected = self.expected[id]
        require(regs["status"] & 21 == 5, "IRET did not enter user with IE on and EXL off")
        require(regs["ptbr"] == self.selected(id), "CPU PTBR differs from current TCB")
        require(regs["pc"] == (pc if pc is not None else expected[C["TF_EPC"] // 4]),
                "IRET resumed at the wrong EPC")
        require([regs[f"r{i}"] for i in range(32)] == expected[:32], "GPR/tp context corrupted")
        require(regs["fcsr"] == expected[C["TF_FCSR"] // 4], "FCSR context corrupted")
        # Peek uses a cached translation when available, but does not fill the
        # CPU TLB. The separate TLS case executes an actual user-mode load.
        response = self.cmd(f"x 0x{regs['r28']:X} 1")
        require(f"{id * 0x11111111:08X}" in response, "tp/TLS resolves to the wrong task data")

    def start(self, pc, tls=False):
        self.baseline_bitmap = self.bitmap()
        require(self.call("taskCreate") == 1 and self.call("taskCreate") == 2,
                "could not create two tasks")
        for id in (1, 2):
            self.seed(id, pc)
            if tls:
                self.set_context(id, {"R1": DATA, "R10": id, "R28": DATA, "R30": DATA})
                self.write(self.m.words(self.address(id, "pages"), 3)[1], id * 0x11111111)
                self.expected[id] = self.context(id)
        self.bridge()
        self.prepare(self.s["taskStart"], (1000000,))
        regs = self.stop(pc)
        self.check_user(regs, 1)
        self.check_queue(1)
        return regs

    def bitmap(self):
        ram = self.m.words(self.s["kernelRamEnd"], 1)[0]
        return self.m.words(self.s["memory__pageBitmap"], (ram // PAGE + 31) // 32)

    @staticmethod
    def parse_devices(text):
        timer = re.search(r"timer\s+count (\d+) reload (\d+) value (\d+) control ([0-9A-F]+)( expired)?", text)
        pic = re.search(r"pic\s+lines ([0-9A-F]{8}) enable ([0-9A-F]{8})", text)
        require(timer and pic, "monitor lacks timer/PIC state")
        return dict(count=int(timer[1]), reload=int(timer[2]), value=int(timer[3]),
                    control=int(timer[4], 16), expired=bool(timer[5]),
                    lines=int(pic[1], 16), enable=int(pic[2], 16))

    def devices(self):
        return self.parse_devices(self.cmd("info"))

    def restore_context(self, id):
        words = self.expected[id]
        self.m.commands([f"wp 0x{self.address(id, 'context') + i * 4:X} 0x{word:X}"
                         for i, word in enumerate(words)])

    def inject_call(self, selected, regs, name, args, pointer_result=False):
        # Borrow the selected return frame to invoke a trusted kernel API.
        # Copy its original user frame into data first; taskBlock saves that
        # frame, not this supervisor invocation. No handler code is changed.
        original = self.context(selected)
        copied = self.scratch + 256
        self.m.commands([f"wp 0x{copied + i * 4:X} 0x{word:X}" for i, word in enumerate(original)])
        values = dict(EPC=self.s[name], STATUS=16, R30=self.field(selected, "kernelStackTop") - 256,
                      R31=self.s["trapRestoreFrame" if pointer_result else "taskKernelResume.idle"])
        if selected and not pointer_result:
            stack_frame = values["R30"]
            self.m.commands([f"wp 0x{stack_frame + i * 4:X} 0x{word:X}"
                             for i, word in enumerate(original)])
            values["R31"] = self.s["trapEntry.restore"]
        values.update({f"R{i}": copied if value is None else value for i, value in enumerate(args, 1)})
        self.set_frame(regs["r1"], values)

    def block(self, selected, resume):
        require(self.stop(self.s["trapEntry"])["cause"] == 0, "block fixture did not enter by timer")
        regs = self.stop(self.dispatch_return)
        self.inject_call(selected, regs, "taskBlock", (None, 17), pointer_result=True)
        returned = self.stop(resume)
        require(self.field(selected, "state") == 4 and self.field(selected, "waitReason") == 17,
                "taskBlock did not publish Blocked")
        return returned

    def wake_from_idle(self, id):
        require(self.stop(self.s["trapEntry"])["cause"] == 0, "idle did not wake by timer IRQ")
        regs = self.stop(self.dispatch_return)
        self.inject_call(0, regs, "taskWake", (id,))
        returned = self.stop(self.busy)
        require(self.field(id, "waitReason") == 0, "wakeup retained an event wait")
        self.check_user(returned, id)
        self.check_queue(id)

    def terminal(self, outgoing, retiring, fault=False):
        pc = DATA + PAGE if fault else self.syscalls[-1]
        self.set_context(retiring, dict(EPC=pc, R9=1, R1=123))
        self.expected[retiring] = self.context(retiring)
        held_top = self.field(retiring, "kernelStackTop")
        require(self.stop(self.s["trapEntry"])["cause"] == 0, "terminal fixture did not switch by timer")
        self.stop(pc if not fault else self.s["trapEntry"])
        if not fault:
            trapped = self.stop(self.s["trapEntry"])
            require(trapped["cause"] == 12, "exit did not use SYSCALL")
        else:
            trapped = self.regs()
            require(trapped["cause"] == 8 and trapped["badaddr"] == pc, "wrong fatal user fault")
        returned = self.stop(self.busy)
        require(self.field(retiring, "state") == 3 and self.field(retiring, "waitReason") == 0,
                "terminal task retained a runnable state or wait")
        require(self.field(retiring, "kernelStackTop") == 0 and self.field(retiring, "directory") == 0,
                "terminal task retained resources")
        # reaped is a byte field; read its containing aligned word.
        address = self.address(retiring, "reaped")
        require(self.m.words(address & ~3, 1)[0] >> ((address & 3) * 8) & 255 == 1,
                "terminal task was not reaped")
        require(returned["r30"] != held_top and returned["ptbr"] == self.selected(outgoing),
                "survivor references the retired stack/root")
        self.check_user(returned, outgoing)
        self.check_queue(outgoing)

    def lifecycle(self, fault_first=False):
        self.block(2, self.busy)
        self.check_queue(1)
        self.block(1, self.s["taskKernelResume.idle"])
        self.selected(0)
        self.check_queue(0)
        # Stop on both sides of WFI: the following instruction is reachable
        # only after the timer level releases the CPU with IE still zero.
        wfi = next(pc for pc in range(self.s["taskKernelResume.idle"], self.s["userCodeStart"], 4)
                   if disassemble(instruction(self.data, pc), pc) == "wfi")
        before = self.stop(wfi)
        require(before["status"] & 21 == 0, "idle enables IRQs before WFI")
        self.stop(wfi + 4)
        pending = self.devices()
        require(pending["expired"] and pending["lines"] & 4, "WFI did not wake with a pending timer")
        self.wake_from_idle(1)
        # Wake a second waiter while the first task runs. The kernel API
        # returns through the existing restore instructions on its own stack.
        require(self.stop(self.s["trapEntry"])["cause"] == 0, "missing wake IRQ")
        regs = self.stop(self.dispatch_return)
        self.inject_call(1, regs, "taskWake", (2,))
        self.stop(self.busy)
        self.restore_context(1)
        self.check_queue(1)
        self.terminal(1, 2, fault=fault_first)
        # The survivor gets another timer quantum after its peer is reaped.
        require(self.stop(self.s["trapEntry"])["cause"] == 0, "survivor stopped receiving timer IRQs")
        self.stop(self.dispatch_return)
        self.set_context(1, dict(EPC=DATA + PAGE if not fault_first else self.syscalls[-1], R9=1, R1=123))
        if fault_first:
            self.stop(self.syscalls[-1])
        require(self.stop(self.s["trapEntry"])["cause"] == (12 if fault_first else 8),
                "missing final exit/fault")
        self.stop(self.s["taskKernelResume.idle"])
        self.check_queue(0)
        expected = list(self.baseline_bitmap)
        bottom = self.field(0, "kernelStackBottom")
        for page in range(bottom // PAGE - 1, bottom // PAGE + 2):
            expected[page // 32] |= 1 << (page % 32)
        require(self.bitmap() == expected, "exit/fault leaked physical pages")
        for id in (1, 2):
            require(self.field(id, "directory") == 0 and self.field(id, "kernelStackTop") == 0 and
                    self.field(id, "waitReason") == 0, "dead task retains resource/wait pointers")
        return dict(block_wake=True, idle_wfi=True, exit_fault=True, no_page_leaks=True)

    def switch(self, id, cause):
        before = self.regs()
        trapped = self.stop(self.s["trapEntry"])
        require(trapped["cause"] == cause and trapped["status"] & 24 == 24,
                "wrong hardware trap cause/status")
        require([trapped[f"r{i}"] for i in range(32)] == [before[f"r{i}"] for i in range(32)],
                "hardware trap changed GPRs")
        epc = before["pc"]
        require(trapped["epc"] == epc, "hardware EPC differs from interrupted instruction")
        if cause == 0:
            pending = self.devices()
            require(pending["expired"] and pending["lines"] & 4 and
                    pending["enable"] == 4 and pending["control"] == 3,
                    "hardware IRQ is not a configured timer expiry")
            require(pending["count"] >= getattr(self, "next_tick", 0),
                    "timer IRQ repeated before the next period")
        self.selected(id)
        dispatched = self.stop(self.dispatch_return)
        top = self.field(id, "kernelStackTop")
        require(dispatched["r30"] == top - C["TF_SIZE"], "IRQ used the wrong kernel stack")
        saved = self.context(id)
        expected = list(self.expected[id])
        expected[C["TF_EPC"] // 4] = epc + (4 if cause == 12 else 0)
        if cause == 12:
            expected[1] = 0
        require(saved[:32] == expected[:32], "saved GPRs are corrupted")
        for field in ("EPC", "FCSR", "PTBR"):
            require(saved[C["TF_" + field] // 4] == expected[C["TF_" + field] // 4],
                    f"saved {field} is corrupted")
        self.expected[id] = saved
        if cause == 0:
            acknowledged = self.devices()
            require(not acknowledged["expired"] and acknowledged["lines"] & 4 == 0,
                    "timer EXPIRED/IRQ remains asserted after dispatch")
            self.next_tick = acknowledged["count"] + acknowledged["value"]
        selected = 3 - id
        self.check_queue(selected)
        target = self.expected[selected][C["TF_EPC"] // 4]
        returned = self.stop(target)
        self.check_user(returned, selected)
        self.switches += 1
        return selected

    def tls(self):
        load = CODE + 16
        require(disassemble(instruction(self.data, self.s["userCodeStart"] + 16), load) ==
                "lw r3, 0(r30)", "TLS fixture does not point at the existing user load")
        running = 1
        for _ in range(32):
            self.cmd("del all")
            self.cmd("s 1")
            loaded = self.regs()
            require(loaded["pc"] == load + 4 and loaded["r3"] == running * 0x11111111 and
                    loaded["r28"] == DATA and loaded["r30"] == DATA,
                    "CPU load through the restored TLS address used a stale mapping")
            before = self.stop(self.syscalls[1])
            # The unchanged blob executed its load, store and debug syscall.
            # Snapshot this live context before observing the next yield.
            expected = list(self.expected[running])
            expected[:32] = [before[f"r{i}"] for i in range(32)]
            expected[C["TF_EPC"] // 4] = before["pc"]
            self.expected[running] = expected
            parked = 3 - running
            self.set_context(parked, dict(EPC=load, R1=DATA, R28=DATA, R30=DATA, R10=parked))
            self.expected[parked] = self.context(parked)
            running = self.switch(running, 12)
        return dict(cpu_user_loads=32, same_virtual_address=f"{DATA:08X}",
                    different_data=True, restored_tp=True, populated_tlb=True)

    def regressions(self):
        cases = [(0, 0, 0), (0, 255, 0), (0, 256, 0xFFFFFFEA),
                 (0, 0xFFFFFFFF, 0xFFFFFFEA), (3, 77, 0xFFFFFFDA),
                 (0xFFFFFFFF, 88, 0xFFFFFFDA)]
        for number, arg, result in cases:
            for sp in (0, 3, 0xDEADBEE8):
                self.set_context(2, dict(EPC=self.syscalls[0], R9=number, R1=arg, R30=sp))
                self.expected[2] = self.context(2)
                require(self.stop(self.s["trapEntry"])["cause"] == 0, "missing syscall-fixture timer")
                self.stop(self.dispatch_return)
                before = self.stop(self.syscalls[0])
                self.check_user(before, 2)
                require(self.stop(self.s["trapEntry"])["cause"] == 12, "missing user SYSCALL")
                returned = self.stop(self.syscalls[0] + 4)
                expected = dict(before, r1=result, pc=before["pc"] + 4)
                for name in [f"r{i}" for i in range(32)] + ["fcsr", "ptbr", "status", "pc"]:
                    require(returned[name] == expected[name], f"returning syscall corrupted {name}")
                self.stop(self.busy)
                self.check_queue(1)
        # Re-run the existing armed supervisor BREAK/SYSCALL register test
        # with TIMER and PIC still enabled. Supervisor work keeps IE clear.
        require(self.stop(self.s["trapEntry"])["cause"] == 0, "missing trap-regression timer")
        regs = self.stop(self.dispatch_return)
        self.inject_call(2, regs, "trapRegisterSelfTest", ())
        self.set_frame(regs["r1"], dict(R31=self.s["taskKernelSp"]))
        # The scheduler epilogue also calls taskKernelSp while reaping. Pass
        # that call before using it as the self-test's return breakpoint.
        self.stop(self.s["trapRegisterSelfTest"])
        returned = self.stop(self.s["taskKernelSp"])
        require(returned["r1"] == 0 and returned["status"] & 21 == 0,
                "supervisor register self-test failed with timer enabled")
        devices = self.devices()
        require(devices["enable"] == 4 and devices["control"] == 3,
                "regression tests disabled timer/PIC")
        return dict(returning_user_syscalls=18, invalid_user_sp=True,
                    supervisor_register_selftest=True, timer_enabled=True)

    def stress(self, count):
        """Batch paused reads; retain hardware-entry and post-IRET snapshots."""
        current = (self.m.words(self.s["currentTask"], 1)[0] - self.s["tasks"]) // self.size + 1
        live = self.regs()
        # The TCB table and the ready ring are separate tables carved from RAM, and the
        # scalars beside them are separate variables: dump each, not one contiguous span.
        span = 2 * self.size // 4  # the two stress tasks
        capacity = self.m.words(self.s["taskCapacity"], 1)[0]
        require(0 < span <= 1024 and 0 < capacity <= 4095, "TCB snapshot exceeds monitor capacity")
        for index in range(count):
            selected = 3 - current
            # Each continue is awaited before dependent reads. Only reads of
            # the already paused machine are batched in one monitor request.
            self.m.commands(["del all", f"b 0x{self.s['trapEntry']:X}"])
            self.log.append(self.m.command("c"))
            entry_text = self.m.commands(["r", "info"])
            self.log.append(entry_text)
            trapped = Monitor.registers(entry_text)
            pending = self.parse_devices(entry_text)
            require(trapped["pc"] == self.s["trapEntry"] and trapped["cause"] == 0 and
                    trapped["epc"] == self.busy and trapped["status"] & 24 == 24,
                    "stress did not enter on the busy-loop hardware IRQ EPC")
            require([trapped[f"r{i}"] for i in range(32)] == [live[f"r{i}"] for i in range(32)] and
                    trapped["ptbr"] == live["ptbr"] and trapped["fcsr"] == live["fcsr"],
                    "hardware IRQ changed the live context")
            require(pending["expired"] and pending["lines"] & 4 and pending["enable"] == 4 and
                    pending["control"] == 3 and pending["count"] >= getattr(self, "next_tick", 0),
                    "missing expiry or repeated IRQ before the next period")
            top = self.field(current, "kernelStackTop")
            self.m.commands(["del all", f"b 0x{self.busy:X}"])
            self.log.append(self.m.command("c"))
            text = self.m.commands(["r", f"xp 0x{self.s['tasks']:X} {span}",
                                    f"xp 0x{self.s['currentTask']:X} 1",
                                    f"xp 0x{self.s['task__readyHead']:X} 1",
                                    f"xp 0x{self.s['task__readyCount']:X} 1",
                                    f"xp 0x{self.s['task__readyQueue']:X} {capacity}",
                                    f"xp 0x{C['KERNEL_SP']:X} 3",
                                    f"xp 0x{top - C['TF_SIZE']:X} {C['TF_SIZE'] // 4}",
                                    f"x 0x{DATA + selected * 16:X} 1", "info"])
            self.log.append(text)
            live = Monitor.registers(text)
            memory = memory_words(text)
            words = lambda address, n: [memory[address + 4 * i] for i in range(n)]
            field = lambda id, name: memory[self.address(id, name)]
            expected = self.expected[selected]
            require(live["pc"] == self.busy and live["status"] & 21 == 5 and
                    live["ptbr"] == field(selected, "ptbr") and
                    [live[f"r{i}"] for i in range(32)] == expected[:32] and
                    live["fcsr"] == expected[C["TF_FCSR"] // 4], "stress restored the wrong context")
            require(memory[self.s["currentTask"]] == self.address(selected, "id") and
                    words(C["KERNEL_SP"], 3) == [field(selected, "kernelStackTop"),
                        field(selected, "kernelStackBottom"), field(selected, "kernelStackTop")],
                    "stress selected inconsistent TCB/PTBR/stack words")
            saved = words(self.address(current, "context"), C["TF_SIZE"] // 4)
            require(words(top - C["TF_SIZE"], C["TF_SIZE"] // 4) == saved,
                    "IRQ frame is not on the outgoing task's kernel stack")
            require(saved[:32] == self.expected[current][:32] and
                    saved[C["TF_EPC"] // 4] == self.busy and saved[C["TF_FCSR"] // 4] == trapped["fcsr"] and
                    saved[C["TF_PTBR"] // 4] == trapped["ptbr"], "stress saved a corrupted context")
            head, ready = memory[self.s["task__readyHead"]], memory[self.s["task__readyCount"]]
            require(head < capacity and ready == 1 and
                    memory[self.s["task__readyQueue"] + 4 * head] == current and
                    field(current, "state") == 1 and field(selected, "state") == 2,
                    "stress queue is not one Ready task and one Running task")
            for id in (1, 2):
                address = self.address(id, "queued")
                require(bool(memory[address & ~3] >> ((address & 3) * 8) & 255) == (id == current),
                        "stress queued flag differs from Ready membership")
            require(memory[DATA + selected * 16] == selected * 0x11111111,
                    "stress restored tp resolves to stale data")
            acknowledged = self.parse_devices(text)
            require(not acknowledged["expired"] and acknowledged["lines"] & 4 == 0,
                    "stress IRQ remains asserted after IRET")
            self.next_tick = acknowledged["count"] + acknowledged["value"]
            current = selected
            if (index + 1) % 1000 == 0:
                print(f"  timer: {index + 1} CPU switches", flush=True)
        self.switches = count

    def configure_quantum(self, hz):
        # Use actual CPU memcpy MMIO accesses; the monitor cannot write MMIO.
        # Reprogramming only the fixture quantum makes the long stress run
        # practical, while separate short cases retain the default 100 Hz.
        def transfer(destination, source):
            require(self.stop(self.s["trapEntry"])["cause"] == 0, "missing quantum-fixture timer")
            regs = self.stop(self.dispatch_return)
            selected = (self.m.words(self.s["currentTask"], 1)[0] - self.s["tasks"]) // self.size + 1
            self.inject_call(selected, regs, "memcpy", (destination, source, 4))
            returned = self.stop(self.busy)
            self.restore_context(selected)
            self.check_user(returned, selected)

        transfer(self.scratch + 64, C["TIMER_FREQUENCY"])
        frequency = self.m.words(self.scratch + 64, 1)[0]
        require(hz > 0 and frequency // hz > 0, "invalid stress timer frequency")
        period = frequency // hz
        self.write(self.scratch + 64, period)
        transfer(C["TIMER_RELOAD"], self.scratch + 64)
        self.write(self.scratch + 64, 3)
        transfer(C["TIMER_CONTROL"], self.scratch + 64)
        devices = self.devices()
        require(devices["reload"] == period and devices["control"] == 3 and devices["enable"] == 4,
                "CPU did not program the stress quantum")
        self.next_tick = devices["count"] + devices["value"]


def run_case(case, data, symbols, emulator, rom, timeout, layout, switches, log_dir, stress_hz=1000):
    with ready_monitor(data, emulator, rom, timeout, full_image=case == "natural") as opened:
        monitor, process, stdout, stderr = opened
        monitor.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        p = None
        try:
            if case == "natural" and "bootstrapInit" in symbols:
                # Normal boot now runs the receive-only service and client.
                # Legacy fixture cases still use taskCreate's original blob.
                from probe_bootstrap_cpu import BootProbe, preflight as bootstrap_preflight
                boot = BootProbe(monitor, symbols, bootstrap_preflight(data, symbols))
                boot.inspect_resources(data)
                boot.natural()
                transcript, uart = boot.log, uart_text(stdout)
                require("LA/IX microkernel v1.0.0\n" in uart and "PANIC" not in uart, "natural service exchange failed")
                result = dict(case=case, boot_trap_selftests=True, bootstrap_service=True,
                              client_exit=True, server_blocked=True)
            elif case == "natural":
                transcript = [monitor.receive()]
                transcript.append(monitor.stop_at(symbols["taskKernelResume.idle"]))
                require(monitor.words(symbols["tasks"] + layout[1]["state"], 1) == [3] and
                        monitor.words(symbols["tasks"] + layout[0] + layout[1]["state"], 1) == [3],
                        "natural demo did not finish both tasks")
                require(monitor.words(symbols["currentTask"], 1) == [symbols["idleTask"]],
                        "natural demo did not select idle")
                uart = uart_text(stdout)
                require("TrapFrame and syscall self-tests passed" in uart and "PANIC" not in uart,
                        "boot/trap regression failed")
                for id in (1, 2):
                    require(f"task {id} stopped, state=3 code=0 cause=12" in uart,
                            "natural demo faulted instead of executing exit(0)")
                result = dict(case=case, boot_trap_selftests=True, two_user_exits=True)
            else:
                p = SchedulerProbe(monitor, data, symbols, timeout, layout)
                pc = p.syscalls[1] if case == "yield" else CODE + 16 if case == "tls" else p.busy
                p.start(pc, tls=case == "tls")
                if case in ("exit", "fault"):
                    result = dict(case=case, **p.lifecycle(fault_first=case == "fault"))
                elif case == "tls":
                    result = dict(case=case, **p.tls())
                elif case == "regressions":
                    result = dict(case=case, **p.regressions())
                else:
                    running = 1
                    count = 32 if case == "yield" else switches
                    started = time.monotonic()
                    if case == "timer" and count >= 1000:
                        if stress_hz != 100:
                            p.configure_quantum(stress_hz)
                        p.stress(count)
                    for index in range(count if case == "yield" or count < 1000 else 0):
                        if case == "yield":
                            # Re-arm only the parked context; observe the
                            # outgoing syscall and its EPC+4 unchanged.
                            parked = 3 - running
                            p.set_context(parked, {"EPC": p.syscalls[1], "R9": 2})
                            p.expected[parked] = p.context(parked)
                        running = p.switch(running, 12 if case == "yield" else 0)
                        if index and index % 1000 == 0:
                            print(f"  {case}: {index + 1} CPU switches", flush=True)
                    result = dict(case=case, switches=count, seconds=round(time.monotonic() - started, 3),
                                  all_gprs=True, fcsr=True, restored_tp=True,
                                  irq_epc_unchanged=case == "timer", expired_cleared=case == "timer",
                                  quantum_hz=stress_hz if case == "timer" and count >= 1000 else 100,
                                  queue=True, selected_stack=True)
                transcript = p.log
                uart = uart_text(stdout)
                require("PANIC" not in uart, "CPU scheduler panicked")
            stderr.seek(0)
            (log_dir / (case + ".monitor.txt")).write_text("\n".join(transcript))
            (log_dir / (case + ".uart.txt")).write_text(uart)
            (log_dir / (case + ".emulator.txt")).write_text(stderr.read())
            return result
        except Exception:
            if p is not None:
                (log_dir / (case + ".monitor.txt")).write_text("\n".join(p.log))
            stderr.seek(0)
            (log_dir / (case + ".uart.txt")).write_text(uart_text(stdout))
            (log_dir / (case + ".emulator.txt")).write_text(stderr.read())
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("map", type=Path)
    parser.add_argument("--emulator", type=Path, default=ROOT / "bin/wrm081632")
    parser.add_argument("--rom", type=Path, default=ROOT / "bin/firmware.rom")
    parser.add_argument("--log-dir", type=Path, default=LAIX / "build/acceptance/scheduler_cpu")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--switches", type=int, default=20000)
    parser.add_argument("--stress-hz", type=int, default=1000)
    parser.add_argument("--case", action="append",
                        choices=("natural", "yield", "timer", "tls", "exit", "fault", "regressions"))
    args = parser.parse_args()
    require(args.switches > 0 and args.timeout > 0 and args.stress_hz > 0,
            "switches, timeout and stress-hz must be positive")
    paths = dict(image=args.image.resolve(), map=args.map.resolve(),
                 emulator=args.emulator.resolve(), rom=args.rom.resolve())
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    data = paths["image"].read_bytes()
    symbols = symbols_from_map(paths["map"])
    layout = preflight(data, symbols)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    report = dict(complete=False, method="ready CPU image; no build; saved-context fixtures",
                  artifacts={name: dict(path=str(paths[name]), sha256=value) for name, value in hashes.items()},
                  results=[])
    try:
        for case in args.case or ("natural", "yield", "timer", "tls", "exit", "fault", "regressions"):
            report["results"].append(run_case(case, data, symbols, paths["emulator"], paths["rom"],
                                              args.timeout, layout, args.switches, args.log_dir, args.stress_hz))
            print(f"PASS {case}", flush=True)
        require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name]
                    for name, path in paths.items()), "ready artifacts changed")
        report["complete"] = True
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        report["error"] = str(error)
        print(f"FAIL scheduler CPU probe: {error}", flush=True)
    (args.log_dir / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
