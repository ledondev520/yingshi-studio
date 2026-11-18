# 映室 · H3 视频工作台

输入创意并选择片长，由 Gemini 编排分镜，交给 H3 GPU 工作端渲染、拼接和回传。

生产服务器通过既有部署定时器跟踪 GitHub `main`；合并后执行校验、切换版本并检查健康状态。

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

## Colab MCP 自动启动

提交图片或视频任务后，现有自动调度器通过 Google 官方 `https://colaboratory.googleapis.com/mcp` 调用 `list_runtimes`、`get_runtime`、`create_runtime` 和 `delete_runtime`，复用服务器私有 OAuth 登录及配额项目。无需每次打开 Colab 网页，也无需新增 API 密钥；GPU 型号、精度、模型、队列和回传流程沿用现有设置。

MCP 返回 JSON 或 SSE；失败时使用原 REST 请求恢复，创建保持相同 `runtimeId`、`requestId` 和规格，避免重复分配，并保留 Google 的额度错误原因。长时间工作仍通过现有 CLI/Jupyter 执行连接运行，持续连接恢复和断点续跑不变。MCP 不提供额外 GPU 配额，不保证库存，也不消除模型下载和加载时间。`python3 validate_colab_mcp.py` 验证协议、运行时复用、创建、释放及失败恢复，不分配真实 GPU。

## 断点恢复

每个完成镜头在进入下一镜前上传到 VPS 私有 `shot-checkpoints/`，以任务渲染计划和 SHA-256 校验。换卡或租约过期后先恢复已完成镜头，再生成缺失部分；中转票据清理不再删除持久片段。修改分镜、精度或画质会使旧计划的片段失效。仍未完成的采样步不会跨机器恢复。

自动工作端在 Colab 内核中运行实际任务，避免启动器提前返回导致内核空闲。运行时仍可能被平台回收，故片段备份是必要保障。`validate_resume.py` 验证失联、清理、跨租约恢复及只渲染缺失镜头；`validate_launch.py` 验证启动器不会早于实际工作返回。

约 100 秒未收到工作端心跳后，自动调度会检查原运行时。Colab 确认 404 时立即撤销旧租约、将受影响任务重新排队并按需求补卡，不再等满十分钟；副卡未完成的镜头也会重新开放领取。网络超时和临时服务错误继续重连，不视为运行时被回收。运行时仍在但工作端停止时会尝试重新启动，忙碌内核不强行打断。页面显示自动恢复及换卡准备状态；已完成镜头继续复用，未完成采样仍需重算。

## 多人并发容量

自动 GPU 池最多 32 个槽位，依次申请 A100、G4、H100，不回退到 L4/T4。32 是软件上限，不是 Google 对可用卡数的承诺。只按需求补卡，不接管其他会话；队列清空后，闲置约 10 分钟释放自动申请的卡。

全站最多 320 个编排中、排队或运行任务；每位访客最多 10 个未完成任务；同一 IP 每小时最多 600 次新提交，支持多人共用网络。空闲执行端优先领取匹配的新视频，没有匹配的新视频时才协助独立镜头；同片仍最多一主五副。

显卡租约失效后保留已完成片段并重新排队，不因累计三次断线直接作废；模型实际报错仍按失败处理。没有每日算力预算或任务完成次数限制，有需求就持续使用已获批算力；库存、账号额度或付费单元不足时任务保存并重试。`validate_multiuser.py` 验证十用户、32 路领取、身份隔离、机型准入、断线重排及队列边界；`autowake/check.py` 以模拟云端验证 32 个申请槽，不消耗算力。

短暂断网、429 和服务端暂时错误时，主卡带原租约重连，副卡保留原分镜票据；无需等待租约超时即可续跑，完成后继续领取下一条。实际渲染错误仍标为失败，不做无休止重算。

## 在线预览与高清下载

原片上传并校验后即保存，下载和续拍始终读取原片。Web 服务另用 FFmpeg 在 CPU 上串行生成轻量预览，不重新调用模型；启动后也会补齐已有成片。需安装 `ffmpeg` / `ffprobe` 并启用 libx264、AAC 编码器。

预览按原画幅缩至 1280×720 或 720×1280 以内（不放大），使用 H.264 CRF 26、最高目标码率 2.5 Mbps、AAC 96 kbps、每 2 秒关键帧和 faststart；压缩结果更大时保留原文件大小。限制两条编码线程、降低进程优先级，单次超时 15 分钟，失败至少等 5 分钟再试。预览临时文件验证后原子发布，原片从不覆盖；服务重启可继续补齐。

