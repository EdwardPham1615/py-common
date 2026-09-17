#!/usr/bin/env bash
# Each extra must be sufficient on its own for the modules it promises.
#
# The unit suite cannot check this. It runs under `--extra all`, so every
# optional dependency is importable and a module that reaches outside its own
# extra looks fine. The failure only appears for a consumer who installed
# exactly what the README told them to -- and then it is total: in 0.2.0,
# `py-common[http]` could not import `py_common.http.middleware` at all,
# because a package __init__ pulled in `redis` from the `cache` extra.
#
# So: one throwaway environment per extra, install just that extra, import what
# it is supposed to provide. Slow enough to keep out of `make test`, cheap
# enough to run on every CI build.
set -euo pipefail

# extra:module[,module...]  -- what installing that extra alone must give you.
CASES=(
  "http:py_common.http,py_common.http.middleware"
  "security:py_common.security"
  "cache:py_common.cache"
  "storage:py_common.storage"
  "telemetry:py_common.telemetry"
  "runtime:py_common.runtime"
  "persistence:py_common.persistence"
  "migrations:py_common.persistence.migrations"
  "profiling:py_common.telemetry.profiler"
)

# Deliberately not a case: `grpc` on its own. It contributes grpcio and the OTel
# gRPC instrumentation to code that lives under `py_common.runtime`, so a gRPC
# service installs `[runtime,grpc]`. There is no module `[grpc]` alone owns.

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
failed=0

for case in "${CASES[@]}"; do
  extra="${case%%:*}"
  modules="${case#*:}"
  venv="$workdir/$extra"

  uv venv "$venv" --quiet --python "$(cat .python-version)"
  uv pip install --python "$venv" --quiet ".[$extra]"

  for module in ${modules//,/ }; do
    if out=$("$venv/bin/python" -c "import $module" 2>&1); then
      printf '  ok      [%s] %s\n' "$extra" "$module"
    else
      printf '  FAILED  [%s] %s\n' "$extra" "$module"
      printf '%s\n' "$out" | sed 's/^/          /' | tail -4
      failed=1
    fi
  done
  rm -rf "$venv"
done

if [ "$failed" -ne 0 ]; then
  echo
  echo "An extra does not stand on its own. Move the offending import into the" >&2
  echo "code path that needs it, or declare the dependency in that extra." >&2
  exit 1
fi
