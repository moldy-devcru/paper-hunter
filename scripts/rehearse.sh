#!/usr/bin/env bash
# Full-cycle dry rehearsal: the whole pipeline, offline, against a throwaway world.
#
# Zero network (socket.connect is patched to raise inside the run), zero credentials,
# zero orders (the only router is DryRunRouter), zero writes outside --workdir. The
# deployed journal and IV store under /opt are opened READ-ONLY, and the run fails if
# anything under /opt changed.
#
#   scripts/rehearse.sh                       # temp workdir, report to stdout
#   scripts/rehearse.sh --keep                # keep the temp workdir and print its path
#   scripts/rehearse.sh --json /tmp/rep.json  # machine-readable report
#
# Exit status is 0 only if every stage ran. Any FAIL block is a rehearsal finding, not
# a crash: read the report before deciding what it means.
set -euo pipefail

cd "$(dirname "$0")/.."

PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
  if [[ -x .venv/bin/python ]]; then
    PY=.venv/bin/python
  else
    PY=python3
  fi
fi

WORKDIR=""
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep)
      WORKDIR="$(mktemp -d -t paper-hunter-rehearsal-XXXXXX)"
      ARGS+=(--workdir "$WORKDIR" --keep)
      ;;
    *)
      ARGS+=("$1")
      ;;
  esac
  shift
done

exec "$PY" -m executor.rehearsal ${ARGS[@]+"${ARGS[@]}"}