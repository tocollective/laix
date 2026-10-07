// Never finishes: for trying the shell's run limit.
import { yield } from "../syscalls.m"

let binSpinMain(start: UWord, bytes: UWord): Void {
    while true yield()
}
export { binSpinMain }
