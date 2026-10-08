"""G5 staged teardown: bounded reaping sections, interruption and cancellation.

Source-level checks of `taskReap` and the idle loop; they establish ordering and
resource conservation, not CPU timing (see probe_limits_latency_cpu.py).
"""
import re
import unittest

from source_m import LAYOUT as C
from test_ipc_handles import error
from test_runtime_tasks import fixture, create
from test_task import TaskEntered, USER_DATA
from test_kernel import LAIX
from test_idle import IdleTaskM, IdleMachine
from test_timer_irq import TimerMemory

EMPTY, DEAD = 0, 3
IDLE_STAGE = 1  # TASK_IDLE_STAGE: a stage ran and more roots remain


def spawn_dead(vm, count):
    """Create and terminate `count` children, leaving all of them unreaped."""
    children = [create(vm) for _ in range(count)]
    for child in children:
        vm.invoke(C['SYS_TASK_TERMINATE'], child, 7)
        assert vm.result(1)[0] == 0
        assert vm.field('state', child) == DEAD and not vm.field('reaped', child)
    return children


def stage(vm):
    """One selected-stack reaping section; returns whether another root waits."""
    task = vm.globals['currentTask']
    vm.cpu_sp = vm.memory[task + vm.task_type.field('kernelStackTop').offset] - 32
    vm.controls[0] = C['STATUS_EXL']
    return vm.call('taskReap')


def held(vm, child):
    """Every physical page one dead task keeps until its own stage commits."""
    return [vm.field('directory', child), vm.field('kernelStackBottom', child), *vm.pages(child)]


def free(vm):
    return len(vm.free_pages())


class StagedReapTests(unittest.TestCase):
    def test_stage_bound_and_idle_marker_are_the_documented_constants(self):
        source = (LAIX / 'src/task/task.m').read_text()
        self.assertEqual(re.search(r'let TASK_REAP_STAGE_TASKS: UWord = (\d+)', source)[1], '1')
        self.assertEqual(re.search(r'let TASK_IDLE_STAGE: UWord = (\d+)', source)[1], str(IDLE_STAGE))

    def test_each_stage_commits_one_root_and_pins_every_other_dead_task(self):
        vm = fixture(1)
        baseline = free(vm)
        children = spawn_dead(vm, 7)
        pinned = {child: held(vm, child) for child in children}
        for child in children:
            self.assertTrue(all(pinned[child]))
            self.assertTrue(all(not vm.call('physicalPageAvailable', page) for page in pinned[child]))
        freed = []
        for n in range(1, 8):
            before = free(vm)
            pending = stage(vm)
            freed.append(free(vm) - before)
            self.assertEqual(bool(pending), n < 7, f'stage {n}')
            for position, child in enumerate(children):
                if position < n:
                    # Committed: nothing of it remains and its pages are reusable.
                    self.assertTrue(vm.field('reaped', child))
                    self.assertEqual((vm.field('directory', child), vm.field('kernelStackBottom', child)), (0, 0))
                    self.assertTrue(all(vm.call('physicalPageAvailable', page) for page in pinned[child]))
                else:
                    # Not yet staged: root, stack and frames are untouched, so no
                    # partial mapping is visible and nothing is freed early.
                    self.assertFalse(vm.field('reaped', child))
                    self.assertEqual(held(vm, child), pinned[child])
                    self.assertTrue(all(not vm.call('physicalPageAvailable', page) for page in pinned[child]))
        self.assertEqual(len(set(freed)), 1, 'every stage tears down one comparable root')
        self.assertEqual(free(vm), baseline)
        self.assertFalse(stage(vm), 'nothing is left to stage')

    def test_stage_order_is_by_slot_so_no_dead_task_is_skipped(self):
        vm = fixture(1)
        children = spawn_dead(vm, 5)
        for expected in range(1, 6):
            stage(vm)
            self.assertEqual([c for c in children if vm.field('reaped', c)], children[:expected])

    def test_creation_between_stages_never_reuses_a_pinned_slot(self):
        vm = fixture(1)
        children = spawn_dead(vm, 4)
        self.assertTrue(stage(vm))
        self.assertEqual(vm.field('state', children[0]), EMPTY)
        pinned = {child: held(vm, child) for child in children[1:]}
        replacement = create(vm)
        # Only the committed slot is Empty; the three pinned ones stay Dead.
        self.assertEqual(replacement & 255, children[0] & 255)
        self.assertGreater(replacement >> 8, children[0] >> 8)
        for child in children[1:]:
            self.assertEqual(vm.field('state', child), DEAD)
            self.assertEqual(held(vm, child), pinned[child])
        # The replacement's frames are disjoint from every page still pinned.
        self.assertTrue(set(held(vm, replacement)).isdisjoint({p for pages in pinned.values() for p in pages}))
        while stage(vm):
            pass
        self.assertTrue(all(vm.field('reaped', c) for c in children[1:]))
        self.assertEqual(vm.field('state', replacement), 1)
        self.assertFalse(vm.field('reaped', replacement))
        self.assertTrue(all(held(vm, replacement)))

    def test_termination_arriving_between_stages_joins_the_pending_set(self):
        vm = fixture(1)
        children = spawn_dead(vm, 3)
        late = create(vm)
        self.assertTrue(stage(vm))
        # Cancellation while teardown is in flight: a live task is terminated.
        vm.invoke(C['SYS_TASK_TERMINATE'], late, 9)
        self.assertEqual(vm.result(1)[0], 0)
        self.assertEqual(vm.field('state', late), DEAD)
        self.assertFalse(vm.field('reaped', late))
        steps = 0
        while stage(vm):
            steps += 1
        # Two roots remained from before, plus the late one: three stages, two pending.
        self.assertEqual(steps, 2)
        self.assertTrue(all(vm.field('reaped', c) for c in children + [late]))
        self.assertEqual(vm.field('state', late), EMPTY)

    def test_second_terminate_and_early_collect_between_stages(self):
        vm = fixture(1)
        children = spawn_dead(vm, 2)
        baseline = held(vm, children[1])
        self.assertTrue(stage(vm))
        # The pending child is already dead: terminate reports ESRCH.
        vm.invoke(C['SYS_TASK_TERMINATE'], children[1], 1)
        self.assertEqual(vm.result(1)[0], error(3))
        # Collection hands over the completion without the reclaimed flag, while
        # the root stays pinned until its own stage.
        vm.invoke(C['SYS_TASK_COLLECT'], children[1], USER_DATA)
        self.assertEqual(vm.result(1)[0], 0)
        event = vm.event()
        self.assertEqual((event[0], event[2], event[3]), (children[1], DEAD, 7))
        self.assertEqual(event[4] & C['TASK_EVENT_RECLAIMED'], 0)
        self.assertEqual(held(vm, children[1]), baseline)
        self.assertFalse(stage(vm))
        self.assertEqual(vm.field('state', children[1]), EMPTY)
        self.assertTrue(all(vm.call('physicalPageAvailable', page) for page in baseline))

    def test_completion_reports_reclaimed_only_after_its_own_stage(self):
        vm = fixture(1)
        children = spawn_dead(vm, 2)
        reclaimed = C['TASK_EVENT_RECLAIMED']
        flags = []
        for _ in range(3):
            vm.invoke(C['SYS_TASK_INSPECT'], children[1], USER_DATA)
            self.assertEqual(vm.result(1)[0], 0)
            flags.append(vm.event()[4] & reclaimed)
            stage(vm)
        self.assertEqual(flags, [0, 0, reclaimed])

    def test_budget_of_a_pinned_task_is_released_only_by_its_stage(self):
        vm = fixture(1)
        children = spawn_dead(vm, 2)
        open_budgets = lambda: sum(bool(vm.memory[vm.table_base('spaceBudgets') + i * vm.decls['spaceBudgets'].sym.type.target.size +
                                                   vm.decls['spaceBudgets'].sym.type.target.field('directory').offset])
                                   for i in range(vm.globals['spaceBudgetCount']))
        before = open_budgets()
        stage(vm)
        self.assertEqual(open_budgets(), before - 1)
        stage(vm)
        self.assertEqual(open_budgets(), before - 2)