`/api/preview/<id>` 与原片接口共享鉴权、Range 和 HEAD；仅私有缓存且每次复验权限。准备中或失败会显示状态，并始终提供高清原片下载。`validate_previews.py` 验证真实转码、新上传与旧片补齐、失败重试、原片完整性和权限隔离。

## 图片创作
创作台可切换视频和图片。图片通过 Gemini 4 Hydrogen（LOW） 扩写中文画面设计及英文 H3-Image 提示词，保留原语言可见文字；原有视频导演仍使用 Argon。图片只使用 MiniMax-H3-Image 原始 BF16，不加载量化版本。默认横图 4:3（2048×1536），另有竖图 3:4（1536×2048）及方幅 1:1（2048×2048）；旧任务的 5:3/3:5 尺寸继续兼容，官方六步采样。
图片以 `image-bf16` 独立执行模式进入现有队列，视频工作端不会领取图片。自动调度只申请 A100/H100，并由执行端验证 80GB 显存及 130GiB 可用主机 RAM；不回退 G4 或小显存卡。`colab-image-worker.py` 由既有私有调度身份启动；也可在已挂载 Drive、已有 studio-device.json 配对的空闲运行时启动，直接复用原版缓存与 Drive 输出。首次安装与约 135GB 权重准备耗时较长；同一进程连续接单时复用管线及本机缓存，运行时被回收后需重新准备。
生成 PNG 保存至 Drive，并通过现有认证上传保留 VPS 私有副本。图片预览、下载和追踪链接沿用任务所有者/访客隔离。验证：`python3 validate_images.py`；视频回归沿用 validate_multiuser、validate_pool 与 validate_worker_loop。部署同时更新服务端、前端、图片执行脚本及 autowake/controller.py，并重启两个服务；数据库仅新增可空的 aspect 列，历史任务保留。

## 冷启动与模型缓存

图片和视频分别显示执行端实测的启动环境、下载权重、复用本机权重与加载模型阶段。准备回报不算可接单心跳，不会提前领取任务。权重进度按本机已完整写入且大小符合清单的文件统计，显示已就绪 GB、总 GB、文件数及本次复用量；临时下载文件不计入，文件完成前数字可能保持不变。环境启动和模型加载不显示推算百分比。超过 90 秒没有准备回报时明确标注最后一次实测，避免将停滞包装成持续加载。

同一运行时连续接单复用本机权重；图片工作进程还复用已加载管线。重启进程但保留本机文件时跳过完整文件下载，重新加载模型。Colab 运行时被回收后，其临时磁盘缓存可能消失，新机器仍需要传输权重。当前图片自动工作端从固定模型源恢复约 135GB 权重，尚未接入 Drive 权重后备；量化视频约 40GB 权重先使用快速模型源，源不可用时再从私有 Drive 缓存恢复。Drive 备份不会自动成为新 GPU 的本机缓存。

GPU 准备从分镜编排阶段开始，空闲执行端优先复用；视频申请顺序为 A100 → G4 → H100，图片保留 A100 → H100。空闲十分钟回收保留，不设置全天占卡，也不以循环计算模拟使用。页面分别显示在线、已分配准备中、申请中和 Google 活动运行时限制。

当前量化视频的四个固定权重文件约 40GB，以原仓库 revision 的 SHA-256 校验后保存至私有 Google Drive。缓存索引保存在 `/var/lib/h3-autowake/model-cache-drive.json`，只有 size、sha256 和 Drive 校验结果吻合的文件才发布。Google 凭据留在服务器，已配对执行端通过认证接口流式读取；服务器不落地整套模型。换卡仍需要网络传输和加载，持久缓存不等于零冷启动。

自动工作端先复用完整的本机文件；新机器采用两路并行直连固定版本的模型源，避免把大模型经过 VPS 中转而拖慢启动。原始来源不可用时读取私有持久缓存，支持断点续传，校验完整后原子替换本机文件。当前 BF16 与 Turbo LoRA 仍使用既有下载路径，未宣称其已被缓存。公网无匿名缓存下载入口，也不公开云盘分享权限。

`python3 validate_model_cache.py` 验证鉴权、断点恢复、SHA-256、损坏保护与未发布缓存回退。

图片和视频由两个独立编排循环处理，各自按提交顺序取任务，互不等待对方的模型输出；同类型仍顺序编排。两者均通过 SSE 接收模型输出、持续保存部分文本，前端沿用约 650ms 的编排进度读取；图片先展示中文画面设计，再逐步展开英文生图提示词。只有完整输出通过校验后才进入 GPU 队列。验证：`python3 validate_planning.py`。
