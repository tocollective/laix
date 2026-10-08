#!/bin/sh
# One kernel. The kernel starts init, and init builds the system from the image
# catalog (docs/INIT.md). What differs between builds is data: LAIX_SESSION names
# the session init runs (tools/sessions.py lists them) and decides the storage
# volume appended to the image. The test entries that check kernel code from kernel
# mode (MMU, trap, stack, latency) and the reference boots of the acceptance
# campaign are built by tools/build_fixture.sh.
set -eu

laix_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(dirname -- "$laix_dir")
mkdir -p "$laix_dir/build"

session=${LAIX_SESSION:-shell}
number=$(python3 "$laix_dir/tools/sessions.py" "$session")

python3 "$laix_dir/tools/pack_unifont.py" \
    "${LAIX_FONT:-$repo_dir/vendor/SDL/test/unifont-15.1.05.hex}" \
    "$laix_dir/fonts/unifont-console.laf" --index "$laix_dir/fonts/unifont-index.laf" \
    --extent "$laix_dir/fonts/storage-extent.bin"

obj_dir="$laix_dir/build/obj/kernel"
mkdir -p "$obj_dir"

# The user programs init can start, and the volume and storage root of the session.
sh "$laix_dir/tools/build_services.sh" init
user_dir="$laix_dir/build/init"
storage_volume=
case "$session" in
    shell)
        # A WFS1 filesystem holding the demonstration programs the shell can run.
        storage_volume="$user_dir/shell.volume"
        python3 "$laix_dir/tools/build_shell_volume.py" "$storage_volume" --services "$user_dir"
        python3 "$laix_dir/tools/storage_root.py" "$laix_dir/fonts/storage-extent.bin" --writable --session "$number" \
            --bytes "$(python3 "$laix_dir/tools/append_volume.py" --size "$storage_volume")"
        ;;
    fs)
        storage_volume="$user_dir/fs.volume"
        python3 "$laix_dir/tools/build_fs_volume.py" "$storage_volume" >/dev/null
        python3 "$laix_dir/tools/storage_root.py" "$laix_dir/fonts/storage-extent.bin" --writable --session "$number" \
            --bytes "$(python3 "$laix_dir/tools/append_volume.py" --size "$storage_volume")"
        ;;
    loader)
        # The child lives on the volume, not in the kernel image: the approved
        # storage root covers exactly that volume instead of the font.
        storage_volume="$user_dir/hello.elf"
        python3 "$laix_dir/tools/storage_root.py" "$laix_dir/fonts/storage-extent.bin" --session "$number" \
            --bytes "$(python3 "$laix_dir/tools/append_volume.py" --size "$storage_volume")"
        ;;
    *)
        # The font volume packed above; only the session changes in its root.
        python3 "$laix_dir/tools/storage_root.py" "$laix_dir/fonts/storage-extent.bin" --keep --session "$number"
        ;;
esac

# mc.py's image mode always adds crt0/trap. Compile modules separately and
# link our start object first. Same-name module .asm files are included by M.
python3 "$repo_dir/mc/asm.py" -c "$laix_dir/src/arch/wrm081632/start.asm" -o "$obj_dir/start.o"
set -- "$obj_dir/start.o"
python3 "$repo_dir/mc/mc.py" -c "$laix_dir/src/kernel/main.m" -o "$obj_dir/main.o"
set -- "$@" "$obj_dir/main.o"
for module in arch/wrm081632/defs kernel/boot kernel/init_bootstrap mm/memory mm/mmu mm/runtime mm/sharing trap/trap_frame ipc/objects ipc/transfer task/start task/service_start task/runtime_start task/recovery_start task/control task/program task/recovery task/task task/tables ipc/ipc trap/trap kernel/panic drivers/debug_uart drivers/timer drivers/irq drivers/device_table drivers/service_devices drivers/resources drivers/input_device drivers/net_device drivers/rnd console/font/data; do
    mkdir -p "$(dirname -- "$obj_dir/$module.o")"
    python3 "$repo_dir/mc/mc.py" -c "$laix_dir/src/$module.m" -o "$obj_dir/$module.o"
    set -- "$@" "$obj_dir/$module.o"
done
python3 "$repo_dir/mc/asm.py" -c "$repo_dir/mc/runtime/mem.asm" -o "$obj_dir/mem.o"
python3 "$repo_dir/mc/ld.py" --layout boot "$@" "$obj_dir/mem.o" \
    -o "$laix_dir/build/laix.img" --map "$laix_dir/build/laix.map"

if [ -n "$storage_volume" ]; then
    python3 "$laix_dir/tools/append_volume.py" "$laix_dir/build/laix.img" "$storage_volume"
else
    python3 "$laix_dir/tools/append_font.py" \
        "$laix_dir/build/laix.img" "$laix_dir/fonts/unifont-console.laf"
fi

printf 'Boot image: %s (session %s)\n' "$laix_dir/build/laix.img" "$session"
