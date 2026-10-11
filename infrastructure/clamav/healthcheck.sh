#!/bin/sh
# The reference ClamAV container's health check (infrastructure/compose.production.yml):
# clamd answers PING, and its signatures are recent. freshclam failing quietly (mirror
# unreachable, rate-limited, a full volume) would otherwise leave clamd healthy while it
# scans with ever older signatures (adversarial review P3). The daily database's build time
# comes from clamd's own VERSION reply ("ClamAV 1.5.4/28150/Sun Oct 11 06:26:01 2026");
# older than CLAMAV_MAX_SIGNATURE_AGE_S (3 days by default) is unhealthy.
set -eu

max_age_s="${CLAMAV_MAX_SIGNATURE_AGE_S:-259200}"

if [ "$(echo PING | nc -w 5 127.0.0.1 3310)" != "PONG" ]; then
  echo "clamd: no answer"
  exit 1
fi
reply="$(echo VERSION | nc -w 5 127.0.0.1 3310)"
built="${reply##*/}"
if ! when="$(date -u -D '%a %b %d %H:%M:%S %Y' -d "$built" +%s 2>/dev/null)"; then
  echo "clamd: unreadable VERSION reply: $reply"
  exit 1
fi
age=$(( $(date -u +%s) - when ))
if [ "$age" -gt "$max_age_s" ]; then
  echo "clamd: signatures from $built are $age s old (freshclam isn't updating them)"
  exit 1
fi
echo "clamd: up, signatures from $built"
