const q=s=>document.querySelector(s), qa=s=>[...document.querySelectorAll(s)];
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const accountToken=q('meta[name="account-token"]')?.content||'';
let latestStatus={}, latestScreenUrl='', screenLoadedAt=0, logPaused=false;
let history=[];

function fmt(sec){sec=Math.max(0,Number(sec||0));if(sec<60)return `${Math.floor(sec)}s`;const m=Math.floor(sec/60);if(m<60)return `${m}m ${Math.floor(sec%60)}s`;const h=Math.floor(m/60);return `${h}h ${m%60}m`;}
function set(id,v){const e=q(id);if(e)e.textContent=v;}
function toast(msg,type='ok'){const e=document.createElement('div');e.className=`toast ${type}`;e.textContent=msg;q('#toastStack').appendChild(e);setTimeout(()=>e.remove(),4200);}
async function req(url,opt={}){const r=await fetch(url,{cache:'no-store',...opt});const raw=await r.text();let x={};try{x=raw?JSON.parse(raw):{};}catch{}if(!r.ok)throw new Error(x.error||raw||`HTTP ${r.status}`);return x;}
async function post(url,body={}){return req(url,{method:'POST',headers:{'Content-Type':'application/json','X-Account-Token':accountToken},body:JSON.stringify(body)});}
function collectConfig(){return{
 slot:+q('#slot').value,marches_to_send:+q('#marchesCfg').value,auto_fill_all_marches:q('#autoFillMarches').checked,
 gather_radius:+q('#radius').value,search_mode:q('#searchMode').value,
 builtin_gem_finder:q('#builtinGemFinder').checked,gem_selector_strategy:q('#gemSelectorStrategy').value,
 castle_hold_ms:+q('#castleHoldMs').value,tree_hold_ms:+q('#treeHoldMs').value,detector_mode:q('#detectorMode').value,
 temporal_confirmation:q('#temporalConfirm').checked,ai_verify_borderline:q('#aiVerifyBorderline').checked,
 turbo_dispatch:q('#turboDispatch').checked,
 reliable_press_hold:q('#reliablePressHold').checked,press_hold_ms:+q('#pressHoldMs').value,
 occupied_cooldown_seconds:+q('#occupiedCooldown').value,ai_fallback:q('#aiFallback').checked,
 debug_capture_failures:q('#debugCapture').checked,loadout_color:q('#loadout').value,
 daily_runtime_limit:+q('#runtimeCfg').value,retry_seconds:+q('#retry').value,bring_marches_home:q('#bringHome').checked
};}
async function saveConfig(){const x=await post('/api/config',collectConfig());toast('Configuration saved');return x;}
async function startBot(){try{await saveConfig();await post('/api/bot/start');toast('Gem mission started');}catch(e){toast(e.message,'error')}}
async function stopBot(){try{await post('/api/bot/stop');toast('Gem mission stopped');}catch(e){toast(e.message,'error')}}
async function startEmu(){try{await saveConfig();await post('/api/emulator/start');toast('RoK launch requested');}catch(e){toast(e.message,'error')}}
async function restartEmu(){try{await saveConfig();await post('/api/emulator/restart');toast('LDPlayer restarting');}catch(e){toast(e.message,'error')}}
async function checkConnection(){try{await saveConfig();const x=await post('/api/emulator/gpu-repair');toast(`LDPlayer verified: ${x.serial}`);}catch(e){toast(e.message,'error')}}
async function selfTest(){try{toast('Running GemOps self-test…');const x=await post('/api/gemops/self-test');toast(x.ok?'GemOps self-test passed':'Self-test failed',x.ok?'ok':'error');if(x.output)console.log(x.output);}catch(e){toast(e.message,'error')}}

const pageTitles={overview:'Mission Overview',live:'Live Operations',intel:'Gem detection',performance:'Performance',companion:'Companion Runtime',settings:'Settings',account:'Account Center',terminal:'Event Console'};
function showPage(name){window.scrollTo(0,0);qa('.nav').forEach(x=>x.classList.toggle('active',x.dataset.page===name));qa('.page').forEach(x=>x.classList.toggle('active',x.dataset.panel===name));set('#pageTitle',pageTitles[name]||'GemOps');if(name==='live')syncLiveClone();}
qa('.nav').forEach(x=>x.onclick=()=>showPage(x.dataset.page));
q('#quickConfig').onclick=()=>showPage('settings');

