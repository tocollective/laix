#!/bin/sh
set -eu

laix_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(dirname -- "$laix_dir")
mkdir -p "$laix_dir/build"

python3 "$laix_dir/tools/pack_unifont.py" \
    "${LAIX_FONT:-$repo_dir/vendor/SDL/test/unifont-15.1.05.hex}" \
    "$laix_dir/fonts/unifont-console.laf" --index "$laix_dir/fonts/unifont-index.laf"

# mc.py's image mode always adds crt0/trap. Compile modules separately and
# link our start object first. Same-name module .asm files are included by M.
obj_dir="$laix_dir/build/obj/${LAIX_CONSOLE:-uart}"
mkdir -p "$obj_dir"
python3 "$repo_dir/mc/asm.py" -c "$laix_dir/src/arch/wrm081632/start.asm" -o "$obj_dir/start.o"
set -- "$obj_dir/start.o"
main_source=${LAIX_MAIN:-$laix_dir/src/kernel/main.m}
case "${LAIX_CONSOLE:-uart}" in
    uart) ;;
    memory|sharing)
        if [ -n "${LAIX_MAIN:-}" ]; then
            printf '%s\n' 'LAIX_CONSOLE=memory/sharing cannot be combined with LAIX_MAIN' >&2
            exit 1
        fi
        sh "$laix_dir/tools/build_runtime_memory.sh" "$LAIX_CONSOLE"
        main_source="$laix_dir/tests/programs/mm/runtime_memory.m"
        if [ "$LAIX_CONSOLE" = sharing ]; then main_source="$laix_dir/tests/programs/mm/memory_sharing.m"; fi
        ;;
    supervisor)
        if [ -n "${LAIX_MAIN:-}" ]; then
            printf '%s\n' 'LAIX_CONSOLE=supervisor cannot be combined with LAIX_MAIN' >&2
            exit 1
        fi
        main_source="$laix_dir/src/kernel/supervisor_main.m"
        ;;
    screen|services)
        if [ -n "${LAIX_MAIN:-}" ]; then
            printf '%s\n' 'LAIX_CONSOLE=screen/services cannot be combined with LAIX_MAIN' >&2
            exit 1
        fi
        if [ "$LAIX_CONSOLE" = services ]; then
            main_source="$laix_dir/src/kernel/simple_main.m"
        else
            main_source="$laix_dir/src/kernel/screen_main.m"
        fi
        sh "$laix_dir/tools/build_services.sh" "$LAIX_CONSOLE"
        ;;
    *) printf '%s\n' 'LAIX_CONSOLE must be uart, screen, services, supervisor, memory or sharing' >&2; exit 1 ;;
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
for module in arch/wrm081632/defs kernel/boot kernel/bootstrap mm/memory mm/mmu mm/runtime mm/sharing trap/trap_frame ipc/objects task/start task/service_start task/runtime_start task/control task/task ipc/ipc trap/trap kernel/panic drivers/debug_uart drivers/timer drivers/irq drivers/service_devices drivers/input_device drivers/videocard console/console drivers/rnd console/font/font console/font/glyph_cache console/font/data; do
    mkdir -p "$(dirname -- "$obj_dir/$module.o")"
    python3 "$repo_dir/mc/mc.py" -c "$laix_dir/src/$module.m" -o "$obj_dir/$module.o"
    set -- "$@" "$obj_dir/$module.o"
done
if [ "${LAIX_CONSOLE:-uart}" = screen ] || [ "${LAIX_CONSOLE:-uart}" = services ]; then
    bootstrap_module=kernel/service_bootstrap
    if [ "$LAIX_CONSOLE" = services ]; then bootstrap_module=kernel/simple_bootstrap; fi
    for module in task/program kernel/service_policy "$bootstrap_module"; do
        python3 "$repo_dir/mc/mc.py" -c "$laix_dir/src/$module.m" -o "$obj_dir/$module.o"
        set -- "$@" "$obj_dir/$module.o"
    done
fi
if [ "${LAIX_CONSOLE:-uart}" = memory ] || [ "${LAIX_CONSOLE:-uart}" = sharing ]; then
    python3 "$repo_dir/mc/mc.py" -c "$laix_dir/src/task/program.m" -o "$obj_dir/program.o"
    set -- "$@" "$obj_dir/program.o"
fi
if [ "${LAIX_CONSOLE:-uart}" = supervisor ]; then
    python3 "$repo_dir/mc/mc.py" -c "$laix_dir/src/kernel/supervisor_bootstrap.m" -o "$obj_dir/supervisor_bootstrap.o"
    set -- "$@" "$obj_dir/supervisor_bootstrap.o"
fi
python3 "$repo_dir/mc/asm.py" -c "$repo_dir/mc/runtime/mem.asm" -o "$obj_dir/mem.o"
image_name=laix
if [ "${LAIX_CONSOLE:-uart}" != uart ]; then image_name=$LAIX_CONSOLE; fi
python3 "$repo_dir/mc/ld.py" --layout boot "$@" "$obj_dir/mem.o" \
    -o "$laix_dir/build/$image_name.img" --map "$laix_dir/build/$image_name.map"

python3 "$laix_dir/tools/append_font.py" \
    "$laix_dir/build/$image_name.img" "$laix_dir/fonts/unifont-console.laf"

printf 'Boot image: %s\n' "$laix_dir/build/$image_name.img"
