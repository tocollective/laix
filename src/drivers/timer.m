// One timer expiry is one scheduling quantum, not elapsed-time accounting.
import { CR_STATUS, STATUS_IE, PIC_ENABLE, PIC_CLAIM, PIC_NO_IRQ,
    PIC_LINE_COUNT, TIMER_COUNT_LO, TIMER_COUNT_HI, TIMER_FREQUENCY,
    TIMER_RELOAD, TIMER_CONTROL, TIMER_STATUS,
    TIMER_IRQ, TIMER_IRQ_MASK, TIMER_ENABLE, TIMER_PERIODIC, TIMER_EXPIRED } from "../arch/wrm081632/defs.m"
import { debugPrint } from "debug_uart.m"
import { irqNotify, irqTimerTick } from "irq.m"

let mut timerReady: Bool
let mut timerBootClock: UWord
let MAX_WAIT_SECONDS: UWord = 60

// M has 32-bit words; keep COUNT and deadlines as two unsigned halves.
type TimerCount {
    lo: UWord,
    hi: UWord,
}

let timerSetClock(clock: UWord): Void { timerBootClock = clock }

let timerRate(): UWord {
    let frequency: *volatile UWord = TIMER_FREQUENCY as *volatile UWord
    let rate: UWord = frequency[0]
    if rate != 0 return rate
    return timerBootClock
}

// COUNT runs even when countdown CONTROL and CPU interrupts are disabled.
let timerReadCount(count: *mut TimerCount): Void {
    let low: *volatile UWord = TIMER_COUNT_LO as *volatile UWord
    let high: *volatile UWord = TIMER_COUNT_HI as *volatile UWord
    while true {
        let hi: UWord = high[0]
        let lo: UWord = low[0]
        if high[0] == hi {
            count.lo = lo
            count.hi = hi
            return
        }
    }
}

let timerDeadline(seconds: UWord, deadline: *mut TimerCount): Bool {
    let rate: UWord = timerRate()
    if seconds == 0 || seconds > MAX_WAIT_SECONDS || rate == 0 return false
    timerReadCount(deadline)
    // Bounded repeated addition avoids overflowing frequency * seconds.
    for i: UWord in 0..seconds {
        let previous: UWord = deadline.lo
        deadline.lo += rate
        if deadline.lo < previous deadline.hi += 1
    }
    return true
}

let timerDeadlineReached(now: *TimerCount, deadline: *TimerCount): Bool {
    let mut high: UWord = now.hi - deadline.hi
    if now.lo < deadline.lo high -= 1
    // Sign of the modular 64-bit difference; waits are shorter than 2^63.
    return high & (1 << 31) == 0
}

// Poll for equality, or a change when changed=true. An observed completion
// wins at the deadline boundary. Never sleep here: callers may have IE=0.
let waitUntil(register: *volatile UWord, mask: UWord, value: UWord,
    changed: Bool, seconds: UWord, device: *UByte): Bool {
    if (((register[0] & mask) == value) != changed) return true
    let mut deadline: TimerCount
    let mut now: TimerCount
    if !timerDeadline(seconds, &mut deadline) {
        debugPrint("LA/IX: invalid timer deadline waiting for $s\n", device)
        return false
    }
    while true {
        if (((register[0] & mask) == value) != changed) return true
        timerReadCount(&mut now)
        if timerDeadlineReached(&now, &deadline) {
            debugPrint("LA/IX: timeout waiting for $s\n", device)
            return false
        }
    }
}

let timerPeriod(frequency: UWord, quantumHz: UWord): UWord {
    // Reject configurations that would program RELOAD=0 (one tick on WRM).
    if frequency == 0 || quantumHz == 0 || quantumHz > frequency return 0
    return frequency / quantumHz
}

// Called only after the scheduler and trap branch exist, with CPU IRQs off.
// PIC becomes live here; IE becomes live only at the subsequent IRET.
let timerInit(clock: UWord, quantumHz: UWord): Bool {
    let enabled: *volatile mut UWord = PIC_ENABLE as *volatile mut UWord
    if timerReady || mfcr(CR_STATUS) & STATUS_IE != 0 || enabled[0] != 0 return false
    timerSetClock(clock)
    let period: UWord = timerPeriod(timerRate(), quantumHz)
    if period == 0 return false
    let control: *volatile mut UWord = TIMER_CONTROL as *volatile mut UWord
    let reload: *volatile mut UWord = TIMER_RELOAD as *volatile mut UWord
    let status: *volatile mut UWord = TIMER_STATUS as *volatile mut UWord
    control[0] = 0
    status[0] = TIMER_EXPIRED
    reload[0] = period
    fence()
    timerReady = true
    control[0] = TIMER_ENABLE | TIMER_PERIODIC
    fence()
    enabled[0] = TIMER_IRQ_MASK
    fence()
    return true
}

// Idle must have a live periodic level source even while CPU IE is clear.
let timerCanSleep(): Bool {
    let enabled: *volatile UWord = PIC_ENABLE as *volatile UWord
    let control: *volatile UWord = TIMER_CONTROL as *volatile UWord
    let reload: *volatile UWord = TIMER_RELOAD as *volatile UWord
    return timerReady && enabled[0] & TIMER_IRQ_MASK != 0 &&
        control[0] & (TIMER_ENABLE | TIMER_PERIODIC) == (TIMER_ENABLE | TIMER_PERIODIC) &&
        reload[0] != 0
}

// CLAIM is read-only and does not acknowledge a level. WRM has no PIC EOI.
// Mask an unhandled line before diagnostics so IRET cannot retrigger it.
let timerInterrupt(): Bool {
    let claim: *volatile UWord = PIC_CLAIM as *volatile UWord
    let enabled: *volatile mut UWord = PIC_ENABLE as *volatile mut UWord
    let status: *volatile mut UWord = TIMER_STATUS as *volatile mut UWord
    let irq: UWord = claim[0]
    if irq == PIC_NO_IRQ {
        debugPrint("LA/IX: spurious IRQ (PIC CLAIM empty)\n")
        return false
    }
    if irq == TIMER_IRQ && timerReady && status[0] & TIMER_EXPIRED != 0 {
        status[0] = TIMER_EXPIRED
        fence() // deassert the device before selecting/restoring any context
        irqTimerTick()
        return true
    }
    if irq < PIC_LINE_COUNT && irqNotify(irq) return false
    if irq < PIC_LINE_COUNT enabled[0] &= ~(1 << irq)
    else enabled[0] = 0
    fence()
    debugPrint("LA/IX: masked unexpected IRQ $u\n", irq)
    return false
}

export { TimerCount, timerReady, timerSetClock, timerRate, timerReadCount,
    timerDeadline, timerDeadlineReached, waitUntil, timerPeriod, timerInit, timerCanSleep, timerInterrupt }
