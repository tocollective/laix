# A8 limits and latency acceptance

Accepted on 2026-10-05 for the [published contract](../docs/LIMITS_AND_LATENCY.md).
[Source, compiler, executable and CPU provenance](LIMITS_LATENCY_PROVENANCE.json).
WRM and ROM were reused unchanged; only LA/IX images were built.

## Requirement-to-evidence mapping

| Implementation requirement | Implementation and evidence |
|---|---|
| Separate concurrent/lifetime/domain limits | Contract tables identify pools, per-domain charges, reserves, error codes and persistent retirement counters; allocator/objects/control/grant/transfer source tests |
| Sustainable reply policy | `taskConstructImage` skips reply-exhausted slots; final generation completes, later calls return `EOVERFLOW`; explicit supervised replacement and boot lifetime published |
| Old tokens never regain authority | `test_last_reply_generation_retires_slot_and_replacement_rejects_stale_right`, all-retired creation failure, existing A1/A3/A4/A6 reuse tests; CPU final `0x7fffff02`, repeated right rejection and exhaustion |
| Measure copying/validation/queues/allocation/reaping | `probe_limits_latency_cpu.py` and `probe_device_latency_cpu.py` record real CPU cycle/retirement counters from trapEntry to IRET, including safe-stack reaping |
| State image/memory/workload | Provenance binds 600 KiB latency kernel boot span (2,441,216-byte complete disk) at 32 MiB and 756 KiB Services boot span (2,600,960-byte complete disk) at 2 MiB, 128 MHz, existing WRM/ROM and service ELFs; workload described below |
| Bound every syscall, including unused capacity | Dispatcher work inventory; IPC destination capacity limited before walks; 128 ordinary leaves/eight private user tables per root, alias charging, transactional table/mapping failure; CPU buffer case revalidates an unused portion of a valid 32-byte cross-page capacity |
| Split work if measurements require | Current workloads stay within 500 ms regression budget. No staged work introduced; larger future budgets require staged pin/publication/cancellation design and new acceptance |
| CPU/shared-server admission | 100 Hz RR, one runnable/wait record per domain, task/child quotas, FIFO Service admission, synchronous bounded reply copying; source seven-caller case and CPU four rounds with seven callers |
| Round-robin baseline / priorities only for stated requirements | Contract explicitly excludes hard real-time, priorities/donation and CPU billing; existing scheduler tests and real timer switching |
| Context-switch/IPC measurements before ASID caching | 400 timer IRQ samples plus timed-call/accept/reply samples with full TLB flushing |
| ASID lease protocol if caching added | Conditional requirement closed as not applicable: no caching added; baseline invalidation ordering and existing MMU tests preserved; future lease/reuse/stale-translation conditions published |
| Nonnested trap protocol | Trap assembly unchanged; full register/stack/FCSR tests and CPU context checks. No kernel preemption added |

| Acceptance requirement | Evidence and scope |
|---|---|
| Last-valid / first-invalid generations, no wrap | New A8 reply, memory-space/region/grant, device-operation/extent tests; existing task/handle/endpoint/IRQ/transfer retirement cases; CPU last-valid reply and first-invalid call. Near-exhaustion source/CPU data fixtures avoid millions of redundant calls |
| Long-lived client policy / stale rights | Source client completion, `EOVERFLOW`, death/reap/collect, different-slot replacement, new accepted call and old-token rejection; all eligible slots exhausted returns `ENFILE` without allocation. CPU covers boundary/exhaustion; replacement itself has source evidence |
| Timer/device progress at largest workloads | Real eight-task teardown, 64 KiB populate, 16-page allocate/map/edit; following idle timer IRQ; separate maximum 136-byte Input/512-byte DMA copy with actual device IRQ and canaries |
| Multiple callers / bounded admission / fairness | Source eight-task fixture, one held client and six successful peers; CPU four rounds, seven callers, FIFO order, held-client cancellation, late reply rejection and reference baseline |
| Interruption/cancellation between split stages | Not applicable: no resumable operation. Existing map staging remains unpublished and rollback tests conserve frames/charges. New two-table quota test rejects a batch without partial publication |
| Stale translations after cached-ASID lease reuse | Not applicable: no cached leases. Full flush before PTBR is unchanged; existing MMU source/CPU acceptance remains the baseline, without claiming a new lease campaign |
| Separate real timing from logical tests | Full source suite: 389 tests in 530.308 s, before two additional A8 generation tests. Final A8 suite: eight tests pass. AST timer fixtures never establish the timings below |

## CPU results

