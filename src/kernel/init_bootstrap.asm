; Trusted boot embeds: the init program and the image catalog it may create tasks
; from. Catalog rows {start, end} become images 2.. in this order; the kernel holds
; no per-image names (user/init/images.m names them). The ELF files come from
; tools/build_services.sh (profile init).
    .rodata
    .align 4
    .globl initImage, initImageEnd, initCatalog, initCatalogEnd
initImage:
    .incbin "../../build/init/init.elf"
initImageEnd:
    .align 4
image_console:
    .incbin "../../build/init/console.elf"
image_console_end:
    .align 4
image_disk:
    .incbin "../../build/init/disk.elf"
image_disk_end:
    .align 4
image_files:
    .incbin "../../build/init/files.elf"
image_files_end:
    .align 4
image_input:
    .incbin "../../build/init/input.elf"
image_input_end:
    .align 4
image_fs:
    .incbin "../../build/init/fs.elf"
image_fs_end:
    .align 4
image_exec:
    .incbin "../../build/init/exec.elf"
image_exec_end:
    .align 4
image_shell:
    .incbin "../../build/init/shell.elf"
image_shell_end:
    .align 4
image_banner:
    .incbin "../../build/init/banner.elf"
image_banner_end:
    .align 4
image_simpleApplication:
    .incbin "../../build/init/simple-application.elf"
image_simpleApplication_end:
    .align 4
image_loader:
    .incbin "../../build/init/loader.elf"
image_loader_end:
    .align 4
image_fsclient:
    .incbin "../../build/init/fsclient.elf"
image_fsclient_end:
    .align 4
image_netdrv:
    .incbin "../../build/init/netdrv.elf"
image_netdrv_end:
    .align 4
image_ip:
    .incbin "../../build/init/ip.elf"
image_ip_end:
    .align 4
image_netclient:
    .incbin "../../build/init/netclient.elf"
image_netclient_end:
    .align 4
image_screen:
    .incbin "../../build/init/screen.elf"
image_screen_end:
    .align 4
image_storage:
    .incbin "../../build/init/storage.elf"
image_storage_end:
    .align 4
image_application:
    .incbin "../../build/init/application.elf"
image_application_end:
    .align 4
image_recEcho:
    .incbin "../../build/init/rec-echo.elf"
image_recEcho_end:
    .align 4
image_recDisk:
    .incbin "../../build/init/rec-disk.elf"
image_recDisk_end:
    .align 4
image_recFiles:
    .incbin "../../build/init/rec-files.elf"
image_recFiles_end:
    .align 4
image_recClient:
    .incbin "../../build/init/rec-client.elf"
image_recClient_end:
    .align 4
image_recBitmap:
    .incbin "../../build/init/rec-bitmap.elf"
image_recBitmap_end:
    .align 4
image_recScreen:
    .incbin "../../build/init/rec-screen.elf"
image_recScreen_end:
    .align 4
image_recScreenClient:
    .incbin "../../build/init/rec-screen-client.elf"
image_recScreenClient_end:
    .align 4
initCatalog:
    .word image_console, image_console_end
    .word image_disk, image_disk_end
    .word image_files, image_files_end
    .word image_input, image_input_end
    .word image_fs, image_fs_end
    .word image_exec, image_exec_end
    .word image_shell, image_shell_end
    .word image_banner, image_banner_end
    .word image_simpleApplication, image_simpleApplication_end
    .word image_loader, image_loader_end
    .word image_fsclient, image_fsclient_end
    .word image_netdrv, image_netdrv_end
    .word image_ip, image_ip_end
    .word image_netclient, image_netclient_end
    .word image_screen, image_screen_end
    .word image_storage, image_storage_end
    .word image_application, image_application_end
    .word image_recEcho, image_recEcho_end
    .word image_recDisk, image_recDisk_end
    .word image_recFiles, image_recFiles_end
    .word image_recClient, image_recClient_end
    .word image_recBitmap, image_recBitmap_end
    .word image_recScreen, image_recScreen_end
    .word image_recScreenClient, image_recScreenClient_end
initCatalogEnd:
