#!/bin/sh
set -eu
laix_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
repo_dir=$(dirname -- "$laix_dir")
service_dir="$laix_dir/build/services"
mkdir -p "$service_dir"

# Separate user ELF images; the kernel accepts only these trusted boot embeds.
# No boot crt0, trap handler, kernel module, TLS or relocation at user entry.
case "${1:-screen}" in
    screen) images='screen storage application' ;;
    services) images='input disk files simple-application' ;;
    loader) images='input disk files loader hello' ;;
    fs) images='input disk fs fsclient' ;;
    shell) images='disk fs exec shell bin-hello bin-count bin-spin' ;;
    net) images='netdrv ip netclient' ;;
    *) printf '%s\n' 'Service profile must be screen, services, loader, fs, shell or net' >&2; exit 1 ;;
esac
for image in $images; do
    case "$image" in
        input) modules='services/input' ;;
        disk) modules='services/disk' ;;
        files) modules='services/files' ;;
        fs) modules='services/fs' ;;
        fsclient) modules='../tests/programs/fs/client services/fsclient' ;;
        exec) modules='services/exec services/fsclient heap starthandles words' ;;
        shell) modules='apps/shell services/fsclient services/execclient starthandles keymap text console words' ;;
        bin-hello) modules='bin/hello text console' ;;
        netdrv) modules='services/netdrv starthandles' ;;
        ip) modules='services/ip starthandles words' ;;
        netclient) modules='../tests/programs/net/client services/ipclient text console words starthandles' ;;
        bin-count) modules='bin/count text console' ;;
        bin-spin) modules='bin/spin' ;;
        simple-application)
            modules='services/application services/client'
            if [ "${LAIX_ACCEPTANCE_INPUT:-0}" = 1 ]; then modules='../tests/programs/console/input_client services/client'; fi
            if [ "${LAIX_ACCEPTANCE_FILEREAD:-0}" = 1 ]; then modules='../tests/programs/console/file_read_client services/client'; fi
            ;;
        loader) modules='services/loader services/client heap loadfile' ;;
        hello) modules='../tests/programs/loader/hello' ;;
        screen) modules='screen/server screen/unicode screen/font screen/cache screen/video' ;;
        storage) modules='screen/storage' ;;
        application) modules='screen/application screen/client' ;;
    esac
    set --
    for module in $modules syscalls; do
        object="$service_dir/$image-$(basename -- "$module").o"
        python3 "$repo_dir/mc/mc.py" -c "$laix_dir/user/$module.m" -o "$object"
        set -- "$@" "$object"
    done
    entry_source=
    case "$image" in
        disk|files|fs) entry_source="$laix_dir/user/services/${image}_entry.asm" ;;
        screen) entry_source="$laix_dir/user/screen/server_entry.asm" ;;
        storage) entry_source="$laix_dir/user/screen/storage_entry.asm" ;;
    esac
    if [ -n "$entry_source" ]; then
        python3 "$repo_dir/mc/asm.py" -c "$entry_source" -o "$service_dir/$image-entry.o"
        set -- "$@" "$service_dir/$image-entry.o"
    fi
    python3 "$repo_dir/mc/mc.py" -c "$laix_dir/src/task/service_start.m" -o "$service_dir/$image-start.o"
    python3 "$repo_dir/mc/mc.py" -c "$laix_dir/src/task/runtime_start.m" -o "$service_dir/$image-task-abi.o"
    python3 "$repo_dir/mc/mc.py" -c "$laix_dir/src/arch/wrm081632/defs.m" -o "$service_dir/$image-defs.o"
    python3 "$repo_dir/mc/asm.py" -c "$repo_dir/mc/runtime/mem.asm" -o "$service_dir/$image-mem.o"
    python3 "$repo_dir/mc/ld.py" --layout exec --base 0x41000000 "$@" \
        "$service_dir/$image-start.o" "$service_dir/$image-task-abi.o" "$service_dir/$image-defs.o" "$service_dir/$image-mem.o" \
        -o "$service_dir/$image.elf" --map "$service_dir/$image.map"
done
