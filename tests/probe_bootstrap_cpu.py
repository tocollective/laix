#!/usr/bin/env python3
"""Boot embedded service/client images on an existing WRM executable.

The natural case runs init and both user images unchanged. Negative cases
change only initial saved user contexts in temporary machines, targeting
existing user instructions; they never emit code or alter input artifacts.
"""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess

from probe_boot import Monitor, ready_monitor, require
from probe_mmu_cpu import symbols_from_map
from probe_scheduler_cpu import task_layout, uart_text
from run_ready import ROOT, check_layout
from test_console_service import request, response
from source_m import LAYOUT as C
from test_kernel import LAIX, check_m

CODE, DATA, START, PAGE = 0x40000000, 0x40001000, 0x40002000, 4096


def preflight(data, symbols):
    check_layout(symbols)
    require(len(data) >= 16, "missing boot header")
    magic, sectors, entry, reserved = struct.unpack_from("<4I", data)
    require(magic == C["BOOT_MAGIC"] and sectors > 0 and reserved == 0 and
            len(data) >= sectors * 512 and entry + C["BOOT_LOAD"] == symbols["kernelStart"],
            "invalid boot image/map")
    required = {"bootstrapInit", "bootstrapServerStart", "bootstrapServerEnd",
                "bootstrapClientStart", "bootstrapClientEnd", "taskStart", "tasks",
                "currentTask", "idleTask", "taskKernelResume.idle"}
    require(required <= symbols.keys(), "image lacks bootstrap symbols: " + ", ".join(sorted(required - symbols.keys())))
    size, offsets = task_layout()
    require(symbols["currentTask"] - symbols["tasks"] == 8 * size, "image has a different Task ABI")
    for prefix in ("bootstrapServer", "bootstrapClient"):
        begin, end = symbols[prefix + "Start"], symbols[prefix + "End"]
        require(symbols["__start_text"] <= begin < end <= symbols["__stop_text"] and
                begin % 4 == end % 4 == 0 and end - begin <= PAGE, "invalid embedded image range")
    # Locate unchanged instructions for the negative saved-context fixtures.
    begin, end = symbols["bootstrapClientStart"], symbols["bootstrapClientEnd"]
    instructions = list(struct.unpack_from(f"<{(end - begin) // 4}I", data, begin - C["BOOT_LOAD"]))
    from disasm import disassemble
    asm = {CODE + i * 4: disassemble(word, CODE + i * 4) for i, word in enumerate(instructions)}
    find = lambda text: next(pc for pc, instruction in asm.items() if instruction == text)
    call_pc = next(pc + 4 for pc, instruction in asm.items() if instruction == "addi r9, r0, 21")
    return size, offsets, dict(call=call_pc, syscall=find("syscall"), load=find("lw r5, 0(r8)"), store=find("sw r4, 0(r30)"))


