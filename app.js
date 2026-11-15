'use strict';

const $ = id => document.getElementById(id);
const tx = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text !== undefined && text !== null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};

let role = 'guest';
let parentId = '';
let duration = 15;
let jobs = [];
let jobTotals = null;
const PAGE_SIZE = 8;
let workKind = 'video';
let workKindInitialized = false;
const filters = { video: 'all', image: 'all' };
const pages = { video: 1, image: 1 };
let focusJobId = '';
let version = 0;
let timer = null;
let msgTimer = null;
let saveChain = Promise.resolve();
let logged = false;
let requestId = null;
let activeJob = '';
let watchToken = 0;
const modelMode = 'quantized';
const ALLOWED_SECONDS = [15, 30, 45, 60, 75];
let creationKind = 'video';
let imageAspect = 'landscape';
let onlineModes = null;
let serverClockOffset = 0;
let previewJob = '';
let stopPlaybackCheck = () => {};
let copiedJobId = '';

function snapDuration(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return 15;
  return ALLOWED_SECONDS.reduce(
    (best, cur) => (Math.abs(cur - n) < Math.abs(best - n) ? cur : best),
    ALLOWED_SECONDS[0]
  );
}

const openShotJobs = new Set();
const openPromptJobs = new Set();
const openFailedSections = new Set();
const rerunningJobs = new Set();
let jobsMsgTimer = null;
let boardJob = null;
let boardJobId = '';
let boardManual = null; // null | 'open' | 'closed'

const states = {
  planning: '创作中',
  local: '本机方案',
  queued: '排队中',
  running: '生成中',
  done: '已完成',
  failed: '未完成',
  cancelled: '已取消'
};

const aspectLabels = {
  landscape: '横图',
  portrait: '竖图',
  square: '方幅'
};

