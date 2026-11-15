"""Gemini 4 Argon plans timing, direction and H3 rendering mode."""
import json
import h3_guidance
import time
import threading
import urllib.error
import urllib.request

MODEL = 'gemini-4-argon'
PROJECT = 'YOUR_GOOGLE_CLOUD_PROJECT'
URL = f'https://aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/global/publishers/google/models/{MODEL}:generateContent'
_gate = threading.BoundedSemaphore(1)
ROUTES = {'continuous':'连续长镜头，不切镜；多段时保持末帧动作连贯', 'story':'叙事', 'mood':'氛围', 'dialogue':'对白'}

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

def generate(url, key_path, body, emit):
    if emit:url=url.replace(':generateContent',':streamGenerateContent?alt=sse')
    req=urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={'Content-Type':'application/json','X-goog-api-key':key_path.read_text().strip()})
    with urllib.request.urlopen(req,timeout=48) as r:
        if not emit:
            return ''.join(p.get('text','') for p in rjson(r) if not p.get('thought'))
        raw=''; last=[]
        for line in r:
            if not line.startswith(b'data:'):continue
            line=line[5:].strip()
            if not line or line==b'[DONE]':continue
            event=json.loads(line)
            if 'error' in event:raise ValueError('upstream stream failed')
            parts=(event.get('candidates') or [{}])[0].get('content',{}).get('parts',[])
            raw+=''.join(p.get('text','') for p in parts if not p.get('thought'))
            partial=preview(raw)
            if partial!=last and any(partial):
                emit(partial)
                last=partial
        return raw

def rjson(response):
    return json.load(response)['candidates'][0]['content']['parts']

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
    body={'systemInstruction':{'parts':[{'text':instructions}]},'contents':[{'role':'user','parts':[{'text':json.dumps({'创意':brief,'总秒数':total,'参考时长':suggested},ensure_ascii=False)}]}],
          'generationConfig':{'responseMimeType':'application/json','responseSchema':{'type':'ARRAY','items':{'type':'STRING'}},'thinkingConfig':{'thinkingLevel':'LOW'},'maxOutputTokens':max(3200,850*min(12,max(n,4))+1200)}}
    def partial(parts):
        shots=[]
        for line in parts[2:14]:
            sec,sep,txt=line.partition('|')
            if sep and sec.strip().isascii() and sec.strip().isdigit() and 1<=int(sec.strip())<=15 and txt.strip():shots.append(txt.split('|||',1)[0].removeprefix('[CUT]').strip().rstrip('|')[:2000])
        if shots and emit:emit(shots)
    if not _gate.acquire(blocking=False):fail(429,'正在编写另一份分镜，请稍后重试。')
    try:
        for attempt in range(2):
            for retry in range(3):
                try:
                    raw=generate(URL,keys/'vertex-api-key',body,partial if emit else None)
                    break
                except urllib.error.HTTPError as e:
                    if e.code in (429,503) and retry<2:
                        wait=e.headers.get('Retry-After','')
                        delay=min(30,int(wait)) if wait.isdigit() else (4,12)[retry]
                        time.sleep(max(1,delay));continue
                    fail(503,f'Argon 请求未成功（{e.code}），请稍后重试。')
                except (TimeoutError,urllib.error.URLError):fail(504,'Argon 暂时未响应，请重试。')
            try:
                arr=json.loads(raw)
                if not isinstance(arr,list) or not 3<=len(arr)<=14 or arr[0] not in ROUTES or arr[1] not in ('hd','fast'):raise ValueError()
                shots=[]
                for entry in arr[2:]:
                    shot=h3_guidance.unpack(entry,len(shots),arr[0],allow_cut=True)
                    shot['prompt_guides']=skill_sources
                    shots.append(shot)
                if sum(x['seconds'] for x in shots)!=total:raise ValueError()
                return {'model':MODEL,'route':arr[0],'quality':arr[1],'shots':shots}
            except (KeyError,TypeError,IndexError,AttributeError,ValueError):
                if attempt:fail(502,'Argon 的镜头安排未通过校验，没有提交到 GPU。')
                body['contents'][0]['parts'].append({'text':'上次结构、引用或时长有误。每段严格用 秒数|中文分镜|||英文画面|||英文场内声|||英文配乐；不写引用编号。请严格使用参考时长列表的每段秒数，并检查数组头两个字段。'})
    finally:_gate.release()
