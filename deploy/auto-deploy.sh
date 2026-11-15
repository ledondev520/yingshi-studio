#!/usr/bin/env bash
# Zero-downtime atomic release deployer for Yingshi H3 Studio.
# Safely promotes new origin/main commits into /opt/h3-studio/current without
# touching /var/lib/h3-studio (SQLite, media, checkpoints) or active GPU leases.
set -euo pipefail

BASE_DIR="${H3_BASE_DIR:-/opt/h3-studio}"
REPO_DIR="${H3_REPO_DIR:-$BASE_DIR/repo}"
RELEASES_DIR="${H3_RELEASES_DIR:-$BASE_DIR/releases}"
CURRENT_LINK="${H3_CURRENT_LINK:-$BASE_DIR/current}"
DATA_DIR="${STUDIO_DATA:-/var/lib/h3-studio}"
KEYS_DIR="${STUDIO_KEYS:-/etc/h3-studio}"
REPO_URL="${H3_REPO_URL:-https://github.com/ledondev520/yingshi-studio.git}"
BRANCH="${H3_BRANCH:-main}"
STUDIO_UNIT="${H3_STUDIO_UNIT:-h3-studio.service}"
AUTOWAKE_UNIT="${H3_AUTOWAKE_UNIT:-auto}"
HEALTH_URL="${H3_HEALTH_URL:-http://127.0.0.1:${STUDIO_PORT:-18763}/api/health}"
LOCK_FILE="${H3_DEPLOY_LOCK:-/run/lock/h3-studio-deploy.lock}"
SYSTEMCTL_BIN="${H3_SYSTEMCTL:-systemctl}"
KEEP_RELEASES="${H3_KEEP_RELEASES:-5}"
DRAIN_SECONDS="${H3_PLANNING_DRAIN_SECONDS:-45}"

mkdir -p "$(dirname "$LOCK_FILE")" "$RELEASES_DIR"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  exit 0
fi

extract_legacy_projects() {
  local src_dir="$1"
  mkdir -p "$KEYS_DIR"
  python3 - "$src_dir" "$KEYS_DIR" <<'PY'
import ast, sys
from pathlib import Path

src, keys = Path(sys.argv[1]), Path(sys.argv[2])
pairs = [
    (src / "director.py", keys / "gcp-project"),
    (src / "autowake" / "colab_rest.py", keys / "colab-project"),
]
for code_path, target in pairs:
    if target.is_file() and target.read_text().strip() not in ("", "YOUR_GOOGLE_CLOUD_PROJECT"):
        continue
    if not code_path.is_file():
        continue
    try:
        tree = ast.parse(code_path.read_text())
    except Exception:
        continue
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "PROJECT" for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                val = node.value.value.strip()
                if val and val != "YOUR_GOOGLE_CLOUD_PROJECT":
                    target.write_text(val + "\n")
                    target.chmod(0o644)
PY
}

if [ ! -d "$REPO_DIR/.git" ]; then
  rm -rf "$REPO_DIR"
  git clone --quiet --branch "$BRANCH" "$REPO_URL" "$REPO_DIR"
else
  git -C "$REPO_DIR" fetch --quiet origin "$BRANCH"
fi

REMOTE_SHA="$(git -C "$REPO_DIR" rev-parse "origin/$BRANCH")"
LIVE_SHA=""
if [ -f "$CURRENT_LINK/.deployed-sha" ]; then
  LIVE_SHA="$(tr -d '[:space:]' < "$CURRENT_LINK/.deployed-sha")"
fi

if [ "$REMOTE_SHA" = "$LIVE_SHA" ] && [ -L "$CURRENT_LINK" ]; then
  exit 0
fi

echo "[h3-deploy] Preparing release $REMOTE_SHA (previous: ${LIVE_SHA:-legacy})..."

if [ -d "$CURRENT_LINK" ] && [ ! -L "$CURRENT_LINK" ]; then
  extract_legacy_projects "$CURRENT_LINK"
fi

STAGE_DIR="$RELEASES_DIR/$REMOTE_SHA"
rm -rf "$STAGE_DIR.tmp"
mkdir -p "$STAGE_DIR.tmp"
git -C "$REPO_DIR" archive "$REMOTE_SHA" | tar -x -C "$STAGE_DIR.tmp"
printf '%s\n' "$REMOTE_SHA" > "$STAGE_DIR.tmp/.deployed-sha"