async function api(p, body) {
  const res = await fetch('api/' + p, {
    credentials: 'same-origin',
    cache: 'no-store',
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || '请求失败');
  return data;
}

function localRead(k, fallback) {
  try {
    return JSON.parse(localStorage.getItem('frame-room:' + k)) ?? fallback;
  } catch {
    return fallback;
  }
}

function localWrite(k, v) {
  localStorage.setItem('frame-room:' + k, JSON.stringify(v));
}

function rememberJobs(list) {
  if (!Array.isArray(list) || !list.length) return;
  const map = new Map(localRead('tokens', []).filter(x => x?.id && x?.token).map(x => [x.id, x.token]));
  for (const j of list) {
    if (j?.id && j?.watch_token) map.set(j.id, j.watch_token);
  }
  localWrite('tokens', [...map.entries()].slice(-24).map(([id, token]) => ({ id, token })));
}

function jobToken(id) {
  return localRead('tokens', []).find(x => x?.id === id)?.token || '';
}

function forgetJobToken(id) {
  localWrite('tokens', localRead('tokens', []).filter(x => x?.id && x.id !== id));
}

function isMobileViewport() {
  return window.matchMedia('(max-width: 760px)').matches;
}

function shortGpuName(raw) {
  const s = String(raw || '').trim();
  if (!s) return 'GPU';
  if (/blackwell|6000/i.test(s)) return 'RTX 6000';
  const m = /\b(H100|A100|G4)\b/i.exec(s);
  if (m) return m[1].toUpperCase();
  return 'GPU';
}

function paintWorkerStatus(w) {
  const n = Number(w?.online_count) || 0;
  const run = Number(w?.running_jobs) || 0;
  const q = Number(w?.queued_jobs) || 0;
  const loading = Number(w?.loading_count) || 0;
  const requesting = Number(w?.requesting_count) || 0;
  const limit = Boolean(w?.runtime_limit);
  const on = Boolean(w?.online || n > 0);

  $('gpuDot').dataset.online = on ? '1' : '0';
  if (on) {
    $('gpu').textContent = isMobileViewport()
      ? (n > 1 ? `${n} 卡在线` : 'GPU 在线')
      : (n > 1 ? `${n} 张 GPU 在线` : `${shortGpuName(w?.gpu)} 在线`);
  } else if (loading > 0) {
    $('gpu').textContent = 'GPU 准备中';
  } else if (requesting > 0) {
    $('gpu').textContent = '申请 GPU 中';
  } else if (limit) {
    $('gpu').textContent = '等待空闲卡';
  } else {
    $('gpu').textContent = '按需唤醒';
  }

  const tag = $('workerSeen');
  tag.dataset.online = on ? '1' : '0';
  if (on) {
    if (run > 0) tag.textContent = n > 1 ? `${n} 卡 · ${run} 生成中` : `${run} 个生成中`;
    else if (loading > 0) tag.textContent = `${n} 卡在线 · 新卡准备中`;
    else if (q > 0) tag.textContent = 'GPU 即将接单';
    else tag.textContent = n > 1 ? `${n} 卡就绪` : 'GPU 就绪';
  } else if (loading > 0) {
    tag.textContent = loading > 1 ? `${loading} 卡准备中` : 'GPU 准备中';
  } else if (requesting > 0) {
    tag.textContent = '申请 GPU 中';
  } else if (limit) {
    tag.textContent = '等待空闲卡';
  } else {
    tag.textContent = q > 0 ? '唤醒 GPU 中' : '按需唤醒';
  }
}

function showRole(value) {
  role = value;
  logged = true;
}

function paintMode() {
  const image = creationKind === 'image';
  $('promptMeta').textContent = image ? '主体 · 构图 · 光线 · 风格' : '画面 · 动作 · 声音 · 对白';
  $('videoDuration').hidden = image;
  $('imageCanvas').hidden = !image;

  for (const el of document.querySelectorAll('[name=creationKind]')) {
    el.checked = el.value === creationKind;
  }
  for (const el of document.querySelectorAll('[name=imageAspect]')) {
    el.checked = el.value === imageAspect;
  }

  const hint = $('qualityHint');
  if (hint) {
    const wake = !image && onlineModes && !onlineModes.includes('quantized') ? ' 提交后自动唤醒 GPU。' : '';
    hint.textContent = image
      ? '写下简短画面想法即可，系统会自动补全构图与光影细节。'
      : '量化加速 · 默认 768p。可写明「竖屏」「快速预览」或「保留原声」。' + wake;
  }
}

function durations() {
  duration = snapDuration(duration);
  $('duration').value = duration;
  $('durationText').textContent = duration + ' 秒';
  $('duration').setAttribute('aria-valuetext', duration + ' 秒');
  for (const btn of $('durationStops').children) {
    btn.setAttribute('aria-pressed', String(Number(btn.dataset.sec) === duration));
  }
}

function syncComposerMeta() {
  const len = $('prompt').value.length;
  $('count').textContent = len + ' / 4000';
  $('clearPrompt').hidden = !len && !parentId;
}

function showFormMsg(text, autoHideMs = 0) {
  clearTimeout(msgTimer);
  $('workMsg').textContent = text || '';
  if (text && autoHideMs > 0) {
    msgTimer = setTimeout(() => {
      if ($('workMsg').textContent === text) $('workMsg').textContent = '';
    }, autoHideMs);
  }
}

function showJobsMsg(text, autoHideMs = 4500) {
  const el = $('jobsMsg');
  if (!el) return;
  clearTimeout(jobsMsgTimer);
  el.textContent = text || '';
  if (text && autoHideMs > 0) {
    jobsMsgTimer = setTimeout(() => {
      if (el.textContent === text) el.textContent = '';
    }, autoHideMs);
  }
}

function renderShotRows(container, list, isImage = false, writing = false) {
  container.replaceChildren();
  for (const [i, shot] of list.entries()) {
    const n = tx('div', '', 'shot');
    const box = tx('div', '');
    if (!isImage) {
      box.append(tx('label', '分镜 ' + (i + 1) + (shot.seconds ? ' · ' + shot.seconds + ' 秒' : '')));
    }
    box.append(tx('p', shot.prompt));
    if (isImage && shot.h3_prompt) {
      const details = document.createElement('details');
      details.open = writing;
      details.append(tx('summary', writing ? '正在完善' : '完整描述'), tx('p', shot.h3_prompt));
      box.append(details);
    }
    n.append(tx('span', String(i + 1).padStart(2, '0')), box);
    container.append(n);
  }
}

function renderShots(j = boardJob) {
  if (j !== undefined) {
    if ((j?.id || '') !== boardJobId) {
      boardJobId = j?.id || '';
      boardManual = null;
    }
    boardJob = j;
  }
  const cur = boardJob;
  const card = $('boardCard');
  const c = $('shots');
  const toggle = $('toggleShots');
  const view = cur?.shots || [];
  const writing = cur?.state === 'planning';
  const failed = cur?.state === 'failed';
  const isImage = cur ? cur.kind === 'image' : creationKind === 'image';
  const empty = !writing && !view.length && !failed;

  $('boardHeading').textContent = isImage ? '画面设计' : '分镜脚本';
  card.dataset.empty = empty ? '1' : '0';

  $('shotLabel').textContent = writing
    ? '创作中'
    : view.length
      ? (isImage ? '描述已就绪' : `${view.length} 镜 · ${cur.seconds} 秒`)
      : failed
        ? '创作中断'
        : '等待创意';

  const hint = $('shotHint');
  if (failed) {
    hint.hidden = false;
    hint.textContent = cur.message || '编排失败，请重试。';
  } else if (writing) {
    hint.hidden = false;
    hint.textContent = isImage
      ? '正在扩写构图、光线、材质与细节…'
      : (view.length ? `已写出 ${view.length} 段分镜，正在继续丰富细节…` : '导演正在拆解并丰富分镜…');
  } else if (view.length) {
    hint.hidden = true;
    hint.textContent = '';
  } else {
    hint.hidden = false;
    hint.textContent = isImage
      ? '提交简短创意，系统会补全完整画面描述。'
      : '提交后，分镜会在这里逐段出现。';
  }

  const mobile = isMobileViewport();
  const canCollapse = !writing && (mobile ? view.length > 0 : view.length > 3);
  const defaultCollapsed = canCollapse && mobile;
  const collapsed = canCollapse
    ? (boardManual ? boardManual === 'closed' : defaultCollapsed)
    : false;

  card.dataset.collapsed = collapsed ? '1' : '0';
  toggle.hidden = !canCollapse;
  if (canCollapse) {
    toggle.textContent = collapsed
      ? (isImage ? '展开描述' : `展开 ${view.length} 镜`)
      : '收起';
  }

  renderShotRows(c, view, isImage, writing);
}

$('toggleShots').onclick = () => {
  const isClosed = $('boardCard').dataset.collapsed === '1';
  boardManual = isClosed ? 'open' : 'closed';
  renderShots();
};

async function watchJob(j) {
  const ticket = ++watchToken;
  activeJob = j.id;
  rememberJobs([j]);
  renderShots(j);
  while (['planning', 'queued', 'running'].includes(j.state) && ticket === watchToken) {
    await new Promise(r => setTimeout(r, j.state === 'planning' ? 650 : 4000));
    if (ticket !== watchToken) return;
    const tok = j.watch_token || jobToken(j.id);
    try {
      j = (await api('creation/' + encodeURIComponent(j.id) + (tok ? '?token=' + encodeURIComponent(tok) : ''))).job;
      rememberJobs([j]);
    } catch {
      continue;
    }
    if (ticket !== watchToken) return;
    const idx = jobs.findIndex(x => x.id === j.id);
    if (idx >= 0) {
      jobs[idx] = j;
      renderJobs();
    }
    renderShots(j);
  }
  if (ticket === watchToken) await refresh();
}

function save(doneLabel = '草稿已保存') {
  if (!logged) return Promise.resolve();
  const v = version;
  const data = {
    kind: creationKind,
    aspect: imageAspect,
    precision: modelMode,
    parent: parentId,
    prompt: $('prompt').value,
    seconds: snapDuration(duration),
    shots: []
  };
  saveChain = saveChain.catch(() => {}).then(async () => {
    if (v < version) return;
    try {
      if (role === 'owner') await api('draft', data);
      else localWrite('draft', data);
      if (v === version) $('draftStatus').textContent = doneLabel;
    } catch {
      if (v === version) $('draftStatus').textContent = '保存失败 · 请检查网络';
    }
  });
  return saveChain;
}

function changed() {
  requestId = null;
  version++;
  syncComposerMeta();
  $('draftStatus').textContent = '正在保存…';
  clearTimeout(timer);
  timer = setTimeout(() => save('草稿已保存'), 550);
}

function clearComposer(statusLabel = '已清空', focusInput = true) {
  requestId = null;
  version++;
  clearTimeout(timer);
  $('prompt').value = '';
  parentId = '';
  paintReply();
  syncComposerMeta();
  showFormMsg('');
  $('draftStatus').textContent = statusLabel;
  save(statusLabel);
  if (focusInput) $('prompt').focus();
}

function paintPrompt(d) {
  creationKind = d?.kind === 'image' ? 'image' : 'video';
  imageAspect = ['square', 'landscape', 'portrait'].includes(d?.aspect) ? d.aspect : 'landscape';
  paintMode();
  parentId = creationKind === 'image' ? '' : (d?.parent || '');
  paintReply();
  $('prompt').value = d?.brief || d?.prompt || '';
  duration = snapDuration(d?.seconds);
  syncComposerMeta();
  durations();
  if (d?.shots?.length) renderShots(d);
}

function detailDate(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return '';
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZone: 'Asia/Shanghai'
  }).format(new Date(n * 1000));
}

function fmtEta(sec) {
  if (sec >= 60) return '约 ' + Math.round(sec / 60) + ' 分钟';
  return '约 ' + Math.max(1, Math.round(sec)) + ' 秒';
}

function conciseQueueMsg(j) {
  const msg = String(j.message || '');
  const prefix = j.recovering ? 'GPU 断线自动恢复 · ' : '';
  const pos = j.queue_position ? `第 ${j.queue_position} 位 · ` : '';
  if (msg.includes('即将接单')) return prefix + pos + 'GPU 就绪，即将开始';
  if (msg.includes('运行时已达上限')) {
    return prefix + pos + '账号 3 卡名额已满（自动腾退闲置卡中；若浏览器开有手动 Colab 页可关闭释放）';
  }
  if (msg.includes('准备模型') || msg.includes('加载模型')) return prefix + pos + '正在加载模型，稍后自动开始';
  if (msg.includes('前序任务') || msg.includes('在线渲染')) return prefix + pos + '前序任务生成中，空闲后自动接单';
  if (msg.includes('额度受限')) return prefix + pos + '云端算力繁忙，已保存并自动重试';
  if (msg.includes('申请')) return prefix + pos + '正在唤醒匹配的云端 GPU';
  if (msg.includes('断线')) return 'GPU 断线，已保留完成片段并自动重排';
  return prefix + (pos ? `${pos}等待空闲 GPU` : '已就绪，等待空闲 GPU');
}

