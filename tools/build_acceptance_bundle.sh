#!/bin/sh
# Produce LA/IX artifacts only. The emulator and ROM must already exist.
set -eu
laix_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
# Profiles select their own mains, resources and fixture policy.
unset LAIX_MAIN LAIX_CONSOLE LAIX_FONT LAIX_ACCEPTANCE_INPUT LAIX_RECOVERY_FIXTURES
profile=$1
destination=$2
emulator=$3
rom=$4
case "$profile" in
    uart) sh "$laix_dir/build.sh" ;;
    screen|services|memory|sharing|objects|supervisor)
        LAIX_CONSOLE=$profile sh "$laix_dir/build.sh" ;;
    recovery) LAIX_CONSOLE=recovery LAIX_RECOVERY_FIXTURES=1 sh "$laix_dir/build.sh" ;;
    recovery-production) LAIX_CONSOLE=recovery LAIX_RECOVERY_FIXTURES=0 sh "$laix_dir/build.sh" ;;
    latency) LAIX_MAIN="$laix_dir/tests/programs/limits/latency.m" sh "$laix_dir/build.sh" ;;
    media)
        LAIX_FONT="$laix_dir/tests/programs/console/media.hex" LAIX_CONSOLE=services sh "$laix_dir/build.sh" ;;
    hid)
        LAIX_ACCEPTANCE_INPUT=1 sh "$laix_dir/tools/build_services.sh" services
        LAIX_MAIN="$laix_dir/tests/programs/console/input_stress.m" sh "$laix_dir/build.sh" ;;
    uart-stress|screen-stress)
        sh "$laix_dir/tools/build_services.sh" screen
        sh "$laix_dir/tools/build_stress_client.sh"
        main=uart_stress
        if [ "$profile" = screen-stress ]; then main=screen_stress; fi
        LAIX_MAIN="$laix_dir/tests/programs/console/$main.m" sh "$laix_dir/build.sh" ;;
    *) printf '%s\n' 'Unsupported acceptance profile' >&2; exit 1 ;;
esac
python3 -B "$laix_dir/tests/acceptance_bundle.py" pack --profile "$profile" --dest "$destination" \
    --emulator "$emulator" --rom "$rom" --built-from-current-source \
    --producer-run-id "${GITHUB_RUN_ID:-local}"
