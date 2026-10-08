// Catalog image IDs, in the order of the rows in src/kernel/init_bootstrap.asm.
// The kernel keeps no names; image 1 is its approved self-test fixture. A new
// program is a row there, a constant here and an entry in tools/build_services.sh
// (profile init); tests/test_init.py keeps the three in the same order.
let IMAGE_CONSOLE: UWord = 2
let IMAGE_DISK: UWord = 3
let IMAGE_FILES: UWord = 4
let IMAGE_INPUT: UWord = 5
let IMAGE_FS: UWord = 6
let IMAGE_EXEC: UWord = 7
let IMAGE_SHELL: UWord = 8
let IMAGE_BANNER: UWord = 9
let IMAGE_SIMPLE_APPLICATION: UWord = 10
let IMAGE_LOADER: UWord = 11
let IMAGE_FSCLIENT: UWord = 12
let IMAGE_NETDRV: UWord = 13
let IMAGE_IP: UWord = 14
let IMAGE_NETCLIENT: UWord = 15
let IMAGE_SCREEN: UWord = 16
let IMAGE_STORAGE: UWord = 17
let IMAGE_APPLICATION: UWord = 18
let IMAGE_REC_ECHO: UWord = 19
let IMAGE_REC_DISK: UWord = 20
let IMAGE_REC_FILES: UWord = 21
let IMAGE_REC_CLIENT: UWord = 22
let IMAGE_REC_BITMAP: UWord = 23
let IMAGE_REC_SCREEN: UWord = 24
let IMAGE_REC_SCREEN_CLIENT: UWord = 25
export { IMAGE_CONSOLE, IMAGE_DISK, IMAGE_FILES, IMAGE_INPUT, IMAGE_FS, IMAGE_EXEC, IMAGE_SHELL, IMAGE_BANNER,
    IMAGE_SIMPLE_APPLICATION, IMAGE_LOADER, IMAGE_FSCLIENT, IMAGE_NETDRV, IMAGE_IP, IMAGE_NETCLIENT,
    IMAGE_SCREEN, IMAGE_STORAGE, IMAGE_APPLICATION, IMAGE_REC_ECHO, IMAGE_REC_DISK, IMAGE_REC_FILES,
    IMAGE_REC_CLIENT, IMAGE_REC_BITMAP, IMAGE_REC_SCREEN, IMAGE_REC_SCREEN_CLIENT }
