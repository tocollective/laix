# План микроядра LA/IX

Каждый этап имеет отдельный документ с действиями, проверками и условием
готовности. `[x]` означает подтверждённое выполнение конкретного пункта;
`[ ]` — работу или проверку, которую ещё нужно завершить. Проверка по
исходникам и запуск образа на CPU указываются отдельно.

## Состояние на 2026-10-04

| № | Этап | Состояние |
| --- | --- | --- |
| 1 | [Вход, TrapFrame и диагностика](01_BOOT_TRAPS.md) | Выполнен для supervisor mode; повторно проверен на готовых образах |
| 2 | [Физическая память и MMU](02_MEMORY_MMU.md) | Выполнен для одного CPU: учёт/владение, MMU API, TLB/ASID и W^X; 13 CPU-сценариев на готовом образе |
| 3 | [Пользовательская задача и syscall](03_USER_TASK_SYSCALLS.md) | Выполнен для одной задачи: 11 CPU-сценариев Task/syscall/fault и 9 сценариев копирования user-буферов при EXL=1 |
| 4 | [Планировщик, таймер и IRQ](04_SCHEDULER_IRQ.md) | Passed seven CPU cases and 20,000 timer switches; [acceptance report](../tests/SCHEDULER_ACCEPTANCE.md) |
| 5 | [IPC и права на объекты](05_IPC_RIGHTS.md) | Rights/transport source checks pass; request/reply passes ten CPU cases and 128 exchanges; standalone Raw transport CPU acceptance remains pending |
| 6 | [Сервисы и драйверы вне ядра](06_USER_SERVICES.md) | UART service and optional screen/bitmap services implemented; 11 UART and 14 screen CPU cases pass. Restart and additional failure/stress acceptance remain pending |

## Общий чек-лист

- [x] Завершить этап 1: собственный старт, возврат из trap и аварийный дамп.
- [x] Завершить этап 2: распределитель страниц, операции отображения,
  защита секций и отсутствие обходных алиасов.
- [x] Завершить этап 3: задача в UM, syscall/exit, user faults и
  копирование user-буферов подтверждены на CPU.
- [x] Завершить этап 4: две задачи, yield, вытеснение, idle и завершение задач.
- [ ] Завершить этап 5: блокирующий обмен сообщениями с проверкой прав.
- [ ] Завершить этап 6: приложение использует отдельный консольный сервер;
  отказ сервиса не останавливает ядро.

Первый сквозной результат: две задачи в разных адресных пространствах
обмениваются сообщениями, одна может аварийно завершиться, а другая
продолжает работать. Затем приложение получает услугу от сервера через IPC.

## Как двигаться

The first stage-6 UART text service is implemented: [embedded init](BOOTSTRAP.md)
loads the isolated server and application with exact resource grants. The
[console protocol](CONSOLE_SERVICE.md) and [acceptance record](../tests/CONSOLE_SERVICE_ACCEPTANCE.md)
cover bounded output, errors and ordering. Default UART boot does not initialize
the screen console. The optional [screen boot](SCREEN_IRQ_DMA.md) moves
rendering/font/cache into user components with exclusive NX mappings,
notification-based IRQ waits and a narrow physical-DMA broker. Its eight
implementation items are complete; [acceptance](../tests/SCREEN_IRQ_DMA_ACCEPTANCE.md)
records source and CPU evidence. Continue with restart and additional service
failure/CPU stress.

Next: stage 5 IPC and rights. Stage 4 CPU acceptance is recorded in
[the scheduler report](../tests/SCHEDULER_ACCEPTANCE.md). Вход одной задачи, syscall/exit и уход по user fault подтверждены
в [приёмке этапа 3](../tests/USER_ACCEPTANCE.md), копирование user-буферов —
в [отдельной CPU-приёмке](../tests/USER_BUFFERS_ACCEPTANCE.md).
Планировщику нужен корректный
пользовательский контекст, а блокирующему IPC — возможность переводить
задачи в ожидание и будить их. Сервисы опираются на все эти механизмы.

Этапы не требуют заранее писать ELF-загрузчик, файловую систему или сложные
политики планирования: первые пользовательские программы можно включить
в образ, задачи обходить round-robin, а IPC начать с коротких сообщений.

## Проверка первого этапа

Повторно прошли 43 проверки исходников, три готовых образа и monitor-проверка
BSS, boot info, стека, IRQ и четырёх возвратов из trap. Сборка кода при этой
проверке не запускалась. Подробности — в [этапе 1](01_BOOT_TRAPS.md) и
[отчёте приёмки](../tests/ACCEPTANCE.md).

Не переносить отметки готовности на более поздние изменённые образы без
повторной проверки. Результат supervisor self-test не заменяет запуск
user mode, а наличие таблицы MMU не заменяет распределитель страниц.

Проверки этапа 2: исходники распределителя/MMU и 13 CPU-сценариев
`probe_mmu_cpu.py` на готовом образе без сборки. Подробности — в
[отчёте MMU](../tests/MMU_ACCEPTANCE.md). User faults подтверждены через
IRET; загрузка пользовательских задач этим прогоном не проверяется.

## Контракты машины

- [ABI: регистры, стек, syscall и старт процесса](../../docs/ABI.md).
- [ISA: режимы, исключения, IRET и MMU](../../docs/INSTRUCTIONS.md).
- [Устройства и boot protocol](../../docs/SPECIFICATION.md).
- [Язык M: аппаратные функции и runtime](../../mc/docs/spec/07-hardware.md).