function conciseRunStage(j) {
  const s = String(j.message || '').trim();
  if (j.recovering) return '任务回报中断，正在检查并自动恢复…';
  if (j.kind === 'image') {
    if (j.progress >= 92) return '正在保存图片…';
    if (s.includes('准备 GPU') || s.includes('加载')) return '正在准备生图环境…';
    if (s.includes('领取任务')) return 'GPU 已接单，即将开始生图…';
    return '正在生成图片…';
  }
  const m = /^生成第 (\d+)\/(\d+) 镜$/.exec(s);
  if (m) return `第 ${m[1]}/${m[2]} 镜渲染中…`;
  if (s.includes('准备 GPU')) return '正在准备渲染环境…';
  if (s.includes('领取任务')) return 'GPU 已接单，即将开始…';
  if (s.includes('拼接视频')) return '正在合成音画与配乐…';
  if (s.includes('独立分镜')) return '等待并行分镜汇总…';
  return s || '正在生成…';
}

function preparationDetail(list, container) {
  const labels = { environment: '启动环境', download: '下载权重', copying: '传入本机缓存', cache: '复用本机权重', loading: '加载模型', failed: '准备失败，等待自动重试' };
  const gb = n => (n / 1e9).toFixed(1) + ' GB';
  const now = Math.floor(Date.now() / 1000) + serverClockOffset;
  for (const p of list || []) {
    const kind = p.mode === 'image-bf16' ? '图片' : p.mode === 'bf16' ? '视频 · 原始 BF16' : '视频 · 量化版';
    const box = tx('div', '', 'jobLive preparation');
    const title = kind + ' GPU · ' + (labels[p.stage] || '等待执行端回报');
    const top = tx('div', '', 'samplerTop');
    top.append(tx('b', title));
    box.append(top);
    if (p.bytes_total > 0) {
      const bar = document.createElement('progress');
      bar.max = p.bytes_total;
      bar.value = p.bytes_done;
      bar.setAttribute('aria-label', title + '，按完整文件统计');
      box.append(bar, tx('p', `权重已就绪 ${gb(p.bytes_done)} / ${gb(p.bytes_total)} · ${p.files_done} / ${p.files_total} 个文件`, 'statusLine'));
      if (p.reused_bytes > 0) box.append(tx('p', `其中 ${gb(p.reused_bytes)} 已复用本机缓存`, 'statusLine'));
      box.append(tx('p', '按完整文件更新，不含尚未完成的文件', 'statusLine'));
    } else {
      const detail = p.stage === 'failed' ? '任务已保留，后台会重新尝试。' : p.stage === 'loading'
        ? '正在将模型载入内存和显存，完成后自动生成。'
        : '正在安装依赖或启动执行环境，完成后自动进入下一阶段。';
      box.append(tx('p', detail, 'statusLine'));
    }
    const age = Math.max(0, Math.floor(now - p.seen));
    const elapsed = age < 60 ? `${age} 秒` : `${Math.floor(age / 60)} 分 ${age % 60} 秒`;
    box.append(tx('p', (p.stale ? '准备进度回报已中断，以上为最后一次实测 · ' : '最近回报：') + elapsed + '前', 'statusLine' + (p.stale ? ' statusWarn' : '')));
    container.append(box);
  }
}

function buildLiveBox(j) {
  const isImage = j.kind === 'image';
  if (j.state === 'planning') {
    const box = tx('div', '', 'jobLive');
    const n = j.shots?.length || 0;
    box.append(tx(
      'p',
      isImage
        ? '正在扩写构图、光线与画面细节…'
        : (n ? `正在编排分镜（已写出 ${n} 段）…` : '已收到创意，导演正在编排分镜…'),
      'statusLine'
    ));
    return box;
  }

  if (j.state === 'queued') {
    const box = tx('div', '', 'jobLive');
    box.append(tx('p', conciseQueueMsg(j), 'statusLine' + (j.recovering ? ' statusWarn' : '')));
    return box;
  }

  if (j.state === 'running') {
    const box = tx('div', '', 'jobLive');
    const now = Math.floor(Date.now() / 1000) + serverClockOffset;
    const age = Math.max(0, now - j.updated);
    const samples = j.sampling || [];
    const totalShots = Math.max(1, j.shots?.length || 1);

    if (samples.length) {
      const shown = samples.slice(0, 2);
      for (const s of shown) {
        const stale = now - s.seen > 80;
        const leftText = isImage
          ? `画面采样 · ${s.done}/${s.total} 步`
          : `第 ${s.shot}/${totalShots} 镜 · ${s.done}/${s.total} 步`;
        const rightText = stale
          ? '等待同步'
          : s.done === s.total
            ? '画面解码中…'
            : s.sec_per_step
              ? '剩余' + fmtEta((s.total - s.done) * s.sec_per_step)
              : '测速中…';

        const wrap = tx('div', '', 'sampler');
        const top = tx('div', '', 'samplerTop');
        top.append(tx('b', leftText), tx('span', rightText));
        const bar = document.createElement('progress');
        bar.max = s.total;
        bar.value = s.done;
        bar.setAttribute('aria-label', leftText);
        wrap.append(top, bar);
        box.append(wrap);
      }
      if (samples.length > 2) {
        box.append(tx('p', `另有 ${samples.length - 2} 个镜头正在并行加速`, 'statusLine'));
      }
    } else {
      const pct = Math.max(0, Math.min(99, Number(j.progress) || 0));
      const stage = conciseRunStage(j);
      const wrap = tx('div', '', 'sampler');
      const top = tx('div', '', 'samplerTop');
      top.append(tx('b', stage), tx('span', pct > 0 ? pct + '%' : ''));
      const bar = document.createElement('progress');
      bar.max = 100;
      bar.value = Math.max(5, pct);
      bar.setAttribute('aria-label', stage);
      wrap.append(top, bar);
      box.append(wrap);
    }

    if (j.recovering || age > 100) {
      box.append(tx('p', '任务回报中断，正在检查 GPU；确认失联后自动重新调度', 'statusLine statusWarn'));
    }
    return box;
  }

  if (j.state === 'done' && !j.media_ready) {
    const box = tx('div', '', 'jobLive');
    box.append(tx('p', '作品已生成，正在回传同步…', 'statusLine'));
    return box;
  }

  if (j.state === 'failed' && j.message) {
    const box = tx('div', '', 'jobLive');
    box.append(tx('p', j.message, 'statusLine statusErr'));
    return box;
  }

  return null;
}

function jobKind(j) {
  return j?.kind === 'image' || j?.precision === 'image-bf16' ? 'image' : 'video';
}

function isActiveState(state) {
  return ['planning', 'queued', 'running'].includes(state);
}

function isFailedState(state) {
  return state === 'failed' || state === 'cancelled';
}

function setWorkKind(next, persist = true) {
  workKind = next === 'image' ? 'image' : 'video';
  workKindInitialized = true;
  if (persist) localWrite('work-kind', workKind);
}

