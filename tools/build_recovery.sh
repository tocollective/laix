#!/bin/sh
set -eu
laix_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
repo_dir=$(dirname -- "$laix_dir")
output="$laix_dir/build/recovery-user"
mkdir -p "$output"
# Reference boot of tools/build_fixture.sh (LAIX_FIXTURE=recovery). The production
# supervisor is init's recovery session now (user/recovery/supervisor.m, run by
# user/init), so only the fixtures are built here.
# LAIX_RECOVERY_FIXTURES=1 builds the service-fault fixtures. =lifetime keeps the
# production Echo, Disk and Files images and replaces only the supervisor with
# the G4 lifetime scenario (tests/programs/lifetime); catalog image 5 is still
# the supervisor image, which the scenario also runs as its client.
policy_module=
case "${LAIX_RECOVERY_FIXTURES:-1}" in
    1) image_source=tests/programs/recovery; images='echo disk files policy' ;;
    lifetime)
        image_source=user/recovery
        images='echo disk files policy'
        policy_module=tests/programs/lifetime/policy
        ;;
    *) printf '%s\n' 'LAIX_RECOVERY_FIXTURES must be 1 or lifetime (production is the recovery session)' >&2; exit 1 ;;
esac
for image in $images; do
    set --
    modules="$image_source/$image"
    if [ "$image" = policy ] && [ -n "$policy_module" ]; then modules="$policy_module"; fi
    if [ "$image" = policy ]; then
        modules="$modules user/recovery/policy"
    else
        modules="$modules $image_source/server user/services/disk user/services/files src/task/service_start"
    fi
    for module in $modules user/syscalls src/task/runtime_start src/task/recovery_start src/arch/wrm081632/defs; do
        module_name=$(printf '%s' "$module" | tr / -)
        object="$output/$image-$module_name.o"
        # The fixture and reusable policy share a basename; preserve both objects.
        if [ "$module" = user/recovery/policy ]; then object="$output/$image-supervisor.o"; fi
        python3 "$repo_dir/mc/mc.py" -c "$laix_dir/$module.m" -o "$object"
        set -- "$@" "$object"
    done
    python3 "$repo_dir/mc/asm.py" -c "$repo_dir/mc/runtime/mem.asm" -o "$output/mem.o"
    output_name=$image
    python3 "$repo_dir/mc/ld.py" --layout exec --base 0x41000000 "$@" "$output/mem.o" \
        -o "$output/$output_name.elf" --map "$output/$output_name.map"
done
