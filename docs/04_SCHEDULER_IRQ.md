# Этап 4. Планировщик, таймер и IRQ

[План ядра](KERNEL.md) · [Предыдущий этап](03_USER_TASK_SYSCALLS.md) · [Следующий этап](05_IPC_RIGHTS.md)

Status: round-robin yield, timer preemption and supervisor WFI idle passed
source checks and seven CPU cases on a preserved ready image, including
20,000 timer switches. See [CPU acceptance](../tests/SCHEDULER_ACCEPTANCE.md).
No build was performed.
Цель — две задачи с независимыми контекстами,
переключение сначала по yield, затем по таймеру, ожидание и завершение.
Зависимости: память и хотя бы одна работающая user-задача из этапов 2–3.

## Что сделать

### Учёт задач и добровольное переключение

- [x] Расширить TCB: состояние, сохранённый TrapFrame, каталог/PTBR,
  ASID, границы kernel-стека, причина ожидания и код завершения.
- [x] Определить состояния Ready, Running, Blocked, Dead и разрешённые
  переходы. Ready-задача должна быть в очереди ровно один раз.
- [x] Реализовать ограниченную таблицу задач и очередь round-robin;
  для начала одной аппаратной нити и одного потока на задачу достаточно.
- [x] Добавить syscall yield: сохранить контекст текущей задачи,
  продвинуть её EPC и выбрать следующую Ready-задачу.
- [x] Подготовить первые синтетические кадры задач и общий путь их восстановления.
- [x] Определить интерфейс выбора контекста: trapDispatch возвращает
  указатель на выбранный кадр, а эпилог восстанавливает его через общий путь.
- [x] При выборе задачи согласованно менять текущий TCB, PTBR/ASID,
  KERNEL_SP и границы стека; сохранять отображения пролога и обоих стеков.
- [x] Переключать каталог и выполнять TLB-протокол до возврата в выбранный
  контекст; не терять пользовательские tp, ra, sp и FCSR.
- [x] Не возвращать завершённую задачу в Ready; освобождать её kernel-стек
  и каталог после завершения их использования CPU.

Для syscall сохранённый EPC продвигается на 4. Для IRQ аппаратный EPC уже
указывает на следующую невыполненную инструкцию: дополнительно сдвигать
его нельзя. Это разные причины входа в один планировщик.

### Контракт добровольного планировщика

`src/task/task.m` хранит `tasks[8]`, `currentTask` и кольцевую очередь ID.
ID и ASID равны номеру слота + 1. Dead-слоты пока не переиспользуются:
таблица ограничивает общее число успешно созданных задач за загрузку.
Неудачное создание оставляет Empty и откатывает все выделенные ресурсы.
`taskPrepare()` создаёт первую задачу, `taskCreate()` — следующие;
`main.m` создаёт две задачи с отдельными каталогами, кодом, данными и стеками.

The above describes the legacy scheduler fixtures. Normal boot now calls
[trusted embedded init](BOOTSTRAP.md), which stages `Created` tasks through
`taskCreateImage`, installs their startup/resource grants and publishes both
through `taskPublish` before `taskStart`. The original demo APIs remain
kernel-only and reject creation once scheduling has started. The scheduler
CPU probe's natural case follows the new server/client boot when bootstrap
symbols are present; its yield/timer/lifecycle fixtures keep the original blob.

Допустимы `Empty → Ready`, `Ready → Running`,
`Running → Ready/Blocked/Dead`, `Blocked → Ready`.
`taskEnqueue()` — единственная публикация Ready; флаг `queued` запрещает
повторную постановку. Выбор удаляет голову и переводит задачу в Running.
`taskBlock(frame, reason)` сохраняет кадр и ненулевую причину ожидания;
`taskWake(id)` будит только Blocked и сбрасывает причину. Это kernel-интерфейс
для будущего IPC, syscall ожидания пока отсутствует.
Queue mutations require `IE=0` or `EXL=1`; PIC ENABLE may remain active.
`taskWake()` saves STATUS, disables IE, rechecks Blocked, publishes Ready,
and restores STATUS. Block, finish and selection run with EXL held, so a
wakeup cannot enqueue Dead or race the removal of the running task.