function updateWorkSwitch(videoJobs, imageJobs) {
  const vFailed = videoJobs.filter(j => isFailedState(j.state)).length;
  const iFailed = imageJobs.filter(j => isFailedState(j.state)).length;
  const vValid = videoJobs.length - vFailed;
  const iValid = imageJobs.length - iFailed;
  const vTotal = Math.max(vValid, (Number(jobTotals?.video) || 0) - vFailed);
  const iTotal = Math.max(iValid, (Number(jobTotals?.image) || 0) - iFailed);
  const vActive = videoJobs.filter(j => isActiveState(j.state)).length;
  const iActive = imageJobs.filter(j => isActiveState(j.state)).length;

  $('jobCount').textContent = `视频 ${vTotal} · 图片 ${iTotal}`;
  $('videoKindCount').textContent = `${vTotal} 个`;
  $('imageKindCount').textContent = `${iTotal} 张`;

  const vBadge = $('videoActiveBadge');
  vBadge.hidden = !vActive;
  vBadge.textContent = vActive ? `${vActive} 进行中` : '';

  const iBadge = $('imageActiveBadge');
  iBadge.hidden = !iActive;
  iBadge.textContent = iActive ? `${iActive} 进行中` : '';

  $('kindVideoTab').setAttribute('aria-selected', String(workKind === 'video'));
  $('kindImageTab').setAttribute('aria-selected', String(workKind === 'image'));

  const lib = $('workLibrary');
  lib.dataset.kind = workKind;
  lib.setAttribute('aria-labelledby', workKind === 'image' ? 'kindImageTab' : 'kindVideoTab');
  $('libraryLabel').textContent = workKind === 'image' ? '图片列表' : '视频列表';
}

function updateFilterTabs(kindJobs) {
  const curFilter = filters[workKind] || 'all';
  const totalCount = kindJobs.length;
  const activeCount = kindJobs.filter(j => isActiveState(j.state)).length;
  const doneCount = kindJobs.filter(j => j.state === 'done').length;
  for (const btn of $('tabs').children) {
    const f = btn.dataset.filter;
    btn.setAttribute('aria-pressed', String(f === curFilter));
    if (f === 'active') btn.textContent = activeCount ? `进行中 ${activeCount}` : '进行中';
    else if (f === 'done') btn.textContent = doneCount ? `已完成 ${doneCount}` : '已完成';
    else btn.textContent = totalCount ? `全部 ${totalCount}` : '全部';
  }
}

function inferAspectFromJob(j) {
  if (['square', 'landscape', 'portrait'].includes(j?.aspect)) return j.aspect;
  const sh = j?.shots?.[0];
  if (sh?.width && sh?.height) {
    if (sh.width > sh.height) return 'landscape';
    if (sh.height > sh.width) return 'portrait';
    return 'square';
  }
  return 'landscape';
}

function buildPageItems(page, totalPages) {
  if (totalPages <= 7) {
    return Array.from({ length: totalPages }, (_, i) => i + 1);
  }
  if (page <= 4) {
    return [1, 2, 3, 4, 5, '…', totalPages];
  }
  if (page >= totalPages - 3) {
    return [1, '…', totalPages - 4, totalPages - 3, totalPages - 2, totalPages - 1, totalPages];
  }
  return [1, '…', page - 1, page, page + 1, '…', totalPages];
}

