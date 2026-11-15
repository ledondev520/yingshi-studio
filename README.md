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

- 在 `/etc/h3-studio/gcp-project`（或环境变量 `H3_GCP_PROJECT`）中配置 Vertex 项目 ID，并在 `STUDIO_KEYS/vertex-api-key` 中保存凭证（兼容直接修改 `director.py`）。密钥只供后端读取。
- 按实际站点配置 `STUDIO_ORIGIN`、Nginx 和工作端中的 `BASE`。源码保留了原站点的公开域名，复用时请改为自己的域名。
- 使用具有足够显存、模型访问权限及配额的 GPU，运行 `colab-worker.ipynb`（量化）或 `colab-worker-bf16.ipynb`（原始 BF16），完成设备配对。
- 自动唤醒是可选部署组件，需要单独配置 `google-auth`、`colab_cli`、OAuth 登录及其运行环境；Colab 配额项目 ID 可保存在 `/etc/h3-studio/colab-project`（或 `H3_COLAB_PROJECT`）。仓库不包含这些登录数据。
- 若需让线上服务器自动跟踪 `main` 分支无损热更，可在服务器上运行一次 `sudo bash deploy/install.sh`：脚本会自动保留现有 GCP 项目配置、在切换 `/opt/h3-studio/current` 软链前执行本地校验并等待编排中任务完成，若健康检查未通过则自动回滚。

没有附带数据库、用户任务、视频、模型权重、API 密钥、OAuth token 或私有部署脚本。不要将这些文件提交到仓库。

## 验证

以下测试使用临时 SQLite，不分配 GPU、不调用付费模型：

```bash
python3 validate_previews.py # 需要本机 ffmpeg / ffprobe（libx264、AAC）
python3 validate_pool.py
python3 validate_scale.py
python3 validate_multiuser.py
python3 validate_worker_loop.py
node --check app.js
# 片段中转测试需要提供一段很小的测试 MP4：
python3 validate_relay.py /path/to/small-test.mp4
```

## 来源

仓库的起始快照来自 release `sampling-b0f687dbe207`；后续扩容、续跑与连续接单修复见 Git 历史。两个 Google Cloud 项目 ID 在公开源码中使用占位符，部署时须保留自己的真实配置。

`vendor/minimax-h3` 是已有的官方提示词资料快照，来源和固定提交见 [UPSTREAM.md](vendor/minimax-h3/UPSTREAM.md)。第三方资料、模型和依赖仍适用各自许可。本次公开源码未另行授予开源许可证。

## 断点恢复

每个完成镜头在进入下一镜前上传到 VPS 私有 `shot-checkpoints/`，以任务渲染计划和 SHA-256 校验。换卡或租约过期后先恢复已完成镜头，再生成缺失部分；中转票据清理不再删除持久片段。修改分镜、精度或画质会使旧计划的片段失效。仍未完成的采样步不会跨机器恢复。

自动工作端在 Colab 内核中运行实际任务，避免启动器提前返回导致内核空闲。运行时仍可能被平台回收，故片段备份是必要保障。`validate_resume.py` 验证失联、清理、跨租约恢复及只渲染缺失镜头；`validate_launch.py` 验证启动器不会早于实际工作返回。

## 多人并发容量

自动 GPU 池最多 32 个槽位，依次申请 A100、H100、G4，不回退到 L4/T4。32 是软件上限，不是 Google 对可用卡数的承诺。只按需求补卡，不接管其他会话；队列清空后，闲置约 10 分钟释放自动申请的卡。

全站最多 320 个编排中、排队或运行任务；每位访客最多 10 个未完成任务；同一 IP 每小时最多 600 次新提交，支持多人共用网络。空闲执行端优先领取匹配的新视频，没有匹配的新视频时才协助独立镜头；同片仍最多一主五副。

显卡租约失效后保留已完成片段并重新排队，不因累计三次断线直接作废；模型实际报错仍按失败处理。没有每日算力预算或任务完成次数限制，有需求就持续使用已获批算力；库存、账号额度或付费单元不足时任务保存并重试。`validate_multiuser.py` 验证十用户、32 路领取、身份隔离、机型准入、断线重排及队列边界；`autowake/check.py` 以模拟云端验证 32 个申请槽，不消耗算力。

短暂断网、429 和服务端暂时错误时，主卡带原租约重连，副卡保留原分镜票据；无需等待租约超时即可续跑，完成后继续领取下一条。实际渲染错误仍标为失败，不做无休止重算。

## 在线预览与高清下载

原片上传并校验后即保存，下载和续拍始终读取原片。Web 服务另用 FFmpeg 在 CPU 上串行生成轻量预览，不重新调用模型；启动后也会补齐已有成片。需安装 `ffmpeg` / `ffprobe` 并启用 libx264、AAC 编码器。

预览按原画幅缩至 1280×720 或 720×1280 以内（不放大），使用 H.264 CRF 26、最高目标码率 2.5 Mbps、AAC 96 kbps、每 2 秒关键帧和 faststart；压缩结果更大时保留原文件大小。限制两条编码线程、降低进程优先级，单次超时 15 分钟，失败至少等 5 分钟再试。预览临时文件验证后原子发布，原片从不覆盖；服务重启可继续补齐。

`/api/preview/<id>` 与原片接口共享鉴权、Range 和 HEAD；仅私有缓存且每次复验权限。准备中或失败会显示状态，并始终提供高清原片下载。`validate_previews.py` 验证真实转码、新上传与旧片补齐、失败重试、原片完整性和权限隔离。