Syscall 2, M-обёртка `yield()`: результат r1=0, EPC += 4 один раз,
текущая задача ставится в хвост и выбирается голова. При одной задаче
восстанавливается она же. User demo выводит ID, делает yield, выводит ID
ещё раз и завершается: user-маркеры двух задач идут в порядке `121\n2\n`
(между ними также выводятся строки диагностики ядра).
Timer preemption may change this demo order; the sequence above describes
cooperative yields without an intervening timer expiry.

`trapDispatch(frame): *TrapFrame` возвращает живой кадр для обычного
возврата и supervisor self-test, либо TCB-кадр выбранной задачи. Начальные
кадры и последующие переключения используют общий assembly restore/IRET.
Сохраняются все GPR, включая tp/ra/sp, FCSR и STATUS. Каталог активируется
через `FENCE → TLBI.ALL → MTCR PTBR` с ASID выбранной задачи;
кеширование ASID пока не применяется. Перед возвратом согласованно
обновляются `currentTask`, `KERNEL_SP` и обе границы kernel-стека.

Kernel-стек — три смежных физических кадра: guard + 8 КиБ RW/NX supervisor.
`allocPageRun()` не резервирует частичный диапазон при фрагментации/OOM.
MMU убирает guard из общих таблиц RAM, поэтому все существующие и новые
каталоги сохраняют одинаковые отображения пролога, TCB и обоих стеков.
User-стек находится в собственном каталоге каждой задачи.

Exit/fault переводит только текущую задачу в Dead и выбирает Ready.
When Ready is empty, `currentTask` selects the separate `idleTask` TCB.
Its reserved ID 0 cannot be obtained through `taskGet()` or enqueued, and
does not consume any of the eight user slots. Idle shares the kernel root
with ASID 0 and owns a separate guard + 8 KiB supervisor RW/NX stack under
allocator owner 9. This stack remains allocated for the scheduler lifetime.
The initial frame has zero GPR/FCSR, SP at its stack top, EPC at
`taskKernelResume` and `STATUS=EXL`. IRET enters with IE=UM=EXL=0.
Selection updates the current TCB, PTBR and trusted stack slots together.

`taskIdlePoll()` checks Ready with CPU IRQs disabled. Ready work dispatches
immediately through the common restore path, without waiting for a timer.
Otherwise `timerCanSleep()` requires an initialized timer, a nonzero RELOAD,
periodic ENABLE and an unmasked timer PIC line before allowing WFI.
Assembly keeps IE=0 through the empty check, FENCE and WFI. On WRM an
asserted IRQ line releases WFI even with IE=0; the level request remains
pending because no handler can acknowledge it in that interval. Only after
WFI returns does idle enable IE, allowing the IRQ handler to acknowledge
the timer and check Ready with EXL held. An empty IRQ return loops back to
disable IE and check the queue again. This single-CPU protocol covers IRQs
pending before the check, arriving before WFI, and arriving during sleep.
The periodic timer remains enabled even after all user tasks terminate.

При другом выбранном кадре эпилог
переносит SP на выбранный kernel-стек, сохраняет указатель кадра в callee-saved
r10 и вызывает `taskReap()`. Проверка реального SP через `taskKernelSp()`
не допускает освобождение используемого стека. Затем уничтожается неактивный
каталог, освобождаются user-кадры, восстанавливается supervisor-алиас guard
с TLBI и освобождается весь kernel-стек. Контекст Dead остаётся в TCB.

`tests/test_scheduler.py` интерпретирует проверенный M AST и assembly:
512 переключений двух разных полных контекстов, кольцо из восьми задач,
одиночный yield, запрет дублей/Dead/Blocked, блокировку и повторное пробуждение,
разные данные при одинаковом VA, ASID/TLB/слова входа, общие отображения
стеков, фрагментацию/OOM и освобождение после перехода на выбранный стек.
Это проверка алгоритмов исходников и синтетических аппаратных переходов;
она не подтверждает исполнение нового образа на CPU.

### Таймер и обработчик IRQ