function goToPage(nextPage) {
  pages[workKind] = Math.max(1, Number(nextPage) || 1);
  renderJobs();
  const anchor = $('historyTitle');
  if (anchor && anchor.getBoundingClientRect().top < 0) {
    anchor.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

function renderPager(totalItems, page, totalPages, startIdx, shownCount) {
  const lib = $('workLibrary');
  const pager = $('jobPager');
  const topPager = $('topPager');
  const info = $('pagerInfo');
  const controls = $('pagerControls');

  if (!totalItems) {
    lib.dataset.paged = '0';
    pager.hidden = true;
    topPager.hidden = true;
    topPager.replaceChildren();
    controls.replaceChildren();
    return;
  }

  lib.dataset.paged = '1';
  pager.hidden = false;
  topPager.hidden = totalPages <= 1;
  topPager.replaceChildren();
  if (totalPages > 1) {
    const tPrev = tx('button', '‹', 'topPageBtn');
    tPrev.type = 'button';
    tPrev.disabled = page <= 1;
    tPrev.setAttribute('aria-label', '上一页');
    tPrev.onclick = () => goToPage(page - 1);
    const tText = tx('span', `${page} / ${totalPages} 页`);
    const tNext = tx('button', '›', 'topPageBtn');
    tNext.type = 'button';
    tNext.disabled = page >= totalPages;
    tNext.setAttribute('aria-label', '下一页');
    tNext.onclick = () => goToPage(page + 1);
    topPager.append(tPrev, tText, tNext);
  }

  const isImg = workKind === 'image';
  const unit = isImg ? '张图片' : '个视频';
  const shortUnit = isImg ? '张' : '个';
  if (isMobileViewport()) {
    info.textContent = `第 ${page}/${totalPages} 页 · 共 ${totalItems} ${shortUnit}`;
  } else if (totalPages > 1) {
    info.textContent = `第 ${page} / ${totalPages} 页 · 显示 ${startIdx + 1}–${startIdx + shownCount}（共 ${totalItems} ${unit}）`;
  } else {
    info.textContent = `共 ${totalItems} ${unit} · 第 1 / 1 页`;
  }

  controls.replaceChildren();

  const prev = tx('button', '上一页', 'pageBtn');
  prev.type = 'button';
  prev.disabled = page <= 1;
  prev.onclick = () => goToPage(page - 1);
  controls.append(prev);

  for (const item of buildPageItems(page, totalPages)) {
    if (item === '…') {
      const dots = tx('span', '…', 'pageDots');
      dots.setAttribute('aria-hidden', 'true');
      controls.append(dots);
    } else {
      const b = tx('button', String(item), 'pageBtn pageNum' + (item === page ? ' isCurrent' : ''));
      b.type = 'button';
      b.setAttribute('aria-label', `第 ${item} 页`);
      if (item === page) {
        b.setAttribute('aria-current', 'page');
      } else {
        b.onclick = () => goToPage(item);
      }
      controls.append(b);
    }
  }

  const next = tx('button', '下一页', 'pageBtn');
  next.type = 'button';
  next.disabled = page >= totalPages;
  next.onclick = () => goToPage(page + 1);
  controls.append(next);
}

function emptyLibraryText(isImage, curFilter) {
  if (curFilter === 'active') {
    return isImage ? '当前没有进行中的图片任务。' : '当前没有进行中的视频任务。';
  }
  if (curFilter === 'done') {
    return isImage ? '还没有已完成的图片作品。' : '还没有已完成的视频成片。';
  }
  return isImage
    ? '还没有图片作品。在上方创作台选择「图片」写下创意，生成的图片会独立保存在这里。'
    : '还没有视频成片。在上方创作台选择「视频」写下创意，生成的短片会独立保存在这里。';
}

async function rerunJob(j) {
  if (rerunningJobs.has(j.id)) return;
  const k = jobKind(j);
  const isImage = k === 'image';
  const brief = (j.brief || j.prompt || '').trim();
  if (!brief) return;
  rerunningJobs.add(j.id);
  renderJobs();
  try {
    const r = await api('create', {
      prompt: brief,
      kind: isImage ? 'image' : 'video',
      aspect: isImage ? inferAspectFromJob(j) : 'landscape',
      precision: modelMode,
      seconds: isImage ? 0 : snapDuration(j.seconds),
      parent: isImage ? '' : (j.parent || ''),
      request_id: crypto.randomUUID()
    });
    rememberJobs([r.job]);
    setWorkKind(k, true);
    if (filters[k] === 'done') filters[k] = 'all';
    pages[k] = 1;
    boardManual = null;
    showJobsMsg('已用相同创意重新发起任务，正在生成全新提示词…', 5000);
    watchJob(r.job);
    await refresh();
    const newEl = document.querySelector(`[data-job-id="${r.job.id}"]`);
    if (newEl) newEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (e) {
    showJobsMsg(e.message, 5500);
  } finally {
    rerunningJobs.delete(j.id);
    renderJobs();
  }
}

function fillComposerFromJob(j) {
  const isImage = jobKind(j) === 'image';
  const brief = j.brief || j.prompt || '';
  if ($('prompt').value.trim() && $('prompt').value !== brief && !confirm('要用这条任务的提示词替换当前输入框内容吗？')) return;
  creationKind = isImage ? 'image' : 'video';
  if (isImage) imageAspect = inferAspectFromJob(j);
  setWorkKind(creationKind, true);
  paintMode();
  parentId = isImage ? '' : (j.parent || '');
  paintReply();
  if (!isImage) {
    duration = snapDuration(j.seconds);
    durations();
  }
  $('prompt').value = brief;
  changed();
  showFormMsg('已填入创意，可修改后重新提交。', 4000);
  $('prompt').scrollIntoView({ behavior: 'smooth', block: 'center' });
  $('prompt').focus();
}

function buildJobCard(j) {
  const isImage = jobKind(j) === 'image';
  const failed = isFailedState(j.state);
  const expanded = openShotJobs.has(j.id);
  const card = tx('div', '', 'job' + (expanded ? ' isExpanded' : '') + (failed ? ' isFailed' : ''));
  card.dataset.jobId = j.id;

  // 1. Top row: state pill + timestamp
  const head = tx('div', '', 'jobHead');
  const pill = tx('span', j.recovering ? '自动恢复' : (states[j.state] || j.state), 'pill');
  pill.dataset.state = j.recovering ? 'queued' : j.state;
  head.append(pill, tx('span', detailDate(j.created), 'jobTime'));
  card.append(head);

  // 2. Compact spec row
  const metaRow = tx('div', '', 'jobMetaRow');
  if (!isImage && j.parent) metaRow.append(tx('span', '续拍', 'seqBadge'));
  const specs = tx('span', '', 'specGroup');
  const sh0 = j.shots?.[0];
  const specItems = isImage
    ? [
        aspectLabels[inferAspectFromJob(j)] || '横图',
        sh0?.width && sh0?.height && !isMobileViewport() ? `${sh0.width}×${sh0.height}` : ''
      ].filter(Boolean)
    : [
        (j.total_seconds || j.seconds) + ' 秒',
        j.shots?.length ? j.shots.length + ' 镜' : '',
        j.precision === 'bf16' ? 'BF16' : '',
        j.quality === 'fast' ? '快速预览' : ''
      ].filter(Boolean);
  for (const item of specItems) specs.append(tx('span', item, 'spec'));
  metaRow.append(specs);
  card.append(metaRow);

  // 3. Prompt text (clamped to 2 lines, tap to expand/collapse)
  const fullTitle = j.brief || j.prompt || '未命名创意';
  const promptEl = tx('p', fullTitle, 'jobPrompt' + (openPromptJobs.has(j.id) ? ' isOpen' : ''));
  promptEl.title = '点击展开或收起完整提示词';
  promptEl.onclick = () => {
    if (openPromptJobs.has(j.id)) openPromptJobs.delete(j.id);
    else openPromptJobs.add(j.id);
    promptEl.classList.toggle('isOpen');
  };
  card.append(promptEl);

  // 4. Clean live progress / GPU preparation / status block
  if (j.preparation?.length && !j.sampling?.length && j.state !== 'planning') {
    preparationDetail(j.preparation, card);
  } else {
    const live = buildLiveBox(j);
    if (live) card.append(live);
  }

  // 5. Optional inline storyboard / visual description view
  if (expanded && j.shots?.length) {
    const shotBox = tx('div', '', 'jobShots');
    renderShotRows(shotBox, j.shots, isImage, j.state === 'planning');
    card.append(shotBox);
  }

  // 6. Action buttons (anchored to card bottom)
  const actions = tx('div', '', 'jobActions');

  if (j.state === 'done') {
    if (j.media_ready) {
      const label = isImage ? '▶ 查看' : '▶ 播放';
      const pb = tx('button', label, 'actBtn actPrimary');
      pb.type = 'button';
      pb.onclick = () => openPreview(j);
      actions.append(pb);
    }

    const busy = rerunningJobs.has(j.id);
    const rb = tx('button', busy ? '提交中…' : '重试', 'actBtn');
    rb.type = 'button';
    rb.disabled = busy;
    rb.title = '使用相同创意，重新生成全新提示词再跑一遍';
    rb.onclick = () => rerunJob(j);
    actions.append(rb);

    if (j.media_ready) {
      const rawUrl = mediaUrl(j);
      const dl = tx('a', isImage ? '下载' : '下载', 'actBtn');
      dl.href = rawUrl + (rawUrl.includes('?') ? '&' : '?') + 'download=1';
      dl.download = '映室-' + j.id.slice(0, 8) + (isImage ? '.png' : '.mp4');
      actions.append(dl);
    }

    if (!isImage && j.media_ready) {
      const seq = tx('button', '续拍', 'actBtn');
      seq.type = 'button';
      seq.onclick = () => {
        if ($('prompt').value.trim() && $('prompt').value !== j.brief && !confirm('当前输入框已有内容，要清空并从这条成片结尾续拍吗？')) return;
        creationKind = 'video';
        setWorkKind('video', true);
        paintMode();
        parentId = j.id;
        $('prompt').value = '';
        paintReply();
        changed();
        showFormMsg('写下接下来发生什么，所选时长为新增片段长度。');
        $('prompt').scrollIntoView({ behavior: 'smooth', block: 'center' });
        $('prompt').focus();
      };
      actions.append(seq);
    }
  }

  if (failed) {
    const busy = rerunningJobs.has(j.id);
    const rb = tx('button', busy ? '提交中…' : '重试', 'actBtn');
    rb.type = 'button';
    rb.disabled = busy;
    rb.onclick = () => rerunJob(j);
    actions.append(rb);

    const retry = tx('button', '修改创意', 'actBtn');
    retry.type = 'button';
    retry.onclick = () => fillComposerFromJob(j);
    actions.append(retry);
  }

  if (j.shots?.length) {
    const btnText = isImage
      ? (expanded ? '收起' : '描述')
      : (expanded ? '收起' : `分镜(${j.shots.length})`);
    const sb = tx('button', btnText, 'actBtn');
    sb.type = 'button';
    sb.onclick = () => {
      if (openShotJobs.has(j.id)) openShotJobs.delete(j.id);
      else openShotJobs.add(j.id);
      renderJobs();
    };
    actions.append(sb);
  }

  if (j.state === 'queued') {
    const cancelBtn = tx('button', '取消排队', 'actBtn');
    cancelBtn.type = 'button';
    cancelBtn.onclick = async () => {
      try {
        const tok = j.watch_token || jobToken(j.id);
        await api('cancel', { id: j.id, ...(tok ? { watch_token: tok } : {}) });
        await refresh();
      } catch (e) {
        showJobsMsg(e.message);
      }
    };
    actions.append(cancelBtn);
  }

  const tok = j.watch_token || jobToken(j.id);
  if (tok) {
    const lk = tx('button', copiedJobId === j.id ? '已复制 ✓' : '分享', 'actBtn actSubtle');
    lk.type = 'button';
    lk.onclick = async () => {
      const url = location.origin + location.pathname + '#job=' + j.id + '.' + tok;
      try {
        await navigator.clipboard.writeText(url);
        copiedJobId = j.id;
        lk.textContent = '已复制 ✓';
        setTimeout(() => {
          if (copiedJobId === j.id) copiedJobId = '';
          lk.textContent = '分享';
        }, 1600);
      } catch {
        showJobsMsg('分享链接：' + url, 8000);
      }
    };
    actions.append(lk);
  }

  if (actions.childElementCount) card.append(actions);
  return card;
}

function renderFailedDrawer(failedList) {
  const wrap = $('failedWrap');
  if (!wrap) return;
  wrap.replaceChildren();
  const curFilter = filters[workKind] || 'all';
  if (curFilter !== 'all' || !failedList.length) return;

  const details = document.createElement('details');
  details.className = 'failedFold';
  details.open = openFailedSections.has(workKind);

  const summary = document.createElement('summary');
  summary.className = 'failedSummary';
  const sumLeft = tx('span', `未完成 / 失败记录 (${failedList.length})`);
  const sumRight = tx('span', details.open ? '收起 ▴' : '展开 ▾', 'foldToggleText');
  summary.append(sumLeft, sumRight);
  details.append(summary);

  const kindSnapshot = workKind;
  details.ontoggle = () => {
    if (details.open) openFailedSections.add(kindSnapshot);
    else openFailedSections.delete(kindSnapshot);
    sumRight.textContent = details.open ? '收起 ▴' : '展开 ▾';
  };

  const fGrid = tx('div', '', 'jobGrid failedGrid');
  for (const j of failedList) fGrid.append(buildJobCard(j));
  details.append(fGrid);
  wrap.append(details);
}

function renderJobs() {
  const c = $('jobs');
  c.replaceChildren();

  const videoJobs = jobs.filter(j => jobKind(j) === 'video');
  const imageJobs = jobs.filter(j => jobKind(j) === 'image');
  updateWorkSwitch(videoJobs, imageJobs);

  const isImageView = workKind === 'image';
  const kindJobs = isImageView ? imageJobs : videoJobs;
  const activeDone = kindJobs.filter(j => !isFailedState(j.state));
  const failedList = kindJobs.filter(j => isFailedState(j.state));
  updateFilterTabs(activeDone);

  const curFilter = filters[workKind] || 'all';
  const seen = activeDone.filter(j =>
    curFilter === 'all' ||
    (curFilter === 'active' ? isActiveState(j.state) : j.state === 'done')
  );

  renderFailedDrawer(failedList);

  if (!seen.length) {
    renderPager(0, 1, 1, 0, 0);
    c.append(tx('div', emptyLibraryText(isImageView, curFilter), 'empty'));
    return;
  }

  const totalPages = Math.max(1, Math.ceil(seen.length / PAGE_SIZE));
  const page = Math.min(Math.max(1, pages[workKind] || 1), totalPages);
  pages[workKind] = page;
  const startIdx = (page - 1) * PAGE_SIZE;
  const pageItems = seen.slice(startIdx, startIdx + PAGE_SIZE);

  for (const j of pageItems) {
    c.append(buildJobCard(j));
  }

  renderPager(seen.length, page, totalPages, startIdx, pageItems.length);
}

async function recoverGuestJobs(current) {
  const known = new Set(current.map(j => j.id));
  const missing = localRead('tokens', []).filter(x => x?.id && x?.token && !known.has(x.id)).slice(-6);
  if (!missing.length) return current;
  const found = await Promise.all(missing.map(async x => {
    try {
      return (await api('creation/' + encodeURIComponent(x.id) + '?token=' + encodeURIComponent(x.token))).job;
    } catch (e) {
      if (String(e?.message || '').includes('没有此任务')) forgetJobToken(x.id);
      return null;
    }
  }));
  const merged = [...current, ...found.filter(Boolean)];
  merged.sort((a, b) => (b.created || 0) - (a.created || 0));
  return merged;
}

async function refresh(first = false) {
  try {
    const d = await api('state');
    const prev = role;
    if (Number.isFinite(d.now)) serverClockOffset = d.now - Math.floor(Date.now() / 1000);
    showRole(d.role || 'guest');
    const owner = role === 'owner';
    jobTotals = d.job_totals || null;
    rememberJobs(d.jobs || []);
    jobs = owner ? (d.jobs || []) : await recoverGuestJobs(d.jobs || []);
    rememberJobs(jobs);

    const w = d.worker || {};
    onlineModes = d.online_modes || null;
    paintMode();
    paintWorkerStatus(w);
    $('preparation').replaceChildren();
    preparationDetail(w.preparation, $('preparation'));
    $('preparation').hidden = !w.preparation?.length;

    if (focusJobId) {
      const target = jobs.find(j => j.id === focusJobId);
      focusJobId = '';
      if (target) {
        const k = jobKind(target);
        setWorkKind(k, true);
        filters[k] = 'all';
        if (isFailedState(target.state)) {
          openFailedSections.add(k);
        } else {
          const idx = jobs.filter(j => jobKind(j) === k && !isFailedState(j.state)).findIndex(j => j.id === target.id);
          pages[k] = idx >= 0 ? Math.floor(idx / PAGE_SIZE) + 1 : 1;
        }
      }
    }

    if (first || prev !== role) {
      const local = localRead('draft', null);
      const draft = owner ? (d.draft || (local ? { ...local, parent: '' } : null)) : local;
      paintPrompt(draft);
      $('draftStatus').textContent = (draft?.brief || draft?.prompt) ? '草稿已恢复' : '自动保存草稿';
      if (owner && !d.draft && draft) changed();
      const current = jobs.find(j => j.state === 'planning') ||
        jobs.find(j => j.state === 'running') ||
        jobs.find(j => j.state === 'queued');
      if (!workKindInitialized) {
        const hasVideos = jobs.some(j => jobKind(j) === 'video');
        const hasImages = jobs.some(j => jobKind(j) === 'image');
        const initialKind = current
          ? jobKind(current)
          : (draft?.kind === 'image' || (!hasVideos && hasImages))
            ? 'image'
            : 'video';
        setWorkKind(initialKind, false);
      }
      if (current) watchJob(current);
      else renderShots(jobs.find(j => jobKind(j) === creationKind) || jobs[0] || null);
    } else if (activeJob) {
      const live = jobs.find(j => j.id === activeJob);
      if (live) renderShots(live);
    }

    renderJobs();
    if (previewJob) {
      const current = jobs.find(j => j.id === previewJob);
      if (current) renderPreview(current);
    }
    if (first) $('submit').disabled = false;
  } catch {
    $('workerSeen').dataset.online = '0';
    $('workerSeen').textContent = '重连中…';
  }
}

document.querySelectorAll('[name=creationKind]').forEach(el => {
  el.onchange = () => {
    creationKind = el.value;
    if (creationKind === 'image') parentId = '';
    setWorkKind(creationKind, true);
    paintMode();
    paintReply();
    if (!boardJob || !isActiveState(boardJob.state)) {
      renderShots(jobs.find(j => jobKind(j) === creationKind) || null);
    }
    renderJobs();
    changed();
  };
});

document.querySelectorAll('[name=imageAspect]').forEach(el => {
  el.onchange = () => {
    imageAspect = el.value;
    changed();
  };
});

$('prompt').oninput = () => {
  showFormMsg('');
  changed();
};

$('clearPrompt').onclick = () => clearComposer('已清空', true);

$('duration').oninput = () => {
  duration = snapDuration($('duration').value);
  durations();
  changed();
};

$('durationStops').onclick = e => {
  const b = e.target.closest('[data-sec]');
  if (!b) return;
  const next = snapDuration(b.dataset.sec);
  if (next === duration) return;
  duration = next;
  durations();
  changed();
};

$('workSwitch').onclick = e => {
  const b = e.target.closest('[data-work-kind]');
  if (!b) return;
  const next = b.dataset.workKind;
  if (next === workKind) return;
  setWorkKind(next, true);
  renderJobs();
};

$('workSwitch').onkeydown = e => {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
  e.preventDefault();
  const next = workKind === 'video' ? 'image' : 'video';
  setWorkKind(next, true);
  renderJobs();
  $(next === 'image' ? 'kindImageTab' : 'kindVideoTab').focus();
};

$('tabs').onclick = e => {
  const b = e.target.closest('[data-filter]');
  if (!b) return;
  const next = b.dataset.filter;
  if (filters[workKind] === next) return;
  filters[workKind] = next;
  pages[workKind] = 1;
  renderJobs();
};

$('submit').onclick = async () => {
  const b = $('submit');
  const prompt = $('prompt').value.trim();
  if (!prompt) {
    showFormMsg('先写下一段创意。');
    $('prompt').focus();
    return;
  }
  const v = version;
  b.disabled = true;
  b.dataset.busy = '1';
  b.textContent = '正在提交…';
  requestId = requestId || crypto.randomUUID();
  try {
    const r = await api('create', {
      prompt,
      kind: creationKind,
      aspect: imageAspect,
      precision: modelMode,
      seconds: snapDuration(duration),
      parent: parentId,
      request_id: requestId
    });
    requestId = null;
    rememberJobs([r.job]);

    // Clear the submitted creative immediately so the composer is ready for the next task
    if (v === version) {
      clearComposer('已提交', false);
    }

    setWorkKind(jobKind(r.job), true);
    if (filters[workKind] === 'done') filters[workKind] = 'all';
    pages[workKind] = 1;
    boardManual = null;
    showFormMsg('已加入下方对应作品列表，可继续输入下一条创意。', 4500);
    watchJob(r.job);
    await refresh();
  } catch (e) {
    showFormMsg(e.message);
  } finally {
    b.disabled = false;
    delete b.dataset.busy;
    b.textContent = '提交生成 →';
  }
};

async function boot() {
  const savedKind = localRead('work-kind', '');
  if (savedKind === 'video' || savedKind === 'image') {
    workKind = savedKind;
    workKindInitialized = true;
  }
  renderJobs();
  const hash = new URLSearchParams(location.hash.slice(1));
  const token = hash.get('activate');
  const shared = hash.get('job');
  if (token || shared) history.replaceState(null, '', location.pathname + location.search);
  if (token) {
    try {
      await api('activate', { token });
      $('activationMsg').textContent = '此浏览器已连接到你的工作台。';
      setTimeout(() => {
        if ($('activationMsg').textContent.includes('已连接到你的工作台')) $('activationMsg').textContent = '';
      }, 4500);
    } catch (e) {
      $('activationMsg').textContent = e.message;
    }
  }
  if (shared) {
    const [jid, jtok] = shared.split('.');
    if (/^[a-f0-9]{32}$/.test(jid || '') && /^[a-f0-9]{32}$/.test(jtok || '')) {
      rememberJobs([{ id: jid, watch_token: jtok }]);
      focusJobId = jid;
      $('activationMsg').textContent = '已打开分享作品，正在同步进度…';
      setTimeout(() => {
        if ($('activationMsg').textContent.includes('正在同步进度')) $('activationMsg').textContent = '';
      }, 4000);
    }
  }
  await refresh(true);
}

window.addEventListener('hashchange', () => {
  if (location.hash.startsWith('#activate=') || location.hash.startsWith('#job=')) boot();
});

let lastMobile = isMobileViewport();
window.addEventListener('resize', () => {
  renderShots();
  const nowMobile = isMobileViewport();
  if (nowMobile !== lastMobile) {
    lastMobile = nowMobile;
    renderJobs();
  }
});

boot();
setInterval(() => {
  if (!document.hidden) refresh(!logged);
}, 5000);

let previewWatch = 0;

function clearPreview() {
  stopPlaybackCheck();
  stopPlaybackCheck = () => {};
  clearInterval(previewWatch);
  previewWatch = 0;
  const frame = $('frame');
  for (const v of frame.querySelectorAll('video')) {
    v.onwaiting = null;
    v.onplaying = null;
    v.onerror = null;
    v.onloadedmetadata = null;
    v.pause();
    v.removeAttribute('src');
    v.load();
  }
  frame.replaceChildren();
}

function mediaUrl(j, preview = false, bust = 0) {
  const tok = j.watch_token || jobToken(j.id);
  const q = [];
  if (tok) q.push('token=' + encodeURIComponent(tok));
  if (bust) q.push('r=' + bust);
  return 'api/' + (preview ? 'preview/' : 'media/') + encodeURIComponent(j.id) + (q.length ? '?' + q.join('&') : '');
}

function bufferedEnd(v, at) {
  let end = 0;
  for (let i = 0; i < v.buffered.length; i++) {
    const a = v.buffered.start(i);
    const b = v.buffered.end(i);
    if (a <= at + 0.5 && b > end) end = b;
  }
  return end;
}

function playPreview(v) {
  v.play().catch(e => {
    if (!$('frame').contains(v)) return;
    v.dataset.playError = e.name;
    $('previewStatus').textContent = e.name === 'NotAllowedError'
      ? '浏览器需要再次确认，请点「播放／重试」。'
      : '视频尚未开始播放，请重试或下载高清原片。';
    $('retryPreview').hidden = false;
  });
}

// Temporary playback-check instrumentation: only active through an explicit diagnostic link.
function checkPlayback(v, j) {
  const run = new URLSearchParams(location.search).get('playback_check');
  if (!/^[a-f0-9]{16}$/.test(run || '')) return () => {};
  const start = performance.now();
  const events = [];
  const controller = new AbortController();
  let active = true;
  let probeTimer;
  for (const event of ['loadedmetadata', 'loadeddata', 'playing', 'waiting', 'stalled', 'error']) {
    v.addEventListener(event, () => {
      events.push([event, Math.round(v.currentTime * 10) / 10, v.readyState]);
      if (events.length > 12) events.shift();
    });
  }
  const timer = setTimeout(async () => {
    let bytes = 0;
    let status = 0;
    let probeError = '';
    let total = 0;
    const began = performance.now();
    probeTimer = setTimeout(() => controller.abort(), 12000);
    try {
      const res = await fetch(mediaUrl(j, true), {
        headers: { Range: 'bytes=0-65535' },
        cache: 'no-store',
        signal: controller.signal
      });
      status = res.status;
      total = Number((res.headers.get('Content-Range') || '').split('/')[1]) || 0;
      if (res.ok) {
        const reader = res.body.getReader();
        while (true) {
          const chunk = await reader.read();
          if (chunk.done) break;
          bytes += chunk.value.length;
        }
      }
    } catch (e) {
      probeError = e.name;
    } finally {
      clearTimeout(probeTimer);
    }
    if (!active) return;
    const report = {
      run,
      elapsed: Math.round(performance.now() - start),
      seconds: Math.round(v.currentTime * 10) / 10,
      ready: v.readyState,
      network: v.networkState,
      paused: v.paused,
      error: v.error?.code || 0,
      playError: v.dataset.playError || '',
      buffered: v.buffered.length ? v.buffered.end(v.buffered.length - 1) : 0,
      status,
      bytes,
      total,
      bps: Math.round(bytes / ((performance.now() - began) / 1000)),
      probeError,
      events
    };
    try {
      const res = await fetch('api/health?playback_check=' + encodeURIComponent(btoa(JSON.stringify(report))), { cache: 'no-store' });
      if (active && res.ok) $('previewStatus').textContent += ' 检测结果已送达。';
    } catch {
      if (active) $('previewStatus').textContent += ' 检测结果发送失败。';
    }
  }, 10000);
  return () => {
    active = false;
    clearTimeout(timer);
    clearTimeout(probeTimer);
    controller.abort();
  };
}

function renderPreview(j) {
  const image = j.kind === 'image';
  const frame = $('frame');
  const state = image ? 'ready' : (j.preview_status || 'pending');
  const key = j.id + ':' + state;
  if (frame.dataset.preview === key) return;
  frame.dataset.preview = key;

  clearPreview();
  $('retryPreview').hidden = true;

  const url = mediaUrl(j);
  $('downloadFilm').href = url + (url.includes('?') ? '&' : '?') + 'download=1';
  $('downloadFilm').download = '映室-' + j.id.slice(0, 8) + (image ? '.png' : '.mp4');
  $('previewHeading').textContent = image ? '图片预览' : '成片预览';
  $('downloadFilm').textContent = image ? '下载图片' : '下载高清原片';

  if (image) {
    const img = document.createElement('img');
    img.src = url;
    img.alt = j.brief || '生成图片';
    img.onerror = () => {
      $('previewStatus').textContent = '图片加载失败，请关闭后重试或下载原图。';
    };
    frame.replaceChildren(img);
    $('previewStatus').textContent = '';
    return;
  }

  const readyText = '轻量预览 · 下载可获取高清原片。';
  $('previewStatus').textContent = state === 'ready'
    ? readyText
    : state === 'failed'
      ? '预览暂不可用，后台会重试；高清原片可直接下载。'
      : '预览版准备中，完成后即可播放；高清原片已保存，可直接下载。';

  if (state !== 'ready') return;
  const v = document.createElement('video');
  v.src = mediaUrl(j, true);
  v.controls = true;
  v.playsInline = true;
  v.preload = 'auto';
  v.setAttribute('aria-label', j.brief || '成片预览');

  let retries = 0;
  let recovering = false;
  let lastTime = -1;
  let lastBuf = -1;
  let stalledTicks = 0;

  const recover = (manual = false) => {
    if ((recovering && !manual) || previewJob !== j.id) return;
    if (!manual && retries >= 3) {
      $('previewStatus').textContent = '预览加载失败，可重试或下载高清原片。';
      $('retryPreview').hidden = false;
      clearInterval(previewWatch);
      previewWatch = 0;
      return;
    }
    recovering = true;
    retries = manual ? 0 : retries + 1;
    stalledTicks = 0;
    const resumeAt = Number.isFinite(v.currentTime) && v.currentTime > 0.2 ? v.currentTime : 0;
    const wantPlay = manual || (!v.paused && !v.ended) || Boolean(v.error);
    $('previewStatus').textContent = '网络波动，正在自动续播…';
    $('retryPreview').hidden = false;
    v.pause();
    v.removeAttribute('src');
    v.load();
    v.onloadedmetadata = () => {
      v.onloadedmetadata = null;
      if (resumeAt > 0 && (!Number.isFinite(v.duration) || resumeAt < v.duration - 0.15)) {
        try { v.currentTime = resumeAt; } catch {}
      }
      recovering = false;
      lastTime = v.currentTime;
      lastBuf = bufferedEnd(v, v.currentTime);
      if (wantPlay) playPreview(v);
    };
    v.src = mediaUrl(j, true, Date.now() + '_' + retries);
    v.load();
  };

  v._recover = recover;
  v.onwaiting = () => {
    if (!recovering) {
      $('previewStatus').textContent = '正在缓冲视频…';
      $('retryPreview').hidden = false;
    }
  };
  v.onplaying = () => {
    recovering = false;
    stalledTicks = 0;
    delete v.dataset.playError;
    $('previewStatus').textContent = readyText;
    $('retryPreview').hidden = true;
  };
  v.onerror = () => {
    recovering = false;
    recover(false);
  };

  frame.append(v);
  stopPlaybackCheck = checkPlayback(v, j);
  playPreview(v);

  previewWatch = setInterval(() => {
    if (!v.isConnected || previewJob !== j.id) {
      clearInterval(previewWatch);
      previewWatch = 0;
      return;
    }
    if (document.hidden || recovering || v.paused || v.ended) {
      stalledTicks = 0;
      lastTime = v.currentTime;
      lastBuf = bufferedEnd(v, v.currentTime);
      return;
    }
    const t = v.currentTime;
    const b = bufferedEnd(v, t);
    if (Math.abs(t - lastTime) < 0.02 && b <= lastBuf + 0.02 && v.readyState < 3) {
      stalledTicks++;
      if (stalledTicks >= 3) recover(false);
    } else {
      stalledTicks = 0;
    }
    lastTime = t;
    lastBuf = b;
  }, 1000);
}

function openPreview(j) {
  if (!j.media_ready || !j.id) return;
  previewJob = j.id;
  delete $('frame').dataset.preview;
  $('preview').showModal();
  renderPreview(j);
}

$('retryPreview').onclick = () => {
  const v = $('frame').querySelector('video');
  if (!v) return;
  if (v.error || (!v.paused && v.readyState < 3)) v._recover?.(true);
  else playPreview(v);
};

$('closePreview').onclick = () => $('preview').close();
$('preview').onclose = () => {
  previewJob = '';
  clearPreview();
  delete $('frame').dataset.preview;
};

function paintReply() {
  $('lengthLabel').textContent = parentId ? '新增时长' : '成片时长';
  const j = jobs.find(x => x.id === parentId);
  $('reply').hidden = !parentId;
  $('replyText').textContent = parentId
    ? '从原片结尾续拍 · ' + ((j?.brief || j?.prompt || '已保存的创意').slice(0, 42))
    : '';
  $('prompt').placeholder = creationKind === 'image'
    ? '写下你的创意'
    : parentId
      ? '写接下来发生什么，例如：镜头靠近，两人听着雨声一起笑。'
      : '比如：大雨敲着窗，家里的橘猫和雪纳瑞围着仙女棒聊天。让我们听见雨声和它们的对白。';
  syncComposerMeta();
}

$('newIdea').onclick = () => {
  parentId = '';
  paintReply();
  changed();
};