class BootProbe:
    def __init__(self, monitor, symbols, layout):
        self.m, self.s = monitor, symbols
        self.size, self.offsets, self.instructions = layout
        self.log = [monitor.receive()]
        self.stop(symbols["taskStart"])

    def address(self, id, field):
        return self.s["tasks"] + (id - 1) * self.size + self.offsets[field]

    def field(self, id, field):
        return self.m.words(self.address(id, field), 1)[0]

    def stop(self, pc):
        self.m.command("del all")
        output = self.m.stop_at(pc)
        self.log.append(output)
        return Monitor.registers(output)

    def leaf(self, root, virtual):
        entry = self.m.words(root + 4 * (virtual >> 22), 1)[0]
        if entry & 1 == 0:
            return 0
        if entry & 14:
            return entry
        return self.m.words((entry & ~4095) + 4 * ((virtual >> 12) & 1023), 1)[0]

    def inspect_resources(self, data):
        handle = check_m(LAIX / "src/ipc/objects.m")[0].scope["Handle"].type
        resources = []
        for id, prefix, role, rights, devices in ((1, "bootstrapServer", 1, 2, 1), (2, "bootstrapClient", 2, 1, 0)):
            root, boot = self.field(id, "directory"), self.field(id, "bootPage")
            pages = self.m.words(self.address(id, "pages"), 3)
            resources.extend([root, boot, *pages, self.field(id, "kernelStackBottom")])
            require(self.field(id, "state") == 1, "init did not publish both Ready tasks")
            require(self.field(id, "deviceRights") == devices, "wrong UART operation grant")
            record = self.m.words(boot, 12)
            require(record == [C["START_MAGIC"], 1, 48, role, id, record[5], rights, devices, DATA, PAGE, 32, C["START_PROTOCOL_CONSOLE"]],
                    "invalid start record")
            require(self.leaf(root, START) == boot | 19 and self.leaf(root, CODE) == pages[0] | 27 and
                    self.leaf(root, DATA) == pages[1] | 23, "wrong task mapping permissions")
            require(self.leaf(root, C["UART_BASE"]) & 16 == 0, "user MMIO is exposed")
            begin, end = self.s[prefix + "Start"], self.s[prefix + "End"]
            expected = list(struct.unpack_from(f"<{(end - begin) // 4}I", data, begin - C["BOOT_LOAD"]))
            require(self.m.words(pages[0], len(expected)) == expected, "loader copied a different image")
            table = self.address(id, "handles")
            entries = [self.m.words(table + i * handle.size, handle.size // 4) for i in range(16)]
            live = [entry for entry in entries if entry[handle.field("object").offset // 4]]
            require(len(live) == 1 and live[0][handle.field("rights").offset // 4] == rights,
                    "temporary root or excessive rights escaped into user mode")
        require(len(set(resources)) == len(resources), "tasks share private memory resources")

    def natural(self):
        for id in (1, 2):
            regs = self.stop(CODE)
            require(regs["status"] & 21 == 5 and regs["r1"] == START and regs["r2"] == 48 and
                    regs["ptbr"] == self.field(id, "ptbr"), "wrong initial user entry")
            require(self.m.words(self.s["currentTask"], 1) == [self.address(id, "id")], "wrong selected task")
            if id == 2:
                require(self.field(1, "state") == 4 and self.field(1, "waitReason") == 5,
                        "server did not sleep in accept before the first client")
        self.stop(self.s["taskKernelResume.idle"])
        require(self.field(1, "state") == 4 and self.field(1, "waitReason") == 5 and
                self.field(2, "state") == 3 and self.field(2, "exitCode") == 0 and
                self.field(2, "bootPage") == 0 and self.field(2, "deviceRights") == 0 and
                self.m.words(self.s["currentTask"], 1) == [self.s["idleTask"]],
                "boot exchange failed or did not leave the server sleeping")

    def malformed(self, message, errno):
        page = self.m.words(self.address(2, "pages"), 3)[1]
        padded = message + b"\0" * (-len(message) % 4)
        writes = [f"wp 0x{page + i:X} 0x{int.from_bytes(padded[i:i + 4], 'little'):X}"
                  for i in range(0, len(padded), 4)]
        changes = dict(EPC=self.instructions["call"], R9=21,
                       R1=self.m.words(self.field(2, "bootPage") + 20, 1)[0],
                       R2=DATA, R3=len(message), R4=DATA + 128, R5=12)
        frame = self.address(2, "context")
        writes += [f"wp 0x{frame + C['TF_' + name]:X} 0x{value:X}" for name, value in changes.items()]
        self.m.commands(writes)
        for _ in range(100):
            regs = self.stop(self.instructions["call"] + 4)
            if self.m.words(self.s["currentTask"], 1) == [self.address(2, "id")]:
                break
        else:
            raise ValueError("reply did not resume the client")
        require(regs["status"] & 21 == 5 and regs["r1"] == 12,
                "malformed request did not receive a bounded reply in user mode")
        actual = b"".join(word.to_bytes(4, "little") for word in self.m.words(page + 128, 3))
        require(actual == response(-errno), "wrong console protocol error or nonzero output count")
        require(self.field(1, "state") == 4 and self.field(1, "waitReason") == 5,
                "server failed to return to Blocked after invalid input")

    def negative(self, case):
        if case in ("uart-denied", "task-api-denied"):
            # A valid image still requires factory authority on the current API.
            changes = dict(EPC=self.instructions["syscall"],
                           R9=0 if case == "uart-denied" else C["SYS_TASK_CREATE"],
                           R1=33 if case == "uart-denied" else 1)
        elif case == "mmio-denied":
            changes = dict(EPC=self.instructions["load"], R8=C["UART_BASE"])
        else:
            changes = dict(EPC=self.instructions["store"], R30=START)
        frame = self.address(2, "context")
        self.m.commands([f"wp 0x{frame + C['TF_' + name]:X} 0x{value:X}" for name, value in changes.items()])
        if case in ("uart-denied", "task-api-denied"):
            regs = self.stop(self.instructions["syscall"] + 4)
            require(regs["status"] & 21 == 5 and regs["r1"] == 0xFFFFFFFF,
                    "unprivileged syscall did not return the expected error")
        else:
            self.stop(self.s["taskKernelResume.idle"])
            cause, address = (9, C["UART_BASE"]) if case == "mmio-denied" else (10, START)
            saved = self.m.words(frame, C["TF_SIZE"] // 4)
            require(self.field(2, "state") == 3 and saved[C["TF_CAUSE"] // 4] == cause and
                    saved[C["TF_BADADDR"] // 4] == address and self.field(1, "state") == 4,
                    "user access did not fault locally while the server remained live")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("map", type=Path)
    parser.add_argument("--emulator", type=Path, default=ROOT / "bin/wrm081632")
    parser.add_argument("--rom", type=Path, default=ROOT / "bin/firmware.rom")
    parser.add_argument("--log-dir", type=Path, default=LAIX / "build/acceptance/bootstrap")
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()
    report = dict(complete=False, cases=[])
    args.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        require(args.timeout > 0, "timeout must be positive")
        paths = dict(image=args.image, map=args.map, emulator=args.emulator.resolve(), rom=args.rom.resolve())
        hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
        data, symbols = args.image.read_bytes(), symbols_from_map(args.map)
        layout = preflight(data, symbols)
        malformed = {"short-header": (b"\x01\x01", 22),
                     "bad-version": (request(b"BAD", version=2), 22),
                     "bad-type": (request(b"BAD", kind=2), 22),
                     "bad-length": (request(b"BAD", length=28), 22),
                     "oversize-text": (request(length=255), 90),
                     "bad-text": (request(b"BAD\x1b"), 22)}
        for case in ("natural", "uart-denied", "task-api-denied", "mmio-denied", "start-write-denied", *malformed):
            with ready_monitor(data, paths["emulator"], paths["rom"], args.timeout, full_image=True) as opened:
                monitor, process, stdout, stderr = opened
                probe = BootProbe(monitor, symbols, layout)
                probe.inspect_resources(data)
                if case in malformed:
                    probe.malformed(*malformed[case])
                elif case == "natural":
                    probe.natural()
                else:
                    probe.negative(case)
                uart = uart_text(stdout)
                require("PANIC" not in uart, "user operation caused a kernel panic")
                if case == "natural":
                    require("LA/IX microkernel v1.0.0\n" in uart, "client bytes did not reach the UART service")
                if case in malformed:
                    require("BAD" not in uart, "invalid request printed a prefix before validation")
                if case == "uart-denied":
                    require("!" not in uart, "denied UART byte escaped")
                (args.log_dir / f"{case}.uart.txt").write_text(uart)
                (args.log_dir / f"{case}.monitor.txt").write_text("\n".join(probe.log))
            report["cases"].append(case)
            print("PASS bootstrap CPU: " + case)
        require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name] for name, path in paths.items()),
                "input artifacts changed during the probe")
        report.update(complete=True, sha256=hashes)
    except (ValueError, OSError, StopIteration, subprocess.TimeoutExpired) as error:
        report["error"] = str(error)
        print("FAIL bootstrap CPU: " + str(error))
    (args.log_dir / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