- [x] Dispatch CAUSE=0 separately from synchronous exceptions and read PIC CLAIM.
- [x] Provide the timer branch before enabling PIC or user interrupts.
- [x] Derive RELOAD from TIMER FREQUENCY, falling back to boot CLOCK; reject
  zero frequency, zero quantum rate and rates that produce a zero period.
- [x] Program TIMER RELOAD/CONTROL and enable only IRQ 2 in PIC.
- [x] Write 1 to TIMER STATUS.EXPIRED before selecting or restoring a context.
  CLAIM does not acknowledge a device; WRM has no PIC EOI.
- [x] Rotate the running task on quantum expiry, preserving IRQ EPC. Protect
  the ready queue and serialize block, wake, finish and selection.
- [x] Mask an unexpected asserted line before printing a UART diagnostic.
  An empty CLAIM is diagnosed as spurious; an invalid claim masks all lines.
  A timer claim without EXPIRED is masked as an unexpected request.
- [x] Set user PIE only after timer initialization. Enter supervisor idle
  with IE=EXL=0; enable IE only after the masked WFI returns.
- [x] Keep EXL=1 throughout dispatch and restoration. Nested IRQs require
  a separate design for TRAP_SAVED_R1 and saved control registers.

`src/drivers/timer.m` initializes the timer while CPU IE and PIC ENABLE are
zero. `taskStart(kernelBootInfo.clock)` prepares the idle frame and scheduler,
then initializes the timer before selecting the first task. The default
`SCHEDULER_QUANTUM_HZ=100` gives `RELOAD=frequency/100`; the frequency is
hardware time, independent of emulator CPU throughput. CONTROL=3 restarts
periodic countdown, stale EXPIRED is cleared first, and PIC ENABLE=4 is the
last device-enable write. CPU IE stays clear until IRET restores PIE.

The timer's MMIO page is `0xFD003000` (CONTROL at `0xFD003014`);
`0xFD001000` belongs to the keyboard. `tests/test_kernel.py` compares timer
and PIC addresses, register offsets and flags against the emulator headers,
independently of the constants used by the device fixtures.

`tests/test_timer_irq.py` interprets the checked M and assembly sources with
a level-triggered PIC and W1C timer fixture. It covers frequency/fallback and
invalid settings, stale expiry, activation order, preemption without yield,
all GPR/FCSR and unchanged IRQ EPC, coalesced expiry, single-task resumption,
unexpected line masking, spurious claims, idle wakeup and exit with an IRQ
pending. These are source-level checks, not execution of a built CPU image.

`tests/test_idle.py` interprets the idle assembly and the actual M queue
check with a level IRQ/WFI fixture. It covers immediate Ready dispatch,
pending IRQs at each queue-to-WFI instruction boundary, wakeup during sleep,
empty IRQ returns, the private stack's shared supervisor mappings and guard,
exclusion of idle from the user queue, missing wakeup sources and stack OOM.
These synthetic transitions do not verify CPU pipeline execution.

- [x] Add COUNT deadlines with coherent HI–LO–HI reads and `waitUntil`;
  use them for video BUSY, glyph-disk BUSY/DONE and console FRAME waits.
  Timeouts report a UART diagnostic and fail the driver instead of hanging.

`timerReadCount()` retries if COUNT_HI changes across the COUNT_LO read.
COUNT advances independently of countdown CONTROL, PIC ENABLE and CPU IE,
so waits work during console initialization and in non-nested trap handlers.
`timerDeadline(seconds, out)` supports 1..60 seconds and derives ticks from
FREQUENCY or boot CLOCK, provided by `kernelInit()` before console setup.
Two-word addition/comparison handles low-word carry and full COUNT wrap;
there is no reliance on the number of IRQs or emulator CPU throughput.

`waitUntil(register, mask, value, changed, seconds, device)` polls equality
(or inequality when `changed=true`), returning Bool. An observed completion
wins at the deadline boundary. Unavailable clock information or invalid
settings fail immediately with a diagnostic. The helper does not enable IRQs,
change TIMER CONTROL, or sleep in WFI while the caller may have IE=0.

