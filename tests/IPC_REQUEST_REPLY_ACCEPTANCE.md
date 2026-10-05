# Service request/reply acceptance

Current readiness is tracked in [A9](ACCEPTANCE_CI.md) and
[the readiness matrix](../docs/READINESS_MATRIX.md). Newer Screen/simple-service
images execute the compiled M `accept` helper. The helper limitation and hashes
below remain the historical claim for this report's own image.

Date: 2026-10-04. Implementation, source acceptance and request/reply CPU
acceptance are complete. All ten CPU cases passed on preserved matching
ready artifacts. No build, compiler code generation,
assembler emission or linker execution was used for verification.

## Source acceptance

The complete LA/IX suite passed **230 tests**:

```sh
python3 -B -m unittest discover -s laix/tests -p 'test_*.py'
python3 -B mc/mc.py --check laix/src/kernel/main.m
```

`test_kernel.py` also type-checks `user/syscalls.m` as a module, checks the new
dispatcher branches and compares M and assembly syscall/errno constants.
All returning branches end in `return`, preventing syscall fallthrough.

`test_ipc_request_reply.py` executes the checked M ASTs of the real transport,
object lifecycle, scheduler and MMU copy helpers together. Its ten tests cover:

- FIFO acceptance of two clients, reverse-order replies and distinct client memory.
- Both arrival orders, continued client blocking after accept, EPC/r3..r31/FCSR preservation and exact wake counts.
- Foreign, malformed, inactive, stale and repeated reply rejection without client writes or wakes.
- Self-call rejection, Service-mode access rules, receive-right copying restrictions and full-range admission checks.
- Persistent request generations, last permitted generation and exhaustion without wrap.
- Kernel request snapshots, small accept retry and blocked service buffer revalidation.
- Correctable service source errors versus terminal client destination errors.
- Explicit destruction, last receive close and manager exit/fault after management-handle close, cancelling queued and accepted requests.
- Suspended service/client termination, reply revocation before page reclamation and preservation of unrelated FIFO requests.
- Zero/maximal messages, overlapping buffers, cross-page copies into distinct frames and 32 exchanges with timer-driven scheduler rotations.

`test_ipc_service_helper.py` interprets the parsed instructions of the actual
`user/syscalls.asm` companion through the M `accept` wrapper. It checks empty
and maximal success, a token at the generation limit, errors with a nonzero
size in r2, and clearing a previously stored token on failure. The output is
the word-aligned eight-byte `AcceptResult` defined by the M module. Wrapper
checks also verify all five `call` arguments and the `reply` token forwarding.

Existing capability, raw transport, scheduler, trap and memory checks passed.
The scheduler regression checks every TCB's eight-byte TrapFrame alignment;
the enlarged TCB preserves that alignment with an explicit padding word.
IRQ/device transitions in source tests are fixtures, not evidence of CPU
execution of the new implementation.

## CPU acceptance

`probe_ipc_request_reply_cpu.py` preflights the ready WRMB header, executable
user blob, required IPC symbols, eight 560-byte TCBs and the 16-entry endpoint
pool against checked source layouts. Older scheduler images are rejected.
Four probe unit tests reject incompatible layouts/symbols, corrupt Ready
membership, premature wakeups, lost wait metadata and damaged preserved
contexts. These fixture checks are separate from the CPU runs below.

The CPU probe boots a temporary headless machine with networking disabled and
a loopback-only monitor. It invokes the image's existing bootstrap APIs before
root issuance is sealed, creating four isolated tasks: one service, two clients
and a task owning a different service. Every IPC operation enters user mode
through IRET, executes the unchanged loader blob's final SYSCALL with the
specified syscall number and arguments, and returns to its existing self-branch.
Monitor changes are limited to saved return contexts and fixture data; executable
instructions and input artifacts remain unchanged.

Requests, replies, user-buffer validation/copying, IRQ entry, FIFO/Ready queues,
revocation and resource reclamation execute the ready image's actual kernel
instructions. Buffer permission changes, generation exhaustion and termination
of suspended tasks are controlled kernel/data fixtures. Service exit uses an
actual user exit syscall; service fault uses an actual unmapped user instruction
fetch. A blocked task cannot execute its own exit or fault.

The final run passed all **10 cases**, observed **474 IPC syscalls** and
**1,017 actual user timer IRQs**:

