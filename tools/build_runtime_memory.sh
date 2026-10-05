#!/bin/sh
set -eu
laix_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
repo_dir=$(dirname -- "$laix_dir")
profile=${1:-memory}
case "$profile" in
    memory) source=runtime_memory_user ;;
    sharing) source=memory_sharing_user ;;
    *) printf '%s\n' 'Memory fixture must be memory or sharing' >&2; exit 1 ;;
esac
output="$laix_dir/build/$profile-user"
mkdir -p "$output"
set --
for module in tests/programs/mm/$source user/heap user/syscalls src/task/runtime_start src/arch/wrm081632/defs; do
    object="$output/$(basename -- "$module").o"
    python3 "$repo_dir/mc/mc.py" -c "$laix_dir/$module.m" -o "$object"
    set -- "$@" "$object"
done
python3 "$repo_dir/mc/asm.py" -c "$repo_dir/mc/runtime/mem.asm" -o "$output/mem.o"
python3 "$repo_dir/mc/ld.py" --layout exec --base 0x41000000 "$@" "$output/mem.o" \
    -o "$output/$profile.elf" --map "$output/$profile.map"