Video BUSY and FRAME have a one-second timeout. The first timeout disables
further drawing, uploads and control writes for this driver instance;
`videoSetMode()` and `videoWaitFrame()` return false, drawing commands return
`VIDEO_WAIT_TIMEOUT=0xFFFFFFFF`, and VRAM uploads return false. Console setup
checks the mode result; a failed FRAME wait latches `consoleFailed()` and
stops subsequent output. The UART identifies the failed wait.

Glyph-cache disk BUSY and DONE have five-second timeouts. BUSY expiry issues
no new disk command; DONE expiry uploads no data and publishes no cache slot.
Either timeout disables the cache until explicit initialization. An abandoned
DMA retains its static RAM buffer, so late completion cannot write freed
memory or expose an unfinished glyph. No device abort is assumed.

`tests/test_device_waits.py` checks torn reads, boundary completion, frequency
fallback, invalid settings, both counter wraps, stalled BUSY/DONE/FRAME,
console failure propagation, late disk completion and successful cache hits.
These checks interpret source and device fixtures without building an image.

EXPIRED хранит наличие события, а не число пропущенных периодов.
Для прошедшего времени использовать свободный COUNT с согласованным
чтением старшей/младшей половины; не считать каждый IRQ точным секундомером.

### Ожидание, idle и завершение

- [x] Добавить перевод Running → Blocked и пробуждение Blocked → Ready,
  пока хотя бы для тестового события; предусмотреть будущие IPC-очереди.
- [x] Create a separate idle task with a trusted stack and WFI.
- [x] Check Ready and enter WFI without losing a wakeup event; require
  a configured IRQ source before sleeping.
- [x] При exit или user fault завершать только соответствующую задачу;
  другой пользовательский контекст продолжает работать.
- [x] При отсутствии задач выбрать idle/явное завершение теста, а не
  восстанавливать освобождённый кадр.

## Что проверить

- [x] Две задачи поочерёдно работают через yield с предсказуемым порядком.
- [x] Без yield обе задачи получают CPU благодаря таймеру; одна busy-loop
  не удерживает процессор навсегда.
- [x] Проверить десятки тысяч переключений с разными GPR, FCSR и tp/TLS.
- [x] Одинаковый user-адрес в двух каталогах содержит разные данные;
  переключение не оставляет старую TLB-трансляцию.
- [x] В очереди нет дублей; Blocked/Dead никогда не исполняются.
- [x] Пробуждение работает до, во время и после решения перейти в idle.
- [x] Отсутствие Ready-задач приводит к WFI, событие возвращает выполнение.
- [x] Прерывание приходит на корректный kernel-стек выбранной задачи;
  current TCB, PTBR и низкие слова стека соответствуют друг другу.
- [x] После IRQ выполнение продолжается с исходного аппаратного EPC.
- [x] EXPIRED действительно снят; нет непрерывного повторного IRQ без нового периода.
- [x] Exit/fault одной задачи не останавливает другую и не оставляет
  утечек страниц, ожидающих событий или указателей на её стек.
- [x] Пройти прежние boot/trap/user-тесты с включённым таймером.

CPU evidence and reproduction commands are in
[`tests/SCHEDULER_ACCEPTANCE.md`](../tests/SCHEDULER_ACCEPTANCE.md).
The long run uses a CPU-programmed 1 kHz stress quantum; seven functional
cases retain the default 100 Hz quantum. Every stress switch compares full
GPR/FCSR/tp contexts, IRQ EPC, the selected TCB/PTBR/stack, Ready membership
and the next timer COUNT deadline. Idle boundary coverage combines actual
CPU block/wake/WFI with checked-source instruction-boundary fixtures.
All 179 source/tool tests pass without building.

## Когда этап готов

Две изолированные задачи работают и с yield, и с вытеснением;
ожидающая задача не занимает CPU, idle просыпается, завершение одной
задачи не разрушает другую. Приоритеты, SMP и real-time политики позже.
Контракты — [IRQ и IRET](../../docs/INSTRUCTIONS.md#interrupts) и
[PIC/таймер](../../docs/SPECIFICATION.md).