['#startBot','#liveStartBot'].forEach(s=>{const e=q(s);if(e)e.onclick=startBot});
['#stopBot','#liveStopBot'].forEach(s=>{const e=q(s);if(e)e.onclick=stopBot});
['#startEmu','#liveStartEmu'].forEach(s=>{const e=q(s);if(e)e.onclick=startEmu});
['#restartEmu','#liveRestart'].forEach(s=>{const e=q(s);if(e)e.onclick=restartEmu});
['#gpuRepair','#liveCheck'].forEach(s=>{const e=q(s);if(e)e.onclick=checkConnection});
q('#selfTest').onclick=selfTest;q('#saveConfig').onclick=saveConfig;
['#refreshScreen','#liveRefresh'].forEach(s=>{const e=q(s);if(e)e.onclick=screen});
q('#refreshDiagnostics').onclick=diagnostics;

const presets={
 accuracy:{searchMode:'smart',detectorMode:'strict',temporalConfirm:true,aiVerifyBorderline:true,turboDispatch:true},
 throughput:{searchMode:'fast',detectorMode:'strict',temporalConfirm:true,aiVerifyBorderline:true,turboDispatch:true},
 wide:{searchMode:'wide',detectorMode:'strict',temporalConfirm:true,aiVerifyBorderline:true,turboDispatch:true},
 eco:{searchMode:'eco',detectorMode:'strict',temporalConfirm:true,aiVerifyBorderline:false,turboDispatch:true}
};
qa('.presets button').forEach(b=>b.onclick=()=>{qa('.presets button').forEach(x=>x.classList.remove('active'));b.classList.add('active');const p=presets[b.dataset.preset];q('#searchMode').value=p.searchMode;q('#detectorMode').value=p.detectorMode;q('#temporalConfirm').checked=p.temporalConfirm;q('#aiVerifyBorderline').checked=p.aiVerifyBorderline;q('#turboDispatch').checked=p.turboDispatch;q('#builtinGemFinder').checked=true;q('#reliablePressHold').checked=true;q('#gemSelectorStrategy').value=b.dataset.preset==='throughput'?'cycle':'highest_first';toast(`${b.querySelector('b').textContent} preset loaded`);});

