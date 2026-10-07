#!/bin/sh
# User images of the supervised display profile (LAIX_CONSOLE=screenrecovery):
# Echo, bitmap storage, Screen and the supervisor/client. With
# LAIX_SCREEN_RECOVERY_FIXTURES=scenario (or 1) the Screen and supervisor are the
# CPU crash fixtures from tests/programs/screenrecovery; with =watchdog only the
# Screen is, and the production supervisor and client meet its faults. Echo and
# bitmap storage never are fixtures.
set -eu
laix_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
repo_dir=$(dirname -- "$laix_dir")
output="$laix_dir/build/screen-recovery-user"
mkdir -p "$output"
screen_module=user/recovery/screen
policy_module=user/recovery/screen_supervisor
policy_image=supervisor
case "${LAIX_SCREEN_RECOVERY_FIXTURES:-0}" in
    0) ;;
    1|scenario)
        screen_module=tests/programs/screenrecovery/screen
        policy_module=tests/programs/screenrecovery/scenario
        policy_image=policy
        ;;
    watchdog) screen_module=tests/programs/screenrecovery/screen ;;
    *) printf '%s\n' 'LAIX_SCREEN_RECOVERY_FIXTURES must be 0, scenario or watchdog' >&2; exit 1 ;;
esac
screen_logic='user/screen/server user/screen/unicode user/screen/font user/screen/cache user/screen/video src/task/service_start'
for image in echo bitmap screen $policy_image; do
    set --
    case "$image" in
        echo) modules='user/recovery/echo user/recovery/server user/services/disk user/services/files src/task/service_start' ;;
        bitmap) modules='user/recovery/bitmap user/screen/storage src/task/service_start' ;;
        screen) modules="$screen_module $screen_logic" ;;
        *) modules="$policy_module user/recovery/policy user/screen/client" ;;
    esac
    for module in $modules user/syscalls src/task/runtime_start src/task/recovery_start src/arch/wrm081632/defs; do
        module_name=$(printf '%s' "$module" | tr / -)
        object="$output/$image-$module_name.o"
        python3 "$repo_dir/mc/mc.py" -c "$laix_dir/$module.m" -o "$object"
        set -- "$@" "$object"
    done
    python3 "$repo_dir/mc/asm.py" -c "$repo_dir/mc/runtime/mem.asm" -o "$output/mem.o"
    output_name=$image
    if [ "$image" = supervisor ]; then output_name=policy; fi
    python3 "$repo_dir/mc/ld.py" --layout exec --base 0x41000000 "$@" "$output/mem.o" \
        -o "$output/$output_name.elf" --map "$output/$output_name.map"
done
