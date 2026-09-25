#!/usr/bin/env bash
# T2a helper: sample the HOST-side cgroup v2 memory high-water mark of a running container.
#
# Why this exists: docker removes the container's cgroup directory as soon as the container
# exits, so a post-run host-side read of memory.peak is impossible (it silently yields null).
# The only honest way to get a host-side peak is to sample it while the container is alive.
# The driver samples the same counter from *inside* the container; this is the out-of-band
# cross-check that the in-container view of the cgroup is the real one.
#
# usage: sample_host_peak.sh <container-id-short> <output-file> [interval-seconds]
set -uo pipefail

CID="${1:?container id}"
OUTF="${2:?output file}"
INTERVAL="${3:-2}"

FULL="$(docker inspect -f '{{.Id}}' "${CID}" 2>/dev/null)" || { echo "[sample] no such container ${CID}" >&2; exit 1; }
CG="/sys/fs/cgroup/docker/${FULL}"
[[ -d "${CG}" ]] || { echo "[sample] no cgroup dir ${CG}" >&2; exit 1; }

: > "${OUTF}"
echo "[sample] watching ${CG} every ${INTERVAL}s -> ${OUTF}" >&2
while :; do
    peak="$(cat "${CG}/memory.peak" 2>/dev/null)" || break
    cur="$(cat "${CG}/memory.current" 2>/dev/null)"
    swapc="$(cat "${CG}/memory.swap.current" 2>/dev/null)"
    oom="$(awk '/^oom_kill /{print $2}' "${CG}/memory.events" 2>/dev/null)"
    printf '%s peak=%s current=%s swap_current=%s oom_kill=%s\n' \
        "$(date -u +%FT%TZ)" "${peak}" "${cur}" "${swapc}" "${oom}" >> "${OUTF}"
    running="$(docker inspect -f '{{.State.Running}}' "${CID}" 2>/dev/null)"
    [[ "${running}" == "true" ]] || break
    sleep "${INTERVAL}"
done
echo "[sample] container gone; final host peak line below" >&2
tail -n 1 "${OUTF}" >&2 || true
