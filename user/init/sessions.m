// Session numbers: what the build asks init to run. The build writes the number
// into the storage root (tools/storage_root.py --session), the kernel hands it to
// init as its start argument, and tools/sessions.py reads this file, so a number
// is defined here once. Zero is the default session.
let SESSION_SHELL: UWord = 0
let SESSION_CONSOLE: UWord = 1
let SESSION_SERVICES: UWord = 2
let SESSION_LOADER: UWord = 3
let SESSION_FS: UWord = 4
let SESSION_NET: UWord = 5
let SESSION_SCREEN: UWord = 6
let SESSION_RECOVERY: UWord = 7
let SESSION_SCREENRECOVERY: UWord = 8
export { SESSION_SHELL, SESSION_CONSOLE, SESSION_SERVICES, SESSION_LOADER, SESSION_FS, SESSION_NET, SESSION_SCREEN,
    SESSION_RECOVERY, SESSION_SCREENRECOVERY }
