#!/bin/sh
# Reference boots for the source tests and the CPU acceptance campaign. The
# product is built by build.sh: one kernel, started into init, with the session
# picked by data. A fixture is the same kernel with its own entry point
# (tests/programs/boot) that places a fixed task graph without init, so the old
# probes and the source tests of the kernel mechanisms keep their layout.
#   LAIX_FIXTURE=uart|screen|services|loader|fs|shell|net|recovery|screenrecovery|
#                supervisor|soak|memory|sharing|objects  sh tools/build_fixture.sh
set -eu

laix_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
repo_dir=$(dirname -- "$laix_dir")
mkdir -p "$laix_dir/build"

python3 "$laix_dir/tools/pack_unifont.py" \
    "${LAIX_FONT:-$repo_dir/vendor/SDL/test/unifont-15.1.05.hex}" \
    "$laix_dir/fonts/unifont-console.laf" --index "$laix_dir/fonts/unifont-index.laf" \
    --extent "$laix_dir/fonts/storage-extent.bin"

# mc.py's image mode always adds crt0/trap. Compile modules separately and
# link our start object first. Same-name module .asm files are included by M.
obj_dir="$laix_dir/build/obj/${LAIX_FIXTURE:-uart}"
mkdir -p "$obj_dir"
python3 "$repo_dir/mc/asm.py" -c "$laix_dir/src/arch/wrm081632/start.asm" -o "$obj_dir/start.o"
set -- "$obj_dir/start.o"
main_source=${LAIX_MAIN:-$laix_dir/tests/programs/boot/uart_main.m}
case "${LAIX_FIXTURE:-uart}" in
    uart) ;;
    memory|sharing)
        if [ -n "${LAIX_MAIN:-}" ]; then
            printf '%s\n' 'LAIX_FIXTURE=memory/sharing cannot be combined with LAIX_MAIN' >&2
            exit 1
        fi
        sh "$laix_dir/tools/build_runtime_memory.sh" "$LAIX_FIXTURE"
        main_source="$laix_dir/tests/programs/mm/runtime_memory.m"
        if [ "$LAIX_FIXTURE" = sharing ]; then main_source="$laix_dir/tests/programs/mm/memory_sharing.m"; fi
        ;;
    recovery)
        if [ -n "${LAIX_MAIN:-}" ]; then
            printf '%s\n' 'LAIX_FIXTURE=recovery cannot be combined with LAIX_MAIN' >&2
            exit 1
        fi
        sh "$laix_dir/tools/build_recovery.sh"
        main_source="$laix_dir/tests/programs/boot/recovery_main.m"
        ;;
    screenrecovery)
        if [ -n "${LAIX_MAIN:-}" ]; then
            printf '%s\n' 'LAIX_FIXTURE=screenrecovery cannot be combined with LAIX_MAIN' >&2
            exit 1
        fi
        sh "$laix_dir/tools/build_screen_recovery.sh"
        main_source="$laix_dir/tests/programs/boot/screen_recovery_main.m"
        ;;
    objects)
        if [ -n "${LAIX_MAIN:-}" ]; then
            printf '%s\n' 'LAIX_FIXTURE=objects cannot be combined with LAIX_MAIN' >&2
            exit 1
        fi
        sh "$laix_dir/tools/build_runtime_objects.sh"
        main_source="$laix_dir/tests/programs/ipc/runtime_objects.m"
        ;;
    supervisor)
        if [ -n "${LAIX_MAIN:-}" ]; then
            printf '%s\n' 'LAIX_FIXTURE=supervisor cannot be combined with LAIX_MAIN' >&2
            exit 1
        fi
        main_source="$laix_dir/tests/programs/boot/supervisor_main.m"
        ;;
    loader)
        if [ -n "${LAIX_MAIN:-}" ]; then
            printf '%s\n' 'LAIX_FIXTURE=loader cannot be combined with LAIX_MAIN' >&2
            exit 1
        fi
        main_source="$laix_dir/tests/programs/boot/loader_main.m"
        sh "$laix_dir/tools/build_services.sh" loader
        # The child lives on the storage volume, not in the kernel image: the
        # approved storage root covers exactly that volume instead of the font.
        storage_volume="$laix_dir/build/services/hello.elf"
        python3 "$laix_dir/tools/storage_root.py" "$laix_dir/fonts/storage-extent.bin" \
            --bytes "$(python3 "$laix_dir/tools/append_volume.py" --size "$storage_volume")"
        ;;
    fs)
        if [ -n "${LAIX_MAIN:-}" ]; then
            printf '%s\n' 'LAIX_FIXTURE=fs cannot be combined with LAIX_MAIN' >&2
            exit 1
        fi
        main_source="$laix_dir/tests/programs/boot/fs_main.m"
        sh "$laix_dir/tools/build_services.sh" fs
        # The volume is a WFS1 filesystem, and the root covering it is writable:
        # the only profile whose Disk owner may write the medium.
        storage_volume="$laix_dir/build/services/fs.volume"
        python3 "$laix_dir/tools/build_fs_volume.py" "$storage_volume" >/dev/null
        python3 "$laix_dir/tools/storage_root.py" "$laix_dir/fonts/storage-extent.bin" --writable \
            --bytes "$(python3 "$laix_dir/tools/append_volume.py" --size "$storage_volume")"
        ;;
    shell)
        if [ -n "${LAIX_MAIN:-}" ]; then
            printf '%s\n' 'LAIX_FIXTURE=shell cannot be combined with LAIX_MAIN' >&2
            exit 1
        fi
        main_source="$laix_dir/tests/programs/boot/shell_main.m"
        sh "$laix_dir/tools/build_services.sh" shell
        # The volume is a WFS1 filesystem holding the demonstration programs the
        # shell can run; the root covering it is writable.
        storage_volume="$laix_dir/build/services/shell.volume"
        python3 "$laix_dir/tools/build_shell_volume.py" "$storage_volume" --services "$laix_dir/build/services"
        python3 "$laix_dir/tools/storage_root.py" "$laix_dir/fonts/storage-extent.bin" --writable \
            --bytes "$(python3 "$laix_dir/tools/append_volume.py" --size "$storage_volume")"
        ;;
    net)
        if [ -n "${LAIX_MAIN:-}" ]; then
            printf '%s\n' 'LAIX_FIXTURE=net cannot be combined with LAIX_MAIN' >&2
            exit 1
        fi
        main_source="$laix_dir/tests/programs/boot/net_main.m"
        sh "$laix_dir/tools/build_services.sh" net
        ;;
    soak)
        if [ -n "${LAIX_MAIN:-}" ]; then
            printf '%s\n' 'LAIX_FIXTURE=soak cannot be combined with LAIX_MAIN' >&2
            exit 1
        fi
        main_source="$laix_dir/tests/programs/boot/soak_main.m"
        ;;
    screen|services)
        if [ -n "${LAIX_MAIN:-}" ]; then
            printf '%s\n' 'LAIX_FIXTURE=screen/services cannot be combined with LAIX_MAIN' >&2
            exit 1
        fi
        if [ "$LAIX_FIXTURE" = services ]; then
            main_source="$laix_dir/tests/programs/boot/simple_main.m"
        else
            main_source="$laix_dir/tests/programs/boot/screen_main.m"
        fi
        sh "$laix_dir/tools/build_services.sh" "$LAIX_FIXTURE"
        ;;
    *) printf '%s\n' 'LAIX_FIXTURE must be uart, screen, services, loader, fs, shell, net, supervisor, soak, memory, sharing, objects, recovery or screenrecovery' >&2; exit 1 ;;