function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));}
function renderFeed(events){const box=q('#candidateFeed');if(!events?.length){box.innerHTML='<div class="feed-empty">Waiting for candidate telemetry…</div>';return;}box.innerHTML=[...events].reverse().map(e=>`<div class="feed-row ${esc(String(e.verdict||'').toLowerCase())}"><span class="feed-time">${esc(e.time||'')}</span><b>${esc(e.verdict||'EVENT')}</b><span class="feed-confidence">${Math.round(Number(e.confidence||0)*100)}%</span><span class="feed-reason">${esc(e.reason||'')}</span></div>`).join('');}
function setBar(id,val,max=1){const e=q(id);if(e)e.style.width=`${Math.max(0,Math.min(100,Number(val||0)/max*100))}%`;}
function pipeline(task){const t=String(task||'').toLowerCase();const map=[['#pipe-search',['find_gem','search','leave_city','gem_flow']],['#pipe-detect',['detect','candidate','validate']],['#pipe-gather',['gather']],['#pipe-troop',['new_troop','free_march','slot']],['#pipe-march',['march','dispatch']]];map.forEach(([id,keys])=>q(id)?.classList.toggle('active',keys.some(k=>t.includes(k))));}
function healthScore(x){let score=100;if(!x.companion?.passed)return 0;if(x.state==='error'||x.state==='blocked')score-=40;if(Number(x.stuck_seconds||0)>20)score-=20;if(Number(x.turbo_fallbacks||0)>5)score-=8;if(x.failure_code)score-=8;return Math.max(15,score);}
function updateHistory(x){const now=Date.now();history.push({t:now,m:Number(x.marches_sent||0),runtime:Number(x.runtime_seconds||0)});history=history.filter(p=>now-p.t<30*60*1000).slice(-90);drawCharts();}
function rateFromStatus(x){const h=Number(x.runtime_seconds||0)/3600;return h>0.02?Number(x.marches_sent||0)/h:0;}
function chartPoints(w,h){if(history.length<2)return '';const rates=history.map(p=>p.runtime>30?p.m/(p.runtime/3600):0);const max=Math.max(1,...rates);return rates.map((v,i)=>`${(i/(rates.length-1))*w},${h-(v/max)*(h-8)-4}`).join(' ');}
function drawCharts(){const s=chartPoints(500,90),b=chartPoints(800,240);if(q('#sparkLine'))q('#sparkLine').setAttribute('points',s);if(q('#bigChartLine'))q('#bigChartLine').setAttribute('points',b);if(s){const pts=s.split(' ');q('#sparkArea').setAttribute('d',`M0,90 L${pts.join(' L')} L500,90 Z`)}if(b){const pts=b.split(' ');q('#bigChartArea').setAttribute('d',`M0,240 L${pts.join(' L')} L800,240 Z`)}}
async function status(){
 try{
  const x=await req('/api/status');latestStatus=x;renderRuntime(x);
  const state=x.state||'stopped', task=x.task||'idle', rate=rateFromStatus(x);
  set('#systemState',state.toUpperCase());q('#systemDot').className=state==='running'?'running':'';
  set('#botState',state);set('#task',task);set('#marches',x.marches_sent||0);set('#freeGems',x.free_gems_found||0);set('#coverage',`${Number(x.coverage_percent||0).toFixed(1)}%`);set('#avgDispatch',x.avg_dispatch_seconds?`${Number(x.avg_dispatch_seconds).toFixed(2)}s`:'—');set('#falsePrevented',x.false_positive_prevented||0);set('#candidateRate',`${Number(x.candidate_success_rate||0).toFixed(1)}%`);set('#marchRate',`${rate.toFixed(1)} dispatch/hr`);set('#lastGemSmall',x.last_find_seconds?`last find ${x.last_find_seconds}s`:'last find —');set('#searchModeSmall',`${String(x.mode||'smart').toUpperCase()} search`);set('#detectorSmall',`${String(x.detector_mode||'strict').toUpperCase()} detector`);set('#runtime',fmt(x.runtime_seconds));set('#actions',x.actions||0);set('#aiCalls',x.ai_calls||0);set('#note',x.note||'—');set('#liveTask',task);
  const pill=q('#statePill');pill.className=`state-pill ${state}`;pill.querySelector('b').textContent=state.toUpperCase();
  const hs=healthScore(x);set('#healthScore',hs);set('#healthLabel',hs>=90?'NOMINAL':hs>=70?'WATCH':'RECOVERING');q('#healthRing').style.background=`conic-gradient(var(--g) ${hs*3.6}deg,#132832 0)`;
  pipeline(task);set('#throughputNow',`${rate.toFixed(1)}/hr`);set('#throughputTrend',rate>0?'live estimate':'warming up');
  set('#avgFind',x.avg_find_seconds?`${x.avg_find_seconds}s`:'—');set('#occupiedSkipped',x.occupied_skipped||0);set('#learnedScale',x.preferred_gem_scale?`${Number(x.preferred_gem_scale).toFixed(2)}×`:'learning');set('#builtinFinderState',x.builtin_gem_finder?'ON':'OFF');set('#selectorCount',x.last_selector_count||0);set('#uniqueViewsOverview',x.unique_views||0);set('#stuckRecoveriesOverview',x.stuck_recoveries||0);set('#turboSuccessOverview',x.turbo_dispatch_success||0);set('#failureCodeOverview',x.failure_code||'none');
  set('#liveOpsTask',task);set('#liveOpsState',state);set('#liveOpsRuntime',fmt(x.runtime_seconds));set('#liveOpsActions',x.actions||0);set('#liveAvgDispatch',x.avg_dispatch_seconds?`${Number(x.avg_dispatch_seconds).toFixed(2)}s`:'—');set('#liveTurboSuccess',x.turbo_dispatch_success||0);set('#liveTurboFallback',(x.turbo_fallbacks||0)+(x.turbo_slot_fallbacks||0));set('#liveMarches',x.marches_sent||0);
  set('#searchModeLive',String(x.mode||'smart').toUpperCase());set('#detectorModeLive',String(x.detector_mode||'strict').toUpperCase());set('#radarCoverage',`${Number(x.coverage_percent||0).toFixed(1)}%`);set('#coverageBig',`${Number(x.coverage_percent||0).toFixed(1)}%`);setBar('#coverageBar',x.coverage_percent,100);
  const rad=Math.max(.1,Number(x.search_radius_units||1)),sx=Number(x.search_x||0),sy=Number(x.search_y||0);set('#searchX',sx.toFixed(2));set('#searchY',sy.toFixed(2));set('#radiusUnits',rad.toFixed(2));const dot=q('#radarDot');if(dot){dot.style.left=`${Math.max(4,Math.min(96,50+(sx/rad)*46))}%`;dot.style.top=`${Math.max(4,Math.min(96,50-(sy/rad)*46))}%`;}
  set('#searchSwipes',x.search_swipes||0);set('#candidatesSeen',x.gem_candidates_seen||0);set('#candidateChecksLabel',`${x.candidate_checks||0} checked`);set('#fpBlockedBig',x.false_positive_prevented||0);set('#preventionRate',`${Number(x.prevention_rate||0).toFixed(1)}% prevention`);set('#temporalRejects',x.temporal_rejections||0);set('#aiCandidateVerify',x.ai_candidate_verifications||0);set('#aiCandidateRejected',`${x.ai_candidate_rejections||0} rejected`);set('#watchdog',Number(x.stuck_seconds||0)>20?'RECOVERING':'READY');set('#failureCode',x.failure_code||'no failures');
  const c=x.last_candidate||{};set('#candidateVerdict',(c.verdict||'WAITING').toUpperCase());q('#candidateVerdict').className=`verdict ${String(c.verdict||'').toLowerCase()}`;set('#candidateConfidence',`${Math.round(Number(c.confidence||0)*100)}%`);setBar('#candidateConfidenceBar',c.confidence);set('#sigTemplate',Number(c.template||0).toFixed(2));set('#sigHist',Number(c.hist||0).toFixed(2));set('#sigRed',Number(c.red||0).toFixed(2));set('#sigEdge',Number(c.edge||0).toFixed(2));set('#sigAxe',Number(c.axe||0).toFixed(2));set('#sigGreen',Number(c.green||0).toFixed(4));setBar('#barTemplate',c.template);setBar('#barHist',c.hist);setBar('#barRed',c.red);setBar('#barEdge',c.edge);setBar('#barAxe',c.axe);setBar('#barGreen',Math.min(1,Number(c.green||0)*30));set('#candidateReason',c.reason||'No candidate analyzed yet.');renderFeed(x.candidate_events||[]);
  set('#perfRate',rate.toFixed(1));set('#perfFind',x.avg_find_seconds?`${x.avg_find_seconds}s`:'—');set('#perfDispatch',x.avg_dispatch_seconds?`${Number(x.avg_dispatch_seconds).toFixed(2)}s`:'—');set('#perfCandidateRate',`${Number(x.candidate_success_rate||0).toFixed(1)}%`);set('#perfTrend',rate>0?'live':'warming up');set('#templateHits',x.template_hits||0);set('#templateMisses',x.template_misses||0);set('#cvRejects',(x.cv_candidate_rejections||0)+(x.cv_occupied_rejections||0));set('#postClickRejects',x.post_click_rejections||0);set('#duplicateViews',x.duplicate_views||0);set('#stuckRecoveries',x.stuck_recoveries||0);set('#occupiedMemory',x.occupied_memory||0);set('#uniqueViews',x.unique_views||0);
  if(!logPaused){set('#log',x.log_tail||'');const pre=q('#log');pre.scrollTop=pre.scrollHeight;}set('#logLines',`${String(x.log_tail||'').split('\n').filter(Boolean).length} lines`);
  updateHistory(x);
 }catch(e){renderRuntime({companion:{state:'OFFLINE',passed:false,error:'Nexus connection lost'}});}
}
async function screen(){
 try{
  const r=await fetch(`/api/screen?t=${Date.now()}`,{cache:'no-store'});if(!r.ok)throw new Error('No screenshot');
  const serial=r.headers.get('X-GemBot-Device');if(serial){set('#device',serial);set('#liveOpsDevice',serial);}
  const blob=await r.blob(),url=URL.createObjectURL(blob),im=q('#liveScreen'),old=latestScreenUrl;latestScreenUrl=url;im.src=url;im.style.display='block';q('#screenWait').style.display='none';screenLoadedAt=Date.now();if(old)setTimeout(()=>URL.revokeObjectURL(old),1000);syncLiveClone();syncModal();set('#screenClock',new Date().toLocaleTimeString());set('#screenAge','just now');
 }catch{}
}
function syncLiveClone(){const box=q('#liveClone');if(!box)return;box.innerHTML=latestScreenUrl?`<img src="${latestScreenUrl}" alt="Live LDPlayer">`:'<span class="feed-empty">No live frame yet.</span>';}
function syncModal(){const box=q('#modalScreen');if(box)box.innerHTML=latestScreenUrl?`<img src="${latestScreenUrl}" alt="Expanded LDPlayer">`:'';}
async function diagnostics(){try{const x=await req('/api/gemops/diagnostics');set('#diagTemplates',x.template_count??'—');set('#diagAI',x.ai_fallback?'enabled':'disabled');set('#diagSearch',String(x.mode||'—').toUpperCase());set('#diagDetector',String(x.detector_mode||latestStatus.detector_mode||'—').toUpperCase());set('#diagForeground',x.foreground_package||'—');toast('Diagnostics refreshed');}catch(e){toast(e.message,'error')}}

