#!/usr/bin/env bash
# Fail when any of the hostnames resolves inside any of the comma-separated containers.
set -euo pipefail

IFS=',' read -r -a containers <<< "${1:?containers}"
shift
failed=0
for container in "${containers[@]}"; do
  for hostname in "$@"; do
    status=0
    address="$(docker exec "$container" getent hosts "$hostname")" || status=$?
    case "$status" in
      0)
        echo "${hostname} resolves in ${container}: ${address}" >&2
        failed=1
        ;;
      2) echo "${hostname} does not resolve in ${container}" ;;
      *)
        echo "Cannot look up ${hostname} in ${container} (exit ${status})" >&2
        failed=1
        ;;
    esac
  done
done
exit "$failed"