| Case | CPU evidence |
| --- | --- |
| `exchange` | Two clients, FIFO acceptance, reversed replies, kernel request snapshot, distinct destination roots, foreign/malformed/stale/repeated tokens, self-call, Raw/Service separation and generation exhaustion |
| `accept_first` | Service blocks first; delivery grants its reply token while the client remains Blocked; generic wake is rejected; reply resumes the client |
| `buffers` | Full-range admission checks, small blocked accept and retry, service source retry, terminal client errors, receive revalidation, noncontiguous cross-page request/response copies and full response-capacity revalidation beyond the bytes copied |
| `destroy` | Destroy cancels both queued and already accepted clients; later calls return -EPIPE and old reply tokens return -EBADF |
| `last_receive` | Closing one receive copy preserves pending calls; closing the last receive copy revokes the service and releases its wait pins |
| `exit` | Exit after closing the management handle still revokes queued/accepted calls and reaps the service's directory/stack |
| `fault` | Real user page fault after management close cancels both client wait states; the third task keeps running under the timer |
| `blocked_service` | Kernel termination of a suspended service cancels its own accept and outstanding accepted calls; peers resume normally |
| `client_death` | Termination of an accepted client invalidates its right before reap; its frames/directory are free in the physical bitmap and the next client's FIFO request succeeds |
| `stress` | 128 full 32-byte exchanges alternate clients, plus a zero-byte exchange with arbitrary buffer VAs; overlapping request/response buffers work under 777 timer IRQs without lost data or wait-reference leaks |

Every syscall checks hardware cause/EPC, saved EPC advancing once, r3..r31 and
FCSR preservation, including across client blocking and reply/cancellation.
Every observed IRET checks all restored GPRs/FCSR and the selected PTBR/TCB/
kernel stack. Ready membership remains unique and excludes Blocked/Dead tasks.
Terminal paths clear snapshots, response pointers, reply ownership and wait
metadata; surviving tasks continue using actual syscalls and timer IRQs.
Artifact and probe hashes are compared again before the run can report success.
UART/monitor/emulator logs are retained per case, with no kernel panic.

## Reproduction and preserved artifacts

From the WRM repository, without building:

```sh
python3 -B laix/tests/probe_ipc_request_reply_cpu.py \
  laix/build/acceptance/ipc_request_reply_cpu/inputs/laix.img \
  laix/build/acceptance/ipc_request_reply_cpu/inputs/laix.map \
  --emulator laix/build/acceptance/ipc_request_reply_cpu/inputs/wrm081632 \
  --rom laix/build/acceptance/ipc_request_reply_cpu/inputs/firmware.rom \
  --exchanges 128 \
  --log-dir laix/build/acceptance/ipc_request_reply_cpu/verified_contexts

python3 -B -m unittest discover -s laix/tests -p 'test_*.py'
```

The accepted image/map were copied from the newly available `build/laix.img`
and `build/laix.map`; the probe did not build them. Preserved inputs and logs
are outside Git in `build/acceptance/ipc_request_reply_cpu/`.
`verified_contexts/results.json` records `complete=true` and
`acceptance_complete=true`, all ten outcomes and artifact/probe hashes.
`functional/`, `lifecycle/` and `verified/` retain earlier successful runs.
The older incompatible-image audit in `build/acceptance/ipc_request_reply/`
is historical and is superseded by this CPU acceptance.

| Artifact | SHA-256 |
| --- | --- |
| `inputs/laix.img` | `0198053ee814fc04a33790e99c84dff0745fd7ebb9e44b5f451a015cbe34937f` |
| `inputs/laix.map` | `a35c9ee0ab192781bcdd61ba22ef8325f6eda6bf34a97f72df90945da3b62928` |
| `inputs/wrm081632` | `5e7d9f7f90ccbc7929b894c118fecbecb04fe58165b16f1843f7412026e2448c` |
| `inputs/firmware.rom` | `4e42ef9742fa0b70efca1c8a113482770dda9ce68b113bcf001942a0ede0d0b5` |

The accepted image contains the kernel ABI and original assembly user blob;
it does not contain `ipcAcceptResult` or a compiled M service loop. The M helper
retains its source acceptance; executing it in the first user service image is
a stage-6 acceptance check. These runs also do not establish standalone Raw
send/recv CPU acceptance or service device access. See the
[contract](../docs/IPC_REQUEST_REPLY.md) for the implemented protocol and
[stage 6](../docs/06_USER_SERVICES.md) for the first application service.