q('#openScreenModal').onclick=()=>{q('#screenModal').classList.add('open');syncModal()};qa('[data-close]').forEach(e=>e.onclick=()=>q('#screenModal').classList.remove('open'));
q('#copyLog').onclick=async()=>{try{await navigator.clipboard.writeText(q('#log').textContent);toast('Log copied')}catch{toast('Clipboard unavailable','error')}};
q('#pauseLog').onclick=()=>{logPaused=!logPaused;q('#pauseLog').textContent=logPaused?'Resume Scroll':'Pause Scroll';};
q('#logFilter').oninput=e=>{const term=e.target.value.toLowerCase(),raw=latestStatus.log_tail||'';q('#log').textContent=term?raw.split('\n').filter(x=>x.toLowerCase().includes(term)).join('\n'):raw;};

function updateScreenAge(){if(!screenLoadedAt)return;const s=Math.floor((Date.now()-screenLoadedAt)/1000);set('#screenAge',s<2?'just now':`${s}s ago`);}
setInterval(updateScreenAge,1000);

async function accountPost(action,payload={}){return req('/api/account/action',{method:'POST',headers:{'Content-Type':'application/json','X-Account-Token':accountToken},body:JSON.stringify({action,...payload})});}
function setAccountButtons(disabled){['#accountDropdown','#loginSelected','#loginAnother','#chooseSaved','#prepareEmailCode','#submitEmailCode','#loginCredentials','#changePassword'].forEach(s=>{const e=q(s);if(e)e.disabled=disabled});}
function codeState(){const email=q('#codeEmail').value.trim(),code=q('#emailCode').value.trim();q('#emailCode').disabled=!email;if(!email)q('#emailCode').value='';q('#submitEmailCode').disabled=!email||!code;}
q('#codeEmail').oninput=codeState;q('#emailCode').oninput=codeState;codeState();
q('#accountDropdown').onclick=()=>runAccount('open_dropdown');
q('#loginSelected').onclick=()=>runAccount('login_selected');
q('#loginAnother').onclick=()=>runAccount('login_another');
q('#chooseSaved').onclick=()=>{const account=q('#savedAccount').value.trim();if(!account)return toast('Enter a saved account label/email','error');runAccount('choose_saved',{account})};
q('#prepareEmailCode').onclick=()=>{const email=q('#codeEmail').value.trim();if(!email)return toast('Enter email first','error');runAccount('prepare_email_code_login',{email})};
q('#submitEmailCode').onclick=()=>{const email=q('#codeEmail').value.trim(),verification_code=q('#emailCode').value.trim();if(!email||!verification_code)return toast('Email and verification code required','error');runAccount('login_email_code',{email,verification_code}).finally(()=>{q('#emailCode').value='';codeState()})};
q('#loginCredentials').onclick=()=>{const username=q('#loginUsername').value.trim(),password=q('#loginPassword').value;if(!username||!password)return toast('Email/username and password required','error');runAccount('login_credentials',{username,password}).finally(()=>q('#loginPassword').value='')};
q('#changePassword').onclick=()=>{const current_password=q('#currentPassword').value,new_password=q('#newPassword').value,confirm_password=q('#confirmPassword').value;if(!current_password||!new_password||!confirm_password)return toast('Fill all password fields','error');if(new_password!==confirm_password)return toast('New passwords do not match','error');if(!confirm('Change the active account password?'))return;runAccount('change_password',{current_password,new_password,confirm_password}).finally(()=>['#currentPassword','#newPassword','#confirmPassword'].forEach(s=>q(s).value=''))};
async function runAccount(action,payload={}){try{setAccountButtons(true);await accountPost(action,payload);toast('Account action started')}catch(e){toast(e.message,'error')}}
async function accountStatus(){try{const x=await req('/api/account/status');const state=x.state||'idle';set('#accountState',state);set('#accountMessage',x.error||x.message||'Account controls ready.');set('#accountMethod',x.method||'direct/AI ready');set('#accountScreen',x.screen||'—');setAccountButtons(state==='running');if(state!=='running')codeState()}catch{}}