class StagedReapDeviceTests(unittest.TestCase):
    """A task with a BUSY DMA operation neither spends a stage nor keeps idle awake."""

    def test_non_quiescent_task_is_neither_staged_nor_pending(self):
        from test_simple_services import kernel_fixture
        vm = kernel_fixture()
        for id in (1, 2, 3):
            vm.call('taskEnqueue', vm.call('taskGet', id))
        with self.assertRaises(TaskEntered):
            vm.call('taskStart', 1000000)
        # Task 1 runs. Task 2 owns a BUSY DMA operation; task 3 is quiescent.
        self.assertEqual(vm.globals['currentTask'], vm.call('taskGet', 1))
        vm.call('taskYield', vm.field_address('context', 1))
        self.assertEqual(vm.globals['currentTask'], vm.call('taskGet', 2))
        self.assertGreater(vm.call('deviceSubmit', 2, 0, 16, 1, 0), 0)
        pinned = {2: held(vm, 2), 3: held(vm, 3)}
        vm.call('taskFinish', vm.field_address('context', 2), 9, False)
        current = (vm.globals['currentTask'] - vm.table_base('tasks')) // vm.task_type.size + 1
        victim = 3 if current != 3 else 1
        vm.call('taskTerminateChecked', vm.field_address('context', current), victim, 4)
        self.assertEqual(vm.field('state', victim), DEAD)
        self.assertFalse(stage(vm))
        self.assertTrue(vm.field('reaped', victim))
        self.assertFalse(vm.field('reaped', 2))
        self.assertEqual(held(vm, 2), pinned[2])
        # Still BUSY: nothing is staged and the idle loop is not told to poll again.
        self.assertFalse(stage(vm))
        self.assertEqual(held(vm, 2), pinned[2])
        vm.memory.complete()
        self.assertFalse(stage(vm))
        self.assertTrue(vm.field('reaped', 2))
        self.assertTrue(all(vm.call('physicalPageAvailable', p) for p in pinned[2]))


