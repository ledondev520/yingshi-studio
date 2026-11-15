'use strict';
const $=id=>document.getElementById(id), tx=(tag,text,cls)=>{const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n};
let role='guest',parentId="",duration=15,shots=[],jobs=[],filter='all',version=0,saving=0,timer,saveChain=Promise.resolve(),logged=false,requestId=null;
async function api(p,body){
 const res=await fetch('api/'+p,{credentials:'same-origin',cache:'no-store',method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
 const data=await res.json();if(!res.ok){throw new Error(data.error||'请求失败')}return data;
}
function localRead(k,fallback){try{return JSON.parse(localStorage.getItem('frame-room:'+k))??fallback}catch{return fallback}}
function localWrite(k,v){localStorage.setItem('frame-room:'+k,JSON.stringify(v))}
function rememberJobs(list){
 if(!Array.isArray(list)||!list.length)return;
 const map=new Map(localRead('tokens',[]).filter(x=>x?.id&&x?.token).map(x=>[x.id,x.token]));
 for(const j of list)if(j?.id&&j?.watch_token)map.set(j.id,j.watch_token);
 localWrite('tokens',[...map.entries()].slice(-24).map(([id,token])=>({id,token})));
}
function jobToken(id){return localRead('tokens',[]).find(x=>x?.id===id)?.token||''}
function forgetJobToken(id){localWrite('tokens',localRead('tokens',[]).filter(x=>x?.id&&x.id!==id))}
function workerLabel(w){
 const n=Number(w?.online_count)||0,run=Number(w?.running_jobs)||0,q=Number(w?.queued_jobs)||0;
 const loading=Number(w?.loading_count)||0,requesting=Number(w?.requesting_count)||0;
 const parts=[];
 if(w?.online||n>0)parts.push(n>1?`● ${n} 张云端 GPU 在线`:'● 云端 GPU 在线');
 if(loading)parts.push(`${loading} 张 GPU 已分配，准备模型中`);
 if(requesting)parts.push(`正在申请 ${requesting} 张 GPU`);
 if(run)parts.push(`${run} 个生成中`);
 if(q)parts.push(`${q} 个排队`);
 if(w?.runtime_limit)parts.push('Google 活动运行时已达上限，等待空闲卡');
 if(!run&&!q&&(w?.online||n>0))parts.push('空闲可接单');
 return parts.join(' · ')||'有任务时后台按需申请 GPU';
}
function showRole(value){role=value;logged=true;}
let activeJob='',watchToken=0;
let modelMode='quantized',onlineModes=null,creationKind='video',imageAspect='square';
const modeLabel=v=>v==='image-bf16'?'H3-Image 原版':v==='bf16'?'原始 BF16':'量化版';
function paintMode(){
 const image=creationKind==='image';$('promptMeta').textContent=image?'主体 · 构图 · 光线 · 风格':'画面 · 动作 · 声音 · 对白';$('videoModels').hidden=image;$('videoDuration').hidden=image;$('imageCanvas').hidden=!image;
 for(const el of document.querySelectorAll('[name=creationKind]'))el.checked=el.value===creationKind;
 for(const el of document.querySelectorAll('[name=imageAspect]'))el.checked=el.value===imageAspect;
 const hint=$('qualityHint');if(hint)hint.textContent=image?'Carbon 扩写画面描述，H3-Image 生成图片。首次准备模型需要较长时间。':modelMode==='bf16'?'原生 768p，可在创意里写明画幅、对白和声音。':'默认短边 768 像素。可在创意里写「竖屏」「快速预览」或「保留原声」。';
 $('modelWorker').hidden=role!=='owner';$('modelWorker').href=modelMode==='bf16'?'colab-worker-bf16.ipynb':'colab-worker.ipynb';
 for(const el of document.querySelectorAll('[name=modelPrecision]'))el.checked=el.value===modelMode;
 $('modelNote').textContent=(modelMode==='bf16'?'使用官方原始权重，耗时较长。':'量化权重，出片较快。')+(onlineModes&&!onlineModes.includes(modelMode)?'对应工作端离线，提交后后台会申请 GPU。':'');
}
document.querySelectorAll('[name=modelPrecision]').forEach(el=>el.onchange=()=>{modelMode=el.value;paintMode();newInput()});
document.querySelectorAll('[name=creationKind]').forEach(el=>el.onchange=()=>{creationKind=el.value;parentId='';paintMode();paintReply();newInput()});
document.querySelectorAll('[name=imageAspect]').forEach(el=>el.onchange=()=>{imageAspect=el.value;newInput()});
function durations(){$('duration').value=duration;$('durationText').textContent=duration+' 秒';$('duration').setAttribute('aria-valuetext',duration+' 秒')}
function renderShots(j){
 const c=$('shots');c.replaceChildren();const view=j?.shots||[];const writing=j?.state==='planning',image=j?j.kind==='image':creationKind==='image';
 $('boardHeading').textContent=image?'画面设计':'分镜脚本';
 $('shotLabel').textContent=writing?(image?'Carbon 构思中':'导演编排中'):view.length?(image?'画面描述已就绪':view.length+' 段 · '+j.seconds+' 秒'):'等待创意';
 $('shotHint').textContent=j?.state==='failed'?j.message:writing?(image?'正在扩写构图、光线、材质和细节。':'写好的内容会逐段出现。'):view.length?(image?'画面设计与完整生图提示词已保存。':'分镜已保存，可在任务记录中查看。'):(image?'提交简短创意，AI 会补充完整画面描述。':'提交后，分镜会在这里逐段出现。');
 const cuts=view.filter((shot,i)=>i>0&&shot.cut===true).length;
 if(!writing&&['queued','running'].includes(j?.state)&&j.route!=='continuous'&&view.length>=3&&cuts){
   $('shotHint').textContent='有 '+cuts+' 个独立切镜可提前生成，空闲显卡会自动接手。';
 }
 if(j?.state==='running'&&j.sampling?.length){const s=j.sampling[0];$('shotHint').textContent='第 '+s.shot+' 镜 · 最近实测 '+s.done+'/'+s.total+' 步。各镜的速度与预计耗时见下方任务记录。'}
 for(const [i,shot] of view.entries()){const n=tx('div','','shot'),box=tx('div','');box.append(tx('label',image?'画面描述':'分镜 '+(i+1)+(shot.seconds?' · '+shot.seconds+' 秒':'')),tx('p',shot.prompt));if(image&&shot.h3_prompt){const details=document.createElement('details');details.append(tx('summary','查看完整生图提示词'),tx('p',shot.h3_prompt));box.append(details)}n.append(tx('span',String(i+1).padStart(2,'0')),box);c.append(n)}
}
async function watchJob(j){
 const ticket=++watchToken;activeJob=j.id;rememberJobs([j]);renderShots(j);
 while(['planning','queued','running'].includes(j.state)&&ticket===watchToken){
  await new Promise(r=>setTimeout(r,j.state==='planning'?650:4000));if(ticket!==watchToken)return;
  const tok=j.watch_token||jobToken(j.id);
  try{j=(await api('creation/'+encodeURIComponent(j.id)+(tok?'?token='+encodeURIComponent(tok):''))).job;rememberJobs([j])}catch{if(ticket===watchToken)$('workMsg').textContent='任务已提交，正在重新连接…';continue}
  if(ticket!==watchToken)return;renderShots(j);$('workMsg').textContent=j.message;
 }
 if(ticket===watchToken)await refresh();
}
function changed(){requestId=null;version++;$('count').textContent=$('prompt').value.length+' / 4000';$('draftStatus').textContent='正在保存…';clearTimeout(timer);timer=setTimeout(save,550)}
function save(){if(!logged)return;const v=version,data={kind:creationKind,aspect:imageAspect,precision:modelMode,parent:parentId,prompt:$('prompt').value,seconds:duration,shots:[]};saveChain=saveChain.catch(()=>{}).then(async()=>{if(v<version)return;try{if(role==='owner'){await api('draft',data)}else{localWrite('draft',data)};if(v===version)$('draftStatus').textContent='草稿已保存'}catch(e){$('draftStatus').textContent='保存失败 · 请检查网络'}});return saveChain}
function paintPrompt(d){watchToken++;creationKind=d?.kind==='image'?'image':'video';imageAspect=['square','landscape','portrait'].includes(d?.aspect)?d.aspect:'square';modelMode=d?.precision==='bf16'?'bf16':'quantized';paintMode();parentId=d?.parent||'';paintReply();$('prompt').value=d?.brief||d?.prompt||'';duration=Math.max(15,Math.min(120,Number(d?.seconds)||15));$('count').textContent=$('prompt').value.length+' / 4000';durations();renderShots(d?.shots?.length?d:null)}
const states={planning:'AI 构思中',local:'本机方案',queued:'等待 GPU',running:'生成中',done:'已完成',failed:'未完成',cancelled:'已取消'};
function detailDate(ts){return new Intl.DateTimeFormat('zh-CN',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false,timeZone:'Asia/Shanghai'}).format(new Date(ts*1000))}
let serverClockOffset=0;
function ageLabel(n){n=Math.max(0,Math.floor(n));return n<60?n+' 秒':Math.floor(n/60)+' 分 '+(n%60)+' 秒'}
function runningDetail(j,mid){
 const now=Math.floor(Date.now()/1000)+serverClockOffset,age=Math.max(0,now-j.updated);
 const samples=j.sampling||[],m=/^生成第 (\d+)\/(\d+) 镜$/.exec(j.message||'');
 for(const s of samples){
  const stale=now-s.seen>80, title='第 '+s.shot+'/'+j.shots.length+' 镜 · 采样已完成 '+s.done+'/'+s.total+' 步';
  const box=tx('div','','sampler'),bar=document.createElement('progress');bar.max=s.total;bar.value=s.done;bar.setAttribute('aria-label',title);
  box.append(tx('b',title),bar);
  const fmt=n=>n>=60?'约 '+Math.round(n/60)+' 分钟':'约 '+Math.max(1,Math.round(n))+' 秒';
  const info=stale?'等待 GPU 更新，以上为上次实测':s.done===s.total?'采样完成，等待解码':s.sec_per_step?'每步约 '+Math.round(s.sec_per_step*10)/10+' 秒 · 本镜采样还需'+fmt((s.total-s.done)*s.sec_per_step):'正在测量每步耗时';
  box.append(tx('small',info));mid.append(box)
 }
 if(m&&!samples.some(s=>s.shot===Number(m[1])))mid.append(tx('small',age>100?'暂时没有新的 GPU 回报，正在等待执行端恢复。':'第 '+m[1]+'/'+m[2]+' 镜渲染中，等待采样步数回报。'));
 mid.append(tx('small','提交后 '+ageLabel(now-j.created)+' · '+(age>100?'暂未收到新回报，上次更新在 '+ageLabel(age)+'前':'GPU 最近回报：'+ageLabel(age)+'前')));
}
function renderJobs(){
 const c=$('jobs');c.replaceChildren();const seen=jobs.filter(j=>filter==='all'||(filter==='active'?['planning','queued','running'].includes(j.state):j.state==='done'));
 $('jobCount').textContent=jobs.length+' 个任务';
 if(!seen.length){c.append(tx('div',filter==='all'?'还没有任务。写下第一个创意，作品会出现在这里。':'当前筛选下没有任务。','empty'));return}
 for(const j of seen){
  const row=tx('div','','job'),left=tx('span',states[j.state]||j.state,'pill'),mid=tx('div',''),right=tx('div','');
  const title=(j.parent?'↳ ':'')+(j.brief||j.prompt);mid.append(tx('strong',title.length>90?title.slice(0,90)+'…':title));
  mid.append(tx('small',[detailDate(j.created),modeLabel(j.precision),j.kind==='image'?(j.shots[0]?.width?j.shots[0].width+' × '+j.shots[0].height:'约 400 万像素'):(j.total_seconds||j.seconds)+' 秒',j.quality==='hd'?'高清 768p':j.quality==='fast'?'快速预览':'',j.kind==='image'?'PNG':j.shots.length+' 镜',j.message].filter(Boolean).join(' · ')));
  if(j.state==='running')runningDetail(j,mid);
  if(j.state==='done'&&j.media_ready){const pb=tx('button',j.kind==='image'?'查看图片':j.preview_status==='ready'?'播放预览':'查看成片');pb.onclick=()=>openPreview(j);right.append(pb);
   if(j.kind!=='image'){const f=tx('button','接着续拍');f.onclick=()=>{if($('prompt').value.trim()&&$('prompt').value!==j.brief&&!confirm('从这条成片的结尾续拍？'))return;watchToken++;creationKind='video';modelMode=j.precision==='bf16'?'bf16':'quantized';paintMode();parentId=j.id;$('prompt').value='';shots=[];paintReply();changed();renderShots();$('workMsg').textContent='写下接下来发生什么，所选时长是新增长度。';$('prompt').focus()};right.append(f)}
  }else if(j.state==='done')right.append(tx('small','作品同步中','muted'));
  if(j.state==='queued'){const b=tx('button','取消排队');b.onclick=async()=>{try{const tok=j.watch_token||jobToken(j.id);await api('cancel',{id:j.id,...(tok?{watch_token:tok}:{})});await refresh()}catch(e){$('workMsg').textContent=e.message}};right.append(b)}
  const inspect=tx('button','查看描述');inspect.onclick=()=>{renderShots(j);$('boardHeading').scrollIntoView({behavior:'smooth',block:'center'})};right.append(inspect);
  const tok=j.watch_token||jobToken(j.id);
  if(tok){const lk=tx('button','追踪链接');lk.onclick=async()=>{const url=location.origin+location.pathname+'#job='+j.id+'.'+tok;try{await navigator.clipboard.writeText(url);lk.textContent='已复制';setTimeout(()=>lk.textContent='追踪链接',1500)}catch{$('workMsg').textContent='追踪链接：'+url}};right.append(lk)}
  row.append(left,mid,right);c.append(row)
 }
}
async function recoverGuestJobs(current){
 const known=new Set(current.map(j=>j.id));
 const missing=localRead('tokens',[]).filter(x=>x?.id&&x?.token&&!known.has(x.id)).slice(-6);
 if(!missing.length)return current;
 const found=await Promise.all(missing.map(async x=>{
  try{return (await api('creation/'+encodeURIComponent(x.id)+'?token='+encodeURIComponent(x.token))).job}
  catch(e){if(String(e?.message||'').includes('没有此任务'))forgetJobToken(x.id);return null}
 }));
 const merged=[...current,...found.filter(Boolean)];
 merged.sort((a,b)=>(b.created||0)-(a.created||0));
 return merged;
}
async function refresh(first=false){
 try{const d=await api('state'),prev=role;if(Number.isFinite(d.now))serverClockOffset=d.now-Math.floor(Date.now()/1000);showRole(d.role||'guest');const owner=role==='owner';rememberJobs(d.jobs||[]);jobs=owner?(d.jobs||[]):await recoverGuestJobs(d.jobs||[]);rememberJobs(jobs);const w=d.worker||{};onlineModes=d.online_modes||null;paintMode();$('gpu').textContent=w.online?w.gpu:(w.loading_count?'云端 GPU 准备中':w.requesting_count?'正在申请云端 GPU':w.runtime_limit?'等待 GPU 名额':'云端 GPU 待机');$('workerSeen').textContent=workerLabel(w);if(first||prev!==role){const local=localRead('draft',null),draft=owner?(d.draft||(local?{...local,parent:''}:null)):local;paintPrompt(draft);$('draftStatus').textContent=draft?'草稿已恢复':'自动保存草稿';if(owner&&!d.draft&&draft)changed();const current=jobs.find(j=>['planning','queued','running'].includes(j.state));if(current)watchJob(current)}renderJobs();if(previewJob){const current=jobs.find(j=>j.id===previewJob);if(current)renderPreview(current)}if(first)$('submit').disabled=false}catch(e){$('workerSeen').textContent='网络中断，正在重试'}
}
function newInput(){++watchToken;activeJob='';shots=[];$('workMsg').textContent='';renderShots();changed()}
$('prompt').oninput=newInput;
$('duration').oninput=()=>{duration=Number($('duration').value);durations();newInput()};
$('tabs').onclick=e=>{const b=e.target.closest('[data-filter]');if(!b)return;filter=b.dataset.filter;for(const x of $('tabs').children)x.setAttribute('aria-pressed',x===b);renderJobs()};
$('submit').onclick=async()=>{
 const b=$('submit'),prompt=$('prompt').value.trim();if(!prompt){$('workMsg').textContent='先写下一段创意。';$('prompt').focus();return}
 const v=version;b.disabled=true;requestId=requestId||crypto.randomUUID();try{
  const r=await api('create',{prompt,kind:creationKind,aspect:imageAspect,precision:modelMode,seconds:duration,parent:parentId,request_id:requestId});if(v!==version){await refresh();return}
  requestId=null;rememberJobs([r.job]);$('workMsg').textContent=r.job.message;watchJob(r.job);await refresh();
 }catch(e){$('workMsg').textContent=e.message}finally{b.disabled=false}
};
async function boot(){const hash=new URLSearchParams(location.hash.slice(1));const token=hash.get('activate'),shared=hash.get('job');if(token||shared)history.replaceState(null,'',location.pathname+location.search);if(token){try{await api('activate',{token});$('activationMsg').textContent='此浏览器已连接到你的 GPU 工作台。'}catch(e){$('activationMsg').textContent=e.message}}if(shared){const [jid,jtok]=shared.split('.');if(/^[a-f0-9]{32}$/.test(jid||'')&&/^[a-f0-9]{32}$/.test(jtok||'')){rememberJobs([{id:jid,watch_token:jtok}]);$('activationMsg').textContent='已载入任务追踪凭证，正在同步进度…'}}await refresh(true)}
window.addEventListener('hashchange',()=>{if(location.hash.startsWith('#activate=')||location.hash.startsWith('#job='))boot()});boot();setInterval(()=>{if(!document.hidden)refresh(!logged)},5000);


let previewJob='',stopPlaybackCheck=()=>{};
function mediaUrl(j,preview=false){const tok=j.watch_token||jobToken(j.id);return 'api/'+(preview?'preview/':'media/')+encodeURIComponent(j.id)+(tok?'?token='+encodeURIComponent(tok):'')}
function renderPreview(j){
 const image=j.kind==='image',frame=$('frame'),state=image?'ready':j.preview_status||'pending',key=j.id+':'+state;if(frame.dataset.preview===key)return;frame.dataset.preview=key;
 stopPlaybackCheck();$('retryPreview').hidden=true;const url=mediaUrl(j);$('downloadFilm').href=url+(url.includes('?')?'&':'?')+'download=1';$('downloadFilm').download='映室-'+j.id.slice(0,8)+(image?'.png':'.mp4');
 $('previewHeading').textContent=image?'图片预览':'成片预览';$('downloadFilm').textContent=image?'下载 PNG':'下载高清原片';
 if(image){const img=document.createElement('img');img.src=url;img.alt=j.brief||'生成图片';img.onerror=()=>{$('previewStatus').textContent='图片加载失败，请关闭后重试或下载原图。'};frame.replaceChildren(img);$('previewStatus').textContent='站内私有预览 · 原始 PNG';return}
 $('previewStatus').textContent=state==='ready'?'轻量预览 · 下载可获取高清原片。':state==='failed'?'预览暂不可用，后台会重试；高清原片可直接下载。':'预览版准备中，完成后即可播放；高清原片已保存，可直接下载。';
 frame.replaceChildren();if(state!=='ready')return;
 const v=document.createElement('video');v.src=mediaUrl(j,true);v.controls=true;v.playsInline=true;v.preload='metadata';v.setAttribute('aria-label',j.brief||'成片预览');
 v.onwaiting=()=>{$('previewStatus').textContent='正在缓冲视频…';$('retryPreview').hidden=false};
 v.onplaying=()=>{$('previewStatus').textContent='轻量预览 · 下载可获取高清原片。';$('retryPreview').hidden=true};
 v.onerror=()=>{$('previewStatus').textContent='预览加载失败，可重试或下载高清原片。';$('retryPreview').hidden=false};
 frame.append(v);stopPlaybackCheck=checkPlayback(v,j);playPreview(v);

}
function openPreview(j){if(!j.media_ready||!j.id)return;previewJob=j.id;$('preview').showModal();renderPreview(j)}
$('closePreview').onclick=()=>$('preview').close();$('preview').onclose=()=>{stopPlaybackCheck();previewJob='';const v=$('frame').querySelector('video');if(v){v.pause();v.removeAttribute('src');v.load()}$('frame').replaceChildren();delete $('frame').dataset.preview};

function playPreview(v){
 v.play().catch(e=>{if(!$('frame').contains(v))return;v.dataset.playError=e.name;$('previewStatus').textContent=e.name==='NotAllowedError'?'浏览器需要再次确认，请点「播放／重试」。':'视频尚未开始播放，请重试或下载高清原片。';$('retryPreview').hidden=false});
}
$('retryPreview').onclick=()=>{const v=$('frame').querySelector('video');if(v){if(v.error)v.load();playPreview(v)}};
// Temporary playback-check instrumentation: only active through an explicit diagnostic link.
function checkPlayback(v,j){
 const run=new URLSearchParams(location.search).get('playback_check');if(!/^[a-f0-9]{16}$/.test(run||''))return ()=>{};
 const start=performance.now(),events=[],controller=new AbortController();let active=true,probeTimer;
 for(const event of ['loadedmetadata','loadeddata','playing','waiting','stalled','error'])v.addEventListener(event,()=>{events.push([event,Math.round(v.currentTime*10)/10,v.readyState]);if(events.length>12)events.shift()});
 const timer=setTimeout(async()=>{
  let bytes=0,status=0,probeError='',total=0;const began=performance.now();
  probeTimer=setTimeout(()=>controller.abort(),12000);
  try{
   const res=await fetch(mediaUrl(j,true),{headers:{Range:'bytes=0-65535'},cache:'no-store',signal:controller.signal});status=res.status;
   total=Number((res.headers.get('Content-Range')||'').split('/')[1])||0;
   if(res.ok){const reader=res.body.getReader();while(true){const chunk=await reader.read();if(chunk.done)break;bytes+=chunk.value.length}}
  }catch(e){probeError=e.name}finally{clearTimeout(probeTimer)}
  if(!active)return;
  const report={run,elapsed:Math.round(performance.now()-start),seconds:Math.round(v.currentTime*10)/10,ready:v.readyState,network:v.networkState,paused:v.paused,error:v.error?.code||0,playError:v.dataset.playError||'',buffered:v.buffered.length?v.buffered.end(v.buffered.length-1):0,status,bytes,total,bps:Math.round(bytes/((performance.now()-began)/1000)),probeError,events};
  try{const res=await fetch('api/health?playback_check='+encodeURIComponent(btoa(JSON.stringify(report))),{cache:'no-store'});if(active&&res.ok)$('previewStatus').textContent+=' 检测结果已送达。'}catch{if(active)$('previewStatus').textContent+=' 检测结果发送失败。'}
 },10000);
 return ()=>{active=false;clearTimeout(timer);clearTimeout(probeTimer);controller.abort()};
}


function paintReply(){$('lengthLabel').textContent=parentId?'新增时长':'成片时长';const j=jobs.find(x=>x.id===parentId);$('reply').hidden=!parentId;$('replyText').textContent=parentId?'从原片结尾续拍 · '+((j?.brief||j?.prompt||'已保存的创意').slice(0,38)):'';$('prompt').placeholder=creationKind==='image'?'比如：清晨山间的一间茶馆，暖光透过木窗，门口招牌写着“山间茶事”。电影摄影质感。':parentId?'写接下来发生什么，例如：镜头靠近，两人听着雨声一起笑。':'比如：大雨敲着窗，家里的橘猫和雪纳瑞围着仙女棒聊天。让我们听见雨声和它们的对白。'}
$('newIdea').onclick=()=>{parentId='';paintReply();changed()};


