"""Gemini 4 Argon plans timing, direction and H3 rendering mode."""
import json
import h3_guidance
import time
import threading
import urllib.error
import urllib.request

import os
import re
from pathlib import Path

MODEL = 'gemini-4-argon'
PROJECT = 'YOUR_GOOGLE_CLOUD_PROJECT'
def _resolve_project(keys=None):
    for v in (os.environ.get('H3_GCP_PROJECT'), os.environ.get('GOOGLE_CLOUD_PROJECT')):
        if v and v.strip() and v.strip() != 'YOUR_GOOGLE_CLOUD_PROJECT': return v.strip()
    for d in ([Path(keys)] if keys else []) + [Path('/etc/h3-studio')]:
        try:
            p = d / 'gcp-project'
            if p.is_file() and (t := p.read_text().strip()) and t != 'YOUR_GOOGLE_CLOUD_PROJECT': return t
        except OSError: pass
    return PROJECT
PROJECT = _resolve_project()
URL = f'https://aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/global/publishers/google/models/{MODEL}:generateContent'
_gate = threading.BoundedSemaphore(1)
ROUTES = {'continuous':'连续长镜头，不切镜；多段时保持末帧动作连贯', 'story':'叙事', 'mood':'氛围', 'dialogue':'对白'}
BLOCKED_REASONS = frozenset({'SAFETY', 'PROHIBITED_CONTENT', 'BLOCKLIST', 'SPII', 'IMAGE_SAFETY', 'RECITATION'})
REFUSAL_RE = re.compile(
    r'无法(?:为您)?(?:生成|完成|提供|创作|满足|编写)|不能(?:为您)?(?:生成|提供|创作|编写)|违反.*(?:安全|内容|政策|准则)'
    r'|(?:cannot|can\'t|unable to)\s+(?:generate|create|fulfill|assist|produce|write)|violat(?:es?|ing)\s+(?:safety|content|policy)',
    re.I,
)
SAFETY_MSG = '创意触发了模型安全策略限制（如露骨或敏感动作描写），未生成分镜，没有提交到 GPU；请调整措辞后重试。'

class BlockedError(Exception):
    def __init__(self, reason='SAFETY'):
        super().__init__(reason); self.reason = str(reason or 'SAFETY')

class TruncatedError(Exception):
    pass

class StreamTransientError(Exception):
    def __init__(self, code=503, detail='upstream stream failed'):
        super().__init__(detail); self.code = int(code or 503)

def preview(raw):
    """Decode only array string values seen so far, including the unfinished last one."""
    tail=raw.lstrip()
    if not tail.startswith('['):return []
    tail=tail[1:].lstrip(); values=[]
    while tail.startswith('"'):
        try:
            text,end=json.JSONDecoder().raw_decode(tail)
        except ValueError:
            # Close the unfinished string, dropping an incomplete escape (up to six characters).
            for n in range(min(6,len(tail)-1)+1):
                try: text=json.loads(tail[:len(tail)-n]+'"');break
                except ValueError:continue
            else:break
            values.append(text.encode('utf-8','ignore').decode());break
        values.append(text)
        tail=tail[end:].lstrip()
        if not tail.startswith(','):break
        tail=tail[1:].lstrip()
    return values

def _inspect_event(event):
    if not isinstance(event, dict):
        raise StreamTransientError(502, 'invalid upstream event')
    if 'error' in event:
        err = event['error'] if isinstance(event['error'], dict) else {}
        code = int(err.get('code') or 503)
        msg = str(err.get('message') or err.get('status') or 'upstream stream failed')
        if code == 400 and re.search(r'safety|prohibited|blocked|policy|recitation', msg, re.I):
            raise BlockedError('UPSTREAM_400_SAFETY')
        raise StreamTransientError(code, msg)
    pf = event.get('promptFeedback') or {}
    if pf.get('blockReason'):
        raise BlockedError(pf['blockReason'])
    cands = event.get('candidates') or []
    if not cands:
        return '', None
    cand = cands[0] or {}
    finish = cand.get('finishReason')
    if finish in BLOCKED_REASONS:
        raise BlockedError(finish)
    parts = (cand.get('content') or {}).get('parts') or []
    text = ''.join(p.get('text', '') for p in parts if isinstance(p, dict) and not p.get('thought'))
    return text, finish

def generate(url, key_path, body, emit):
    if emit:url=url.replace(':generateContent',':streamGenerateContent?alt=sse')
    req=urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={'Content-Type':'application/json','X-goog-api-key':key_path.read_text().strip()})
    with urllib.request.urlopen(req,timeout=48) as r:
        if not emit:
            event=json.load(r)
            text,finish=_inspect_event(event)
            if finish=='MAX_TOKENS':raise TruncatedError('MAX_TOKENS')
            if not (event.get('candidates') or []) and not text.strip():raise BlockedError('EMPTY_CANDIDATES')
            return text
        raw=''; last=[]; last_finish=None; saw_candidates=False
        for line in r:
            if not line.startswith(b'data:'):continue
            line=line[5:].strip()
            if not line or line==b'[DONE]':continue
            event=json.loads(line)
            if event.get('candidates'):saw_candidates=True
            chunk,finish=_inspect_event(event)
            if finish:last_finish=finish
            raw+=chunk
            partial=preview(raw)
            if partial!=last and any(partial):
                emit(partial)
                last=partial
        if last_finish=='MAX_TOKENS':raise TruncatedError('MAX_TOKENS')
        if not saw_candidates and not raw.strip():raise BlockedError('EMPTY_CANDIDATES')
        return raw