esac
python3 "$repo_dir/mc/mc.py" -c "$main_source" -o "$obj_dir/main.o"
set -- "$@" "$obj_dir/main.o"
# The MMU CPU probes share a test-only M module and its assembly companion.
case "$(basename -- "$main_source")" in
    mmu_remap.m|mmu_unmap.m|mmu_protect.m|asid_reuse.m)
        python3 "$repo_dir/mc/mc.py" -c "$laix_dir/tests/programs/mm/mmu_probe.m" -o "$obj_dir/mmu_probe.o"
        set -- "$@" "$obj_dir/mmu_probe.o"
        ;;
esac
for module in arch/wrm081632/defs kernel/boot mm/memory mm/mmu mm/runtime mm/sharing trap/trap_frame ipc/objects ipc/transfer task/start task/service_start task/runtime_start task/recovery_start task/control task/program task/recovery task/task task/tables ipc/ipc trap/trap kernel/panic drivers/debug_uart drivers/timer drivers/irq drivers/device_table drivers/service_devices drivers/resources drivers/input_device drivers/net_device drivers/rnd console/font/data; do
    mkdir -p "$(dirname -- "$obj_dir/$module.o")"
    python3 "$repo_dir/mc/mc.py" -c "$laix_dir/src/$module.m" -o "$obj_dir/$module.o"
    set -- "$@" "$obj_dir/$module.o"
