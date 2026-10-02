#!/usr/bin/env bash
# Prove the media sandbox is isolated. Usage: scripts/sandbox_smoke.sh IMAGE
set -euo pipefail
IMG="${1:-fairclip-media:0.2.0}"
FLAGS=(--rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 --memory 512m --user 65534:65534)
pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✕\033[0m %s\n' "$1"; exit 1; }

echo "sandbox smoke test: $IMG"
docker run "${FLAGS[@]}" --entrypoint ffmpeg "$IMG" -hide_banner -version >/dev/null && pass "ffmpeg runs" || fail "ffmpeg missing"
if docker run "${FLAGS[@]}" --entrypoint python3 "$IMG" -c "import socket; socket.create_connection(('1.1.1.1', 443), timeout=3)" 2>/dev/null; then
  fail "network reachable"; else pass "no network"; fi
if docker run "${FLAGS[@]}" --entrypoint python3 "$IMG" -c "open('/usr/bin/pwned','w')" 2>/dev/null; then
  fail "root filesystem writable"; else pass "read-only root filesystem"; fi
uid=$(docker run "${FLAGS[@]}" --entrypoint python3 "$IMG" -c "import os; print(os.getuid())")
[ "$uid" != "0" ] && pass "runs as uid $uid (not root)" || fail "runs as root"
if docker run "${FLAGS[@]}" --entrypoint ffmpeg "$IMG" -hide_banner -protocol_whitelist file -i http://example.com/x.mp4 -f null - 2>/dev/null; then
  fail "ffmpeg fetched a URL"; else pass "ffmpeg cannot open network URLs"; fi
out=$(docker run "${FLAGS[@]}" --entrypoint python3 "$IMG" -c "
import sys; sys.path.insert(0, '/app'); from pathlib import Path; import worker
j = Path('/tmp/job-x')
print(worker.valid({'tool': 'bash', 'args': []}, j), worker.valid({'tool': 'ffmpeg', 'args': ['-i', 'http://evil/x']}, j),
      worker.valid({'tool': 'ffmpeg', 'args': ['-i', '/etc/passwd']}, j), worker.valid({'tool': 'ffmpeg', 'args': ['-i', '/tmp/job-x/../y']}, j))")
[ "$out" = "None None None None" ] && pass "worker policy rejects other tools, URLs, outside paths, traversal" || fail "worker policy: $out"
echo "all sandbox checks passed"
