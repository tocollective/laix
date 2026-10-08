// The sessions: the task graphs the old per-profile kernel boots built, now
// built by init through the runtime calls. Each is the straight line of its
// wiring (user/init/lib.m records the first failure); the build picks one
// through the storage root (user/init/sessions.m).
//
// Wiring, as in the old profiles:
//   console   Console <--- Banner
//   services  Input, Disk <--- Files <--- Application (and Input)
//   loader    the same, the client may load programs it reads from Files
//   fs        Input, Disk (writable) <--- Fs <--- client (and Input)
//   shell     Disk <--- Fs <--- Exec, Shell; Console <--- Exec, Shell; keyboard to Shell
//   net       Net driver <--- IP <--- client; Console <--- client
//   screen    Bitmap storage <--- Screen <--- Application
import { START_ROLE_INPUT, START_ROLE_DISK, START_ROLE_FILE, START_ROLE_CLIENT, START_ROLE_SERVER,
    START_ROLE_STORAGE, START_PROTOCOL_INPUT, START_PROTOCOL_DISK, START_PROTOCOL_FILE,
    START_PROTOCOL_SCREEN, START_PROTOCOL_FONT, DEVICE_UART_TX, DEVICE_INPUT, DEVICE_DISK,
    DEVICE_FONT, DEVICE_SCREEN, DEVICE_NET, EXTENT_WRITE, IMAGE_LOAD_AUTHORITY } from "../../src/arch/wrm081632/defs.m"
import { Service, svcSpawn, svcOneShot, svcDevices, svcExtent, svcServe, svcClient, svcReceive, svcSendTo,
    svcBare, listBegin, listSend, listWord, svcList, svcImages, sessionPublish } from "lib.m"
import { IMAGE_CONSOLE, IMAGE_DISK, IMAGE_FILES, IMAGE_INPUT, IMAGE_FS, IMAGE_EXEC, IMAGE_SHELL,
    IMAGE_BANNER, IMAGE_SIMPLE_APPLICATION, IMAGE_LOADER, IMAGE_FSCLIENT, IMAGE_NETDRV, IMAGE_IP,
    IMAGE_NETCLIENT, IMAGE_SCREEN, IMAGE_STORAGE, IMAGE_APPLICATION } from "images.m"

let sessionConsole(): Word {
    let console: *mut Service = svcSpawn(IMAGE_CONSOLE, true)
    let banner: *mut Service = svcSpawn(IMAGE_BANNER, false)
    svcDevices(console, DEVICE_UART_TX)
    svcReceive(console)
    svcSendTo(banner, console)
    svcOneShot(banner)
    return sessionPublish()
}

// Input, Disk, Files and an application that reads through them.
let sessionServices(): Word {
    let input: *mut Service = svcSpawn(IMAGE_INPUT, true)
    let disk: *mut Service = svcSpawn(IMAGE_DISK, true)
    let files: *mut Service = svcSpawn(IMAGE_FILES, true)
    let app: *mut Service = svcSpawn(IMAGE_SIMPLE_APPLICATION, false)
    svcDevices(input, DEVICE_INPUT)
    svcServe(input, START_ROLE_INPUT, START_PROTOCOL_INPUT, null)
    svcDevices(disk, DEVICE_DISK)
    svcServe(disk, START_ROLE_DISK, START_PROTOCOL_DISK, null)
    svcServe(files, START_ROLE_FILE, START_PROTOCOL_FILE, disk)
    svcClient(app, START_ROLE_CLIENT, START_PROTOCOL_FILE, files, input)
    svcOneShot(app)
    return sessionPublish()
}

// The same graph; its client holds the right to load the programs it reads.
let sessionLoader(): Word {
    let input: *mut Service = svcSpawn(IMAGE_INPUT, true)
    let disk: *mut Service = svcSpawn(IMAGE_DISK, true)
    let files: *mut Service = svcSpawn(IMAGE_FILES, true)
    let loader: *mut Service = svcSpawn(IMAGE_LOADER, false)
    svcDevices(input, DEVICE_INPUT)
    svcServe(input, START_ROLE_INPUT, START_PROTOCOL_INPUT, null)
    svcDevices(disk, DEVICE_DISK)
    svcServe(disk, START_ROLE_DISK, START_PROTOCOL_DISK, null)
    svcServe(files, START_ROLE_FILE, START_PROTOCOL_FILE, disk)
    svcClient(loader, START_ROLE_CLIENT, START_PROTOCOL_FILE, files, input)
    svcImages(loader, IMAGE_LOAD_AUTHORITY)
    svcOneShot(loader)
    return sessionPublish()
}