class StagedReapIdleTests(unittest.TestCase):
    """The idle loop resumes staging after an IRQ window and does not sleep early."""

    def idle_with_dead(self, count):
        vm = IdleTaskM()
        vm.memory = TimerMemory(vm)
        for id in range(1, count + 1):
            self.assertEqual(vm.call('taskCreate'), id)
        with self.assertRaises(TaskEntered):
            vm.call('taskStart', 1000000)
        frame = vm.field_address('context', 1)
        for id in range(2, count + 1):
            self.assertEqual(vm.call('taskTerminateChecked', frame, id, 5), frame)
        self.assertEqual(vm.call('taskFinish', frame, 0, False), vm.idle_address('context'))
        vm.controls[0] = 0
        self.assertEqual([vm.field('state', id) for id in range(1, count + 1)], [DEAD] * count)
        return vm

    def test_idle_poll_reports_the_stage_marker_until_the_last_root_commits(self):
        vm = self.idle_with_dead(3)
        vm.cpu_sp = vm.idle_field('kernelStackTop') - 64
        results = [vm.call('taskIdlePoll') for _ in range(3)]
        self.assertEqual(results, [IDLE_STAGE, IDLE_STAGE, 0])
        self.assertEqual([vm.field('reaped', id) for id in (1, 2, 3)], [1, 1, 1])
        self.assertEqual(vm.call('taskIdlePoll'), 0)

    def test_idle_assembly_polls_again_in_an_irq_window_before_it_sleeps(self):
        vm = self.idle_with_dead(3)
        machine = IdleMachine(vm)
        windows = []

        def inject(cpu, statement):
            if statement.op == 'mtcr' and statement.args == ['status', 'r1']:
                windows.append(sum(bool(vm.field('reaped', id)) for id in (1, 2, 3)))

        machine.run(inject)
        self.assertEqual(machine.stop, 'sleep')
        # One IRQ window after each stage that left work, then a single sleep.
        self.assertEqual(windows, [1, 2])
        self.assertEqual(machine.sleeps, 1)
        self.assertEqual(machine.irq_count, 0)
        self.assertEqual([vm.field('reaped', id) for id in (1, 2, 3)], [1, 1, 1])

    def test_irq_taken_between_stages_does_not_lose_or_repeat_a_stage(self):
        vm = self.idle_with_dead(3)
        machine = IdleMachine(vm)
        raised = []

        def inject(cpu, statement):
            # Raise the timer line as the first window opens: the IRQ is taken
            # between stage one and stage two.
            if statement.op == 'mtcr' and statement.args == ['status', 'r1'] and not raised:
                self.assertEqual(sum(bool(vm.field('reaped', id)) for id in (1, 2, 3)), 1)
                vm.memory.advance(vm.memory.value)
                raised.append(True)

        machine.run(inject)
        self.assertEqual(machine.stop, 'sleep')
        self.assertEqual(machine.irq_count, 1)
        self.assertEqual(machine.sleeps, 1)
        self.assertEqual([vm.field('reaped', id) for id in (1, 2, 3)], [1, 1, 1])
        self.assertFalse(vm.memory.active())


class LatencyBudgetGuardTests(unittest.TestCase):
    """The shared section budget and per-kind ceilings agree with the published contract."""

    def test_budget_ceilings_and_documents_stay_in_agreement(self):
        import probe_limits_latency_cpu as probe
        self.assertEqual(probe.SECTION_BUDGET, 2_560_000)  # 20 ms at 128 MHz
        self.assertEqual(probe.SECTION_BUDGET * 1000 // probe.CLOCK, 20)
        # Staged reaping must stay below one populate-class operation.
        self.assertTrue(all(0 < ceiling <= probe.SECTION_BUDGET for ceiling in probe.KIND_CEILINGS.values()))
        self.assertIn('reap_stage', probe.KIND_CEILINGS)
        self.assertIn('syscall_1', probe.KIND_CEILINGS)
        for name in ('LIMITS_AND_LATENCY.md', 'GAP_05_KERNEL_LATENCY.md'):
            self.assertIn('2,560,000', (LAIX / 'docs' / name).read_text(), name)
        for name in ('probe_loader_cpu.py', 'probe_device_latency_cpu.py'):
            self.assertIn('SECTION_BUDGET', (LAIX / 'tests' / name).read_text(), name)

    def test_the_published_budget_is_not_the_unstaged_one(self):
        # The A8 figure may appear as history, never as the current budget.
        for name in ('LIMITS_AND_LATENCY.md', 'FILES_LOADER.md', 'TARGET_WORKLOAD.md'):
            text = ' '.join((LAIX / 'docs' / name).read_text().split())
            self.assertIsNone(re.search(r'budget[^.]{0,60}\bis \**64,000,000', text), name)


if __name__ == '__main__':
    unittest.main()
