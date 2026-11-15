'use strict';
const $=id=>document.getElementById(id), tx=(tag,text,cls)=>{const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n};
let role='guest',parentId="",duration=15,shots=[],jobs=[],filter='all',version=0,saving=0,timer,saveChain=Promise.resolve(),logged=false,requestId=null;
async function api(p,body){
 const res=await fetch('api/'+p,{credentials:'same-origin',cache:'no-store',method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
 const data=await res.json();if(!res.ok){throw new Error(data.error||'请求失败')}return data;
}
function localRead(k,fallback){try{return JSON.parse(localStorage.getItem('frame-room:'+k))??fallback}catch{return fallback}}
function localWrite(k,v){localStorage.setItem('frame-room:'+k,JSON.stringify(v))}
function showRole(value){role=value;logged=true;}
let activeJob='',watchToken=0;
let modelMode='quantized',onlineModes=null;
const modeLabel=v=>v==='bf16'?'原始 BF16':'量化版';
function paintMode(){
 const hint=$('qualityHint');if(hint)hint.textContent=modelMode==='bf16'?'原生 768p，可在创意里写明画幅、对白和声音。':'默认短边 768 像素。可在创意里写「竖屏」「快速预览」或「保留原声」。';
 $('modelWorker').hidden=role!=='owner';$('modelWorker').href=modelMode==='bf16'?'colab-worker-bf16.ipynb':'colab-worker.ipynb';
 for(const el of document.querySelectorAll('[name=modelPrecision]'))el.checked=el.value===modelMode;
 $('modelNote').textContent=(modelMode==='bf16'?'使用官方原始权重，耗时较长。':'量化权重，出片较快。')+(onlineModes&&!onlineModes.includes(modelMode)?'对应工作端离线，提交后后台会申请 GPU。':'');
}
document.querySelectorAll('[name=modelPrecision]').forEach(el=>el.onchange=()=>{modelMode=el.value;paintMode();newInput()});
function durations(){$('duration').value=duration;$('durationText').textContent=duration+' 秒';$('duration').setAttribute('aria-valuetext',duration+' 秒')}
function renderShots(j){
 const c=$('shots');c.replaceChildren();const view=j?.shots||[];const writing=j?.state==='planning';
 $('shotLabel').textContent=writing?'导演编排中':view.length?view.length+' 段 · '+j.seconds+' 秒':'等待创意';
 $('shotHint').textContent=j?.state==='failed'?j.message:writing?'写好的内容会逐段出现。':view.length?'分镜已保存，可在任务记录中查看。':'提交后，分镜会在这里逐段出现。';
 const cuts=view.filter((shot,i)=>i>0&&shot.cut===true).length;
 if(!writing&&['queued','running'].includes(j?.state)&&j.route!=='continuous'&&view.length>=3&&cuts){
   $('shotHint').textContent='有 '+cuts+' 个独立切镜可提前生成，空闲显卡会自动接手。';
 }
 if(j?.state==='running'&&j.sampling?.length){const s=j.sampling[0];$('shotHint').textContent='第 '+s.shot+' 镜 · 最近实测 '+s.done+'/'+s.total+' 步。各镜的速度与预计耗时见下方任务记录。'}
 for(const [i,shot] of view.entries()){const n=tx('div','','shot'),box=tx('div','');box.append(tx('label','分镜 '+(i+1)+(shot.seconds?' · '+shot.seconds+' 秒':'')),tx('p',shot.prompt));n.append(tx('span',String(i+1).padStart(2,'0')),box);c.append(n)}
}
async function watchJob(j){
 const ticket=++watchToken;activeJob=j.id;renderShots(j);
 while(['planning','queued','running'].includes(j.state)&&ticket===watchToken){
  await new Promise(r=>setTimeout(r,j.state==='planning'?650:4000));if(ticket!==watchToken)return;
  try{j=(await api('creation/'+encodeURIComponent(j.id))).job}catch{if(ticket===watchToken)$('workMsg').textContent='任务已提交，正在重新连接…';continue}
  if(ticket!==watchToken)return;renderShots(j);$('workMsg').textContent=j.message;
 }
 if(ticket===watchToken)await refresh();
}
function changed(){requestId=null;version++;$('count').textContent=$('prompt').value.length+' / 4000';$('draftStatus').textContent='正在保存…';clearTimeout(timer);timer=setTimeout(save,550)}
function save(){if(!logged)return;const v=version,data={precision:modelMode,parent:parentId,prompt:$('prompt').value,seconds:duration,shots:[]};saveChain=saveChain.catch(()=>{}).then(async()=>{if(v<version)return;try{if(role==='owner'){await api('draft',data)}else{localWrite('draft',data)};if(v===version)$('draftStatus').textContent='草稿已保存'}catch(e){$('draftStatus').textContent='保存失败 · 请检查网络'}});return saveChain}
function paintPrompt(d){watchToken++;modelMode=d?.precision==='bf16'?'bf16':'quantized';paintMode();parentId=d?.parent||'';paintReply();$('prompt').value=d?.brief||d?.prompt||'';duration=Math.max(15,Math.min(120,Number(d?.seconds)||15));$('count').textContent=$('prompt').value.length+' / 4000';durations();renderShots(d?.shots?.length?d:null)}
const states={planning:'写分镜',local:'本机方案',queued:'等待 GPU',running:'生成中',done:'已完成',failed:'未完成',cancelled:'已取消'};
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
 if(!seen.length){c.append(tx('div',filter==='all'?'还没有任务。写下第一个镜头，成片会出现在这里。':'当前筛选下没有任务。','empty'));return}
 for(const j of seen){
  const row=tx('div','','job'),left=tx('span',states[j.state]||j.state,'pill'),mid=tx('div',''),right=tx('div','');
  const title=(j.parent?'↳ ':'')+(j.brief||j.prompt);mid.append(tx('strong',title.length>90?title.slice(0,90)+'…':title));
  mid.append(tx('small',[detailDate(j.created),modeLabel(j.precision),(j.total_seconds||j.seconds)+' 秒',j.quality==='hd'?'高清 768p':j.quality==='fast'?'快速预览':'',j.shots.length+' 镜',j.message].filter(Boolean).join(' · ')));
  if(j.state==='running')runningDetail(j,mid);
  if(j.state==='done'&&j.media_ready){const pb=tx('button','播放成片');pb.onclick=()=>openPreview(j);right.append(pb);
   const f=tx('button','接着续拍');f.onclick=()=>{if($('prompt').value.trim()&&$('prompt').value!==j.brief&&!confirm('从这条成片的结尾续拍？'))return;watchToken++;modelMode=j.precision==='bf16'?'bf16':'quantized';paintMode();parentId=j.id;$('prompt').value='';shots=[];paintReply();changed();renderShots();$('workMsg').textContent='写下接下来发生什么，所选时长是新增长度。';$('prompt').focus()};right.append(f)
  }else if(j.state==='done')right.append(tx('small','成片同步中','muted'));
  if(j.state==='queued'){const b=tx('button','取消排队');b.onclick=async()=>{try{await api('cancel',{id:j.id});await refresh()}catch(e){$('workMsg').textContent=e.message}};right.append(b)}
  row.append(left,mid,right);c.append(row)
 }
}
async function refresh(first=false){
 try{const d=await api('state'),prev=role;if(Number.isFinite(d.now))serverClockOffset=d.now-Math.floor(Date.now()/1000);showRole(d.role||'guest');const owner=role==='owner';jobs=d.jobs||[];const w=d.worker||{};onlineModes=d.online_modes||null;paintMode();$('gpu').textContent=w.online?w.gpu:'云端 GPU 离线';$('workerSeen').textContent=w.online?'● 云端 GPU 在线':'有任务时后台按需申请 GPU';if(first||prev!==role){const local=localRead('draft',null),draft=owner?(d.draft||(local?{...local,parent:''}:null)):local;paintPrompt(draft);$('draftStatus').textContent=draft?'草稿已恢复':'自动保存草稿';if(owner&&!d.draft&&draft)changed();const current=jobs.find(j=>j.state==='planning');if(current)watchJob(current)}renderJobs();if(first)$('submit').disabled=false}catch(e){$('workerSeen').textContent='网络中断，正在重试'}
}
function newInput(){++watchToken;activeJob='';shots=[];$('workMsg').textContent='';renderShots();changed()}
$('prompt').oninput=newInput;
$('duration').oninput=()=>{duration=Number($('duration').value);durations();newInput()};
$('tabs').onclick=e=>{const b=e.target.closest('[data-filter]');if(!b)return;filter=b.dataset.filter;for(const x of $('tabs').children)x.setAttribute('aria-pressed',x===b);renderJobs()};
$('submit').onclick=async()=>{
 const b=$('submit'),prompt=$('prompt').value.trim();if(!prompt){$('workMsg').textContent='先写下一段创意。';$('prompt').focus();return}
 const v=version;b.disabled=true;requestId=requestId||crypto.randomUUID();try{
  const r=await api('create',{prompt,precision:modelMode,seconds:duration,parent:parentId,request_id:requestId});if(v!==version){await refresh();return}
  requestId=null;$('workMsg').textContent=r.job.message;watchJob(r.job);await refresh();
 }catch(e){$('workMsg').textContent=e.message}finally{b.disabled=false}
};
async function boot(){const hash=new URLSearchParams(location.hash.slice(1));const token=hash.get('activate');if(token){history.replaceState(null,'',location.pathname+location.search);try{await api('activate',{token});$('activationMsg').textContent='此浏览器已连接到你的 GPU 工作台。'}catch(e){$('activationMsg').textContent=e.message}}await refresh(true)}
window.addEventListener('hashchange',()=>{if(location.hash.startsWith('#activate='))boot()});boot();setInterval(()=>{if(!document.hidden)refresh(!logged)},5000);


function openPreview(j){if(!j.media_ready||!j.id)return;const v=document.createElement('video');v.src='api/media/'+encodeURIComponent(j.id);v.controls=true;v.playsInline=true;v.preload='metadata';v.setAttribute('aria-label',j.brief||'成片预览');$('frame').replaceChildren(v);$('downloadFilm').href=v.src;$('downloadFilm').download='映室-'+j.id.slice(0,8)+'.mp4';$('preview').showModal();v.play().catch(()=>{})}
$('closePreview').onclick=()=>$('preview').close();$('preview').onclose=()=>$('frame').replaceChildren();

function paintReply(){$('lengthLabel').textContent=parentId?'新增时长':'成片时长';const j=jobs.find(x=>x.id===parentId);$('reply').hidden=!parentId;$('replyText').textContent=parentId?'从原片结尾续拍 · '+((j?.brief||j?.prompt||'已保存的创意').slice(0,38)):'';$('prompt').placeholder=parentId?'写接下来发生什么，例如：镜头靠近，两人听着雨声一起笑。':'比如：大雨敲着窗，家里的橘猫和雪纳瑞围着仙女棒聊天。让我们听见雨声和它们的对白。'}
$('newIdea').onclick=()=>{parentId='';paintReply();changed()};


