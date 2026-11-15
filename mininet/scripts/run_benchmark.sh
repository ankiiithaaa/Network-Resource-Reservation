#!/usr/bin/env bash
# From a sudo-capable Ubuntu user. The optional arguments go to test_cases.py.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
for arg in "$@"; do
    case "$arg" in
        --out|--out=*|--checks|--checks=*)
            printf 'Set an output directory by invoking test_cases.py directly.\n' >&2
            exit 2
            ;;
    esac
done
run_dir="results/run-$(date -u +%Y%m%dT%H%M%S%NZ)"
status=0
sudo /usr/bin/python3 test_cases.py "$@" --out "$run_dir" || status=$?
if [[ -d "$run_dir" ]]; then
    sudo chown -R "$(id -u):$(id -g)" "$run_dir"
    printf 'Evidence directory: %s\n' "$run_dir"
fi
if (( status != 0 )); then
    exit "$status"
fi
/usr/bin/python3 plot_results.py "$run_dir/performance_results.csv"