diagnostics();status();screen();accountStatus();
setInterval(status,1500);setInterval(screen,10000);setInterval(accountStatus,1200);

function renderRuntime(x){
 const c=x.companion||{}, pass=c.passed===true;
 set('#companionSummary',pass?'Companion healthy · missions enabled':'Mission gate closed · '+(c.state||'STOPPED'));
 set('#companionHint',pass?'Native + Java hooks are producing live events.':c.service_error||(c.automatic?(c.retry_in_seconds?'Automatic recovery in '+c.retry_in_seconds+'s':'Frida service is starting automatically'):c.error||'Companion service paused; choose Resume service.'));
 q('#companionDot').classList.toggle('pass',pass);
 set('#runtimeBadge',c.state||'STOPPED');q('#runtimeBadge').classList.toggle('pass',pass);
 set('#nativeHealth',c.native?'PASS':'WAITING');set('#javaHealth',c.java?'PASS':'WAITING');
 set('#runtimeError',c.error||(pass?'All required checks passed. Start a mission when ready.':'Preparing and verifying the isolated companion…'));
 set('#labIndex',c.lab_index??'—');set('#mainIndex',c.main_index??'—');set('#labPid',c.pid||'—');set('#fridaVersion',c.version||'—');set('#fridaTransport',c.transport||'Companion loopback');set('#heartbeatAge',c.heartbeat_age_seconds==null?'—':c.heartbeat_age_seconds+'s');set('#hookEvents',c.events||0);set('#fridaMapScans',c.map_requests||0);set('#fridaMapMs',c.map_last_ms==null?'—':c.map_last_ms+' ms');
 set('#companionLog',c.log_tail||'No companion log output yet.');
 ['#startBot','#liveStartBot'].forEach(id=>{const el=q(id);if(el){el.disabled=!pass;el.title=pass?'Start mission':'Companion health PASS is required';}});
 set('#machineState',x.machine_state||x.task||'idle');set('#stateAge',fmt(x.state_age_seconds));set('#stateDeadline',x.state_deadline_seconds?x.state_deadline_seconds+'s':'Standby / no deadline');set('#confirmedSends',x.dispatch_confirmations||0);set('#unconfirmedSends',x.dispatch_unconfirmed||0);set('#adaptiveRetries',x.adaptive_finder_retries||0);
 q('#serviceEvents').innerHTML=(x.service_events||[]).slice(-6).reverse().map(e=>`<p><time>${esc(e.time)}</time> ${esc(e.message)}</p>`).join('')||'No service events yet.';
}
qa('[data-companion]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{await saveConfig();await post('/api/frida/'+b.dataset.companion);toast('Companion '+b.dataset.companion+' requested');await status();}catch(e){toast(e.message,'error')}finally{b.disabled=false}});
renderRuntime({companion:{state:'CONNECTING',passed:false}});
