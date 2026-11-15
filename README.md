# 映室 · H3 视频工作台

输入创意并选择片长，由 Gemini 编排分镜，交给 H3 GPU 工作端渲染、拼接和回传。

这是从现有线上服务导出的可编辑源码快照；发布到 GitHub 不会自动更新线上服务。

## 从哪里修改

| 文件 | 用途 |
| --- | --- |
| `index.html`、`style.css`、`app.js` | 页面、时长选择、任务列表、分镜进度与播放 |
| `director.py`、`h3_guidance.py` | Gemini 调用、导演提示词、分镜输出与校验 |
| `server.py` | HTTP API、SQLite、任务状态、设备绑定 |
| `sampling.py` | 采样设置 |
| `colab-worker.py`、`h3_bf16.py` | GPU 渲染、精度、声音和拼接 |
| `shot_tasks.py`、`shot_relay.py`、`fleet.py` | 分镜分发、片段传输、工作端池 |
| `autowake/` | 可选的 Colab 自动唤醒控制器 |
| `route.conf`、`h3-studio.service` | 现有 Nginx 路由和 systemd 示例 |

修改工作端后，用 `python3 build_workers.py` 重新生成两个 Colab 笔记本。`autowake/build_worker.py` 用来更新自动唤醒工作端的派生脚本。

## 本地启动页面和后端

Web 后端使用 Python 标准库。GPU 工作端有自己的依赖安装步骤，见 Colab 笔记本；不要在普通开发机直接运行 `colab-worker.py`。

```bash
mkdir -p data/secrets
python3 - <<'PY'
from pathlib import Path
import os, secrets
for name in ('admin-key', 'worker-key'):
    path = Path('data/secrets') / name
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(secrets.token_urlsafe(32))
PY

STUDIO_DATA="$PWD/data" \
STUDIO_KEYS="$PWD/data/secrets" \
STUDIO_ORIGIN="http://127.0.0.1:18763" \
python3 server.py
```

打开 `http://127.0.0.1:18763/` 查看页面；`/api/health` 用于健康检查。生产会话 cookie 使用 `/h3-studio/` 路径，需要设备绑定等完整流程时，应按 `route.conf` 配置该路径的反向代理。

实际生成还需要：

- 在 `director.py` 中将 `YOUR_GOOGLE_CLOUD_PROJECT` 设置为自己的项目，并在 `STUDIO_KEYS/vertex-api-key` 中保存凭证。密钥只供后端读取。
- 按实际站点配置 `STUDIO_ORIGIN`、Nginx 和工作端中的 `BASE`。源码保留了原站点的公开域名，复用时请改为自己的域名。
- 使用具有足够显存、模型访问权限及配额的 GPU，运行 `colab-worker.ipynb`（量化）或 `colab-worker-bf16.ipynb`（原始 BF16），完成设备配对。
- 自动唤醒是可选部署组件，需要单独配置 `google-auth`、`colab_cli`、OAuth 登录及其运行环境；`autowake/colab_rest.py` 的项目占位符也需替换。仓库不包含这些登录数据。

没有附带数据库、用户任务、视频、模型权重、API 密钥、OAuth token 或私有部署脚本。不要将这些文件提交到仓库。

## 验证

以下测试使用临时 SQLite，不分配 GPU、不调用付费模型：

```bash
python3 validate_pool.py
python3 validate_scale.py
node --check app.js
# 片段中转测试需要提供一段很小的测试 MP4：
python3 validate_relay.py /path/to/small-test.mp4
```

## 来源

源码快照取自 2026-11-15 检查时正在运行的 release `sampling-b0f687dbe207`。公开副本只将两个 Google Cloud 项目 ID 改为占位符，新增本说明和忽略规则；没有改变线上部署。

`vendor/minimax-h3` 是已有的官方提示词资料快照，来源和固定提交见 [UPSTREAM.md](vendor/minimax-h3/UPSTREAM.md)。第三方资料、模型和依赖仍适用各自许可。本次公开源码未另行授予开源许可证。