// A writable flat filesystem and a client that exercises it.
let sessionFs(): Word {
    let input: *mut Service = svcSpawn(IMAGE_INPUT, true)
    let disk: *mut Service = svcSpawn(IMAGE_DISK, true)
    let fs: *mut Service = svcSpawn(IMAGE_FS, true)
    let client: *mut Service = svcSpawn(IMAGE_FSCLIENT, false)
    svcDevices(input, DEVICE_INPUT)
    svcServe(input, START_ROLE_INPUT, START_PROTOCOL_INPUT, null)
    svcDevices(disk, DEVICE_DISK)
    svcExtent(disk, EXTENT_WRITE)
    svcServe(disk, START_ROLE_DISK, START_PROTOCOL_DISK, null)
    svcServe(fs, START_ROLE_FILE, START_PROTOCOL_FILE, disk)
    svcClient(client, START_ROLE_CLIENT, START_PROTOCOL_FILE, fs, input)
    svcOneShot(client)
    return sessionPublish()
}

// The command shell over the filesystem, Exec and the console.
let sessionShell(): Word {
    let console: *mut Service = svcSpawn(IMAGE_CONSOLE, true)
    let disk: *mut Service = svcSpawn(IMAGE_DISK, true)
    let fs: *mut Service = svcSpawn(IMAGE_FS, true)
    let exec: *mut Service = svcSpawn(IMAGE_EXEC, true)
    let shell: *mut Service = svcSpawn(IMAGE_SHELL, false)
    svcDevices(console, DEVICE_UART_TX)
    svcReceive(console)
    // Disk owns the whole storage root as a writable extent; Fs reads and writes
    // through it. The kernel refuses the write window unless the root allows it.
    svcDevices(disk, DEVICE_DISK)
    svcExtent(disk, EXTENT_WRITE)
    svcServe(disk, START_ROLE_DISK, START_PROTOCOL_DISK, null)
    svcServe(fs, START_ROLE_FILE, START_PROTOCOL_FILE, disk)
    // Exec: its receive handle, the filesystem and the console, and the load
    // authority it passes to SYS_TASK_LOAD. Nobody else may load programs.
    svcReceive(exec)
    listBegin()
    listSend(fs)
    listSend(console)
    svcList(exec)
    svcImages(exec, IMAGE_LOAD_AUTHORITY)
    // Shell: the keyboard, then the filesystem, Exec and the console.
    svcDevices(shell, DEVICE_INPUT)
    svcBare(shell)
    listBegin()
    listSend(fs)
    listSend(exec)
    listSend(console)
    svcList(shell)
    return sessionPublish()
}

// Ethernet card driver, IP stack and a client (ping, name lookup).
let sessionNet(): Word {
    let console: *mut Service = svcSpawn(IMAGE_CONSOLE, true)
    let driver: *mut Service = svcSpawn(IMAGE_NETDRV, true)
    let ip: *mut Service = svcSpawn(IMAGE_IP, true)
    let client: *mut Service = svcSpawn(IMAGE_NETCLIENT, false)
    svcDevices(console, DEVICE_UART_TX)
    svcReceive(console)
    // Only the driver holds the card; its interrupt token is its one start word.
    svcDevices(driver, DEVICE_NET)
    svcReceive(driver)
    listBegin()
    if driver != null listWord(driver.irq)
    svcList(driver)
    svcReceive(ip)
    listBegin()
    listSend(driver)
    svcList(ip)
    svcBare(client)
    listBegin()
    listSend(ip)
    listSend(console)
    svcList(client)
    svcOneShot(client)
    return sessionPublish()
}

// Display server over the bitmap storage service, and an application.
let sessionScreen(): Word {
    let storage: *mut Service = svcSpawn(IMAGE_STORAGE, true)
    let screen: *mut Service = svcSpawn(IMAGE_SCREEN, true)
    let app: *mut Service = svcSpawn(IMAGE_APPLICATION, false)
    svcDevices(storage, DEVICE_FONT)
    svcServe(storage, START_ROLE_STORAGE, START_PROTOCOL_FONT, null)
    svcDevices(screen, DEVICE_SCREEN)
    svcServe(screen, START_ROLE_SERVER, START_PROTOCOL_SCREEN, storage)
    svcClient(app, START_ROLE_CLIENT, START_PROTOCOL_SCREEN, screen, null)
    svcOneShot(app)
    return sessionPublish()
}
export { sessionConsole, sessionServices, sessionLoader, sessionFs, sessionShell, sessionNet, sessionScreen }
