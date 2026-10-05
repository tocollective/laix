#!/bin/sh
set -eu
laix_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
repo_dir=$(dirname -- "$laix_dir")
service_dir="$laix_dir/build/services"
mkdir -p "$service_dir"
set --
for module in tests/programs/console/stress_client user/console user/screen/client user/syscalls src/task/start src/task/service_start src/arch/wrm081632/defs; do
    object="$service_dir/stress-$(basename -- "$module").o"
    # Console and Screen client module basenames differ; no duplicate objects.
    python3 "$repo_dir/mc/mc.py" -c "$laix_dir/$module.m" -o "$object"
    set -- "$@" "$object"
done
python3 "$repo_dir/mc/asm.py" -c "$repo_dir/mc/runtime/mem.asm" -o "$service_dir/stress-mem.o"
python3 "$repo_dir/mc/ld.py" --layout exec --base 0x41000000 "$@" "$service_dir/stress-mem.o" \
    -o "$service_dir/stress-client.elf" --map "$service_dir/stress-client.map"