| Measured section | Maximum CPU cycles | At 128 MHz |
|---|---:|---:|
| Timer IRQ/context switch, full TLB flush | 10,091 | 0.078836 ms |
| Timed 32-byte call | 21,650 | 0.169141 ms |
| Accept / reply | 15,173 / 17,945 | 0.118539 / 0.140195 ms |
| Allocate 16 zeroed pages | 325,268 | 2.541156 ms |
| Map 16 pages across table boundary | 101,395 | 0.792148 ms |
| Populate 64 KiB | 1,753,847 | 13.701930 ms |
| Unmap 16 pages | 538,123 | 4.204086 ms |
| Final EXIT and eight-task cleanup | 12,144,655 | 94.880117 ms |
| Input maximum snapshot, 136 bytes | 16,206 | 0.126609 ms |
| Device submit / finish 512 bytes | 5,705 / 20,619 | 0.044570 / 0.161086 ms |
| Device IRQ handler | 6,106 | 0.047703 ms |

Each of the eight cleanup tasks reaches its 96-frame, eight-table and 128-leaf
budgets. Fixtures allocate/map through real kernel APIs; aliases expand leaf
work without bypassing frame charging. A trusted test-only helper marks seven
peers dead without reaping, then the last client's real EXIT selects idle and
reaps all eight on the idle stack. It verifies that all directories/stacks are
released and that idle WFI subsequently delivers another timer IRQ. Ordinary
images exclude both preparation helpers.

Maximum admitted public copying is a 16-page loader region populated from a
16-page source spanning two page tables. The probe also rejects 65,537 bytes,
returns quota errors atomically in source tests, and performs maximum unmap and
allocation. Cleanup excludes permanently BUSY owners by quarantine rather than
waiting inside the kernel. Permanent-BUSY/pin conservation remains covered by
A7's checked-source cases.

Device measurement enlarges the first actual Disk user submission to one
approved 512-byte sector and redirects Finish to a writable private user page
with before/after canaries. It checks the complete bytes against the boot disk,
512-byte result, both canaries and bounce-pin release. Input is requested at
32 events. The actual DMA request progresses to completion in 48,200 cycles
(0.376563 ms), within its five-second IRQ deadline plus the 500 ms kernel budget.
No monitor write programs DMA, edits a PTE or patches an instruction.

Timer-to-timer progress is checked against **65,280,000 cycles (510 ms)**; the
observed maximum is 12,224,903 cycles (95.507055 ms), recorded in the CPU report/provenance. IRQ-disabled
sections are checked against **64,000,000 cycles (500 ms)**. These are acceptance
thresholds for this workload campaign, not proven universal WCET. Samples end
before IRET itself; interrupt delivery before the entry breakpoint is excluded.
Pauses and monitor reads add no virtual CPU cycles. API fixture preparation and
its synthetic supervisor invocations are excluded from section measurements.

The ordinary UART image additionally passes request/reply `exchange`/`buffers`
and liveness `fifo`/`boundary`/`cancel` CPU regressions. The buffer probe places its
borrowed return frame below temporary reaper frames and verifies registers at
IRET; a pending timer can legitimately schedule another task before the next
user breakpoint. Host elapsed time, mocked deadlines and source-loop counts
are recorded separately from executed CPU counters.

## Reproduce

Use the existing WRM binary and preserved ROM:

```sh
python3 -B -m unittest discover -s laix/tests -p 'test_*.py'
LAIX_MAIN="$PWD/laix/tests/programs/limits/latency.m" sh laix/build.sh
python3 -B laix/tests/probe_limits_latency_cpu.py \
  laix/build/laix.img laix/build/laix.map --rounds 4
LAIX_CONSOLE=services sh laix/build.sh
python3 -B laix/tests/probe_device_latency_cpu.py \
  laix/build/services.img laix/build/services.map
sh laix/build.sh
python3 -B laix/tests/probe_ipc_request_reply_cpu.py \
  laix/build/laix.img laix/build/laix.map \
  --rom laix/build/acceptance/screen-firmware.rom --case exchange --case buffers \
  --log-dir laix/build/acceptance/limits-latency-ipc
python3 -B laix/tests/probe_ipc_liveness_cpu.py \
  laix/build/laix.img laix/build/laix.map \
  --rom laix/build/acceptance/screen-firmware.rom \
  --case fifo --case boundary --case cancel \
  --log-dir laix/build/acceptance/limits-latency-liveness
python3 -B laix/tools/limits_latency_provenance.py --verify
```

The final provenance record stores immutable copies of latency/Services images
and maps under `build/acceptance/limits-latency/artifacts`, source logs, current
kernel/user/compiler/harness hashes and raw CPU logs. When regenerating evidence,
preserve those image/map copies before rebuilding the ordinary UART image,
then regenerate provenance with `limits_latency_provenance.py`.

## Remaining limits

The reply ABI still has finite boot lifetime and explicit client replacement;
no automatic client state migration is provided. Timing covers two specific
images at 2/32 MiB, not every memory size up to the allocator's 128 MiB maximum
or every grant/device combination. Screen timing, a hard real-time deadline,
priorities/donation, cached ASIDs, kernel preemption and resumable bulk teardown
are separate future work, governed by the contract's measurement/staging rules.
A checked A8 box means the scoped evidence above, including explicitly
inapplicable conditional features, rather than an indefinite RPC lifetime or
universal latency guarantee.
