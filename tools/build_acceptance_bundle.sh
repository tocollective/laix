#!/bin/sh
# Produce LA/IX artifacts only. The emulator and ROM must already exist.
set -eu
laix_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
# Profiles are the reference boots of tools/build_fixture.sh: their own mains,
# resources and fixture policy. The product (build.sh, one kernel, sessions) is not a profile.
unset LAIX_MAIN LAIX_CONSOLE LAIX_FIXTURE LAIX_SESSION LAIX_FONT LAIX_ACCEPTANCE_INPUT LAIX_ACCEPTANCE_FILEREAD LAIX_RECOVERY_FIXTURES LAIX_SCREEN_RECOVERY_FIXTURES
profile=$1
destination=$2
emulator=$3
rom=$4
case "$profile" in
    uart) sh "$laix_dir/tools/build_fixture.sh" ;;
    screen|services|memory|sharing|objects|supervisor|soak|loader|fs|shell|net)
        LAIX_FIXTURE=$profile sh "$laix_dir/tools/build_fixture.sh" ;;
    recovery) LAIX_FIXTURE=recovery LAIX_RECOVERY_FIXTURES=1 sh "$laix_dir/tools/build_fixture.sh" ;;
    lifetime) LAIX_FIXTURE=recovery LAIX_RECOVERY_FIXTURES=lifetime sh "$laix_dir/tools/build_fixture.sh" ;;
    screenrecovery) LAIX_FIXTURE=screenrecovery LAIX_SCREEN_RECOVERY_FIXTURES=scenario sh "$laix_dir/tools/build_fixture.sh" ;;
    latency) LAIX_MAIN="$laix_dir/tests/programs/limits/latency.m" sh "$laix_dir/tools/build_fixture.sh" ;;
    media)
        LAIX_FONT="$laix_dir/tests/programs/console/media.hex" LAIX_FIXTURE=services sh "$laix_dir/tools/build_fixture.sh" ;;
    hid)
        LAIX_ACCEPTANCE_INPUT=1 sh "$laix_dir/tools/build_services.sh" services
        LAIX_MAIN="$laix_dir/tests/programs/console/input_stress.m" sh "$laix_dir/tools/build_fixture.sh" ;;
    uart-stress|screen-stress)
        sh "$laix_dir/tools/build_services.sh" screen
        sh "$laix_dir/tools/build_stress_client.sh"
        main=uart_stress
        if [ "$profile" = screen-stress ]; then main=screen_stress; fi
        LAIX_MAIN="$laix_dir/tests/programs/console/$main.m" sh "$laix_dir/tools/build_fixture.sh" ;;
    *) printf '%s\n' 'Unsupported acceptance profile' >&2; exit 1 ;;
esac
python3 -B "$laix_dir/tests/acceptance_bundle.py" pack --profile "$profile" --dest "$destination" \
    --emulator "$emulator" --rom "$rom" --built-from-current-source \
    --producer-run-id "${GITHUB_RUN_ID:-local}"
