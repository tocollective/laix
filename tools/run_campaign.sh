#!/bin/sh
# Produce identified inputs for every acceptance profile, then run the source
# suite and every no-build CPU profile against them. The emulator and ROM must
# already exist; nothing here builds WRM. DESTINATION must be new.
#   sh laix/tools/run_campaign.sh DESTINATION [EMULATOR ROM]
# Afterwards: python3 laix/tools/acceptance_provenance.py --campaign DESTINATION
set -u
laix_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
repo_dir=$(dirname -- "$laix_dir")
destination=$1
emulator=${2:-"$repo_dir/bin/wrm081632"}
rom=${3:-"$repo_dir/bin/firmware.rom"}
profiles=${LAIX_CAMPAIGN_PROFILES:-"uart screen services uart-stress screen-stress memory sharing objects supervisor soak loader fs shell net recovery lifetime screenrecovery latency hid media"}
if [ -e "$destination" ]; then
    printf '%s\n' 'DESTINATION must be new; preserve older campaigns' >&2
    exit 1
fi
mkdir -p "$destination/inputs" "$destination/cpu" "$destination/source"
status=0
python3 -B "$laix_dir/tests/run_source_suite.py" --log-dir "$destination/source" \
    > "$destination/source/run.log" 2>&1 \
    && printf 'PASS source suite\n' || { printf 'FAIL source suite\n'; status=1; }
for profile in $profiles; do
    sh "$laix_dir/tools/build_acceptance_bundle.sh" "$profile" "$destination/inputs/$profile" "$emulator" "$rom" \
        > "$destination/pack-$profile.log" 2>&1 || { printf 'FAIL pack %s\n' "$profile"; status=1; continue; }
    printf 'PASS pack %s\n' "$profile"
done
for profile in $profiles; do
    [ -d "$destination/inputs/$profile" ] || continue
    python3 -B "$laix_dir/tests/acceptance_bundle.py" run --bundle "$destination/inputs/$profile" \
        --profile "$profile" --log-dir "$destination/cpu/$profile" > "$destination/run-$profile.log" 2>&1 \
        && printf 'PASS cpu %s\n' "$profile" || { printf 'FAIL cpu %s\n' "$profile"; status=1; }
done
exit $status