done
# Supervisor rendering is regression-only; ordinary images keep direct UART.
case "$main_source" in
    */tests/programs/console/*|tests/programs/console/*)
        for module in drivers/videocard console/console console/font/font console/font/glyph_cache; do
            python3 "$repo_dir/mc/mc.py" -c "$laix_dir/src/$module.m" -o "$obj_dir/$module.o"
            set -- "$@" "$obj_dir/$module.o"
        done
        ;;
esac
if [ "${LAIX_FIXTURE:-uart}" = loader ] || [ "${LAIX_FIXTURE:-uart}" = fs ] || [ "${LAIX_FIXTURE:-uart}" = shell ] || [ "${LAIX_FIXTURE:-uart}" = net ]; then
    bootstrap_module=boot/loader_bootstrap
    if [ "$LAIX_FIXTURE" = fs ]; then bootstrap_module=boot/fs_bootstrap; fi
    if [ "$LAIX_FIXTURE" = shell ]; then bootstrap_module=boot/shell_bootstrap; fi
    if [ "$LAIX_FIXTURE" = net ]; then bootstrap_module=boot/net_bootstrap; fi
    for module in boot/service_policy "$bootstrap_module"; do
        mkdir -p "$(dirname -- "$obj_dir/$module.o")"
        python3 "$repo_dir/mc/mc.py" -c "$laix_dir/tests/programs/$module.m" -o "$obj_dir/$module.o"
        set -- "$@" "$obj_dir/$module.o"
    done
fi
if [ "${LAIX_FIXTURE:-uart}" = screen ] || [ "${LAIX_FIXTURE:-uart}" = services ]; then
    bootstrap_module=boot/service_bootstrap
    if [ "$LAIX_FIXTURE" = services ]; then bootstrap_module=boot/simple_bootstrap; fi
    for module in boot/service_policy "$bootstrap_module"; do
        mkdir -p "$(dirname -- "$obj_dir/$module.o")"
        python3 "$repo_dir/mc/mc.py" -c "$laix_dir/tests/programs/$module.m" -o "$obj_dir/$module.o"
        set -- "$@" "$obj_dir/$module.o"
    done
fi
if [ "${LAIX_FIXTURE:-uart}" = memory ] || [ "${LAIX_FIXTURE:-uart}" = sharing ] || [ "${LAIX_FIXTURE:-uart}" = objects ]; then
    : # task/program is linked by every runtime-enabled kernel
fi
if [ "${LAIX_FIXTURE:-uart}" = supervisor ]; then
    python3 "$repo_dir/mc/mc.py" -c "$laix_dir/tests/programs/boot/supervisor_bootstrap.m" -o "$obj_dir/supervisor_bootstrap.o"
    set -- "$@" "$obj_dir/supervisor_bootstrap.o"
fi
if [ "${LAIX_FIXTURE:-uart}" = soak ]; then
    python3 "$repo_dir/mc/mc.py" -c "$laix_dir/tests/programs/boot/soak_bootstrap.m" -o "$obj_dir/soak_bootstrap.o"
    set -- "$@" "$obj_dir/soak_bootstrap.o"
fi
# Dedicated acceptance mains include Screen bootstrap and clone real clients.
case "$(basename -- "$main_source")" in
    input_stress.m)
        for module in boot/service_policy boot/simple_bootstrap; do
            object="$obj_dir/acceptance-$(basename -- "$module").o"
            python3 "$repo_dir/mc/mc.py" -c "$laix_dir/tests/programs/$module.m" -o "$object"
            set -- "$@" "$object"
        done
        ;;
    uart_stress.m|screen_stress.m)
        for module in tests/programs/console/multiclient boot/service_policy boot/service_bootstrap; do
            source_module=$module
            case "$module" in boot/*) source_module=tests/programs/$module ;; esac
            object="$obj_dir/acceptance-$(basename -- "$module").o"
            python3 "$repo_dir/mc/mc.py" -c "$laix_dir/$source_module.m" -o "$object"
            set -- "$@" "$object"
        done
        ;;
esac
python3 "$repo_dir/mc/asm.py" -c "$repo_dir/mc/runtime/mem.asm" -o "$obj_dir/mem.o"
image_name=laix
if [ "${LAIX_FIXTURE:-uart}" != uart ]; then image_name=$LAIX_FIXTURE; fi
python3 "$repo_dir/mc/ld.py" --layout boot "$@" "$obj_dir/mem.o" \
    -o "$laix_dir/build/$image_name.img" --map "$laix_dir/build/$image_name.map"

if [ -n "${storage_volume:-}" ]; then
    python3 "$laix_dir/tools/append_volume.py" "$laix_dir/build/$image_name.img" "$storage_volume"
else
    python3 "$laix_dir/tools/append_font.py" \
        "$laix_dir/build/$image_name.img" "$laix_dir/fonts/unifont-console.laf"
fi

if [ "${LAIX_FIXTURE:-uart}" = recovery ]; then
    python3 "$laix_dir/tools/recovery_provenance.py"
fi
if [ "${LAIX_FIXTURE:-uart}" = screenrecovery ]; then
    python3 "$laix_dir/tools/screen_recovery_provenance.py"
fi

printf 'Boot image: %s\n' "$laix_dir/build/$image_name.img"