def rjson(response):
    event=json.load(response)
    _inspect_event(event)
    return event['candidates'][0]['content']['parts']

def _reconcile_seconds(shots, total):
    got = sum(x['seconds'] for x in shots)
    diff = total - got
    if diff == 0:
        return True
    if not shots or abs(diff) > max(4, len(shots) * 2):
        return False
    step = 1 if diff > 0 else -1
    rem = abs(diff)
    for _ in range(rem):
        candidates = [x for x in reversed(shots) if 1 <= x['seconds'] + step <= 15]
        if not candidates:
            return False
        # Spread adjustments evenly across shots starting from the tail.
        target = min(candidates, key=lambda x: x['seconds']) if step > 0 else max(candidates, key=lambda x: x['seconds'])
        target['seconds'] += step
    return sum(x['seconds'] for x in shots) == total

def plan(d, keys, fail, emit=None):
    brief,total=d.get('prompt'),d.get('seconds')
    if not isinstance(brief,str) or not 1<=len(brief.strip())<=8000 or type(total) is not int or not 15<=total<=120:
        fail(400,'请输入创意，并选择 15 到 120 秒。')
    n=(total+14)//15
    suggested=[total//n+(i<total%n) for i in range(n)]
    instructions=(
        '你是 H3 视频导演，用户只提供创意与总秒数。你必须选择镜头走向、渲染模式、每个镜头的秒数和可执行的中文提示词。'
        '只输出一个 JSON 字符串数组。第一项为 continuous/story/dialogue/mood 四者之一。第二项为 hd 或 fast，'
        '默认选 hd 高清成片，适合复杂动作或细节；fast 用于草样或明确追求速度。随后每项形如“15|镜头提示词”，'
        '竖线前为整数秒。1 到 12 段，每段 1–15 秒，总和严格等于总秒数。可采用参考时长，也可以按叙事节奏自行规划。'
        '只有明确不需要上一镜画面的切镜（如无人物的空镜或观众反应），在镜头提示词开头标 [CUT]；同一人物动作、造型或构图需延续时不要标，continuous 路线不要标。'
        '每段约 80–150 字，写清运镜、画面、声音、动作和必要对白，保持人物连续；保留用户指定内容。'
    )
    context,skill_sources=h3_guidance.guidance(brief)
    instructions+='\n'+context+(
        '\n以上只作写作参考。最终交付仍为扁平 JSON 字符串数组，前两项不变，后续每项格式改为：'
        '秒数|中文分镜|||英文画面时间线|||英文场内声景|||英文画外配乐。每项必须有四个非空内容字段；无配乐填 N/A。'
        '英文画面时间线写完整可见动作和说话人，包含原语言的逐字台词、歌词或可见文字；'
        '不要把中文台词或招牌翻成英文。不要输出字段标签、引用编号或工具指令，编号由实际运行模式添加。'
        '每个片段从本地 0 秒开始，后续连续片段从上一段末帧的确切姿势、人物位置和机位继续。'
        '同一首配乐跨段保持速度、乐器与节拍；无音乐/仅环境声要求优先。'
        '中文分镜保持可读，保留画幅、声音与人物约束；每段英文尽量控制在 60–140 词。')
    base_parts=[{'text':json.dumps({'创意':brief,'总秒数':total,'参考时长':suggested},ensure_ascii=False)}]
    body={'systemInstruction':{'parts':[{'text':instructions}]},'contents':[{'role':'user','parts':list(base_parts)}],
          'generationConfig':{'responseMimeType':'application/json','responseSchema':{'type':'ARRAY','items':{'type':'STRING'},'minItems':3,'maxItems':14},'thinkingConfig':{'thinkingLevel':'LOW'},'maxOutputTokens':max(3200,850*min(12,max(n,4))+1200)}}
    def partial(parts):
        shots=[]
        for line in parts[2:14]:
            s_line=re.sub(r'^\[cut\]\s*','',line.strip(),count=1,flags=re.I)
            sec,sep,txt=s_line.partition('|')
            m_sec=re.fullmatch(r'([0-9]{1,2})(?:\.0+)?\s*(?:s|sec|seconds?|秒)?',sec.strip(),re.I) if sep else None
            if m_sec and 1<=int(m_sec.group(1))<=15 and txt.strip():
                shots.append(re.sub(r'^\[cut\]\s*','',txt.split('|||',1)[0],count=1,flags=re.I).strip().rstrip('|')[:2000])
        if shots and emit:emit(shots)
    proj=_resolve_project(keys)
    endpoint=f'https://aiplatform.googleapis.com/v1/projects/{proj}/locations/global/publishers/google/models/{MODEL}:generateContent' if proj!=PROJECT else URL
    if not _gate.acquire(timeout=90 if d.get('id') else 2):fail(429,'正在编写另一份分镜，请稍后重试。')
    try:
        for attempt in range(2):
            raw=''
            try:
                for retry in range(3):
                    try:
                        raw=generate(endpoint,keys/'vertex-api-key',body,partial if emit else None)
                        break
                    except urllib.error.HTTPError as e:
                        detail=''
                        try:detail=e.read(4096).decode('utf-8','ignore')
                        except OSError:pass
                        if e.code==400 and re.search(r'safety|prohibited|blocked|policy|recitation',detail,re.I):
                            raise BlockedError('HTTP_400_SAFETY')
                        if e.code in (429,500,502,503,504) and retry<2:
                            wait=e.headers.get('Retry-After','') if e.headers else ''
                            delay=min(30,int(wait)) if wait.isdigit() else (4,12)[retry]
                            time.sleep(max(1,delay));continue
                        fail(503,f'Argon 请求未成功（{e.code}），请稍后重试。')
                    except StreamTransientError as e:
                        if e.code in (429,500,502,503,504) and retry<2:
                            time.sleep((3,8)[retry]);continue
                        fail(503,'Argon 输出流中断，请稍后重试。')
                    except (TimeoutError,urllib.error.URLError):
                        if retry<1:time.sleep(2);continue
                        fail(504,'Argon 暂时未响应，请重试。')
                arr=json.loads(raw)
                if not isinstance(arr,list):raise ValueError('返回结果不是数组')
                if not arr:
                    if attempt:fail(422,SAFETY_MSG)
                    raise ValueError('返回了空数组')
                if len(arr)<3 or len(arr)>14:
                    if not any('|||' in str(x) for x in arr) and REFUSAL_RE.search(raw):
                        raise BlockedError('MODEL_REFUSAL')
                    raise ValueError(f'数组长度异常（需 3–14 项，实际 {len(arr)} 项）')
                route=str(arr[0]).strip().lower()
                qual=str(arr[1]).strip().lower()
                if qual=='standard':qual='hd'
                if route not in ROUTES or qual not in ('hd','fast'):
                    if not any('|||' in str(x) for x in arr[2:]) and REFUSAL_RE.search(raw):
                        raise BlockedError('MODEL_REFUSAL')
                    raise ValueError('前两项须为路线（continuous/story/dialogue/mood）与画质（hd/fast）')
                shots=[]
                for entry in arr[2:]:
                    if not isinstance(entry,str) or ('|||' not in entry and REFUSAL_RE.search(entry)):
                        if REFUSAL_RE.search(str(entry)):raise BlockedError('MODEL_REFUSAL')
                    shot=h3_guidance.unpack(entry,len(shots),route,allow_cut=True)
                    shot['prompt_guides']=skill_sources
                    shots.append(shot)
                got_total=sum(x['seconds'] for x in shots)
                if got_total!=total and not (attempt and _reconcile_seconds(shots,total)):
                    raise ValueError(f'各镜秒数之和为 {got_total} 秒，须严格等于 {total} 秒')
                return {'model':MODEL,'route':route,'quality':qual,'shots':shots}
            except BlockedError as e:
                print(f'director blocked model={MODEL} reason={e.reason}',flush=True)
                fail(422,SAFETY_MSG)
            except TruncatedError:
                if attempt:fail(502,'Argon 分镜内容过长被截断，没有提交到 GPU；请精简创意或缩短时长后重试。')
                body['generationConfig']['maxOutputTokens']=min(16384,int(body['generationConfig']['maxOutputTokens']*1.6))
                body['contents'][0]['parts']=base_parts+[{'text':f'上次输出过长被截断（MAX_TOKENS）。请精简每段中英文描述字数（每段英文控制在 60–100 词），严格使用参考时长 {suggested} 输出完整 JSON 数组。'}]
            except (KeyError,TypeError,IndexError,AttributeError,ValueError) as e:
                reason= 'JSON 解析失败' if isinstance(e,json.JSONDecodeError) else (str(e) or '格式不符')
                if attempt:fail(502,f'Argon 的镜头安排未通过校验（{reason}），没有提交到 GPU；请重试或微调描述。')
                body['contents'][0]['parts']=base_parts+[{'text':f'上次输出未通过校验：{reason}。每段严格用 秒数|中文分镜|||英文画面|||英文场内声|||英文配乐（无配乐填 N/A，不写引用编号或 <Picture 1> 标签）；请直接使用参考时长 {suggested}（总和 {total} 秒），并确保前两项为有效路线与 hd/fast。'}]
    finally:_gate.release()
