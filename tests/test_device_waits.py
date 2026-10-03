"""COUNT deadlines and failed device waits from checked M sources, no build."""

import unittest

from source_m import SourceM
from test_kernel import LAIX
from test_console import C, VIDEO, STATUS, VideoRegisters, ConsoleM
from test_videocard import VideoM

WORD_MASK = 0xFFFFFFFF
COUNT_MASK = 0xFFFFFFFFFFFFFFFF
SCRATCH, NOW, REGISTER = 0x200000, 0x200010, 0xFE000000
DISK = C["DISK0_BASE"]


class WaitRegisters(VideoRegisters):
    def __init__(self):
        super().__init__()
        self.clock_step = 100
        self.frequency = 1000
        self.ready_at = None
        self.busy = False
        self.frame = 7
        self.disk_started = False
        self.disk_busy = False
        self.disk_done_at = None
        self.disk_command_writes = []
        self.uploads = []
        self.device_writes = []

    def __getitem__(self, address):
        if address == C["TIMER_FREQUENCY"]:
            return self.frequency
        if address == REGISTER:
            return int(self.ready_at is not None and self.clock_count >= self.ready_at)
        if address == VIDEO + STATUS:
            return C["VIDEO_BUSY"] if self.busy else 0
        if address == VIDEO + 0x24:
            return self.frame
        if address == DISK:
            status = C["DISK_PRESENT"]
            if self.disk_busy:
                return status | C["DISK_BUSY"]
            if self.disk_started:
                if self.disk_done_at is not None and self.clock_count >= self.disk_done_at:
                    return status | C["DISK_DONE"]
                return status | C["DISK_BUSY"]
            return status
        return super().__getitem__(address)

    def __setitem__(self, address, value):
        if VIDEO <= address < VIDEO + C["PAGE_SIZE"] or DISK <= address < DISK + 28:
            self.device_writes.append((address, value))
        if C["VRAM_BASE"] <= address < C["VRAM_BASE"] + 0x400000:
            self.uploads.append((address, value))
        if address == DISK + 20:
            self.disk_started = True
            self.disk_command_writes.append(value)
            # Completion bytes are a fixture, not generated machine code.
            buffer = self[DISK + 16]
            for offset in range(0, C["SECTOR_SIZE"], 4):
                super().__setitem__(buffer + offset, 0x12340000 + offset)
        if address == DISK and value & C["DISK_DONE"]:
            self.disk_started = False
        super().__setitem__(address, value)


