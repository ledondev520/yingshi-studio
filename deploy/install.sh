#!/usr/bin/env bash
# One-time setup on the production VPS to enable automatic, zero-downtime
# deployments whenever changes are merged into GitHub main.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 权限运行：sudo bash deploy/install.sh" >&2
  exit 1
fi

BASE_DIR="${H3_BASE_DIR:-/opt/h3-studio}"
CURRENT_LINK="${H3_CURRENT_LINK:-$BASE_DIR/current}"
KEYS_DIR="${STUDIO_KEYS:-/etc/h3-studio}"
REPO_URL="${H3_REPO_URL:-https://github.com/ledondev520/yingshi-studio.git}"
STUDIO_UNIT="${H3_STUDIO_UNIT:-h3-studio.service}"

mkdir -p "$BASE_DIR" "$KEYS_DIR"

# 1. Preserve live GCP project IDs from existing /opt/h3-studio/current before switching to symlink releases.
if [ -d "$CURRENT_LINK" ]; then
  python3 - "$CURRENT_LINK" "$KEYS_DIR" <<'PY'
import ast, sys
from pathlib import Path

src, keys = Path(sys.argv[1]), Path(sys.argv[2])
for rel, out_name in (("director.py", "gcp-project"), ("autowake/colab_rest.py", "colab-project")):
    target = keys / out_name
    if target.is_file() and target.read_text().strip() not in ("", "YOUR_GOOGLE_CLOUD_PROJECT"):
        continue
    code = src / rel
    if not code.is_file():
        continue
    try:
        tree = ast.parse(code.read_text())
    except Exception:
        continue
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "PROJECT" for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                val = node.value.value.strip()
                if val and val != "YOUR_GOOGLE_CLOUD_PROJECT":
                    target.write_text(val + "\n")
                    target.chmod(0o644)
                    print(f"[install] 已提取并持久化 {out_name}: {val}")
PY
fi

# Inject systemd drop-ins so both h3-studio and any autowake service inherit project IDs regardless of /etc/h3-studio permissions.
if [ -s "$KEYS_DIR/gcp-project" ]; then
  GCP_PROJ="$(tr -d '[:space:]' < "$KEYS_DIR/gcp-project")"
  mkdir -p "/etc/systemd/system/${STUDIO_UNIT}.d"
  printf '[Service]\nEnvironment=H3_GCP_PROJECT=%s\n' "$GCP_PROJ" > "/etc/systemd/system/${STUDIO_UNIT}.d/gcp-project.conf"
fi

COLAB_PROJ_FILE="$KEYS_DIR/colab-project"
[ ! -s "$COLAB_PROJ_FILE" ] && COLAB_PROJ_FILE="$KEYS_DIR/gcp-project"
if [ -s "$COLAB_PROJ_FILE" ]; then
  COLAB_PROJ="$(tr -d '[:space:]' < "$COLAB_PROJ_FILE")"
  systemctl list-unit-files --type=service --no-legend 2>/dev/null | awk '{print $1}' | while read -r unit; do
    [ -z "$unit" ] && continue
    if [ "$unit" = "$STUDIO_UNIT" ]; then continue; fi
    exec_line="$(systemctl show -p ExecStart --value "$unit" 2>/dev/null || true)"
    if [[ "$exec_line" == *"autowake/controller.py"* ]]; then
      mkdir -p "/etc/systemd/system/${unit}.d"
      printf '[Service]\nEnvironment=H3_COLAB_PROJECT=%s\n' "$COLAB_PROJ" > "/etc/systemd/system/${unit}.d/colab-project.conf"
      echo "[install] 已为自动唤醒服务 $unit 注入项目配置"
    fi
  done
fi

# 2. Fetch latest repository and install deployer + systemd timer.
TMP_SRC="$(mktemp -d)"
trap 'rm -rf "$TMP_SRC"' EXIT
git clone --quiet --depth 1 "$REPO_URL" "$TMP_SRC"

install -m 0755 "$TMP_SRC/deploy/auto-deploy.sh" /usr/local/sbin/h3-studio-auto-deploy
install -m 0644 "$TMP_SRC/deploy/h3-studio-deploy.service" /etc/systemd/system/h3-studio-deploy.service
install -m 0644 "$TMP_SRC/deploy/h3-studio-deploy.timer" /etc/systemd/system/h3-studio-deploy.timer

systemctl daemon-reload
echo "[install] 执行首次无损同步部署..."
/usr/local/sbin/h3-studio-auto-deploy

systemctl enable --now h3-studio-deploy.timer
echo "[install] 完成！已开启每 60 秒自动跟踪 GitHub main 分支无损热更。"
