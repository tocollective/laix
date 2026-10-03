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
    *) printf '%s\n' 'Service profile must be screen or services' >&2; exit 1 ;;
esac
for image in $images; do
    case "$image" in
        input) modules='services/input' ;;
        disk) modules='services/disk' ;;
        files) modules='services/files' ;;
        simple-application) modules='services/application services/client' ;;
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
    python3 "$repo_dir/mc/mc.py" -c "$laix_dir/src/task/service_start.m" -o "$service_dir/$image-start.o"
    python3 "$repo_dir/mc/mc.py" -c "$laix_dir/src/arch/wrm081632/defs.m" -o "$service_dir/$image-defs.o"
    python3 "$repo_dir/mc/asm.py" -c "$repo_dir/mc/runtime/mem.asm" -o "$service_dir/$image-mem.o"
    python3 "$repo_dir/mc/ld.py" --layout exec --base 0x41000000 "$@" \
        "$service_dir/$image-start.o" "$service_dir/$image-defs.o" "$service_dir/$image-mem.o" \
        -o "$service_dir/$image.elf" --map "$service_dir/$image.map"
done