class DeadlineTests(unittest.TestCase):
    def vm(self):
        return SourceM(LAIX / "src/drivers/timer.m", WaitRegisters())

    def count(self, vm, address, value):
        vm.memory[address] = value & WORD_MASK
        vm.memory[address + 4] = value >> 32

    def test_hi_lo_hi_retries_a_torn_counter_sample(self):
        class TornCount(dict):
            def __init__(self):
                super().__init__()
                self.highs = iter([4, 5, 5, 5])
                self.lows = iter([WORD_MASK, 3])
                self.reads = []

            def __getitem__(self, address):
                if address == C["TIMER_COUNT_HI"]:
                    self.reads.append("HI")
                    return next(self.highs)
                if address == C["TIMER_COUNT_LO"]:
                    self.reads.append("LO")
                    return next(self.lows)
                return super().__getitem__(address)

        vm = SourceM(LAIX / "src/drivers/timer.m", TornCount())
        vm.call("timerReadCount", SCRATCH)
        self.assertEqual(vm.memory.reads, ["HI", "LO", "HI", "HI", "LO", "HI"])
        self.assertEqual((vm.memory[SCRATCH + 4], vm.memory[SCRATCH]), (5, 3))

    def test_deadline_addition_and_comparison_across_word_and_count_wrap(self):
        for start, rate, seconds in ((0xFFFFFFF0, 1000, 5),
                (COUNT_MASK - 15, 1000, 1), (0x1234567800000000, WORD_MASK, 60)):
            vm = self.vm()
            vm.memory.clock_count, vm.memory.clock_step = start, 0
            vm.memory.frequency = rate
            self.assertTrue(vm.call("timerDeadline", seconds, SCRATCH))
            deadline = (start + rate * seconds) & COUNT_MASK
            self.assertEqual((vm.memory[SCRATCH + 4] << 32) | vm.memory[SCRATCH], deadline)
            for offset, reached in ((-1, False), (0, True), (1, True)):
                self.count(vm, NOW, (deadline + offset) & COUNT_MASK)
                self.assertEqual(bool(vm.call("timerDeadlineReached", NOW, SCRATCH)), reached)

    def test_deadline_frequency_fallback_and_rejected_zero_or_unbounded_settings(self):
        vm = self.vm()
        vm.memory.frequency = 0
        vm.call("timerSetClock", 1234)
        self.assertTrue(vm.call("timerDeadline", 5, SCRATCH))
        self.assertEqual(vm.memory[SCRATCH], 6170)
        for seconds in (0, 61, WORD_MASK):
            self.assertFalse(vm.call("timerDeadline", seconds, SCRATCH))
        vm.call("timerSetClock", 0)
        self.assertFalse(vm.call("timerDeadline", 1, SCRATCH))

    def test_wait_completion_boundary_and_timeout_without_countdown_or_irqs(self):
        for ready_at, completed in ((900, True), (1000, True), (1100, False), (None, False)):
            vm = self.vm()
            vm.memory.ready_at = ready_at
            self.assertFalse(vm.globals["timerReady"])
            vm.controls[0] = C["STATUS_EXL"]
            self.assertEqual(bool(vm.call("waitUntil", REGISTER, 1, 1, False, 1, b"test DONE")), completed)
            self.assertEqual(vm.controls[0], C["STATUS_EXL"])
            self.assertEqual(len(vm.output), 0 if completed else 1)
            if not completed:
                self.assertEqual(vm.output[0], (b"LA/IX: timeout waiting for $s\n", b"test DONE"))
            self.assertLessEqual(vm.memory.clock_count, 1100)
        vm = self.vm()
        vm.memory.frequency = 0
        self.assertFalse(vm.call("waitUntil", REGISTER, 1, 1, False, 1, b"test"))
        self.assertIn(b"invalid timer deadline", vm.output[0][0])

    def test_wait_timeout_survives_full_count_wrap_and_ready_is_immediate(self):
        vm = self.vm()
        vm.memory.clock_count = COUNT_MASK - 200
        self.assertFalse(vm.call("waitUntil", REGISTER, 1, 1, False, 1, b"test"))
        self.assertLess(vm.memory.clock_count, 1100)
        vm = self.vm()
        vm.memory.frequency, vm.memory.ready_at = 0, 0
        self.assertTrue(vm.call("waitUntil", REGISTER, 1, 1, False, 1, b"ready"))
        self.assertEqual(vm.memory.clock_count, 0)
        self.assertEqual(vm.output, [])


