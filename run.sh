#!/bin/sh
set -eu

laix_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(dirname -- "$laix_dir")
emulator=${WRM_EMULATOR:-"$repo_dir/bin/wrm081632"}
rom=${WRM_ROM:-"$repo_dir/bin/firmware.rom"}

if [ ! -x "$emulator" ]; then
    printf 'Emulator not found: %s (set WRM_EMULATOR to its path)\n' "$emulator" >&2
    exit 1
fi
if [ ! -f "$rom" ]; then
    printf 'ROM not found: %s (set WRM_ROM to an existing ROM image)\n' "$rom" >&2
    exit 1
fi
case "${LAIX_CONSOLE:-uart}" in
    uart) image_name=laix ;;
    screen|services|fs|shell|net|supervisor|memory|sharing|objects|recovery|screenrecovery) image_name=$LAIX_CONSOLE ;;
    *) printf '%s\n' 'LAIX_CONSOLE must be uart, screen, services, fs, shell, net, supervisor, memory, sharing, objects, recovery or screenrecovery' >&2; exit 1 ;;
esac
if [ ! -f "$laix_dir/build/$image_name.img" ]; then
    printf 'Run laix/build.sh first.\n' >&2
    exit 1
fi

# Boot from hard disk 0 by default; bitmap sectors are read on demand.
case "${LAIX_BOOT:-hdd}" in
    hdd) boot_option=--hdd ;;
    floppy) boot_option=--floppy ;;
    *) printf 'LAIX_BOOT must be hdd or floppy.\n' >&2; exit 1 ;;
esac

exec "$emulator" --rom "$rom" \
    "$boot_option" "$laix_dir/build/$image_name.img" "$@"