echo "[h3-deploy] Running pre-deploy validation checks..."
(
  cd "$STAGE_DIR.tmp"
  python3 validate_previews.py >/dev/null
  python3 validate_pool.py >/dev/null
  python3 validate_preparation.py >/dev/null
  python3 validate_scale.py >/dev/null
  python3 validate_launch.py >/dev/null
  python3 validate_multiuser.py >/dev/null
  python3 validate_worker_loop.py >/dev/null
  python3 validate_images.py >/dev/null
  python3 validate_image_worker.py >/dev/null
  python3 validate_planning.py >/dev/null
  python3 validate_model_cache.py >/dev/null
  if command -v node >/dev/null 2>&1; then
    node --check app.js >/dev/null
  fi
)

rm -rf "$STAGE_DIR"
mv "$STAGE_DIR.tmp" "$STAGE_DIR"

# Wait briefly if a job is actively in Gemini 'planning' state so we don't interrupt a storyboard call.
DB_FILE="$DATA_DIR/studio.sqlite"
if [ -f "$DB_FILE" ] && [ "$DRAIN_SECONDS" -gt 0 ]; then
  for ((i = 0; i < DRAIN_SECONDS; i++)); do
    PLANNING="$(python3 - "$DB_FILE" <<'PY'
import sqlite3, sys
try:
    with sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=2) as c:
        print(c.execute("SELECT COUNT(*) FROM jobs WHERE state='planning'").fetchone()[0])
except Exception:
    print(0)
PY
)"
    if [ "${PLANNING:-0}" = "0" ]; then
      break
    fi
    sleep 1
  done
fi

PREV_TARGET=""
if [ -L "$CURRENT_LINK" ]; then
  PREV_TARGET="$(readlink -f "$CURRENT_LINK" || true)"
elif [ -d "$CURRENT_LINK" ]; then
  BACKUP_DIR="$RELEASES_DIR/legacy-$(date +%Y%m%d-%H%M%S)"
  mv "$CURRENT_LINK" "$BACKUP_DIR"
  PREV_TARGET="$BACKUP_DIR"
fi

switch_current() {
  local target="$1"
  ln -sfn "$target" "$CURRENT_LINK.next"
  mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"
}

check_health() {
  for _ in {1..30}; do
    if python3 - "$HEALTH_URL" <<'PY'
import json, sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as r:
        sys.exit(0 if r.status == 200 and json.load(r).get("ok") is True else 1)
except Exception:
    sys.exit(1)
PY
    then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

switch_current "$STAGE_DIR"
"$SYSTEMCTL_BIN" restart "$STUDIO_UNIT"

if ! check_health; then
  echo "[h3-deploy] ERROR: Health check failed for $REMOTE_SHA; rolling back!" >&2
  if [ -n "$PREV_TARGET" ] && [ -d "$PREV_TARGET" ]; then
    switch_current "$PREV_TARGET"
    "$SYSTEMCTL_BIN" restart "$STUDIO_UNIT" || true
  fi
  exit 1
fi

detect_autowake_units() {
  if [ "$AUTOWAKE_UNIT" != "auto" ]; then
    [ -n "$AUTOWAKE_UNIT" ] && printf '%s\n' "$AUTOWAKE_UNIT"
    return
  fi
  "$SYSTEMCTL_BIN" list-units --type=service --state=active --no-legend --plain 2>/dev/null | awk '{print $1}' | while read -r unit; do
    [ -z "$unit" ] && continue
    if [ "$unit" = "$STUDIO_UNIT" ]; then continue; fi
    exec_line="$("$SYSTEMCTL_BIN" show -p ExecStart --value "$unit" 2>/dev/null || true)"
    if [[ "$exec_line" == *"autowake/controller.py"* ]]; then
      printf '%s\n' "$unit"
    fi
  done
}

while read -r aw_unit; do
  [ -z "$aw_unit" ] && continue
  echo "[h3-deploy] Restarting autowake unit: $aw_unit"
  "$SYSTEMCTL_BIN" restart "$aw_unit"
done < <(detect_autowake_units) || true

# Prune old releases while preserving current and previous targets.
{ ls -1dt "$RELEASES_DIR"/* 2>/dev/null || true; } | tail -n +"$((KEEP_RELEASES + 1))" | while read -r old_dir; do
  if [ "$old_dir" != "$STAGE_DIR" ] && [ "$old_dir" != "$PREV_TARGET" ]; then
    rm -rf "$old_dir"
  fi
done || true

echo "[h3-deploy] Successfully deployed $REMOTE_SHA"