class DeviceFailureTests(unittest.TestCase):
    def video(self):
        vm = VideoM()
        vm.memory = WaitRegisters()
        vm.memory.update({VIDEO + 0x1C: 0x400000})
        return vm

    def cache(self):
        vm = SourceM(LAIX / "src/console/font/glyph_cache.m", WaitRegisters())
        vm.globals["video"] = VIDEO
        vm.memory.update({VIDEO + 0x1C: 0x400000, DISK + 4: 100, DISK + 24: 0})
        info = [C["BOOT_INFO_MAGIC"], 40, 0x100000, DISK, 100,
                C["BOOT_LOAD"], 512, 1000, 0, 0]
        vm.memory.update({C["BOOT_INFO"] + i * 4: value for i, value in enumerate(info)})
        self.assertTrue(vm.call("glyphCacheInit", 32))
        vm.memory.device_writes.clear()
        return vm

    def test_busy_video_times_out_before_any_command_or_upload_and_latches_failure(self):
        for operation, args, failure in (("videoSetMode", (1,), False),
                ("videoFill", (0, 640, 0, 8, 1), WORD_MASK),
                ("videoCopy", (0, 640, 0, 0, 640, 0, 8), WORD_MASK),
                ("videoExpand", (0, 640, 0, 640 * 480, 2, 0, 8, 1, 0), WORD_MASK),
                ("videoWriteWords", (640 * 480, SCRATCH, 1), False)):
            vm = self.video()
            vm.memory.busy = True
            self.assertEqual(vm.call(operation, *args), failure)
            self.assertFalse(vm.memory.device_writes)
            self.assertFalse(vm.memory.uploads)
            self.assertIn(b"video BUSY", vm.output[0])
            self.assertEqual(len(vm.output), 1)
            vm.memory.busy = False  # late recovery cannot silently reuse this driver
            self.assertEqual(vm.call(operation, *args), failure)
            vm.call("videoEnable")
            vm.call("videoSetPalette", 0, 0)
            self.assertFalse(vm.memory.device_writes)
            self.assertEqual(len(vm.output), 1)

    def test_stopped_frame_fails_console_once_and_suppresses_subsequent_draws(self):
        vm = ConsoleM()
        self.assertTrue(vm.call("consoleInit"))
        registers = WaitRegisters()
        registers.update(vm.memory)
        vm.memory = registers
        vm.memory.commands.clear()
        vm.call("waitFrame")
        self.assertTrue(vm.call("consoleFailed"))
        self.assertIn("LA/IX: timeout waiting for $s\n", vm.uart)
        self.assertIn("video frame wait failed", vm.failures())
        previous = list(vm.uart)
        vm.call("waitFrame")
        vm.call("putChar", 0x41)
        self.assertEqual(vm.uart, previous)
        self.assertEqual(vm.memory.commands, [])

    def test_busy_video_during_console_init_propagates_failure(self):
        vm = ConsoleM()
        memory = WaitRegisters()
        memory.update(vm.memory)
        memory.busy = True
        vm.memory = memory
        self.assertFalse(vm.call("consoleInit"))
        self.assertTrue(vm.call("consoleFailed"))
        self.assertIn("video mode setup failed", vm.failures())
        self.assertFalse(vm.memory.device_writes)

    def test_disk_busy_and_missing_done_timeout_without_publishing_glyphs(self):
        for busy in (True, False):
            vm = self.cache()
            vm.memory.disk_busy = busy
            self.assertEqual(vm.call("cacheGlyph", 0), vm.globals["CACHE_GLYPHS"])
            self.assertEqual(len(vm.memory.disk_command_writes), 0 if busy else 1)
            self.assertEqual(len(vm.output), 1)
            self.assertIn(b"glyph disk BUSY" if busy else b"glyph disk DONE", vm.output[0])
            self.assertFalse(vm.memory.uploads)
            self.assertEqual(vm.globals["nextSlot"], 0)
            self.assertTrue(all(vm.memory[vm.addresses["pages"] + i * 4] == WORD_MASK
                                for i in range(16)))
            # A late DMA completion stays unconsumed and cannot publish stale data.
            vm.memory.disk_busy = False
            vm.memory.disk_done_at = vm.memory.clock_count
            self.assertEqual(vm.call("cacheGlyph", 0), vm.globals["CACHE_GLYPHS"])
            self.assertFalse(vm.memory.uploads)
            self.assertEqual(len(vm.output), 1)

    def test_delayed_disk_completion_uploads_once_and_cache_hits_do_not_wait(self):
        vm = self.cache()
        vm.memory.disk_done_at = 300
        self.assertEqual(vm.call("cacheGlyph", 0), 0)
        self.assertEqual(len(vm.memory.uploads), 128)
        self.assertEqual(vm.output, [])
        self.assertEqual(vm.globals["nextSlot"], 1)
        clock = vm.memory.clock_count
        self.assertEqual(vm.call("cacheGlyph", 15), 15)
        self.assertEqual(vm.memory.clock_count, clock)
        self.assertEqual(len(vm.memory.disk_command_writes), 1)
        self.assertEqual(len(vm.memory.uploads), 128)


if __name__ == "__main__":
    unittest.main()
