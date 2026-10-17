/* Void OS interface code — loaded by templates/index.html after a small
   inline block that sets the per-user values (OSNAME, ROLE, SETTINGS…). */
// Utils
function fmtB(b,d=1){if(!b)return'0 B';const k=1024,s=['B','KB','MB','GB','TB'];const i=Math.floor(Math.log(b)/Math.log(k));return(b/Math.pow(k,i)).toFixed(d)+' '+s[i]}
function fmtSpd(bps){if(bps<1024)return Math.round(bps)+' B/s';if(bps<1048576)return(bps/1024).toFixed(1)+' KB/s';return(bps/1048576).toFixed(1)+' MB/s'}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
function escA(s){return String(s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;')}
// A JS string literal for use INSIDE an inline handler attribute, e.g.
//   onclick="f(${jsA(x)})"   (note: no quotes around it — jsA adds them).
// esc()/escA() are HTML escapes: the browser decodes &#39; back into ' before
// the handler's JavaScript is parsed, so '${esc(x)}' inside onclick was never
// safe for untrusted text (cloud file/folder names reached it). JSON quoting
// handles the JS layer; escA handles the HTML-attribute layer around it.
function jsA(s){return escA(JSON.stringify(String(s??'')))}
function enhanceSelect(select){
  if(!select||select.tagName!=='SELECT'||select.dataset.enhanced)return;
  select.dataset.enhanced='1';
  const inlineStyle=select.getAttribute('style');
  const wrap=document.createElement('div');
  wrap.className='csel';
  select.parentNode.insertBefore(wrap,select);
  wrap.appendChild(select);
  select.classList.add('csel-native');
  select.removeAttribute('style');

  const btn=document.createElement('button');
  btn.type='button';btn.className='csel-btn';
  if(inlineStyle)btn.setAttribute('style',inlineStyle);
  const menu=document.createElement('div');
  menu.className='csel-menu';
  wrap.appendChild(btn);wrap.appendChild(menu);

  function renderOptions(){
    menu.innerHTML='';
    [...select.options].forEach(opt=>{
      const item=document.createElement('div');
      item.className='csel-opt'+(opt.value===select.value?' sel':'');
      item.textContent=opt.textContent;
      item.addEventListener('click',()=>{
        select.value=opt.value;
        select.dispatchEvent(new Event('change',{bubbles:true}));
        syncBtn();closeMenu();
      });
      menu.appendChild(item);
    });
  }
  function syncBtn(){
    const opt=select.options[select.selectedIndex];
    btn.innerHTML=`<span>${esc(opt?opt.textContent:'')}</span><svg viewBox="0 0 24 24" class="csel-caret"><polyline points="6 9 12 15 18 9"/></svg>`;
    menu.querySelectorAll('.csel-opt').forEach((o,i)=>o.classList.toggle('sel',select.options[i]&&select.options[i].value===select.value));
  }
  function openMenu(){renderOptions();menu.classList.add('open');btn.classList.add('open')}
  function closeMenu(){menu.classList.remove('open');btn.classList.remove('open')}
  btn.addEventListener('click',e=>{
    e.stopPropagation();
    menu.classList.contains('open')?closeMenu():openMenu();
  });
  document.addEventListener('click',e=>{if(!wrap.contains(e.target))closeMenu()});
  select.addEventListener('change',syncBtn);
  renderOptions();syncBtn();
}
function flagEmoji(code){
  if(!code||code.length!==2)return '';
  return String.fromCodePoint(...[...code.toUpperCase()].map(c=>127397+c.charCodeAt(0)));
}

// ── Spoiler dissolve effect (used to hide session IPs) ─────────────────
// Sparse particles jitter gently in place while hidden; clicking dissolves
// them outward from the click point (nearest particles clear first) until
// the real text underneath is fully exposed.
const REDUCE_MOTION=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const spoilerCanvases=new Set();
const spoilerState=new WeakMap(); // canvas -> {particles, w, h, reveal:{x,y,start}|null, cleared:bool}
let spoilerRAF=null;

function makeSpoilerParticles(w,h){
  const count=Math.max(18,Math.floor((w*h)/16));
  const particles=[];
  for(let i=0;i<count;i++){
    particles.push({
      x:Math.random()*w, y:Math.random()*h,
      r:Math.random()*1.1+0.5,
      baseAlpha:Math.random()*0.45+0.35,
      phase:Math.random()*Math.PI*2,
      speed:Math.random()*0.5+0.4,
    });
  }
  return particles;
}
function drawSpoilerNoise(canvas,now){
  const wrap=canvas.parentElement;
  if(!wrap)return;
  const rect=wrap.getBoundingClientRect();
  const dpr=window.devicePixelRatio||1;
  const w=Math.max(1,Math.round(rect.width)),h=Math.max(1,Math.round(rect.height));
  let st=spoilerState.get(canvas);
  if(canvas.width!==Math.round(w*dpr)||canvas.height!==Math.round(h*dpr)||!st){
    canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);
    canvas.style.width=w+'px';canvas.style.height=h+'px';
    st={particles:makeSpoilerParticles(w,h),w,h,reveal:null,cleared:false};
    spoilerState.set(canvas,st);
  }
  const ctx=canvas.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,w,h);
  // Canvas fillStyle can't parse var(--x) — resolved by the CSS cascade,
  // which canvas drawing never goes through — so read the actual value via
  // getComputedStyle first, to stay in the current theme's accent color.
  const accentRgb=getComputedStyle(document.documentElement).getPropertyValue('--pu-rgb').trim()||'109,74,255';

  let revealRadius=-1;
  if(st.reveal){
    const dur=550,edge=34;
    const p=Math.min(1,(now-st.reveal.start)/dur);
    const eased=1-Math.pow(1-p,3); // ease-out cubic
    revealRadius=eased*(Math.hypot(w,h)+edge+10);
    if(p>=1&&!st.cleared){
      st.cleared=true;
      canvas.classList.add('cleared');
    }
  }
  if(st.cleared)return false; // fully dissolved — nothing left to draw, stop animating it

  for(const pt of st.particles){
    const jx=REDUCE_MOTION?0:Math.sin(now*0.0032*pt.speed+pt.phase)*1.1;
    const jy=REDUCE_MOTION?0:Math.cos(now*0.0038*pt.speed+pt.phase*1.4)*1.1;
    const x=pt.x+jx,y=pt.y+jy;
    let a=pt.baseAlpha;
    if(st.reveal){
      const dist=Math.hypot(x-st.reveal.x,y-st.reveal.y);
      a*=Math.max(0,Math.min(1,(dist-revealRadius)/34+0.5));
    }
    if(a<=0.015)continue;
    ctx.fillStyle=`rgba(${accentRgb},${a})`;
    ctx.beginPath();ctx.arc(x,y,pt.r,0,Math.PI*2);ctx.fill();
  }
  return true;
}
const SPOILER_IDLE_MS=70; // ~14fps while just idly jittering, unrevealed
function ensureSpoilerLoop(){
  if(spoilerRAF||REDUCE_MOTION&&![...spoilerCanvases].some(c=>spoilerState.get(c)?.reveal))return;
  let lastIdleDraw=0;
  const tick=(now)=>{
    if(spoilerCanvases.size===0){spoilerRAF=null;return}
    const anyRevealing=[...spoilerCanvases].some(c=>spoilerState.get(c)?.reveal&&!spoilerState.get(c)?.cleared);
    const dueForIdleDraw=now-lastIdleDraw>=SPOILER_IDLE_MS;
    if(anyRevealing||dueForIdleDraw){
      if(dueForIdleDraw)lastIdleDraw=now;
      for(const canvas of spoilerCanvases){
        if(!canvas.isConnected){spoilerCanvases.delete(canvas);continue}
        const st=spoilerState.get(canvas);
        const revealing=st?.reveal&&!st?.cleared;
        if(revealing||dueForIdleDraw){
          const stillGoing=drawSpoilerNoise(canvas,now);
          if(!stillGoing)spoilerCanvases.delete(canvas);
        }
      }
    }
    spoilerRAF=requestAnimationFrame(tick);
  };
  spoilerRAF=requestAnimationFrame(tick);
}
function wireSpoilers(container){
  container.querySelectorAll('.ip-spoiler').forEach(el=>{
    const canvas=el.querySelector('.ip-spoiler-canvas');
    if(!canvas)return;
    drawSpoilerNoise(canvas,performance.now()); // one frame immediately so it's never blank while the loop spins up
    spoilerCanvases.add(canvas);
    el.addEventListener('click',e=>{
      if(el.classList.contains('revealed')){
        // hide again — instant reset, no reverse animation
        el.classList.remove('revealed');
        canvas.classList.remove('cleared');
        spoilerState.delete(canvas);
        spoilerCanvases.add(canvas);
        drawSpoilerNoise(canvas,performance.now());
        ensureSpoilerLoop();
        return;
      }
      const rect=canvas.getBoundingClientRect();
      const cx=e.clientX-rect.left,cy=e.clientY-rect.top;
      el.classList.add('revealed'); // exposes the real text now; particles dissolve on top of it
      let st=spoilerState.get(canvas);
      if(!st){st={particles:makeSpoilerParticles(rect.width,rect.height),w:rect.width,h:rect.height,reveal:null,cleared:false};spoilerState.set(canvas,st)}
      st.reveal={x:cx,y:cy,start:performance.now()};
      st.cleared=false;
      canvas.classList.remove('cleared');
      spoilerCanvases.add(canvas);
      ensureSpoilerLoop();
    });
  });
  ensureSpoilerLoop();
}
// ── Toasts ──────────────────────────────────────────────────────────────
// toast(message, type, opts) — type: info | success | warning | error.
// opts: {hint, ref, duration, action:{label, fn}, key}
// Errors stay longer (and pause while hovered), show what to do about it
// (hint) and a copyable reference code an admin can look up in the Error
// Log. Identical toasts merge into one with a ×N counter instead of
// stacking up (e.g. the same failure for 30 files in a batch).
const _toastLive=new Map();
// Bridge to state that lives inside the app block below (declared there with
// `let`, so it isn't visible to these page-wide helpers). The app block fills
// it in; on the login/setup pages it stays empty and everything degrades
// gracefully.
var App={curV:'',curP:'',reconnectControl:null};
function toast(m,t='info',opts={}){
  if(t==='ok')t='success';
  const box=document.getElementById('toasts');if(!box)return null;
  const key=opts.key||(t+'|'+m);
  const live=_toastLive.get(key);
  if(live&&live.el.isConnected){live.count++;live.el.querySelector('.toast-count').textContent='\u00d7'+live.count;live.restart();return live}
  const e=document.createElement('div');
  e.className='toast t-'+({success:'ok',error:'err',warning:'warn'}[t]||'info');
  e.setAttribute('role',t==='error'?'alert':'status');
  const icon={success:'\u2713',error:'\u2715',warning:'\u26a0',info:'\u2139'}[t]||'\u2139';
  e.innerHTML=`<span class="toast-ico">${icon}</span><div class="toast-body"><div class="toast-msg"></div>
    <div class="toast-hint"></div><div class="toast-meta"></div></div><span class="toast-count"></span>
    <button class="toast-x" aria-label="Dismiss">\u00d7</button>`;
  e.querySelector('.toast-msg').textContent=m;
  const hintEl=e.querySelector('.toast-hint');if(opts.hint)hintEl.textContent=opts.hint;else hintEl.remove();
  const meta=e.querySelector('.toast-meta');
  if(opts.action){const b=document.createElement('button');b.className='toast-act';b.textContent=opts.action.label;
    b.onclick=()=>{try{opts.action.fn()}finally{close()}};meta.appendChild(b)}
  if(opts.ref){const b=document.createElement('button');b.className='toast-ref';b.title='Copy reference code — an admin can look it up in Admin \u2192 Error Log';
    b.textContent='Ref '+opts.ref+' \u29c9';b.onclick=()=>{navigator.clipboard?.writeText(opts.ref).then(()=>{b.textContent='Copied \u2713'},()=>{})};meta.appendChild(b)}
  if(!meta.children.length)meta.remove();
  box.appendChild(e);
  while(box.children.length>5)box.firstElementChild.remove();
  setTimeout(()=>e.classList.add('show'),10);
  const dur=opts.duration??(t==='error'?(opts.ref||opts.hint?12000:8000):t==='warning'?6000:3500);
  let timer=null;
  function close(){clearTimeout(timer);_toastLive.delete(key);e.classList.remove('show');setTimeout(()=>e.remove(),280)}
  function restart(){clearTimeout(timer);if(dur>0)timer=setTimeout(close,dur)}
  e.addEventListener('mouseenter',()=>clearTimeout(timer));e.addEventListener('mouseleave',restart);
  e.querySelector('.toast-x').onclick=close;
  const h={el:e,count:1,close,restart,update:(msg)=>{e.querySelector('.toast-msg').textContent=msg}};
  _toastLive.set(key,h);restart();
  return h;
}
// Shows an API error result: message + hint + reference code, with a
// context-specific fallback when the server sent nothing usable.
function errToast(d,fallback='That didn\u2019t work'){
  d=d||{};
  return toast(d.error||fallback,'error',{hint:d.hint,ref:d.ref});
}
// Messages for failures where the server (or something in front of it,
// like a reverse proxy or Cloudflare) didn't send a usable JSON body.
function _httpMsg(s){
  return ({0:'Can\u2019t reach Void OS \u2014 the server may be restarting, or your connection dropped.',
    400:'The request was rejected as invalid.',403:'You don\u2019t have permission to do that.',
    404:'Not found \u2014 it may have been moved or removed.',405:'That action isn\u2019t supported here \u2014 try reloading the page.',
    408:'The request timed out.',409:'That conflicts with something that changed \u2014 refresh and try again.',
    413:'That\u2019s too large for the server (or a proxy in front of it) to accept.',
    429:'Too many requests \u2014 wait a moment and try again.',500:'Something went wrong on the server.',
    502:'Void OS isn\u2019t responding \u2014 it may be restarting. Try again in a moment.',
    503:'Void OS is temporarily unavailable \u2014 try again in a moment.',
    504:'The server took too long to respond. Large operations may still be finishing in the background.',
    507:'The server is out of storage space.'})[s]||`The server returned an unexpected error (HTTP ${s}).`;
}
function _fakeResp(status,body){
  return {ok:false,status,headers:new Headers(),json:async()=>body,text:async()=>body.error,blob:async()=>new Blob([])};
}
// fetch wrapper used everywhere. Two guarantees the rest of the page relies on:
//  1. .json() never throws. A non-JSON reply (proxy error page, an HTML 500,
//     an empty body) used to make `await (await api(...)).json()` throw, so
//     the action silently did nothing. Now it yields {ok:false, error:...}.
//  2. A failed response always carries a human-readable `error`.
// Network failures (offline, server down) resolve to a status-0 response
// with an explanation, and are announced once.
const _recentReqs=[];
function _noteReq(o,u,status,t0){
  _recentReqs.push({m:(o&&o.method)||'GET',u:String(u).split('?')[0].slice(0,120),s:status,t:Math.round(performance.now()-t0),at:Date.now()});
  if(_recentReqs.length>8)_recentReqs.shift();
}
async function api(u,o={}){
  let r;const _t0=performance.now();
  try{r=await fetch(u,{credentials:'same-origin',...o});_noteReq(o,u,r.status,_t0)}
  catch(e){
    _noteReq(o,u,e&&e.name==='AbortError'?'aborted':'failed',_t0);
    if(e&&e.name==='AbortError')throw e;
    const msg=navigator.onLine===false?'You\u2019re offline \u2014 check your internet connection.':_httpMsg(0);
    if(typeof Conn!=='undefined'&&!u.startsWith('/api/health')){Conn.lost('fetch')}  // the reconnect screen explains it
    else toast(msg,'error',{key:'network-down'});
    return _fakeResp(0,{ok:false,error:msg,code:'network'});
  }
  // A gateway error *without* Void OS's JSON (cloudflared/nginx/Cloudflare
  // answering because Void OS itself is down) means unreachable, too.
  if([502,503,504,520,521,522,523,524,530].includes(r.status)&&typeof Conn!=='undefined'&&
     !(r.headers.get('content-type')||'').includes('application/json'))Conn.lost('gateway');
  if(r.status===401&&!u.startsWith('/auth')){location.href='/login';throw new Error('Session expired')}
  const raw=r.json.bind(r);
  r.json=async()=>{
    let d=null;try{d=await raw()}catch(_){}
    if(d===null||typeof d!=='object')d=r.ok?{}:{error:_httpMsg(r.status),code:'http_'+r.status};
    else if(!r.ok&&!Array.isArray(d)&&!d.error)d.error=_httpMsg(r.status);
    if(!r.ok&&!Array.isArray(d)&&d.ok===undefined)d.ok=false;
    return d;
  };
  return r;
}
// GET (or any call) that must succeed to be useful: returns the parsed body,
// or throws Error(<the server's message>) so the caller's catch can show WHY
// (e.g. "admin access required") instead of crashing on `.map` of an error
// object or showing a false "nothing here" for a failed request.
async function apiData(u,o){
  const r=await api(u,o);
  const d=await r.json();
  if(!r.ok)throw new Error((d&&d.error)||_httpMsg(r.status));
  return d;
}
// Browser-side crashes are reported to the server (Admin → Error Log) and
// the user gets a message instead of a page that silently stops working.
let _clientErrCount=0;
let _lastNavAt=0;                      // when the user last changed pages (see go())
function reportClientError(kind,message,stack,extra){
  if(_clientErrCount++>=10)return Promise.resolve(null);
  const now=Date.now();
  let build='';try{const sc=[...document.scripts].find(x=>/\/static\/app\.js/.test(x.src));build=sc?(sc.src.split('v=')[1]||''):''}catch(_){}
  return fetch('/api/client-error',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({kind,message:String(message||'').slice(0,300),stack:String(stack||'').slice(0,4000),
      view:App.curV||'',url:location.pathname,stale:!!(extra&&extra.stale),build,
      ui:(typeof SETTINGS!=='undefined'&&(SETTINGS.ui_mode_resolved||SETTINGS.ui_mode))||'',
      requests:_recentReqs.map(r=>({m:r.m,u:r.u,s:r.s,t:r.t,ago:Math.round((now-r.at)/1000)}))})})
    .then(r=>r.ok?r.json():null).catch(()=>null);
}
// A page's async loader that finishes after you've already moved to another page can find
// its elements gone. That's harmless, so it's logged (with that reason) but not shown.
const _NULL_ACCESS=/cannot read propert(?:y|ies) of (?:null|undefined)|is null|is undefined/i;
function _onPageError(kind,err,msg){
  msg=msg||(err&&err.message)||String(err||'');
  if(!msg||msg==='Script error.'||msg==='Session expired'||(err&&err.name==='AbortError'))return;
  const xtermClosing=/'dimensions'/.test(msg)&&/\/static\/vendor\/terminal/.test((err&&err.stack)||'');   // known xterm race while closing
  const stale=xtermClosing||(_NULL_ACCESS.test(msg)&&(Date.now()-_lastNavAt<8000));
  reportClientError(kind,msg,err&&err.stack,{stale}).then(d=>{
    if(stale)return;
    toast('Something went wrong on this page','error',{key:'page-error',
      hint:'Try that again. If it keeps happening, reload the page.',ref:d&&d.ref});
  });
}
window.addEventListener('error',e=>_onPageError('error',e.error,e.message));
window.addEventListener('unhandledrejection',e=>_onPageError('promise',e.reason));
// ── Connection watchdog + "Trying to Reconnect" screen ──────────────
// Notices the server going away from any of: the /ws/control socket
// dropping, a request failing at the network level, a proxy/tunnel answer
// (502/503/504/52x) instead of Void OS, or the browser going offline. It
// then confirms with a probe of /api/health — a blip that heals within
// ~1.5 s never shows anything. If the server really is gone, the page dims
// and a purple ripple says "Trying to Reconnect" while it keeps probing
// with backoff (1s → 2s → 3s → 5s, then every 5s). The moment the server
// answers, the screen fades away, live channels reconnect, and the view
// refreshes. After an admin "Restart", it reloads the page instead (new
// settings), and if the session didn't survive it goes to the login page.
var Conn={
  state:'online', expectRestart:false, attempt:0, _timer:null, _tick:null, _graceTimer:null, _nextAt:0, _shownAt:0,
  DELAYS:[1000,2000,3000,5000],
  // Something suggests the server is unreachable. Cheap to call often.
  lost(reason){
    if(this.state!=='online'||document.visibilityState==='hidden'&&reason==='socket')return this.state==='online'?this._probeSoon(800):undefined;
    this.state='probing';this.attempt=0;
    clearTimeout(this._graceTimer);
    this._graceTimer=setTimeout(()=>{if(this.state!=='online')this.show()},1500);  // ignore blips
    this.probe();
  },
  _probeSoon(ms){clearTimeout(this._timer);this._timer=setTimeout(()=>this.probe(),ms)},
  async probe(){
    clearTimeout(this._timer);
    let ok=false,authed=true;
    try{
      const ctl=new AbortController();const t=setTimeout(()=>ctl.abort(),4000);
      const r=await fetch('/api/health?_='+Date.now(),{credentials:'same-origin',cache:'no-store',signal:ctl.signal});
      clearTimeout(t);
      if(r.ok){const d=await r.json().catch(()=>null);ok=!!(d&&d.ok);authed=!d||d.authenticated!==false}
    }catch(_){}
    if(ok)return this.restored(authed);
    if(this.state==='online')this.state='probing';
    const delay=this.DELAYS[Math.min(this.attempt,this.DELAYS.length-1)];
    this.attempt++;this._nextAt=Date.now()+delay;
    this._timer=setTimeout(()=>this.probe(),delay);
    this._renderStatus();
  },
  restored(authed){
    const wasShown=this.state==='offline';
    this.state='online';this.attempt=0;
    clearTimeout(this._graceTimer);clearTimeout(this._timer);clearInterval(this._tick);
    if(!authed){location.href='/login';return}
    if(this.expectRestart){this.expectRestart=false;this._setText('Back online \u2014 reloading\u2026','');location.reload();return}
    this.hide();
    // Bring live channels back right away instead of waiting out their backoff.
    if(App.reconnectControl)App.reconnectControl();
    if(wasShown){
      toast('Reconnected','success',{key:'reconnected'});
      // Refresh what's on screen (it may be stale), unless a dialog is open
      // with something the user is in the middle of.
      const modalOpen=!document.getElementById('ovr')?.classList.contains('hidden');
      if(!modalOpen&&App.curV){
        if(App.curV==='files'&&typeof rFiles==='function')rFiles(App.curP);
        else if(['dashboard','containers','shares','sessions','access','appstore','intercom'].includes(App.curV))go(App.curV);
      }
    }
  },
  _el(){
    let el=document.getElementById('connOverlay');
    if(el)return el;
    el=document.createElement('div');el.id='connOverlay';el.className='conn-ov';
    el.setAttribute('role','alertdialog');el.setAttribute('aria-modal','true');el.setAttribute('aria-labelledby','connTitle');
    el.innerHTML=`<div class="conn-center">
        <div class="conn-ripple" aria-hidden="true"><span></span><span></span><span></span><span></span></div>
        <div class="conn-title" id="connTitle">Trying to Reconnect<span class="conn-dots"><b>.</b><b>.</b><b>.</b></span></div>
        <div class="conn-sub" id="connSub" aria-live="polite"></div>
        <button class="conn-retry" id="connRetry" type="button">Retry now</button>
      </div>`;
    el.querySelector('#connRetry').onclick=()=>{this.attempt=0;this._setText(null,'Checking\u2026');this.probe()};
    document.body.appendChild(el);
    return el;
  },
  _setText(title,sub){
    const el=this._el();
    if(title!=null)el.querySelector('#connTitle').firstChild.textContent=title;
    if(sub!=null)el.querySelector('#connSub').textContent=sub;
  },
  _renderStatus(){
    if(this.state!=='offline')return;
    const s=Math.max(0,Math.ceil((this._nextAt-Date.now())/1000));
    const why=this.expectRestart?'Void OS is restarting.':navigator.onLine===false?'Your device is offline.':'The server isn\u2019t responding.';
    this._setText(null,s?`${why} Next try in ${s}s`:`${why} Checking\u2026`);
  },
  show(){
    if(this.state==='online')return;
    const first=this.state!=='offline';
    this.state='offline';
    const el=this._el();
    if(first){
      this._shownAt=Date.now();
      this._setText(this.expectRestart?'Restarting Void OS':'Trying to Reconnect',null);
      el.classList.remove('leaving');void el.offsetWidth;el.classList.add('show');
      document.documentElement.classList.add('conn-lost');
      _toastLive.get('network-down')?.close();
      setTimeout(()=>el.querySelector('#connRetry')?.focus({preventScroll:true}),50);
    }
    clearInterval(this._tick);this._tick=setInterval(()=>this._renderStatus(),500);this._renderStatus();
  },
  hide(){
    const el=document.getElementById('connOverlay');
    document.documentElement.classList.remove('conn-lost');
    if(!el)return;
    el.classList.add('leaving');el.classList.remove('show');
    setTimeout(()=>{if(this.state==='online')el.remove()},450);
  },
  // For the admin Restart button: show the screen straight away and reload
  // (rather than resume) once the server is back.
  restarting(){
    this.expectRestart=true;
    if(this.state==='online'){this.state='probing';this.attempt=0}
    this.show();this._renderStatus();
    this._probeSoon(1500);
  },
};
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'&&Conn.state!=='online')Conn.probe()});
window.addEventListener('online',()=>{if(Conn.state!=='online')Conn.probe()});
window.addEventListener('offline',()=>Conn.lost('offline'));
window.addEventListener('beforeunload',()=>{Conn._unloading=true});

// ── File transfers: progress tray ────────────────────────────────────
// Copy/move/delete/import run as server-side jobs. The server keeps the
// result hidden until it's complete, and this tray shows progress until
// then; the folder view refreshes the moment a job finishes, so items
// appear (or disappear) exactly when they're really there (or gone).
const _xfers=new Map();
let _xferPollTimer=null,_xferLastPush=0;
function fmtDur(s){if(s==null||!isFinite(s))return'';if(s<60)return`${Math.max(1,Math.round(s))}s`;
  if(s<3600)return`${Math.floor(s/60)}m ${Math.round(s%60)}s`;return`${Math.floor(s/3600)}h ${Math.round(s%3600/60)}m`}
const _XFER_END=new Set(['done','error','partial','cancelled']);
// ══════════════════════════════════════════════════════════════════
// TRANSFERS PANEL — one Dropbox-style box, bottom-right, for everything that is "in flight":
// uploads from this computer, copy/move/delete/zip jobs on the server and app installs.
// Producers call tpUpsert(); this block owns the DOM (rows are updated in place, never rebuilt,
// so a button under the pointer keeps working while progress ticks).
// ══════════════════════════════════════════════════════════════════
const TP={items:new Map(),filter:'all',collapsed:false,seq:0,raf:0,hideT:null,hover:false,lastDest:null,els:new Map(),sig:'',ro:null,outT:null};
const _TF_ICO={
  up:'<svg viewBox="0 0 24 24"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg>',
  check:'<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  clock:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>',
  spin:'<svg viewBox="0 0 24 24"><path d="M20.5 12a8.5 8.5 0 01-14.6 5.9"/><path d="M3.5 12a8.5 8.5 0 0114.6-5.9"/><path d="M18 3v4h-4"/><path d="M6 21v-4h4"/></svg>',
  ok:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M8.2 12.4l2.5 2.5 5-5.6"/></svg>',
  bad:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5v5"/><path d="M12 16.2h.01"/></svg>',
  skip:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M8.5 12h7"/></svg>',
  chev:'<svg viewBox="0 0 24 24"><path d="M6 9.5l6 6 6-6"/></svg>',
  plus:'<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
};
const _TF_STATE_ICO={queued:'clock',running:'spin',done:'ok',failed:'bad',skipped:'skip'};
function _tfActive(it){return it.state==='queued'||it.state==='running'}
function tfEta(s){
  if(s==null||!isFinite(s)||s<0)return'';
  if(s<1)return'less than a second left';
  if(s<60){const n=Math.round(s);return`${n} second${n===1?'':'s'} left`}
  if(s<3600){const n=Math.ceil(s/60);return`${n} minute${n===1?'':'s'} left`}
  const h=Math.floor(s/3600),m=Math.round(s%3600/60);return`${h} hour${h===1?'':'s'}${m?` ${m} min`:''} left`;
}
function tfCtx(){try{return typeof _tfCtx==='function'?_tfCtx():{curP:'',me:null}}catch(e){return{curP:'',me:null}}}
function tfBadge(name){const m=/\.([A-Za-z0-9]{1,8})$/.exec(name||'');return m?m[1].toUpperCase().slice(0,5):'FILE'}
function tfFolderLabel(p){
  p=String(p||'');
  if(tfCtx().me===p)return'My Files';
  if(!p)return'Files';
  return p.split('/').filter(Boolean).pop()||'Files';
}
function tpUpsert(id,f){
  let it=TP.items.get(id);
  if(!it){it={id,order:++TP.seq};TP.items.set(id,it)}
  Object.assign(it,f);
  if(it.state==='running'&&it.live&&it.loaded!=null){            // smoothed bytes/second for the time-left estimate
    const now=performance.now(),sm=it._s||(it._s={t:now,b:it.loaded,v:0}),dt=(now-sm.t)/1000;
    if(dt>=.5){const v=Math.max(0,(it.loaded-sm.b)/dt);sm.v=sm.v?sm.v*.6+v*.4:v;sm.t=now;sm.b=it.loaded}
    it.speed=sm.v;
  }
  titleRefresh();                 // works in a background tab too: the tab title is the whole point there
  tpSchedule();
  return it;
}
function tpRemove(id){TP.items.delete(id);titleRefresh();tpSchedule()}
function tpSchedule(){
  if(TP.raf)return;
  TP.raf=document.hidden?setTimeout(()=>{TP.raf=0;tpRender()},250):requestAnimationFrame(()=>{TP.raf=0;tpRender()});
}
function tpClearFinished(){
  for(const[id,it]of[...TP.items]){
    if(_tfActive(it))continue;
    TP.items.delete(id);
    if(id.startsWith('x-'))_xfers.delete(id.slice(2));
  }
  tpSchedule();
}
function tpCancelAll(){TP.items.forEach(it=>{if(_tfActive(it)&&it.cancel)it.cancel()})}
let _pendingFilesPath=null,_pendingHighlight=null;
function tpOpenFolder(path,highlight){
  _pendingHighlight=highlight||null;
  if(App.curV==='files'){rFiles(path||'');return}
  _pendingFilesPath=path||'';go('files');
}
function tpAddFiles(){
  let inp=document.getElementById('tfAddInp');
  if(!inp){
    inp=document.createElement('input');inp.type='file';inp.multiple=true;inp.id='tfAddInp';inp.hidden=true;
    inp.addEventListener('change',()=>{
      const dest=App.curV==='files'?tfCtx().curP:(TP.lastDest?TP.lastDest.path:'');
      if(inp.files&&inp.files.length)doUpload(inp.files,dest);
      inp.value='';
    });
    document.body.appendChild(inp);
  }
  inp.click();
}
function tpBuild(){
  const p=document.createElement('div');p.id='tfPanel';p.className='tf';
  p.setAttribute('role','region');p.setAttribute('aria-label','Uploads and transfers');
  p.innerHTML=`
    <div class="tf-top"><div class="tf-top-in">
      <div class="tf-head"><h3 class="tf-title" id="tfTitle">Uploads</h3>
        <div class="tf-head-r"><button class="tf-pill" id="tfAll" type="button">Cancel all</button>
          <button class="tf-ibtn" id="tfCollapse" type="button" aria-label="Collapse">${_TF_ICO.chev}</button></div></div>
      <div class="tf-filters" id="tfFilters" role="tablist">
        <button class="tf-f" role="tab" data-f="all">All</button><button class="tf-f" role="tab" data-f="done">Completed</button>
        <button class="tf-f" role="tab" data-f="skipped">Skipped</button><button class="tf-f" role="tab" data-f="failed">Failed</button></div>
      <div class="tf-list" id="tfList"></div>
    </div></div>
    <div class="tf-foot">
      <div class="tf-bar" id="tfBar" role="status" aria-live="polite" title="Show or hide the list">
        <div class="tf-txt"><div class="tf-lines"><b></b><span></span></div></div>
        <div class="tf-fill"><div class="tf-txt inv"><div class="tf-lines"><b></b><span></span></div></div></div>
        <div class="tf-ico">${_TF_ICO.up}</div>
      </div>
      <button class="tf-add" id="tfAdd" type="button">${_TF_ICO.plus}<span>Add</span></button>
    </div>`;
  p.addEventListener('mouseenter',()=>{TP.hover=true;clearTimeout(TP.hideT);TP.hideT=null});
  p.addEventListener('mouseleave',()=>{TP.hover=false;tpSchedule()});
  p.addEventListener('click',e=>{
    const f=e.target.closest('.tf-f');if(f){TP.filter=f.dataset.f;TP.sig='';tpSchedule();return}
    if(e.target.closest('#tfCollapse')||e.target.closest('#tfBar')){
      TP.collapsed=!TP.collapsed;
      tpSchedule();return;
    }
    if(e.target.closest('#tfAll')){
      if([...TP.items.values()].some(_tfActive))tpCancelAll();else tpClearFinished();
      return;
    }
    if(e.target.closest('#tfAdd')){tpAddFiles();return}
    const g=e.target.closest('.tf-grp a');if(g){e.preventDefault();tpOpenFolder(g.dataset.path);return}
    const row=e.target.closest('.tf-row');if(!row)return;
    const it=TP.items.get(row.dataset.id);if(!it)return;
    if(e.target.closest('.tf-btn')){
      const b=e.target.closest('.tf-btn');
      if(b.dataset.k==='cancel'&&it.cancel)it.cancel();
      else if(b.dataset.k==='act'&&it.action)it.action.fn();
      return;
    }
    if(e.target.closest('.tf-name.link')&&it.onTitle)it.onTitle();
  });
  document.body.appendChild(p);
  if('ResizeObserver' in window){
    const bar=p.querySelector('#tfBar');
    TP.ro=new ResizeObserver(()=>{p.style.setProperty('--bw',bar.offsetWidth+'px');layoutFloatingPanels()});
    TP.ro.observe(bar);TP.ro.observe(p);
  }
  return p;
}
function tpSummary(items){
  const act=items.filter(_tfActive),running=items.filter(i=>i.state==='running');
  const done=items.filter(i=>i.state==='done').length,failed=items.filter(i=>i.state==='failed').length,
    skipped=items.filter(i=>i.state==='skipped').length;
  const allUp=items.every(i=>i.kind==='upload'),total=items.length;
  const verb=allUp?'Uploading':'Transferring',past=allUp?'uploaded':'finished';
  // progress: weighted by size when known, otherwise each item counts the same
  let w=0,acc=0;
  for(const i of items){
    const wi=i.total>0?i.total:1048576,pct=i.state==='done'?100:(i.state==='running'||i.state==='queued'?(i.pct||0):100);
    w+=wi;acc+=wi*pct;
  }
  const pct=w?acc/w:100;
  let l1,l2='',cls='';
  if(act.length){
    const n=Math.min(total,done+failed+skipped+Math.max(1,running.length));
    l1=`${verb} ${n} of ${total} item${total===1?'':'s'}`;
    const sp=running.reduce((a,i)=>a+(i.speed||0),0);
    const rem=act.reduce((a,i)=>a+(i.total>0?Math.max(0,i.total-(i.loaded||0)):0),0);
    const noSize=act.some(i=>!(i.total>0));
    l2=(sp>0&&rem>0&&!noSize)?tfEta(rem/sp):(running.length?'Working\u2026':'Waiting\u2026');
  }else if(failed){
    l1=`${failed} item${failed===1?'':'s'} failed`;l2=done?`${done} ${past}`:'Open the list for details';cls='bad';
  }else if(done){
    l1=done===1?`1 item ${past}`:`${done} items ${past}`;l2=skipped?`${skipped} skipped`:'All done';cls='ok';
  }else{l1='Nothing was transferred';l2=`${skipped} skipped`}
  return{l1,l2,pct:Math.max(0,Math.min(100,pct)),cls,active:act.length,failed,skipped,done};
}
function tpRowEl(it){
  const el=document.createElement('div');el.className='tf-row s-'+it.state;el.dataset.id=it.id;
  el.innerHTML=`<span class="tf-st"></span>
    <div class="tf-main"><div class="tf-name"></div><div class="tf-sub"><span class="tf-badge"></span><span class="tf-det"></span></div></div>
    <button class="tf-btn" type="button" hidden></button><div class="tf-prog"><i></i></div>`;
  el._st=null;
  return el;
}
function tpDetail(it){
  if(it.live&&it.state==='running'&&!it.finalizing){
    let t=`Uploading ${fmtB(it.loaded||0)} / ${fmtB(it.total||0)}`;
    const eta=(it.speed>0&&it.total>0)?(it.total-(it.loaded||0))/it.speed:null;
    if(eta!=null)t+=` - ${tfEta(eta)}`;
    return t+'\u2026';
  }
  return it.detail||'';
}
function tpUpdateRow(el,it){
  if(el._st!==it.state){
    el._st=it.state;el.className='tf-row s-'+it.state;
    el.querySelector('.tf-st').innerHTML=_TF_ICO[_TF_STATE_ICO[it.state]]||'';
  }
  el.classList.toggle('indet',!!it.indet&&it.state==='running');
  const nm=el.querySelector('.tf-name');
  if(nm.textContent!==it.title){nm.textContent=it.title;nm.title=it.title}
  nm.classList.toggle('link',!!it.onTitle);
  const bd=el.querySelector('.tf-badge');if(bd.textContent!==(it.badge||'')){bd.textContent=it.badge||''}
  bd.style.display=it.badge?'':'none';
  const dt=tpDetail(it),de=el.querySelector('.tf-det');
  if(de.textContent!==dt){de.textContent=dt;de.title=dt}
  const b=el.querySelector('.tf-btn');
  let label='',kind='';
  if(_tfActive(it)&&it.cancel){label='Cancel';kind='cancel'}
  else if(it.action){label=it.action.label;kind='act'}
  b.hidden=!label;
  if(label){if(b.textContent!==label)b.textContent=label;b.dataset.k=kind;b.className='tf-btn'+(kind==='act'?' alt':'')}
  const pg=el.querySelector('.tf-prog i');
  const pc=it.state==='running'?Math.max(0,Math.min(100,it.pct||0)):0;
  pg.style.width=pc+'%';
}
function tpRender(){
  const items=[...TP.items.values()].sort((a,b)=>a.order-b.order);
  let p=document.getElementById('tfPanel');
  if(!items.length){
    if(p&&!p.classList.contains('tf-out')){
      p.classList.add('tf-out');clearTimeout(TP.outT);
      TP.outT=setTimeout(()=>{if(!TP.items.size){document.getElementById('tfPanel')?.remove();TP.els.clear();TP.sig='';TP.filter='all';layoutFloatingPanels()}},220);
    }
    return;
  }
  clearTimeout(TP.outT);
  if(!p)p=tpBuild();
  p.classList.remove('tf-out');
  const sum=tpSummary(items);
  // header
  p.classList.toggle('collapsed',TP.collapsed);
  p.querySelector('#tfCollapse').setAttribute('aria-expanded',String(!TP.collapsed));
  p.querySelector('#tfTitle').textContent=items.every(i=>i.kind==='upload')?'Uploads':'Transfers';
  const allBtn=p.querySelector('#tfAll');
  const canCancel=items.some(i=>_tfActive(i)&&i.cancel);
  allBtn.hidden=!(canCancel||(!sum.active&&items.length));
  allBtn.textContent=canCancel?'Cancel all':'Clear';
  p.querySelectorAll('.tf-f').forEach(b=>{
    b.classList.toggle('on',b.dataset.f===TP.filter);
    const n=b.dataset.f==='all'?items.length:items.filter(i=>i.state===b.dataset.f).length;
    b.classList.toggle('dim',b.dataset.f!=='all'&&!n);
  });
  // list
  const visible=TP.filter==='all'?items:items.filter(i=>i.state===TP.filter);
  const list=p.querySelector('#tfList');
  const nodes=[];let lastGroup=null;
  const seen=new Set();
  for(const it of visible){
    const gk=it.group||'';
    if(gk!==lastGroup){
      lastGroup=gk;
      if(it.groupLabel){
        let g=TP.els.get('g:'+gk);
        if(!g){g=document.createElement('div');g.className='tf-grp';TP.els.set('g:'+gk,g)}
        const html=it.groupPath!=null&&it.groupPrefix
          ?`${esc(it.groupPrefix)} <a href="#" data-path="${escA(it.groupPath)}">${esc(it.groupLabel)}</a>`:esc(it.groupLabel);
        if(g._html!==html){g._html=html;g.innerHTML=html}
        nodes.push(g);seen.add('g:'+gk);
      }
    }
    let el=TP.els.get(it.id);
    if(!el){el=tpRowEl(it);TP.els.set(it.id,el);el.classList.add('tf-new');el.addEventListener('animationend',()=>el.classList.remove('tf-new'),{once:true})}
    tpUpdateRow(el,it);
    nodes.push(el);seen.add(it.id);
  }
  if(!visible.length){
    let e=TP.els.get('empty');if(!e){e=document.createElement('div');e.className='tf-empty';TP.els.set('empty',e)}
    e.textContent={done:'Nothing has completed yet',skipped:'Nothing was skipped',failed:'Nothing failed'}[TP.filter]||'Nothing here';
    nodes.push(e);seen.add('empty');
  }
  const sig=nodes.map(n=>n.dataset&&n.dataset.id?n.dataset.id:(n._html||'e')).join('|');
  if(sig!==TP.sig||list.childNodes.length!==nodes.length){
    TP.sig=sig;
    list.replaceChildren(...nodes);
  }
  for(const k of[...TP.els.keys()])if(!seen.has(k)&&!TP.items.has(k)){TP.els.delete(k)}
  // bottom bar
  const bar=p.querySelector('#tfBar');
  bar.className='tf-bar'+(sum.cls?' '+sum.cls:'');
  bar.querySelectorAll('.tf-lines b').forEach(x=>{if(x.textContent!==sum.l1)x.textContent=sum.l1});
  bar.querySelectorAll('.tf-lines span').forEach(x=>{if(x.textContent!==sum.l2)x.textContent=sum.l2});
  bar.querySelector('.tf-fill').style.width=sum.pct+'%';
  bar.querySelector('.tf-ico').innerHTML=sum.active?_TF_ICO.up:(sum.failed?_TF_ICO.bad:_TF_ICO.check);
  const add=p.querySelector('#tfAdd');
  const dest=App.curV==='files'?tfCtx().curP:(TP.lastDest?TP.lastDest.path:null);
  add.hidden=dest==null;
  add.title=dest==null?'':`Add files to ${tfFolderLabel(dest)}`;
  // tidy up on its own once everything is finished and nothing needs attention
  clearTimeout(TP.hideT);TP.hideT=null;
  if(!sum.active&&!sum.failed&&!TP.hover){
    TP.hideT=setTimeout(()=>{
      if(![...TP.items.values()].some(i=>_tfActive(i)||i.state==='failed'))tpClearFinished();
    },sum.skipped&&!sum.done?6000:9000);
  }
  layoutFloatingPanels();
}
// The panel and the toasts both live bottom-right: keep the toasts above the panel.
function layoutFloatingPanels(){
  const p=document.getElementById('tfPanel'),ts=document.getElementById('toasts');
  if(ts)ts.style.bottom=(p&&!p.classList.contains('tf-out')?p.offsetHeight+32:20)+'px';
}
function _xferPct(j){
  if(j.appjob)return j.state==='done'?100:(j.state==='error'?0:(j.pct||0));
  if(_XFER_END.has(j.state))return j.state==='cancelled'?0:100;
  if(j.total_bytes)return Math.min(100,j.done_bytes/j.total_bytes*100);
  if(j.total_files)return Math.min(100,(j.done_files||0)/j.total_files*100);
  return j.total_items?j.done_items/j.total_items*100:0;
}
function _xferStatus(j){
  if(j.appjob)return j.status_text||'';
  const hard=(j.errors||[]).filter(e=>!['links_skipped','renamed'].includes(e.code));
  switch(j.state){
    case'queued':return'Waiting for another transfer to finish\u2026';
    case'running':{
      const parts=[];
      if(j.total_bytes)parts.push(`${fmtB(j.done_bytes)} of ${fmtB(j.total_bytes)}`);
      else if(j.total_files)parts.push(`${j.done_files} of ${j.total_files} files`);
      if(j.speed)parts.push(`${fmtB(j.speed)}/s`);
      if(j.eta)parts.push(`${fmtDur(j.eta)} left`);
      return parts.join(' \u00b7 ')||'Working\u2026';}
    case'done':return(j.errors||[]).length?(j.errors[0].error):'Done';
    case'partial':return`${hard.length} of ${j.total_items} failed \u2014 ${hard[0].error}`;
    case'error':return hard[0]?hard[0].error:'Failed';
    case'cancelled':return j.message||'Cancelled';
  }
  return'';
}
function xferRender(j){
  const ended=_XFER_END.has(j.state);
  const st=j.state==='queued'?'queued':ended?(j.state==='done'?'done':(j.state==='cancelled'?'skipped':'failed')):'running';
  const badge=j.appjob?'APP':({copy:'COPY',move:'MOVE',trash:'TRASH',delete:'DELETE',zip:'ZIP',empty_trash:'TRASH',import:'IMPORT',export:'EXPORT',upload:'FILE'}[j.op]||'TASK');
  tpUpsert('x-'+j.id,{
    kind:j.appjob?'app':'op',title:j.label||'Working',badge,state:st,
    pct:_xferPct(j),indet:!!(j.appjob&&j.indet&&!ended),
    detail:_xferStatus(j)||(st==='running'?'Working\u2026':''),
    group:j.appjob?'apps':'ops',groupLabel:j.appjob?'Apps':'File operations',groupPrefix:'',groupPath:null,
    cancel:(!ended&&!j.appjob)?()=>xferCancel(j.id):null,
    action:(ended&&(j.errors||[]).length>1)?{label:'Details',fn:()=>xferDetails(j.id)}:null,
    onTitle:j.appjob?()=>showJobDlg(j.job_id):null,
  });
}
function xferDetails(id){
  const j=_xfers.get(id);if(!j)return;
  openMdl(j.label,`<div style="display:flex;flex-direction:column;gap:8px;max-height:55vh;overflow:auto">${(j.errors||[]).map(e=>
    `<div class="user-row" style="align-items:flex-start"><div class="user-info"><div class="user-name">${esc(e.name)}</div>
     <div class="user-meta" style="white-space:normal">${esc(e.error)}${e.ref?` <code>ref ${esc(e.ref)}</code>`:''}</div></div></div>`).join('')}</div>`,
    `<button class="btn btn-g" onclick="closeMdl()">Close</button>`);
}
async function xferCancel(id){
  const r=await api(`/api/file/ops/${id}/cancel`,{method:'POST'});const d=await r.json();
  if(!r.ok)errToast(d,'Couldn\u2019t cancel');
}
// Called for every update (WebSocket push or poll).
function xferUpdate(j){
  const prev=_xfers.get(j.id);
  if(!prev&&_XFER_END.has(j.state)&&Date.now()/1000-(j.finished||0)>20)return; // stale, from a reload
  _xfers.set(j.id,j);
  xferRender(j);
  if(_XFER_END.has(j.state)&&!(prev&&_XFER_END.has(prev.state)))xferFinished(j);
  xferEnsurePolling();
}
function xferFinished(j){
  const hard=(j.errors||[]).filter(e=>!['links_skipped','renamed'].includes(e.code));
  if(j.state==='done'){
    const info=(j.errors||[])[0];
    toast(j.label.replace(/^(\w+)ing\b/,(m,v)=>({Copy:'Copied',Mov:'Moved',Delet:'Deleted',Import:'Imported',Empty:'Emptied'})[v]||m),
      info?'warning':'success',info?{hint:info.error}:{});
  }else if(j.state==='partial'||j.state==='error'){
    toast(j.state==='error'?`${j.label} failed`:`${j.label}: ${hard.length} of ${j.total_items} failed`,'error',
      {hint:hard[0]&&`${hard[0].name}: ${hard[0].error}`,ref:hard[0]&&hard[0].ref,
       action:hard.length>1?{label:'Show all',fn:()=>xferDetails(j.id)}:undefined});
  }
  // Refresh whatever the user is looking at that this job changed.
  if(App.curV==='files'){
    const touched=new Set([j.dest_dir,...(j.src_dirs||[])].filter(x=>x!=null));
    if(touched.has(App.curP||'')||j.op==='trash'||j.op==='delete')rFiles(App.curP);
  }
  if((j.op==='empty_trash'||j.op==='trash'||j.op==='delete')&&document.getElementById('emptyTrashBtn')&&window.loadTrash)window.loadTrash();
}
// Fallback polling, only while something's running and pushes have gone quiet.
function xferEnsurePolling(){
  if(_xferPollTimer)return;
  _xferPollTimer=setInterval(async()=>{
    const active=[..._xfers.values()].filter(j=>!_XFER_END.has(j.state)&&!j.appjob);
    if(!active.length){clearInterval(_xferPollTimer);_xferPollTimer=null;return}
    if(Date.now()-_xferLastPush<1500)return;
    for(const j of active){const r=await api(`/api/file/ops/${j.id}`);if(r.ok)xferUpdate(await r.json())}
  },1000);
}
// Start a job. Returns the job (or null). Handles the "these will be
// deleted permanently" confirmation for items on external drives.
async function startFileOp(body,anchorEl){
  const r=await api('/api/file/ops',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  if(d.code==='permanent_required'){
    return new Promise(res=>confirmDlg(`${esc(d.error)}<br><br><strong>This can\u2019t be undone.</strong>`,
      async()=>res(await startFileOp({...body,confirm_permanent:true},anchorEl)),'Delete permanently',anchorEl));
  }
  if(!r.ok||!d.job_id){
    errToast(d,'That couldn\u2019t be started');
    if(d.rejected&&d.rejected.length>1)console.warn('Rejected items',d.rejected);
    return null;
  }
  if(d.rejected&&d.rejected.length){
    toast(`${d.rejected.length} item${d.rejected.length>1?'s were':' was'} skipped`,'warning',
      {hint:`${d.rejected[0].name}: ${d.rejected[0].error}`,duration:9000});
  }
  xferUpdate(d.job);
  return d.job;
}
async function xferResume(){  // pick up jobs still running after a reload
  try{const r=await api('/api/file/ops');if(!r.ok)return;const d=await r.json();
    (d.jobs||[]).filter(j=>!_XFER_END.has(j.state)).forEach(xferUpdate)}catch(_){}
}
window.addEventListener('resize',()=>layoutFloatingPanels());
// ── Notification centre (the bell) ──────────────────────────────────
// Persistent, per-user feed from the server (finished/failed transfers,
// app installs, sync problems, security events, disk space). Unlike a
// toast it's still there after you look away. New items arrive live over
// /ws/control; the unread count shows on every bell (sidebar, mobile top
// bar, home-screen top bar).
let _notifs=[],_notifUnread=0,_notifPanel=null;
const _NOTIF_ICON={success:'\u2713',error:'\u2715',warning:'\u26a0',info:'\u2139'};
function _bellSvg(){return'<svg viewBox="0 0 24 24"><path d="M18 8a6 6 0 10-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 01-3.46 0"/></svg>'}
function notifBadges(){
  document.querySelectorAll('.notif-badge').forEach(b=>{b.textContent=_notifUnread>99?'99+':_notifUnread;b.style.display=_notifUnread?'':'none'});
  titleRefresh();
}
async function notifRefresh(){
  try{const r=await api('/api/notifications');if(!r.ok)return;const d=await r.json();
    _notifs=d.items||[];_notifUnread=d.unread||0;notifBadges();if(_notifPanel)notifRenderList()}catch(_){}
}
// The clock's easter egg: the video plays inside Void OS (a popup with an embedded player), and a
// plain-text notification follows a little later.
async function rickroll(){
  // Prefer the owner's own clip (static/rick.mp4, served through /api/easter/rick); otherwise the embed.
  let local=false;
  try{local=(await api('/api/easter/rick',{method:'HEAD'})).ok}catch(_){}
  // No title, and no way to close it for the first eight seconds (X, Escape and the button are all held back),
  // so the video has a chance to load before anyone dismisses it.
  openMdl('',
    // Nothing of the player's own UI is reachable: controls off, keyboard off, a transparent shield over the
    // picture (no hover, no clicks, no title bar or pause button appearing), and the frame is taller than the
    // window so the strip where the player draws its title and control bar is cropped away.
    local?`<div class="fv-rick-wrap"><video class="fv-rick fv-rick-video" src="/api/easter/rick" autoplay playsinline
        disablepictureinpicture disableremoteplayback controlslist="nodownload noplaybackrate noremoteplayback" tabindex="-1"></video><div class="fv-rick-shield"></div></div>`:
    `<div class="fv-rick-wrap"><iframe class="fv-rick" tabindex="-1" aria-hidden="true"
      src="https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?autoplay=1&controls=0&disablekb=1&fs=0&rel=0&modestbranding=1&iv_load_policy=3&cc_load_policy=0&playsinline=1"
      title="Video" allow="autoplay; encrypted-media" referrerpolicy="strict-origin-when-cross-origin"></iframe><div class="fv-rick-shield"></div></div>`,
    '<button class="btn btn-g" onclick="closeMdl()">Okay, okay</button>',true);
  fvXl(true);
  const vid=document.querySelector('video.fv-rick-video');
  if(vid){vid.play().catch(()=>{vid.muted=true;vid.play().catch(()=>{})})}   // sound if the browser allows it
  const m=document.querySelector('.mdl');
  const x=document.getElementById('mdlX'),f=document.getElementById('mdlF');
  if(m)m.classList.add('rick');
  if(x)x.style.visibility='hidden';
  if(f)f.style.visibility='hidden';
  const HOLD=8000;                      // how long the popup can't be closed
  App.mdlLockUntil=Date.now()+HOLD;
  setTimeout(()=>{
    App.mdlLockUntil=0;
    if(x)x.style.visibility='';if(f)f.style.visibility='';
    if(m)m.classList.remove('rick');
    api('/api/easter/rickroll',{method:'POST'}).catch(()=>{});
  },HOLD);
}
function notifIncoming(item){
  if(_notifs.some(n=>n.id===item.id))return;
  _notifs.unshift(item);_notifs=_notifs.slice(0,100);
  _notifUnread=_notifs.filter(n=>!n.read).length;notifBadges();
  if(_notifPanel)notifRenderList();
  if((item.data||{}).ongoing){appJobFromNotif(item);return}   // an install in progress: no toast, the tray shows it
  // Transfers already toast from the tray; everything else gets a toast too.
  if(item.kind!=='files')toast(item.title,item.level==='success'?'success':item.level,{hint:item.body||undefined,ref:item.ref||undefined,
    action:item.link?{label:'Open',fn:()=>go(item.link)}:undefined});
}
// An existing notification changed (a progress bar moved, "Installing" became "Installed").
function notifUpdate(item){
  const i=_notifs.findIndex(n=>n.id===item.id);
  if(i>=0)_notifs[i]=item;else{_notifs.unshift(item);_notifs=_notifs.slice(0,100)}
  _notifUnread=_notifs.filter(n=>!n.read).length;notifBadges();
  if(_notifPanel)notifRenderList();
  appJobFromNotif(item);
}
function _timeAgo(ts){const s=Date.now()/1000-ts;if(s<60)return'just now';if(s<3600)return`${Math.floor(s/60)}m ago`;
  if(s<86400)return`${Math.floor(s/3600)}h ago`;return new Date(ts*1000).toLocaleDateString()}
function notifRenderList(){
  const list=_notifPanel&&_notifPanel.querySelector('.notif-list');if(!list)return;
  if(!_notifs.length){list.innerHTML='<div class="notif-empty">No notifications yet</div>';return}
  list.innerHTML=_notifs.map(n=>`<div class="notif-item ${n.read?'':'unread'} lv-${esc(n.level)}" data-id="${escA(n.id)}" data-link="${escA(n.link||'')}">
      <span class="notif-ico${(n.data||{}).ongoing?' spin':''}">${(n.data||{}).ongoing?'\u21BB':(_NOTIF_ICON[n.level]||'\u2139')}</span>
      <div class="notif-main"><div class="notif-title">${esc(n.title)}</div>
        ${n.body?`<div class="notif-text">${esc(n.body)}</div>`:''}
        ${(n.data||{}).ongoing?`<div class="as-prog${n.data.progress==null?' indet':''}" style="margin-top:7px"><i style="width:${n.data.progress==null?40:Math.floor(n.data.progress)}%"></i></div>`:''}
        <div class="notif-time">${_timeAgo(n.ts)}${n.ref?` \u00b7 ref <code>${esc(n.ref)}</code>`:''}</div></div>
      <button class="notif-del" title="Remove" data-del="${escA(n.id)}">\u00d7</button></div>`).join('');
}
async function notifMarkRead(ids){
  const body=ids?{ids}:{all:true};
  _notifs.forEach(n=>{if(!ids||ids.includes(n.id))n.read=true});
  _notifUnread=_notifs.filter(n=>!n.read).length;notifBadges();notifRenderList();
  await api('/api/notifications/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
}
function notifClose(){if(_notifPanel){_notifPanel.remove();_notifPanel=null}}
function notifToggle(anchor){
  if(_notifPanel){notifClose();return}
  if(typeof hsClosePop==='function')hsClosePop();
  const p=document.createElement('div');p.className='notif-panel';
  p.innerHTML=`<div class="notif-head"><span>Notifications</span><div style="display:flex;gap:6px">
      <button class="btn btn-g btn-sm" data-act="read">Mark all read</button>
      <button class="btn btn-g btn-sm" data-act="clear">Clear</button></div></div><div class="notif-list"></div>`;
  document.body.appendChild(p);_notifPanel=p;
  const r=anchor.getBoundingClientRect();
  const w=Math.min(380,window.innerWidth-16);p.style.width=w+'px';
  p.style.left=Math.max(8,Math.min(r.left,window.innerWidth-w-8))+'px';
  if(r.top>window.innerHeight/2){p.style.bottom=(window.innerHeight-r.top+8)+'px'}else{p.style.top=(r.bottom+8)+'px'}
  notifRenderList();
  p.addEventListener('click',async e=>{
    e.stopPropagation();
    const act=e.target.closest('[data-act]')?.dataset.act;
    if(act==='read'){notifMarkRead();return}
    if(act==='clear'){const r=await api('/api/notifications',{method:'DELETE'});if(r.ok){_notifs=[];_notifUnread=0;notifBadges();notifRenderList()}else errToast(await r.json());return}
    const del=e.target.closest('[data-del]')?.dataset.del;
    if(del){await api('/api/notifications?id='+encodeURIComponent(del),{method:'DELETE'});
      const was=_notifs.find(n=>n.id===del);_notifs=_notifs.filter(n=>n.id!==del);if(was&&!was.read)_notifUnread--;notifBadges();notifRenderList();return}
    const it=e.target.closest('.notif-item');
    if(it){notifMarkRead([it.dataset.id]);if(it.dataset.link){notifClose();go(it.dataset.link)}}
  });
  // Opening the panel counts as having seen what's in it (after a moment).
  setTimeout(()=>{if(_notifPanel===p&&_notifUnread)notifMarkRead()},2500);
}
document.addEventListener('click',e=>{if(_notifPanel&&!_notifPanel.contains(e.target)&&!e.target.closest('.notif-bell'))notifClose()});
document.addEventListener('keydown',e=>{if(e.key==='Escape')notifClose()});
function notifMountBells(){
  const mk=(cls)=>{const b=document.createElement('button');b.className='notif-bell '+cls;b.type='button';b.title='Notifications';
    b.setAttribute('aria-label','Notifications');b.innerHTML=_bellSvg()+'<span class="notif-badge" style="display:none"></span>';
    b.addEventListener('click',e=>{e.stopPropagation();notifToggle(b)});return b};
  const mob=document.getElementById('mobileTopbar');
  if(mob&&!mob.querySelector('.notif-bell'))mob.appendChild(mk('notif-mob'));
  const sbBottom=document.querySelector('.sidebar .sb-bottom');
  if(sbBottom&&!sbBottom.querySelector('.notif-bell')){
    const b=mk('sbtn notif-sb');b.innerHTML=`<span class="sb-icon">${_bellSvg()}</span><span class="sb-lbl">Notifications</span><span class="notif-badge" style="display:none"></span>`;
    sbBottom.insertBefore(b,sbBottom.querySelector('a[href="/logout"]'));
  }
  const hs=document.querySelector('#hsTopbar .hs-right');
  if(hs&&!hs.querySelector('.notif-bell'))hs.insertBefore(mk('hs-ibtn notif-hs'),hs.querySelector('.hs-avatar'));
  notifBadges();
}


// One load per script, and every caller waits for THAT load to finish. (This used to resolve at once
// whenever the <script> tag was already in the page, even if it hadn't run yet, so a second caller
// went ahead and hit "Terminal is not defined".)
const _scriptLoads=new Map();
function loadScript(src){
  if(_scriptLoads.has(src))return _scriptLoads.get(src);
  const p=new Promise((res,rej)=>{
    if(document.querySelector(`script[src="${src}"]`)){res();return}   // included by the page itself: already run
    const s=document.createElement('script');s.src=src;
    s.onload=()=>res();
    s.onerror=()=>{_scriptLoads.delete(src);s.remove();rej(new Error('Could not load '+src))};
    document.head.appendChild(s);
  });
  _scriptLoads.set(src,p);
  return p;
}

function collectClientInfo(){
  try{
    return {
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone||'',
      language: navigator.language||'',
      platform: navigator.platform||navigator.userAgentData?.platform||'',
      screen: (screen.width&&screen.height)?`${screen.width}x${screen.height}`:'',
      viewport: `${window.innerWidth}x${window.innerHeight}`,
      pixel_ratio: String(window.devicePixelRatio||1),
      touch: String('ontouchstart' in window || navigator.maxTouchPoints>0),
      color_depth: String(screen.colorDepth||''),
    };
  }catch(e){return {}}
}

// ── LOGIN ──────────────────────────────────────────────────────
if(location.pathname==='/login'||document.getElementById('lcard')){
  const card=document.getElementById('lcard');
  const stat=document.getElementById('lstat');
  const lock=document.getElementById('lockSvg'); // may be null now (image logo has no animation)
  const prog=document.getElementById('progW');
  const fill=document.getElementById('progF');
  const btn=document.getElementById('lBtn');
  let busy=false;
  const eyeBtn=document.getElementById('eyeBtn');
  const eyeIco=document.getElementById('eyeIco');
  const EO='<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/>';
  const EC='<path d="M17.94 17.94A10.94 10.94 0 0 1 12 19c-7 0-11-7-11-7a18.5 18.5 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 7 11 7a18.5 18.5 0 0 1-2.16 3.19M14.12 14.12a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>';
  if(eyeBtn)eyeBtn.addEventListener('click',()=>{const p=document.getElementById('upass'),s=p.type==='text';p.type=s?'password':'text';eyeIco.innerHTML=s?EO:EC});
  const un=document.getElementById('uname'),pw=document.getElementById('upass');
  const codeIn=document.getElementById('ucode'),codeRow=document.getElementById('codeRow'),codeBack=document.getElementById('codeBack');
  let needCode=false;   // true once the password was accepted and the account wants a 2FA code
  function codeStep(on){
    needCode=on;
    document.getElementById('unameRow').style.display=on?'none':'';
    document.getElementById('passRow').style.display=on?'none':'';
    codeRow.style.display=on?'':'none';codeBack.style.display=on?'':'none';
    btn.textContent=on?'Verify':'Sign In';
    if(on){stat.textContent='Enter the 6-digit code from your authenticator app, or a backup code';stat.className='lstatus';codeIn.value='';setTimeout(()=>codeIn.focus(),50)}
    else{stat.textContent='Sign in to continue';stat.className='lstatus';pw.value='';codeIn.value='';pw.focus()}
  }
  if(codeIn)codeIn.addEventListener('keydown',e=>{if(e.key==='Enter')doLogin()});
  if(codeBack)codeBack.addEventListener('click',()=>codeStep(false));
  if(un)un.addEventListener('keydown',e=>{if(e.key==='Enter')pw.focus()});
  if(pw)pw.addEventListener('keydown',e=>{if(e.key==='Enter')doLogin()});
  if(btn)btn.addEventListener('click',doLogin);
async function doLogin(){
    if(busy)return;busy=true;btn.disabled=true;stat.className='lstatus';
    const username=un.value.trim(),password=pw.value,code=needCode?codeIn.value.trim():'';
    if(!username||!password){stat.textContent='Enter username and password';stat.className='lstatus err';busy=false;btn.disabled=false;return}
    if(needCode&&!code){stat.textContent='Enter your authentication code';stat.className='lstatus err';busy=false;btn.disabled=false;return}
    try{
      const r=await fetch('/auth',{method:'POST',credentials:'same-origin',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password,code,client_info:collectClientInfo()})});
      if(r.ok){
        lock?.classList.add('lock-open');stat.textContent='Access granted\u2026';stat.className='lstatus ok';
        prog.classList.add('show');requestAnimationFrame(()=>fill.style.width='100%');
        setTimeout(()=>location.href='/',950);return;
      }
      const d=await r.json().catch(()=>({}));
      if(r.status===429){
        let rem=d.retry_after||30;stat.textContent=`Too many attempts \u2014 wait ${rem}s`;stat.className='lstatus err';
        const t=setInterval(()=>{rem--;stat.textContent=`Too many attempts \u2014 wait ${rem}s`;if(rem<=0){clearInterval(t);
          if(needCode){stat.textContent='Enter the 6-digit code from your authenticator app, or a backup code';stat.className='lstatus'}else{stat.textContent='Sign in to continue';stat.className='lstatus'}
          busy=false;btn.disabled=false}},1000);
        return;
      }
      if(d.error==='2fa_required'){codeStep(true);busy=false;btn.disabled=false;return}
      if(d.error==='2fa_invalid'||d.error==='2fa_unreadable'){
        card.classList.add('shake');
        stat.textContent=d.error==='2fa_invalid'?'That code isn\u2019t right \u2014 try again':'This account\u2019s two-factor data can\u2019t be read. Ask the Super User to reset it.';
        stat.className='lstatus err';codeIn.value='';codeIn.focus();setTimeout(()=>card.classList.remove('shake'),450);
        busy=false;btn.disabled=false;return;
      }
      card.classList.add('shake');stat.textContent='Invalid username or password';stat.className='lstatus err';
      pw.value='';pw.focus();setTimeout(()=>card.classList.remove('shake'),450);
    }catch(e){stat.textContent='Network error';stat.className='lstatus err'}
    busy=false;btn.disabled=false;
  }
  if(un)setTimeout(()=>un.focus(),80);
}

// ── SETUP WIZARD ──────────────────────────────────────────────────
if(document.getElementById('scard')){
  const scard=document.getElementById('scard');
  const sstat=document.getElementById('sstat');
  const sprog=document.getElementById('sprogW');
  const sfill=document.getElementById('sprogF');
  const sbtn=document.getElementById('sBtn');
  const sname=document.getElementById('sname'),spass=document.getElementById('spass'),sconf=document.getElementById('sconf');
  const sEyeBtn=document.getElementById('sEyeBtn'),sEyeIco=document.getElementById('sEyeIco');
  const EO='<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/>';
  const EC='<path d="M17.94 17.94A10.94 10.94 0 0 1 12 19c-7 0-11-7-11-7a18.5 18.5 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 7 11 7a18.5 18.5 0 0 1-2.16 3.19M14.12 14.12a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>';
  if(sEyeBtn)sEyeBtn.addEventListener('click',()=>{const s=spass.type==='text';spass.type=s?'password':'text';sEyeIco.innerHTML=s?EO:EC});
  let busy=false;
  [sname,spass,sconf].forEach(el=>el?.addEventListener('keydown',e=>{if(e.key==='Enter')doSetup()}));
  if(sbtn)sbtn.addEventListener('click',doSetup);
  async function doSetup(){
    if(busy)return;
    const username=sname.value.trim(),password=spass.value,confirm=sconf.value;
    if(!username||!password){sstat.textContent='Choose a username and password';sstat.className='lstatus err';return}
    if(!/^[a-z0-9_-]{3,32}$/i.test(username)){sstat.textContent='Username: 3-32 characters, letters/numbers/-/_ only';sstat.className='lstatus err';return}
    if(password.length<8){sstat.textContent='Password must be at least 8 characters';sstat.className='lstatus err';return}
    if(password!==confirm){sstat.textContent='Passwords do not match';sstat.className='lstatus err';return}
    busy=true;sbtn.disabled=true;
    try{
      const r=await fetch('/api/setup',{method:'POST',credentials:'same-origin',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password,client_info:collectClientInfo()})});
      const d=await r.json().catch(()=>({}));
      if(r.ok&&d.ok){
        sstat.textContent='Account created \u2014 let\u2019s finish setting up\u2026';sstat.className='lstatus ok';
        sprog.classList.add('show');requestAnimationFrame(()=>sfill.style.width='100%');
        setTimeout(()=>location.href='/setup',950);return;
      }
      scard.classList.add('shake');sstat.textContent=d.error||'Setup failed';sstat.className='lstatus err';
      setTimeout(()=>scard.classList.remove('shake'),450);
    }catch(e){sstat.textContent='Network error';sstat.className='lstatus err'}
    busy=false;sbtn.disabled=false;
  }
  setTimeout(()=>sname?.focus(),80);
}

// ── ONBOARDING WIZARD (after the account: every setting in .env.example) ────
// The steps come from the server, which reads "first setup/.env.example" — so the wizard asks for
// exactly the settings that file lists. First step: the basics. Then one optional step per group
// of advanced settings (each can be skipped, or "Use defaults for the rest"), then a review.
if(document.getElementById('obCard')){
  const obBody=document.getElementById('obBody'),obStat=document.getElementById('obStat');
  const backBtn=document.getElementById('obBackBtn'),nextBtn=document.getElementById('obNextBtn'),skipBtn=document.getElementById('obSkipBtn');
  const pills=document.getElementById('obSteps');
  let groups=[],obIdx=0,obBusy=false;
  const OB={values:{},initial:{},termPw:''};
  const fid=k=>'obf-'+k;
  const REVIEW=()=>groups.length;                       // the review is the step after the last group

  function fieldHtml(st){
    const v=OB.values[st.key]??'',id=fid(st.key);
    let ctl='';
    if(st.type==='bool'){
      ctl=`<label class="toggle"><input type="checkbox" id="${id}" ${String(v)==='true'?'checked':''}/><span class="toggle-sl"></span></label>`;
      return `<div class="ob-field ob-toggle"><div class="ob-field-l"><label class="fgl" for="${id}">${esc(st.label)}</label>
        ${st.help?`<div class="ob-hint">${esc(st.help)}</div>`:''}</div>${ctl}</div>`;
    }
    if(st.type==='choice'){
      ctl=`<select class="fgi" id="${id}">${(st.options||[]).map(o=>`<option value="${escA(o.value)}" ${o.value===v?'selected':''}>${esc(o.label)}</option>`).join('')}</select>`;
    }else if(st.type==='int'){
      ctl=`<input class="fgi" type="number" min="0" step="1" id="${id}" value="${escA(v)}"/>`;
    }else{
      const ph=st.secret?(st.is_set?'already set — leave blank to keep':''):(st.placeholder||'');
      ctl=`<input class="fgi" type="${st.secret?'password':'text'}" id="${id}" value="${escA(st.secret?'':v)}" placeholder="${escA(ph)}" autocomplete="off" spellcheck="false"/>`;
    }
    const def=st.type==='choice'?((st.options||[]).find(o=>o.value===st.default)||{}).label:st.default;
    return `<div class="ob-field"><label class="fgl" for="${id}">${esc(st.label)}</label>${ctl}
      <div class="ob-hint">${st.help?esc(st.help)+' ':''}${(def!==undefined&&def!=='')?`<span class="ob-def">Default: ${esc(String(def))}</span>`:''}</div></div>`;
  }
  function collect(i){
    const g=groups[i];if(!g)return;
    g.settings.forEach(st=>{
      const el=document.getElementById(fid(st.key));if(!el)return;
      if(st.type==='bool')OB.values[st.key]=el.checked?'true':'false';
      else if(st.secret){if(el.value!=='')OB.values[st.key]=el.value}
      else OB.values[st.key]=el.value.trim();
    });
    const tp=document.getElementById('obTermPw');if(tp)OB.termPw=tp.value;
  }
  const changed=()=>{
    const out={};
    groups.forEach(g=>g.settings.forEach(st=>{
      const now=OB.values[st.key];
      if(now!==undefined&&now!==OB.initial[st.key])out[st.key]=now;
    }));
    return out;
  };
  function paintPills(){
    const basic=groups[obIdx]&&groups[obIdx].basic,review=obIdx===REVIEW();
    pills.innerHTML=`<span class="ob-step ${basic?'active':'done'}">Basics</span>
      <span class="ob-step ${(!basic&&!review)?'active':(review?'done':'')}">Advanced</span>
      <span class="ob-step ${review?'active':''}">Review</span>`;
  }
  function stepHtml(i){
    if(i===REVIEW()){
      const ch=changed(),keys=Object.keys(ch),flat={};
      groups.forEach(g=>g.settings.forEach(s=>flat[s.key]=s));
      const rows=keys.map(k=>{
        const st=flat[k];let was=OB.initial[k],now=ch[k];
        if(st.secret){was='';now='••••••••'}
        if(st.type==='choice'){const lab=v=>((st.options||[]).find(o=>o.value===v)||{}).label||v;was=lab(was);now=lab(now)}
        if(st.type==='bool'){const lab=v=>v==='true'?'On':'Off';was=lab(was);now=lab(now)}
        return `<div class="ob-review-item"><span class="k">${esc(st.label)}</span><span>${was!==''&&was!==undefined?esc(String(was))+' → ':''}${esc(String(now))}</span></div>`;
      });
      if(OB.termPw)rows.push(`<div class="ob-review-item"><span class="k">Terminal password</span><span>Will be set</span></div>`);
      return `<div class="ob-body-step">
        <div class="ob-hint" style="margin-top:0">${rows.length?`These ${rows.length} change${rows.length===1?'':'s'} will be written to .env. Everything you left alone keeps its default, and any of it can be changed later in Settings or by editing .env. A restart is needed to apply them.`
          :'Nothing was changed, so everything runs on its defaults. You can change any of it later in Settings or by editing .env.'}</div>
        ${rows.join('')}
      </div>`;
    }
    const g=groups[i];
    const n=groups.length-1;
    const crumb=g.basic?'The essentials — almost everyone sets these'
      :`Advanced (optional) · ${i} of ${n} — skip any step to keep the defaults`;
    const term=g.id==='terminal'?`<div class="ob-field"><label class="fgl" for="obTermPw">Terminal password (optional, recommended)</label>
        <input class="fgi" type="password" id="obTermPw" value="${escA(OB.termPw||'')}" placeholder="Leave blank for no extra password" autocomplete="new-password"/>
        <div class="ob-hint">A second password required to open the Terminal tab, on top of the account login. It is stored hashed, not in .env.</div></div>`:'';
    return `<div class="ob-body-step"><div class="ob-crumb">${esc(crumb)}</div>
      <div class="ob-gtitle">${esc(g.title)}</div>
      ${g.settings.map(fieldHtml).join('')}${term}</div>`;
  }
  function renderStep(){
    paintPills();
    obBody.innerHTML=stepHtml(obIdx);
    const review=obIdx===REVIEW(),first=obIdx===0;
    backBtn.style.visibility=first?'hidden':'visible';
    nextBtn.textContent=review?'Finish Setup':(obIdx===REVIEW()-1?'Review':'Next');
    skipBtn.textContent=first?'Skip setup':(review?'':'Use defaults for the rest');
    skipBtn.style.visibility=review?'hidden':'visible';
    obStat.textContent='';obStat.className='lstatus';
    obBody.scrollTop=0;
  }
  backBtn.addEventListener('click',()=>{if(obIdx>0){collect(obIdx);obIdx--;renderStep()}});
  skipBtn.addEventListener('click',async()=>{
    if(obBusy)return;
    if(obIdx>0&&obIdx<REVIEW()){                       // keep what was set so far, defaults for the rest
      collect(obIdx);obIdx=REVIEW();renderStep();return;
    }
    obBusy=true;skipBtn.disabled=true;                   // skip the whole setup
    try{await fetch('/api/setup/finish',{method:'POST',credentials:'same-origin'});location.href='/'}
    catch(e){obStat.textContent='Network error';obStat.className='lstatus err';obBusy=false;skipBtn.disabled=false}
  });
  nextBtn.addEventListener('click',async()=>{
    if(obBusy)return;
    if(obIdx<REVIEW()){collect(obIdx);obIdx++;renderStep();return}
    obBusy=true;nextBtn.disabled=true;backBtn.disabled=true;skipBtn.disabled=true;
    obStat.textContent='Saving…';obStat.className='lstatus';
    const fail=msg=>{obStat.textContent=msg;obStat.className='lstatus err';obBusy=false;nextBtn.disabled=false;backBtn.disabled=false;skipBtn.disabled=false};
    try{
      const ch=changed();let restart=false;
      if(Object.keys(ch).length){
        const r=await fetch('/api/setup/env',{method:'POST',credentials:'same-origin',
          headers:{'Content-Type':'application/json'},body:JSON.stringify({values:ch})});
        const d=await r.json().catch(()=>({}));
        if(!r.ok||!d.ok)return fail(d.error||'Failed to save');
        restart=!!d.restart_required;
      }
      if(OB.termPw){
        const r=await fetch('/api/setup/configure',{method:'POST',credentials:'same-origin',
          headers:{'Content-Type':'application/json'},body:JSON.stringify({terminal_password:OB.termPw})});
        const d=await r.json().catch(()=>({}));
        if(!r.ok||!d.ok)return fail(d.error||'Failed to set the terminal password');
        restart=restart||!!d.restart_required;
      }
      await fetch('/api/setup/finish',{method:'POST',credentials:'same-origin'});
      if(restart)showRestartScreen();else location.href='/';
    }catch(e){fail('Network error')}
  });
  function showRestartScreen(){
    pills.style.display='none';
    document.querySelector('.ob-nav').style.display='none';
    obBody.innerHTML=`<div class="ob-body-step" style="text-align:center;align-items:center">
      <div style="font-size:36px">✅</div>
      <div style="font-weight:700;font-size:15px">Setup saved</div>
      <div class="ob-hint" style="text-align:center">These settings need a restart to take effect. If Void OS is running as a
        service (the installer sets one up), it'll come back up on its own. Otherwise, stop and start it
        again (<code>python voidos.py</code> or your usual command).</div>
      <button class="btn btn-d" id="obRestartBtn" style="margin-top:6px">Restart Now</button>
      <button class="btn btn-g btn-sm" id="obLaterBtn">I'll restart it myself</button>
    </div>`;
    document.getElementById('obRestartBtn')?.addEventListener('click',async e=>{
      e.target.disabled=true;e.target.textContent='Restarting…';
      try{await fetch('/api/admin/restart',{method:'POST',credentials:'same-origin'})}catch(err){}
      obStat.textContent='Waiting for Void OS to come back…';obStat.className='lstatus';
      Conn.restarting();  // reloads once the server actually answers, not after a guess
    });
    document.getElementById('obLaterBtn')?.addEventListener('click',()=>location.href='/');
  }
  (async()=>{
    obBody.innerHTML='<div class="loading"><div class="loader"></div></div>';
    try{
      const r=await fetch('/api/setup/env-schema',{credentials:'same-origin'});
      const d=await r.json();
      if(!r.ok)throw new Error(d.error||'Could not load the settings');
      groups=d.groups||[];
      groups.forEach(g=>g.settings.forEach(st=>{OB.values[st.key]=st.value??'';OB.initial[st.key]=st.value??''}));
    }catch(e){
      obStat.textContent=(e&&e.message)||'Could not load the settings list';obStat.className='lstatus err';
      obBody.innerHTML='<div class="ob-hint">The settings list could not be loaded. You can skip this and change everything later in Settings.</div>';
      groups=[];skipBtn.textContent='Skip setup';return;
    }
    renderStep();
  })();
}


// ── APP ────────────────────────────────────────────────────────
if(document.getElementById('vroot')){
let curV='',statsPoll=null,_tunnelPoll=null,_addrPoll=null;
// Bumped by go() on every navigation (and self-bumped by a couple of parameterized
// render functions that can legitimately re-fire for the *same* view, e.g. rFiles
// changing folders). Every async render function captures this at the start and
// re-checks it after each await before touching the DOM — a stale call whose view
// was superseded in the meantime bails out instead of corrupting whatever's now
// showing. See rCtrs/rFiles for the two spots this was an actual visible bug.
let _renderGen=0;
let _ctrlSock=null,_ctrlReconnectDelay=1000,_ctrlReconnectTimer=null,_termAccessRevoked=false;
// Fill in the page-wide bridge (see `var App` near the top of the script).
Object.defineProperties(App,{curV:{get:()=>curV,configurable:true},curP:{get:()=>curP,configurable:true}});
App.reconnectControl=()=>{if(!_ctrlSock||_ctrlSock.readyState>1){_ctrlReconnectDelay=1000;initControlSocket()}};
(function(){var b=document.querySelectorAll('.blob');if(!b.length)return;
  var r=function(a,z){return a+Math.random()*(z-a)},sg=function(){return Math.random()<.5?-1:1};
  b.forEach(function(el){
    el.style.left=r(-5,85).toFixed(1)+'%';el.style.top=r(-5,85).toFixed(1)+'%';el.style.right='auto';el.style.bottom='auto';
    el.style.setProperty('--x0',(sg()*r(5,18)).toFixed(1)+'vw');el.style.setProperty('--y0',(sg()*r(5,18)).toFixed(1)+'vh');
    el.style.setProperty('--x1',(sg()*r(16,34)).toFixed(1)+'vw');el.style.setProperty('--y1',(sg()*r(16,34)).toFixed(1)+'vh');
    el.style.setProperty('--s1',r(.85,1.35).toFixed(2));
    el.style.animationDelay='-'+r(0,20).toFixed(1)+'s';       // start part-way through, so no two loads look alike
  });})();
if(!SETTINGS.show_blobs)document.body.classList.add('blobs-hidden');
if(SETTINGS.blob_speed==='static')document.body.classList.add('blob-speed-static');
else if(SETTINGS.blob_speed==='fast')document.body.classList.add('blob-speed-fast');

// ── Sidebar: one toggle function drives the desktop pin button, the
// mobile hamburger, and the backdrop. On desktop it's a remembered
// per-user preference; on mobile it's just an overlay drawer that
// always starts closed (so it doesn't cover the screen on load) and
// auto-closes again after picking a destination. ───────────────────
const MOBILE_BP=768;
function isMobile(){return window.innerWidth<=MOBILE_BP}
function toggleSidebar(force){
  const sb=document.getElementById('sb');
  if(!sb)return;
  const willOpen=typeof force==='boolean'?force:!sb.classList.contains('open');
  sb.classList.toggle('open',willOpen);
  document.getElementById('navToggle')?.classList.toggle('active',willOpen);
  document.getElementById('mobileMenuBtn')?.classList.toggle('active',willOpen);
  document.getElementById('sbBackdrop')?.classList.toggle('show',willOpen&&isMobile());
  if(!isMobile())saveSettings({sidebar_pinned:willOpen}); // only persist as a desktop preference
}
window.toggleSidebar=toggleSidebar;
(function(){
  document.getElementById('navToggle')?.addEventListener('click',e=>{e.stopPropagation();toggleSidebar()});
  document.getElementById('mobileMenuBtn')?.addEventListener('click',e=>{e.stopPropagation();toggleSidebar()});
  document.getElementById('sbBackdrop')?.addEventListener('click',()=>toggleSidebar(false));
  // Restore the desktop pin preference, but never auto-open the mobile overlay on load
  if(!isMobile()&&SETTINGS.sidebar_pinned===true)toggleSidebar(true);
})();

// Small helper: persist one or more settings to the server for the CURRENT user only
async function saveSettings(patch){
  Object.assign(SETTINGS,patch); // optimistic local update
  try{
    const r=await api('/api/me/settings',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(patch)});
    const d=await r.json();
    if(d.ok)SETTINGS=d.settings;else _lastSaveErr=d;
    return d.ok;
  }catch(e){_lastSaveErr={error:e.message};return false}
}
let _lastSaveErr=null; // the failed response, so callers can show *why*

const THEMES=[
  {id:'horizon',name:'Horizon',bg:'#0b1220',tx:'#eef6ff',c1:'#1fb6ff',c2:'#6fdcff'},
  {id:'void',name:'Void',bg:'#0a0a12',tx:'#f3f1fb',c1:'#6d4aff',c2:'#9b85ff'},
  {id:'light',name:'Daylight',bg:'#f4f3fa',tx:'#1c1830',c1:'#6d4aff',c2:'#5636e0'},
  {id:'emerald',name:'Emerald',bg:'#050d0c',tx:'#eafaf3',c1:'#18a976',c2:'#4eeab0'},
  {id:'crimson',name:'Crimson',bg:'#0f0708',tx:'#fdf1ef',c1:'#d92b3f',c2:'#ff6f6f'},
];
function themeSwatch(t,current){
  const active=t.id===current;
  return `<button class="theme-swatch ${active?'active':''}" data-theme-id="${t.id}" style="background:${t.bg}" title="${esc(t.name)}">
    <span class="theme-swatch-dots"><span style="background:${t.c1}"></span><span style="background:${t.c2}"></span></span>
    <span class="theme-swatch-name" style="color:${t.tx}">${esc(t.name)}</span>
  </button>`;
}
async function setTheme(themeId){
  const prev=document.documentElement.getAttribute('data-theme')||'horizon';
  document.documentElement.setAttribute('data-theme',themeId); // instant preview, no flash
  const ok=await saveSettings({theme:themeId});
  if(ok){toast('Theme updated','success');rSettings()}
  else{errToast(_lastSaveErr,'Couldn\u2019t save the theme');document.documentElement.setAttribute('data-theme',prev)}
}
window.setTheme=setTheme;

async function setBlobSpeed(speed){
  document.body.classList.remove('blob-speed-static','blob-speed-fast'); // instant preview
  if(speed==='static')document.body.classList.add('blob-speed-static');
  else if(speed==='fast')document.body.classList.add('blob-speed-fast');
  document.querySelectorAll('#blobSpeedSeg .seg-btn').forEach(b=>b.classList.toggle('active',b.dataset.speed===speed));
  const ok=await saveSettings({blob_speed:speed});
  if(!ok)errToast(_lastSaveErr,'Couldn\u2019t save that setting');
}
window.setBlobSpeed=setBlobSpeed;

// ── UI Mode (Classic sidebar vs. home-screen-style home screen) ──────────────
const WALLPAPER_PRESET_CSS={
  nebula:'radial-gradient(circle at 20% 20%,#3a1fbf,transparent 55%),radial-gradient(circle at 80% 30%,#1fb6ff,transparent 50%),radial-gradient(circle at 50% 85%,#0d0b18,transparent 60%),#0b1220',
  aurora:'radial-gradient(circle at 15% 80%,#18a976,transparent 55%),radial-gradient(circle at 85% 15%,#1fb6ff,transparent 50%),#081714',
  sunset:'radial-gradient(circle at 30% 100%,#ff8a5c,transparent 55%),radial-gradient(circle at 80% 0%,#d92b3f,transparent 55%),#160b0c',
  mono:'linear-gradient(160deg,#1c2333,#0b1220)',
  forest:'radial-gradient(circle at 70% 20%,#157349,transparent 55%),radial-gradient(circle at 20% 80%,#0d5c40,transparent 55%),#050d0c',
  midnight:'radial-gradient(circle at 50% 0%,#4a3aa8,transparent 60%),#0a0a12',
};
function wallpaperSwatches(current){
  let html='<button class="wallpaper-swatch ' + (!current?'active':'') + '" data-wallpaper-id="" title="None (theme default)" style="background:var(--sf2)">\u2715</button>';
  html+=Object.keys(WALLPAPER_PRESET_CSS).map(id=>
    `<button class="wallpaper-swatch ${current==='preset:'+id?'active':''}" data-wallpaper-id="preset:${id}" title="${id}" style="background:${WALLPAPER_PRESET_CSS[id]}"></button>`
  ).join('');
  if(current.startsWith('custom:')){
    html+=`<button class="wallpaper-swatch active" data-wallpaper-id="${escA(current)}" title="Your upload" style="background:url('${escA(SETTINGS.wallpaper_url||'')}') center/cover"></button>`;
  }
  return html;
}
function applyWallpaper(){
  const shell=document.querySelector('.shell');if(!shell)return;
  const w=SETTINGS.wallpaper||'';
  if(!w){shell.style.removeProperty('--wallpaper-css');shell.classList.remove('has-wallpaper');return}
  let css='';
  if(w.startsWith('preset:'))css=WALLPAPER_PRESET_CSS[w.slice(7)]||'';
  else if(w.startsWith('custom:')&&SETTINGS.wallpaper_url)css=`url('${SETTINGS.wallpaper_url}') center/cover no-repeat`;
  if(css){shell.style.setProperty('--wallpaper-css',css);shell.classList.add('has-wallpaper')}
}
async function setWallpaper(id){
  const prev=SETTINGS.wallpaper;
  SETTINGS.wallpaper=id;applyWallpaper(); // instant preview
  const ok=await saveSettings({wallpaper:id});
  if(ok){toast('Wallpaper updated','success');rSettings()}
  else{errToast(_lastSaveErr,'Couldn\u2019t save the wallpaper');SETTINGS.wallpaper=prev;applyWallpaper()}
}
function applyUiMode(){
  const shell=document.querySelector('.shell');if(!shell)return;
  const mode=SETTINGS.ui_mode_resolved||'classic';
  const was=shell.classList.contains('hs');
  shell.classList.toggle('hs',mode==='home');
  const changed=was!==(mode==='home');
  ensureHsTopbar();
  if(typeof notifMountBells==='function')notifMountBells();
  applyWallpaper();
  if(!changed)return;
  // The page that is open was drawn for the other interface (the home screen has its own
  // System section and clock), and anything sized to the window must follow the sidebar
  // appearing or disappearing. Redraw what depends on it; leave everything else where it is.
  if(typeof hsUpdateCrumb==='function')hsUpdateCrumb();
  if(curV==='dashboard'&&typeof rDash==='function'){
    const gen=_renderGen;
    Promise.resolve().then(()=>{if(gen===_renderGen&&curV==='dashboard')rDash()});
  }
  requestAnimationFrame(()=>window.dispatchEvent(new Event('resize')));
}
async function setUiMode(mode){
  const prev={ui_mode:SETTINGS.ui_mode,ui_mode_resolved:SETTINGS.ui_mode_resolved};
  SETTINGS.ui_mode=mode;
  if(mode!=='auto')SETTINGS.ui_mode_resolved=mode;
  applyUiMode(); // instant preview
  const ok=await saveSettings({ui_mode:mode});
  if(ok){
    try{const me=await(await api('/api/me')).json();SETTINGS=me.settings||SETTINGS}catch(e){}
    applyUiMode();toast('Interface updated','success');rSettings();
  }else{Object.assign(SETTINGS,prev);applyUiMode();errToast(_lastSaveErr,'Couldn\u2019t switch the interface')}
}
async function setUiModeDefault(mode){
  const r=await(await api('/api/server-settings',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({ui_mode_default:mode})})).json();
  if(r.ok){
    toast('Server default updated','success');
    document.querySelectorAll('#uiModeDefaultSeg .seg-btn').forEach(b=>b.classList.toggle('active',b.dataset.mode===mode));
    if(SETTINGS.ui_mode==='auto'){SETTINGS.ui_mode_resolved=mode;applyUiMode()}
  }else errToast(r,'Failed to save');
}
// Builds (once) or removes the sidebar-free top bar used in home-screen mode.
// Built once and toggled with the .hs class rather than re-rendered per
// navigation. HS_VIEWS below is the single list of "system apps" shared
// by the launcher, global search and the home-screen System section, gated
// by the same permission flags the classic sidebar uses — so the two chrome
// styles can't drift apart. (home-screen mode used to hide the sidebar and with
// it the only Logout button — it lives here now.)
function hsViews(){
  return [
    {v:'dashboard',n:'Home',i:'\u{1F3E0}',show:true},
    {v:'files',n:'Files',i:'\u{1F4C1}',show:true},
    {v:'appstore',n:'App Store',i:'\u{1F6D2}',show:CAN_DOCK},
    {v:'containers',n:'Containers',i:'\u{1F433}',show:CAN_DOCK},
    {v:'shares',n:'Share Links',i:'\u{1F517}',show:true},
    {v:'terminal',n:'Terminal',i:'\u2318',show:CAN_TERM},
    {v:'intercom',n:'Intercom',i:'\u{1F399}\uFE0F',show:CAN_INTERCOM},
    {v:'access',n:'Access Control',i:'\u{1F510}',show:IS_ADMIN},
    {v:'sessions',n:'Sessions',i:'\u{1F5A5}\uFE0F',show:true},
    {v:'settings',n:'Settings',i:'\u2699\uFE0F',show:true},
  ].filter(x=>x.show);
}
// App icons: App Store entries carry an https icon URL, which the page's CSP
// won't load directly — /api/app-icon fetches+caches it server-side. Plain
// emoji icons are rendered as text, exactly as before.
function appIconHtml(icon){
  icon=String(icon||'');
  if(/^\/api\/appstore\/icon\//.test(icon))
    return `<img src="${escA(icon)}" alt="" loading="lazy" decoding="async" onerror="this.replaceWith(document.createTextNode('\u{1F4E6}'))"/>`;
  if(/^https:\/\//i.test(icon))
    return `<img src="/api/app-icon?u=${encodeURIComponent(icon)}" alt="" loading="lazy" onerror="this.replaceWith(document.createTextNode('\u{1F4E6}'))"/>`;
  return esc(icon||'\u{1F517}');
}
let _hsApps=[];        // last /api/apps result, reused by global search
let _hsPop=null;       // currently open launcher / avatar menu
function hsClosePop(){
  if(_hsPop){_hsPop.remove();_hsPop=null}
  document.querySelectorAll('.hs-ibtn.open,.hs-avatar.open').forEach(b=>b.classList.remove('open'));
}
function hsOpenPop(btn,el){
  const wasOpen=btn.classList.contains('open');
  hsClosePop();
  if(wasOpen)return;
  btn.classList.add('open');document.body.appendChild(el);_hsPop=el;
}
function hsShowLauncher(btn){
  const el=document.createElement('div');el.className='hs-pop hs-launch';
  el.innerHTML=hsViews().map(x=>`<button data-v="${x.v}" class="${curV===x.v?'cur':''}"><span class="li">${x.i}</span>${esc(x.n)}</button>`).join('');
  el.addEventListener('click',e=>{const b=e.target.closest('[data-v]');if(b){hsClosePop();go(b.dataset.v)}});
  hsOpenPop(btn,el);
}
function hsShowUserMenu(btn){
  const roleLbl=IS_SUPERUSER?'Super User':(ROLE==='admin'?'Admin':'Guest');
  const el=document.createElement('div');el.className='hs-pop hs-menu';
  const item=(act,ico,label,cls='')=>`<div class="ctx-item ${cls}" data-act="${act}"><span style="width:16px;text-align:center">${ico}</span>${label}</div>`;
  el.innerHTML=`<div class="hs-menu-head"><div class="hs-avatar">${esc((UNAME||'?')[0].toUpperCase())}</div>
      <div style="min-width:0"><div class="hs-menu-name">${esc(SETTINGS.display_name||UNAME)}</div><div class="hs-menu-role">${esc(UNAME)} \u00b7 ${roleLbl}</div></div></div>
    ${item('settings','\u2699\uFE0F','Settings')}
    ${item('sessions','\u{1F5A5}\uFE0F','Sessions & devices')}
    ${item('password','\u{1F511}','Change password')}
    ${item('classic','\u{1F5C2}\uFE0F','Switch to Classic layout')}
    <div class="ctx-sep"></div>
    ${item('logout','\u{1F6AA}','Sign out','danger')}`;
  el.addEventListener('click',e=>{
    const it=e.target.closest('[data-act]');if(!it)return;hsClosePop();
    ({settings:()=>go('settings'),sessions:()=>go('sessions'),password:()=>goToPasswordSettings(),
      classic:()=>setUiMode('classic'),logout:()=>{location.href='/logout'}})[it.dataset.act]?.();
  });
  hsOpenPop(btn,el);
}
function hsUpdateCrumb(){
  const c=document.getElementById('hsCrumb');if(!c)return;
  if(!curV||curV==='dashboard'){c.innerHTML='';return}
  const v=hsViews().find(x=>x.v===curV);
  c.innerHTML=`<span class="sep">/</span><b>${esc(v?v.n:curV)}</b>`;
}
// Global search: system apps + installed apps, from any view. Ctrl/Cmd+K or
// "/" focuses it; arrows move, Enter opens, Esc clears. On the home screen it
// also live-filters the grid, which is what the old search did (and only did).
let _hsResIdx=0;
function hsSearchItems(q){
  q=q.trim().toLowerCase();if(!q)return[];
  const sys=hsViews().filter(x=>x.n.toLowerCase().includes(q)).map(x=>({kind:'System',name:x.n,icon:x.i,go:x.v}));
  const apps=_hsApps.filter(a=>(a.name||'').toLowerCase().includes(q)||(a.description||'').toLowerCase().includes(q))
    .map(a=>({kind:'App',name:a.name,icon:appIconHtml(a.icon),url:a.url,pending:a.tunnel_enabled&&a.tunnel_status!=='connected'}));
  return [...apps,...sys].slice(0,12);
}
function hsRenderResults(){
  const inp=document.getElementById('hsSearchInp'),box=document.getElementById('hsResults');if(!inp||!box)return;
  const items=hsSearchItems(inp.value);
  if(!inp.value.trim()){box.classList.remove('show');return}
  _hsResIdx=Math.min(_hsResIdx,Math.max(0,items.length-1));
  box.innerHTML=items.length?items.map((it,i)=>`<div class="hs-res ${i===_hsResIdx?'on':''}" data-i="${i}">
      <span class="ri">${it.icon}</span><span>${esc(it.name)}</span><span class="rk">${it.kind}</span></div>`).join('')
    :`<div class="hs-res-empty">No apps match \u201c${esc(inp.value.trim())}\u201d</div>`;
  box._items=items;box.classList.add('show');
}
function hsOpenResult(it){
  const inp=document.getElementById('hsSearchInp');
  if(!it)return;
  if(inp){inp.value='';inp.blur()}
  document.getElementById('hsResults')?.classList.remove('show');
  hsFilterGrid('');
  if(it.go){go(it.go);return}
  if(it.pending){toast('Tunnel still connecting \u2014 try again shortly','info');return}
  if(it.url&&it.url!=='#')window.open(it.url,'_blank','noopener');
}
function hsFilterGrid(q){
  if(curV!=='dashboard')return;
  q=(q||'').trim().toLowerCase();
  document.querySelectorAll('#tgrid .tile[data-tile-name],#hsSysGrid .tile[data-tile-name]').forEach(t=>{
    t.style.display=(!q||t.dataset.tileName.includes(q))?'':'none';
  });
}
let _hsKeysWired=false;
function ensureHsTopbar(){
  const shell=document.querySelector('.shell');if(!shell)return;
  if(!shell.classList.contains('hs')){document.getElementById('hsTopbar')?.remove();hsClosePop();return}
  if(document.getElementById('hsTopbar')){hsUpdateCrumb();return}
  const bar=document.createElement('div');
  bar.className='hs-topbar';bar.id='hsTopbar';
  const mac=/Mac|iPhone|iPad/.test(navigator.platform||'');
  bar.innerHTML=`
    <button class="hs-ibtn" id="hsLaunchBtn" title="All apps" aria-label="All apps">
      <svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg></button>
    <button class="hs-home-btn" id="hsHomeBtn" title="Home">
      <img src="/static/logo-64.png" width="26" height="26" alt="" style="border-radius:7px;display:block"/>
      <span>${esc(OSNAME)}</span>
    </button>
    <div class="hs-crumb" id="hsCrumb"></div>
    <div class="hs-search"><svg viewBox="0 0 24 24" width="15" height="15"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <input id="hsSearchInp" placeholder="Search apps\u2026" autocomplete="off" aria-label="Search apps"/><kbd>${mac?'\u2318':'Ctrl'} K</kbd>
      <div class="hs-results" id="hsResults"></div></div>
    <div class="hs-right">
      <span class="hs-clock" id="hsClock">\u2014</span>
      <div class="hs-avatar" id="hsAvatarBtn" title="${esc(UNAME)}" role="button" tabindex="0">${esc((UNAME||'?')[0].toUpperCase())}</div>
    </div>`;
  shell.prepend(bar);
  document.getElementById('hsHomeBtn')?.addEventListener('click',()=>go('dashboard'));
  document.getElementById('hsLaunchBtn')?.addEventListener('click',e=>{e.stopPropagation();hsShowLauncher(e.currentTarget)});
  const av=document.getElementById('hsAvatarBtn');
  av.addEventListener('click',e=>{e.stopPropagation();hsShowUserMenu(av)});
  av.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();hsShowUserMenu(av)}});
  const search=document.getElementById('hsSearchInp'),box=document.getElementById('hsResults');
  search.addEventListener('input',()=>{_hsResIdx=0;hsRenderResults();hsFilterGrid(search.value)});
  search.addEventListener('focus',()=>{if(search.value.trim())hsRenderResults()});
  search.addEventListener('blur',()=>setTimeout(()=>box.classList.remove('show'),150));
  search.addEventListener('keydown',e=>{
    const items=box._items||[];
    if(e.key==='ArrowDown'){e.preventDefault();_hsResIdx=Math.min(items.length-1,_hsResIdx+1);hsRenderResults()}
    else if(e.key==='ArrowUp'){e.preventDefault();_hsResIdx=Math.max(0,_hsResIdx-1);hsRenderResults()}
    else if(e.key==='Enter'){e.preventDefault();hsOpenResult(items[_hsResIdx])}
    else if(e.key==='Escape'){search.value='';hsFilterGrid('');box.classList.remove('show');search.blur()}
  });
  box.addEventListener('mousedown',e=>{const r=e.target.closest('.hs-res');if(r){e.preventDefault();hsOpenResult(box._items[+r.dataset.i])}});
  if(!_hsKeysWired){
    _hsKeysWired=true;
    document.addEventListener('click',e=>{if(_hsPop&&!_hsPop.contains(e.target))hsClosePop()});
    document.addEventListener('keydown',e=>{
      if(e.key==='Escape')hsClosePop();
      if(!document.querySelector('.shell.hs'))return;
      const typing=document.activeElement&&(['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)||document.activeElement.isContentEditable);
      if(((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k')||(e.key==='/'&&!typing&&curV!=='terminal')){
        const s=document.getElementById('hsSearchInp');if(s){e.preventDefault();s.focus();s.select()}
      }
    });
    window.addEventListener('resize',hsClosePop);
  }
  const tick=()=>{
    const now=new Date();
    const c=document.getElementById('hsClock');if(c)c.textContent=_clockParts(now).short;
    const big=document.getElementById('hsBigClock');
    if(big&&!big.querySelector('canvas')){const h=_bigClockHtml(now);if(big._h!==h){big.innerHTML=h;big._h=h}}
    const d=document.getElementById('hsBigDate');if(d)d.textContent=now.toLocaleDateString([],{weekday:'long',month:'long',day:'numeric'});
  };
  tick();
  if(window._hsTickT)clearInterval(window._hsTickT);     // the bar is rebuilt each time the home screen is chosen
  window._hsTickT=setInterval(tick,10000);
  hsUpdateCrumb();
  api('/api/apps').then(r=>r.json()).then(a=>{if(Array.isArray(a))_hsApps=a}).catch(()=>{});
}
window.addEventListener('DOMContentLoaded',applyUiMode);
if(document.readyState!=='loading')applyUiMode();

// ── Cloud Storage (Dropbox / Google Drive) ───────────────────────────────
// Which synced cloud folders are mid-sync right now (local path). Painted onto the sidebar tile and the
// folder icon in file lists; refreshed every 3 s while anything is syncing and every 12 s otherwise.
const CLOUD_SYNCING=new Set();let _cloudSyncTimer=null,_cloudSyncKnown=false;
function cloudSyncPaint(){
  document.querySelectorAll('.fitem[data-type="dir"]').forEach(el=>el.classList.toggle('syncing',CLOUD_SYNCING.has(el.dataset.path)));
  document.querySelectorAll('[data-cloud]').forEach(el=>el.classList.toggle('syncing',el.dataset.syncPath!==undefined&&CLOUD_SYNCING.has(el.dataset.syncPath)));
}
async function cloudSyncPoll(){
  clearTimeout(_cloudSyncTimer);
  try{
    const next=new Set();
    const provs=(await apiData('/api/cloud/providers')).filter(p=>p.connected);
    await Promise.all(provs.map(async p=>{
      try{(await apiData(`/api/cloud/${p.id}/sync`)).forEach(x=>{if(x.enabled&&x.entire&&x.state==='syncing')next.add(x.local_path)})}catch(e){}
    }));
    CLOUD_SYNCING.clear();next.forEach(x=>CLOUD_SYNCING.add(x));
    _cloudSyncKnown=provs.length>0;cloudSyncPaint();
  }catch(e){}
  if(_cloudSyncKnown||document.getElementById('fsCloudSection'))
    _cloudSyncTimer=setTimeout(cloudSyncPoll,CLOUD_SYNCING.size?3000:12000);
}
window.cloudSyncPoll=cloudSyncPoll;
async function loadCloudProviders(){
  const el=document.getElementById('cloudProvidersList');if(!el)return;
  try{
    const list=await apiData('/api/cloud/providers');
    el.innerHTML=list.map(p=>`
      <div class="srow" style="width:100%">
        <div><div class="slabel">${esc(p.label)}</div>
          <div class="sdesc">${!p.configured?'Not configured on this server (missing API credentials in .env)':(p.connected?'Connected':'Not connected')}</div></div>
        ${!p.configured?'':p.connected
          ?`<div style="display:flex;gap:8px"><button class="btn btn-g btn-sm" onclick="browseCloud(${jsA(p.id)},${jsA(p.label)})">Browse</button><button class="btn btn-d btn-sm" onclick="disconnectCloud('${p.id}')">Disconnect</button></div>`
          :`<button class="btn btn-p btn-sm" onclick="connectCloud('${p.id}')">Connect</button>`}
      </div>
      ${p.connected?`<div class="sync-panel" id="syncPanel-${p.id}" style="width:100%;padding:12px;margin-top:2px;background:rgba(var(--ov-rgb),.03);border-radius:var(--rs)">
        <div style="font-size:11px;font-weight:600;color:var(--td);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Sync</div>
        <div id="syncList-${p.id}"><div class="loading"><div class="loader"></div></div></div>
        <div id="syncWhole-${p.id}" class="sync-whole" hidden>
          <div class="slabel">Sync my entire ${esc(p.label)}</div>
          <div class="sdesc">Keeps a full copy in your Files (<b>${esc(p.label)}</b> folder), two ways: a change on either side
            shows up on the other within seconds, like the desktop app. Files are stored here, so it needs the disk space.</div>
          <button class="btn btn-p btn-sm" id="syncWholeBtn-${p.id}" onclick="syncEntireCloud('${p.id}')">Start syncing everything</button>
        </div>
        <details style="margin-top:10px"><summary class="sdesc" style="cursor:pointer">Sync just one folder instead</summary>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px;align-items:center">
          <input class="fgi" id="syncLocal-${p.id}" placeholder="${FS_USERS}/you/${esc(p.label)}" style="flex:1;min-width:180px;font-size:12px"/>
          <input class="fgi" id="syncRemote-${p.id}" placeholder="remote folder (blank = root)" style="flex:1;min-width:140px;font-size:12px"/>
          <input class="fgi" id="syncInterval-${p.id}" type="number" min="1" value="5" style="width:70px;font-size:12px" title="Safety sync every N minutes (changes sync sooner)"/>
          <button class="btn btn-p btn-sm" onclick="createCloudSync('${p.id}')">Enable Sync</button>
        </div></details>
      </div>`:''}`).join('');
    list.filter(p=>p.connected).forEach(p=>loadCloudSyncPairs(p.id));
  }catch(e){el.innerHTML=`<div class="sdesc" style="color:var(--rd)">Couldn’t load: ${esc(e.message)}</div>`}
}
const _syncPoll={};
async function loadCloudSyncPairs(provider){
  const el=document.getElementById(`syncList-${provider}`);if(!el)return;
  clearTimeout(_syncPoll[provider]);
  try{
    const pairs=await(await api(`/api/cloud/${provider}/sync`)).json();
    const whole=document.getElementById(`syncWhole-${provider}`);
    if(whole)whole.hidden=pairs.some(p=>p.entire);       // the "sync everything" card only while it isn't running
    if(!pairs.length){el.innerHTML='<div class="sdesc">Nothing is syncing yet.</div>'}
    else el.innerHTML=pairs.map(p=>{
      const r=p.last_result||{};
      let txt,col='var(--td)';
      const pg=p.progress,mb=n=>n>=1073741824?(n/1073741824).toFixed(1)+' GB':n>=1048576?(n/1048576).toFixed(0)+' MB':Math.max(1,Math.round(n/1024))+' KB';
      const pgTxt=pg?(pg.total?` \u2014 ${mb(pg.local)} of ${mb(pg.total)} (${Math.min(99,Math.round(pg.local/pg.total*100))}%)`:` \u2014 ${mb(pg.local)} downloaded so far`):'';
      if(p.state==='syncing')txt=r.at&&!pg?'Syncing\u2026':'First sync in progress'+pgTxt+(pgTxt?'':' \u2014 downloading everything can take a while\u2026');
      else if(!r.at)txt='Waiting for the first sync\u2026';
      else if(r.ok)txt='Up to date \u00b7 last synced '+new Date(r.at*1000).toLocaleTimeString();
      else{txt='Problem \u2014 '+(r.message||'').slice(0,120);col='var(--rd)'}
      const live=p.live?`<span class="srv-tag" title="${p.watching?'Changes on this server sync within seconds':'Syncs every few minutes'}">${p.watching?'Live':'Timed'}</span>`:'';
      return `<div class="srow" style="padding:8px 0">
        <div><div class="slabel" style="font-size:12.5px">${p.entire?`Your whole ${esc(provider==='dropbox'?'Dropbox':'Google Drive')}`:esc(p.remote_path||'/')} \u2194 ${esc(p.local_path)} ${live}</div>
          <div class="sdesc" style="color:${col}">${esc(txt)}</div>
          ${pg&&pg.total&&p.state==='syncing'?`<div style="height:5px;border-radius:3px;background:rgba(var(--ov-rgb),.12);margin-top:6px;max-width:320px;overflow:hidden"><div style="height:100%;width:${Math.min(99,Math.round(pg.local/pg.total*100))}%;background:var(--pu);transition:width .6s"></div></div>`:''}</div>
        <button class="btn btn-d btn-sm" onclick="deleteCloudSync('${provider}','${p.id}')">Stop</button>
      </div>`;
    }).join('');
    if(document.getElementById(`syncList-${provider}`))_syncPoll[provider]=setTimeout(()=>loadCloudSyncPairs(provider),pairs.some(p=>p.state==='syncing')?3000:8000);
  }catch(e){el.innerHTML=`<div class="sdesc" style="color:var(--rd)">Couldn’t load: ${esc(e.message)}</div>`}
}
async function syncEntireCloud(provider){
  const btn=document.getElementById(`syncWholeBtn-${provider}`);if(btn){btn.disabled=true;btn.textContent='Starting\u2026'}
  try{
    const r=await(await api(`/api/cloud/${provider}/sync`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entire:true})})).json();
    if(r.ok){toast('Syncing everything \u2014 the first copy may take a while','success');loadCloudSyncPairs(provider)}
    else errToast(r,'Couldn’t start the sync');
  }catch(e){toast('Couldn’t start the sync: '+e.message,'error')}
  finally{if(btn){btn.disabled=false;btn.textContent='Start syncing everything'}}
}
window.syncEntireCloud=syncEntireCloud;
async function createCloudSync(provider){
  const local=document.getElementById(`syncLocal-${provider}`).value.trim();
  const remote=document.getElementById(`syncRemote-${provider}`).value.trim();
  const interval=parseInt(document.getElementById(`syncInterval-${provider}`).value,10)||5;
  if(!local){toast('Local folder is required','error');return}
  const r=await(await api(`/api/cloud/${provider}/sync`,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({local_path:local,remote_path:remote,interval_minutes:interval})})).json();
  if(r.ok){toast('Sync enabled','success');document.getElementById(`syncLocal-${provider}`).value='';loadCloudSyncPairs(provider)}
  else errToast(r,'Failed to enable sync');
}
async function deleteCloudSync(provider,pairId){
  const r=await(await api(`/api/cloud/sync/${pairId}`,{method:'DELETE'})).json();
  if(r.ok){toast('Sync disabled','success');loadCloudSyncPairs(provider)}
  else errToast(r,'Failed');
}
window.createCloudSync=createCloudSync;window.deleteCloudSync=deleteCloudSync;

// ── Notifications (email / Discord / Telegram) ───────────────────────────
const NOTIFY_EVENT_LABELS={
  login:'New login',login_failed:'Failed login attempt',user_created:'User created',user_deleted:'User deleted',
  password_changed:'Password changed',password_reset:'Password reset','2fa_enabled':'Two-factor turned on','2fa_disabled':'Two-factor turned off','2fa_reset':'Two-factor reset by admin','2fa_backup_used':'Backup code used','2fa_backup_regenerated':'Backup codes regenerated',role_changed:'Role changed',
  permission_changed:'Permission changed',server_settings_changed:'Server setting changed',
  panic_triggered:'Panic button triggered',panic_undo:'Panic undone',setup_completed:'Setup completed',
  superuser_transferred:'Super User transferred',session_revoked:'Session revoked',
  sessions_revoked_others:'Other sessions revoked',backup_exported:'Backup exported',
  cross_site_blocked:'Cross-site request blocked',disk_space_low:'Disk space low',app_install_failed:'App install failed',cloud_sync_failed:'Cloud sync failing',
  backup_imported:'Backup imported',audit_log_cleared:'Audit log cleared',
  storage_root_missing:'Storage drive not present',storage_root_restored:'Storage drive is back',
  usb_auto_mount_failed:'USB drive couldn’t be mounted',usb_repair_failed:'USB drive repair failed',
};
// ── Settings → Updates (Super User) ────────────────────────────────────
let _updPoll=null;
async function loadUpdates(){
  const body=document.getElementById('updBody');if(!body)return;
  const r=await api('/api/admin/update');const d=await r.json();
  if(!r.ok){body.innerHTML=`<div class="sdesc" style="color:var(--rd)">${esc(d.error)}</div>`;return}
  renderUpdates(d);
}
function renderUpdates(d){
  const body=document.getElementById('updBody');if(!body)return;
  const j=d.job||{};const running=j.state==='running';
  const when=d.checked_at?new Date(d.checked_at*1000).toLocaleString():null;
  let status;
  if(!d.configured)status=`<div class="sdesc">${d.disabled?'Updates are turned off on this server (<code>VOID_UPDATES=off</code> in <code>.env</code>). Remove that line and restart Void OS to turn them back on.':'Updates aren\u2019t available in this copy of Void OS.'}</div>`;
  else if(d.error)status=`<div class="sdesc" style="color:var(--rd)">Couldn\u2019t check for updates: ${esc(d.error)}</div>`;
  else if(d.available)status=`<div class="upd-new"><div class="upd-ver">Void OS ${esc(d.latest.version)} is available</div>
      ${d.latest.notes?`<div class="sdesc" style="margin-top:4px;white-space:pre-line">${esc(d.latest.notes)}</div>`:''}${d.latest.verified?'':`<div class="sdesc" style="margin-top:6px">This download comes straight from GitHub over HTTPS and has no published checksum to verify against.</div>`}</div>`;
  else if(when)status=`<div class="sdesc">You\u2019re up to date.</div>`;
  else status=`<div class="sdesc">Not checked yet.</div>`;
  let job='';
  if(running)job=`<div class="upd-job"><div class="upd-step">${esc(j.step||'Working')}\u2026</div>
      <div class="drive-bar"><i style="width:${j.progress||0}%"></i></div></div>`;
  else if(j.state==='error')job=`<div class="sdesc" style="color:var(--rd);margin-top:10px">${esc(j.message||'The update failed.')}
      ${j.ref?` <code>ref ${esc(j.ref)}</code>`:''}</div>`;
  else if(j.state==='done')job=`<div class="sdesc" style="margin-top:10px">Installed ${esc(j.version||'')} \u2014 restarting\u2026</div>`;
  body.innerHTML=`<div class="srow col">
      <div><div class="slabel">Current version</div><div class="sdesc"><b class="tnum">${esc(d.current)}</b>${when?` \u00b7 last checked ${esc(when)}`:''}</div></div>
      ${status}${job}
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">
        <button class="btn btn-g btn-sm" id="updCheckBtn" ${(!d.configured||running)?'disabled':''}>Check now</button>
        ${d.available&&!running?`<button class="btn btn-p btn-sm" id="updInstallBtn">Install ${esc(d.latest.version)}</button>`:''}
      </div>
      <div class="sdesc" style="margin-top:8px">Installing downloads the release, verifies it, installs any new Python packages,
        backs up the current version${d.backups&&d.backups.length?` (kept: ${d.backups.map(esc).join(', ')})`:''} and restarts.
        Your settings, accounts and files are never touched.</div>
    </div>`;
  document.getElementById('updCheckBtn')?.addEventListener('click',async e=>{
    e.currentTarget.disabled=true;e.currentTarget.textContent='Checking\u2026';
    const r=await api('/api/admin/update/check',{method:'POST'});renderUpdates(await r.json());
  });
  document.getElementById('updInstallBtn')?.addEventListener('click',e=>confirmDlg(
    `Install Void OS ${esc(d.latest.version)} now? Void OS restarts when it\u2019s done; anyone using it will see the reconnect screen briefly.`,
    async()=>{const r=await api('/api/admin/update/install',{method:'POST'});const x=await r.json();
      if(!r.ok){errToast(x,'Couldn\u2019t start the update');return}
      _pollUpdates()},'Install',e.currentTarget));
  if(running)_pollUpdates();
}
function _pollUpdates(){
  clearTimeout(_updPoll);
  _updPoll=setTimeout(async()=>{
    const r=await api('/api/admin/update');if(!r.ok)return;   // during the restart the reconnect screen takes over
    const d=await r.json();renderUpdates(d);
    if(d.job&&d.job.state==='done'){Conn.restarting()}
  },900);
}
// ── App addresses (Settings > App addresses) ─────────────────────
const _ADDR_STATE={connected:['Online','gr'],starting:['Connecting\u2026','yw'],reconnecting:['Reconnecting\u2026','yw'],
  error:['Problem','rd'],not_installed:['cloudflared missing','rd'],at_cap:['Tunnel limit reached','rd'],stopped:['Stopped','td']};
async function loadAddresses(){
  const list=document.getElementById('addrList');if(!list)return;
  if(_addrPoll){clearTimeout(_addrPoll);_addrPoll=null}
  let apps=[],dom={};
  try{
    [apps,dom]=await Promise.all([api('/api/apps').then(r=>r.json()),api('/api/admin/domain').then(r=>r.json())]);
  }catch(e){
    if(document.getElementById('addrList'))list.innerHTML='<div class="srow"><div class="sdesc">Couldn\u2019t load the app list. Try Refresh.</div></div>';
    return;
  }
  if(!document.getElementById('addrList'))return; // navigated away while loading
  const dEl=document.getElementById('addrDomain');
  if(dEl)dEl.innerHTML=dom.connected
    ?`<div class="slabel">${esc(dom.domain)}</div><div class="sdesc">Connected. Give an app an address on it with the Edit button below.${(dom.tiles_using||[]).length?' In use by '+dom.tiles_using.map(esc).join(', ')+'.':''}</div>
       ${(dom.tiles_using||[]).length?'':'<div><button class="btn btn-g btn-sm" id="addrDomOff" type="button">Disconnect domain</button></div>'}`
    :`<div class="slabel">Not connected</div>
       <div class="sdesc">Connect a domain that is on Cloudflare (free plan is fine) to give apps fixed addresses like
         jellyfin.yourdomain.com. Create an API token at
         <a href="https://dash.cloudflare.com/profile/api-tokens" target="_blank" rel="noopener">dash.cloudflare.com/profile/api-tokens</a>
         &rarr; <i>Create Custom Token</i> with <b>Account &rsaquo; Cloudflare Tunnel &rsaquo; Edit</b>,
         <b>Zone &rsaquo; DNS &rsaquo; Edit</b> and <b>Zone &rsaquo; Zone &rsaquo; Read</b>, limited to your domain.
         Void keeps it encrypted and never shows it again.${dom.stored_but_unreadable?' A saved connection exists but can\u2019t be read (the server\u2019s secret key changed); connect it again.':''}</div>
       <div style="display:flex;gap:8px;flex-wrap:wrap;width:100%;align-items:flex-end">
         <div class="fg" style="flex:1;min-width:170px;margin:0"><label class="fgl">Domain</label>
           <input class="fgi" id="addrDomName" placeholder="example.com" autocomplete="off" spellcheck="false"/></div>
         <div class="fg" style="flex:2;min-width:200px;margin:0"><label class="fgl">Cloudflare API token</label>
           <input class="fgi" id="addrDomTok" type="password" autocomplete="new-password"/></div>
         <button class="btn btn-p btn-sm" id="addrDomGo" type="button">Connect domain</button>
       </div>`;
  document.getElementById('addrDomGo')?.addEventListener('click',async ev=>{
    const btn=ev.currentTarget,name=document.getElementById('addrDomName').value,tok=document.getElementById('addrDomTok').value;
    if(!name.trim()||!tok.trim()){toast('Enter the domain and the API token','error');return}
    btn.disabled=true;btn.textContent='Checking\u2026';
    try{
      const r=await(await api('/api/admin/domain',{method:'PUT',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({domain:name,api_token:tok})})).json();
      if(r.error){errToast(r);btn.disabled=false;btn.textContent='Connect domain';return}
      toast('Connected '+r.domain,'success');loadAddresses();
    }catch(e){btn.disabled=false;btn.textContent='Connect domain';toast('Could not reach the server','error')}
  });
  document.getElementById('addrDomOff')?.addEventListener('click',async()=>{
    const r=await(await api('/api/admin/domain',{method:'DELETE'})).json();
    if(r.error){errToast(r);return}
    toast('Domain disconnected','info');loadAddresses();
  });
  if(!apps.length){list.innerHTML='<div class="srow"><div class="sdesc">No app tiles yet. Add one from the Dashboard.</div></div>';return}
  let waiting=false;
  list.innerHTML=apps.map(a=>{
    let kind,addr,badge='';
    if(a.tunnel_enabled){
      kind=a.custom_hostname?'Your domain':'Temporary link';
      const st=_ADDR_STATE[a.tunnel_status]||[a.tunnel_status||'Unknown','td'];
      if(a.tunnel_status==='starting'||a.tunnel_status==='reconnecting')waiting=true;
      badge=`<span class="addr-st addr-${st[1]}">${esc(st[0])}</span>`;
      addr=a.url&&a.url!=='#'?a.url:(a.custom_hostname?'https://'+a.custom_hostname:'');
    }else{
      kind=a.tunnel_enabled_before_panic?'Stopped by Panic':'Local only';
      addr=a.url&&a.url!=='#'?a.url:'';
    }
    const lock=a.gate_enabled?'<span class="addr-tag" title="PIN or password required">\ud83d\udd12 Gate</span>':'';
    const ver=a.tunnel_enabled&&a.tunnel_error?`<div class="sdesc addr-err">${esc(a.tunnel_error)}</div>`:'';
    return `<div class="srow addr-row" data-id="${escA(a.id)}">
      <div class="addr-main">
        <div class="addr-name">${appIconHtml(a.icon)}<span>${esc(a.name)}</span><span class="addr-tag">${esc(kind)}</span>${lock}${badge}</div>
        <div class="addr-url">${addr?esc(addr):'<span style="color:var(--td)">No address yet</span>'}</div>${ver}
      </div>
      <div class="addr-act">
        ${addr?`<a class="btn btn-g btn-sm" href="${escA(addr)}" target="_blank" rel="noopener">Open</a>
        <button class="btn btn-g btn-sm" data-copy="${escA(addr)}" type="button">Copy</button>`:''}
        <button class="btn btn-g btn-sm" data-edit="${escA(a.id)}" type="button">Edit</button>
      </div></div>`;
  }).join('');
  list.querySelectorAll('[data-copy]').forEach(b=>b.addEventListener('click',()=>
    navigator.clipboard?.writeText(b.dataset.copy).then(()=>toast('Address copied','success'),()=>toast('Copy failed','error'))));
  list.querySelectorAll('[data-edit]').forEach(b=>b.addEventListener('click',()=>editTile(b.dataset.edit)));
  if(waiting)_addrPoll=setTimeout(()=>{if(document.getElementById('addrList'))loadAddresses()},4000);
}
window.loadAddresses=loadAddresses;

async function loadNotifyPanel(){
  const el=document.getElementById('notifyPanel');if(!el)return;
  try{
    const cfg=await(await api('/api/admin/notifications')).json();
    const dis=IS_SUPERUSER?'':'disabled style="opacity:.5;pointer-events:none"';
    el.innerHTML=`
      <div class="notify-channel">
        <div class="notify-ch-head">${ghSwitch('nEmailEn',cfg.email.enabled,dis)}<label for="nEmailEn" style="cursor:pointer;font-weight:600">Email</label>
          ${IS_SUPERUSER?'<button class="btn btn-g btn-sm" style="margin-left:auto" onclick="testNotifyChannel(\'email\')">Send test</button>':''}</div>
        ${notifyStatusLine(cfg.status&&cfg.status.email)}
        <div class="notify-ch-body" data-ch="email">
          <div class="fg"><label class="fgl">SMTP host</label><input class="fgi" id="nSmtpHost" ${dis} value="${escA(cfg.email.smtp_host)}" placeholder="smtp.gmail.com"/></div>
          <div class="fg"><label class="fgl">Port</label><input class="fgi" id="nSmtpPort" type="number" ${dis} value="${cfg.email.smtp_port}"/></div>
          <div class="fg"><label class="fgl">Username</label><input class="fgi" id="nSmtpUser" ${dis} value="${escA(cfg.email.username)}"/></div>
          <div class="fg"><label class="fgl">Password ${cfg.email.has_password?'(saved \u2014 leave blank to keep)':''}</label><input class="fgi" id="nSmtpPass" type="password" ${dis} placeholder="${cfg.email.has_password?'\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022':''}"/></div>
          <div class="fg"><label class="fgl">From address</label><input class="fgi" id="nSmtpFrom" ${dis} value="${escA(cfg.email.from_addr)}"/></div>
          <div class="fg"><label class="fgl">Send to</label><input class="fgi" id="nSmtpTo" ${dis} value="${escA(cfg.email.to_addr)}" placeholder="you@example.com"/></div>
          ${IS_SUPERUSER?'<button class="btn btn-p btn-sm" onclick="saveNotifyChannel(\'email\')">Save</button>':''}
        </div>
      </div>
      <div class="notify-channel">
        <div class="notify-ch-head">${ghSwitch('nDiscordEn',cfg.discord.enabled,dis)}<label for="nDiscordEn" style="cursor:pointer;font-weight:600">Discord</label>
          ${IS_SUPERUSER?'<button class="btn btn-g btn-sm" style="margin-left:auto" onclick="testNotifyChannel(\'discord\')">Send test</button>':''}</div>
        ${notifyStatusLine(cfg.status&&cfg.status.discord)}
        <div class="notify-ch-body" data-ch="discord">
          <div class="fg"><label class="fgl">Webhook URL ${cfg.discord.has_webhook?'(saved \u2014 leave blank to keep)':''}</label>
            <input class="fgi" id="nDiscordUrl" ${dis} placeholder="${cfg.discord.has_webhook?esc(cfg.discord.webhook_url):'https://discord.com/api/webhooks/...'}"/></div>
          ${IS_SUPERUSER?'<button class="btn btn-p btn-sm" onclick="saveNotifyChannel(\'discord\')">Save</button>':''}
        </div>
      </div>
      <div class="notify-channel">
        <div class="notify-ch-head">${ghSwitch('nTelegramEn',cfg.telegram.enabled,dis)}<label for="nTelegramEn" style="cursor:pointer;font-weight:600">Telegram</label>
          ${IS_SUPERUSER?'<button class="btn btn-g btn-sm" style="margin-left:auto" onclick="testNotifyChannel(\'telegram\')">Send test</button>':''}</div>
        ${notifyStatusLine(cfg.status&&cfg.status.telegram)}
        <div class="notify-ch-body" data-ch="telegram">
          <div class="fg"><label class="fgl">Bot token ${cfg.telegram.has_token?'(saved \u2014 leave blank to keep)':''}</label><input class="fgi" id="nTgToken" ${dis} placeholder="${cfg.telegram.has_token?'\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022':'123456:ABC-DEF...'}"/></div>
          <div class="fg"><label class="fgl">Chat ID</label><input class="fgi" id="nTgChat" ${dis} value="${escA(cfg.telegram.chat_id)}" placeholder="123456789"/></div>
          ${IS_SUPERUSER?'<button class="btn btn-p btn-sm" onclick="saveNotifyChannel(\'telegram\')">Save</button>':''}
        </div>
      </div>
      <div class="notify-channel">
        <div class="notify-ch-head">${ghSwitch('nNtfyEn',cfg.ntfy.enabled,dis)}<label for="nNtfyEn" style="cursor:pointer;font-weight:600">ntfy (phone push)</label>
          ${IS_SUPERUSER?'<button class="btn btn-g btn-sm" style="margin-left:auto" onclick="testNotifyChannel(\'ntfy\')">Send test</button>':''}</div>
        ${notifyStatusLine(cfg.status&&cfg.status.ntfy)}
        <div class="notify-ch-body" data-ch="ntfy">
          <div class="sdesc" style="margin-bottom:8px">Free push notifications on your phone. Install the <b>ntfy</b> app (Android or iPhone), tap <b>+</b>
            and subscribe to the same topic you enter here. On the public server <code>ntfy.sh</code> anyone who knows a topic can read it, so use the
            <b>Generate</b> button for a long random one and keep it private.</div>
          <div class="fg"><label class="fgl">Server</label>
            <input class="fgi" id="nNtfyServer" ${dis} value="${escA(cfg.ntfy.server||'https://ntfy.sh')}" placeholder="https://ntfy.sh"/></div>
          <div class="fg"><label class="fgl">Topic ${cfg.ntfy.has_topic&&!cfg.ntfy.topic?'(saved \u2014 leave blank to keep)':''}</label>
            <div style="display:flex;gap:8px"><input class="fgi" id="nNtfyTopic" ${dis} value="${escA(cfg.ntfy.topic||'')}" placeholder="${cfg.ntfy.has_topic&&!cfg.ntfy.topic?'\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022':'voidos-alerts-\u2026'}" autocomplete="off" spellcheck="false" style="flex:1"/>
              ${IS_SUPERUSER?'<button class="btn btn-g btn-sm" type="button" id="nNtfyGen">Generate</button>':''}</div></div>
          <div class="fg"><label class="fgl">Access token (optional) ${cfg.ntfy.has_token?'(saved \u2014 leave blank to keep)':''}</label>
            <input class="fgi" id="nNtfyToken" type="password" ${dis} autocomplete="off" placeholder="${cfg.ntfy.has_token?'\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022':'only for a protected topic or your own server'}"/></div>
          ${IS_SUPERUSER?'<button class="btn btn-p btn-sm" onclick="saveNotifyChannel(\'ntfy\')">Save</button>':''}
        </div>
      </div>
      <div class="notify-events">
        <div class="fs-title" style="padding-left:0">Notify me for</div>
        ${Object.entries(NOTIFY_EVENT_LABELS).map(([k,label])=>`
          <div class="srow" style="padding:5px 0">
            <span style="font-size:12.5px">${esc(label)}</span>
            ${ghSwitch('nEv-'+k,cfg.events[k]!==false,dis)}
          </div>`).join('')}
      </div>`;
    if(IS_SUPERUSER){
      Object.keys(NOTIFY_EVENT_LABELS).forEach(k=>{
        document.getElementById('nEv-'+k)?.addEventListener('change',e=>
          api('/api/admin/notifications',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({events:{[k]:e.target.checked}})}));
      });
      document.getElementById('nNtfyGen')?.addEventListener('click',()=>{
        const a=new Uint8Array(12);crypto.getRandomValues(a);
        document.getElementById('nNtfyTopic').value='voidos-'+[...a].map(x=>x.toString(16).padStart(2,'0')).join('');
      });
      ['nEmailEn','nDiscordEn','nTelegramEn','nNtfyEn'].forEach(id=>{
        document.getElementById(id)?.addEventListener('change',()=>saveNotifyChannel({nEmailEn:'email',nDiscordEn:'discord',nTelegramEn:'telegram',nNtfyEn:'ntfy'}[id]));
      });
    }
  }catch(e){el.innerHTML=`<div class="sdesc" style="color:var(--rd)">Couldn’t load: ${esc(e.message)}</div>`}
}
async function saveNotifyChannel(channel){
  let body;
  if(channel==='email')body={enabled:document.getElementById('nEmailEn').checked,smtp_host:document.getElementById('nSmtpHost').value.trim(),
    smtp_port:parseInt(document.getElementById('nSmtpPort').value,10)||587,username:document.getElementById('nSmtpUser').value.trim(),
    password:document.getElementById('nSmtpPass').value,from_addr:document.getElementById('nSmtpFrom').value.trim(),to_addr:document.getElementById('nSmtpTo').value.trim()};
  else if(channel==='discord')body={enabled:document.getElementById('nDiscordEn').checked,webhook_url:document.getElementById('nDiscordUrl').value.trim()};
  else if(channel==='ntfy')body={enabled:document.getElementById('nNtfyEn').checked,server:(document.getElementById('nNtfyServer').value||'').trim()||'https://ntfy.sh',
    topic:document.getElementById('nNtfyTopic').value.trim(),token:document.getElementById('nNtfyToken').value.trim()};
  else body={enabled:document.getElementById('nTelegramEn').checked,bot_token:document.getElementById('nTgToken').value.trim(),chat_id:document.getElementById('nTgChat').value.trim()};
  const r=await(await api('/api/admin/notifications',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({[channel]:body})})).json();
  if(r.ok){toast(channel[0].toUpperCase()+channel.slice(1)+' saved','success');loadNotifyPanel()}
  else errToast(r,'Failed to save');
}
// Last real delivery result for a channel (from actual events and tests).
function notifyStatusLine(st){
  if(!st||!st.last_attempt)return'<div class="notify-status ns-none">Nothing sent yet</div>';
  const when=t=>new Date(t*1000).toLocaleString();
  if(st.last_error&&(!st.last_ok||st.last_error_at>st.last_ok))
    return`<div class="notify-status ns-err">\u2715 Last delivery failed (${esc(when(st.last_error_at))}${st.fail_streak>1?`, ${st.fail_streak} in a row`:''}): ${esc(st.last_error)}</div>`;
  return`<div class="notify-status ns-ok">\u2713 Last delivered ${esc(when(st.last_ok))}</div>`;
}
async function testNotifyChannel(channel){
  const r=await(await api(`/api/admin/notifications/test/${channel}`,{method:'POST'})).json();
  if(r.ok)toast(r.message||'Test notification sent','success');
  else toast(r.error||r.message||'The test notification wasn\u2019t sent','error',{duration:15000});
  if(typeof loadNotifyPanel==='function')loadNotifyPanel();  // refresh the delivery status line
}
window.saveNotifyChannel=saveNotifyChannel;window.testNotifyChannel=testNotifyChannel;
async function connectCloud(provider){
  try{
    const r=await(await api(`/api/cloud/${provider}/connect`)).json();
    if(r.url)window.location.href=r.url;
    else errToast(r,'Could not start connection');
  }catch(e){toast('Could not start connection','error')}
}
async function disconnectCloud(provider){
  const r=await(await api(`/api/cloud/${provider}/disconnect`,{method:'POST'})).json();
  if(r.ok){toast('Disconnected','success');loadCloudProviders()}
}
async function browseCloud(provider,label,path){
  path=path||'';
  openMdl(`${esc(label)}`,'<div class="loading"><div class="loader"></div></div>','');
  try{
    const d=await(await api(`/api/cloud/${provider}/list?path=${encodeURIComponent(path)}`)).json();
    if(d.error){document.getElementById('mdlB').innerHTML=`<div class="sdesc">${esc(d.error)}</div>`;return}
    document.getElementById('mdlB').innerHTML=`
      <div style="display:flex;flex-direction:column;gap:2px;max-height:50vh;overflow:auto">
      ${path?`<div class="ctx-item" onclick="browseCloud(${jsA(provider)},${jsA(label)},${jsA(path.split('/').slice(0,-1).join('/'))})">\u2191 ..</div>`:''}
      ${d.entries.map(e=>`<div class="ctx-item" style="display:flex;justify-content:space-between;gap:10px" ${e.type==='dir'?`onclick="browseCloud(${jsA(provider)},${jsA(label)},${jsA(e.id)})"`:''}>
        <span>${e.type==='dir'?'\uD83D\uDCC1':'\uD83D\uDCC4'} ${esc(e.name)}</span>
        ${e.type==='file'?`<button class="btn btn-g btn-sm" onclick="event.stopPropagation();importCloud(${jsA(provider)},${jsA(e.id)},${jsA(e.name)},${jsA(e.size??'')})">Import</button>`:''}
      </div>`).join('')||'<div class="sdesc">Empty folder</div>'}
      </div>`;
  }catch(e){document.getElementById('mdlB').innerHTML='<div class="sdesc">Failed to load</div>'}
}
async function importCloud(provider,remoteId,name,size){
  // Downloads run as a background transfer with progress in the tray; the
  // file appears in its folder once it has fully arrived.
  const dest=curV==='files'?(curP||''):'';
  const r=await api(`/api/cloud/${provider}/import`,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({remote_id:remoteId,name,dest,size:Number.isFinite(+size)?+size:undefined})});
  const d=await r.json();
  if(!r.ok||!d.job){errToast(d,'Import failed');return}
  xferUpdate(d.job);
}
window.connectCloud=connectCloud;window.disconnectCloud=disconnectCloud;window.browseCloud=browseCloud;window.importCloud=importCloud;

// Router
document.querySelectorAll('.sbtn[data-view]').forEach(el=>el.addEventListener('click',()=>go(el.dataset.view)));
function go(v){
  if(isMobile())toggleSidebar(false); // overlay drawer: close after picking a destination
  if(curV===v)return;
  if(curV==='terminal')cleanupTerm();
  if(curV==='intercom')cleanupIntercom();
  if(statsPoll&&v!=='dashboard'){clearInterval(statsPoll);statsPoll=null}
  if(_tilesPoll&&v!=='dashboard'){clearTimeout(_tilesPoll);_tilesPoll=null}
  if(_tunnelPoll&&v!=='settings'){clearTimeout(_tunnelPoll);_tunnelPoll=null}
  if(_addrPoll&&v!=='settings'){clearTimeout(_addrPoll);_addrPoll=null}
  curV=v;
  if(typeof hsClosePop==='function'){hsClosePop();hsUpdateCrumb()}
  _renderGen++; // this navigation supersedes any render already in flight for the old view
  document.querySelectorAll('.sbtn[data-view]').forEach(el=>el.classList.toggle('active',el.dataset.view===v));
  document.getElementById('vroot').innerHTML='';
  _lastNavAt=Date.now();
  const fn=({dashboard:rDash,containers:rCtrs,files:()=>rFilesHome(),terminal:rTerm,intercom:rIntercom,settings:rSettings,admin:rAccess,sessions:rSessions,access:rAccess,appstore:rAppStore,shares:rShares})[v];
  if(!fn)return;
  const gen=_renderGen;
  // A view's loader that is still running when you move on may fail because its page is gone;
  // that's not worth an error message (a real failure of the page you're on is still reported).
  const gone=e=>{
    if(gen!==_renderGen||curV!==v){console.debug('[Void OS] a page you had already left stopped loading:',v,e);
      reportClientError('promise',(e&&e.message)||String(e),e&&e.stack,{stale:true});return true}
    return false;
  };
  try{
    const res=fn();
    if(res&&typeof res.catch==='function')res.catch(e=>{if(!gone(e))throw e});
  }catch(e){if(!gone(e))throw e}
}
window.go=go;

function goToPasswordSettings(){
  _setPage='security';
  const nav=document.querySelector('#setNav .set-nav-btn[data-page="security"]');
  if(nav&&App.curV==='settings')nav.click();   // already on Settings: just switch category
  else go('settings');
  const el=document.getElementById('securitySection');
  if(el){
    el.scrollIntoView({behavior:'smooth',block:'center'});
    el.classList.add('flash-highlight');
    setTimeout(()=>el.classList.remove('flash-highlight'),1600);
  }
  document.getElementById('cpOld')?.focus();
}
window.goToPasswordSettings=goToPasswordSettings;

// Modal
const ovr=document.getElementById('ovr'),mdl=document.getElementById('mdl');
document.getElementById('mdlX')?.addEventListener('click',closeMdl);
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeMdl()});
function openMdl(title,body,foot='',lg=false){
  document.getElementById('mdlT').textContent=title;document.getElementById('mdlB').innerHTML=body;
  document.getElementById('mdlF').innerHTML=foot;mdl.classList.toggle('lg',!!lg);mdl.classList.remove('xl','rick');App.mdlLockUntil=0;
  document.getElementById('mdlX').style.visibility='';document.getElementById('mdlF').style.visibility='';ovr.classList.remove('hidden')}
function closeMdl(){if(App.mdlLockUntil>Date.now())return;     // a popup may hold itself open for a moment (the clock easter egg)
  ovr.classList.add('hidden');document.querySelectorAll('#mdlB audio,#mdlB video').forEach(m=>{try{m.pause()}catch(e){}});
  document.querySelectorAll('#mdlB iframe.fv-rick,#mdlB video.fv-rick').forEach(f=>f.remove())}
// The single shared confirm popover instance (see CSS comment above for why
// this is one reused element positioned per-call, not per-button markup).
let _vcPopEl=null,_vcCloseTimer=null,_vcAnchorEl=null;
function _vcEnsurePop(){
  if(_vcPopEl)return _vcPopEl;
  const el=document.createElement('div');
  el.className='vconfirm-pop';
  el.innerHTML=`<div class="vconfirm-msg" id="vcMsg"></div>
    <div class="vconfirm-actions"><button type="button" class="vconfirm-no" id="vcNo">No</button>
    <button type="button" class="vconfirm-yes" id="vcYes">Yes</button></div>`;
  document.body.appendChild(el);
  _vcPopEl=el;
  document.addEventListener('click',e=>{
    if(el.classList.contains('open')&&!el.contains(e.target)&&e.target!==_vcAnchorEl)_vcClose();
  });
  document.addEventListener('keydown',e=>{if(e.key==='Escape')_vcClose()});
  window.addEventListener('scroll',()=>{if(el.classList.contains('open'))_vcClose()},true);
  window.addEventListener('resize',()=>{if(el.classList.contains('open'))_vcClose()});
  return el;
}
function _vcClose(){
  if(!_vcPopEl)return;
  _vcPopEl.classList.remove('open');
  clearTimeout(_vcCloseTimer);
  _vcAnchorEl=null;
}
// Shows the inline yes/no popover anchored to anchorEl (typically the
// button that was just clicked — event.currentTarget). Auto-dismisses
// after a few seconds so a click-and-walk-away never leaves a live
// "delete this?" armed indefinitely.
function vConfirmAt(anchorEl,msg,onConfirm,yesLabel){
  if(!anchorEl){confirmDlg(msg,onConfirm,yesLabel);return} // no element to anchor to
                                                             // (e.g. a context menu that's
                                                             // already gone) — fall back to the modal
  const el=_vcEnsurePop();
  _vcAnchorEl=anchorEl;
  el.querySelector('#vcMsg').innerHTML=msg;
  const yesBtn=el.querySelector('#vcYes'),noBtn=el.querySelector('#vcNo');
  yesBtn.textContent=yesLabel||'Yes';
  // clone-and-replace to drop any listener from a previous call rather than stacking them
  const newYes=yesBtn.cloneNode(true);yesBtn.replaceWith(newYes);
  const newNo=noBtn.cloneNode(true);noBtn.replaceWith(newNo);
  newYes.addEventListener('click',e=>{e.stopPropagation();_vcClose();onConfirm()});
  newNo.addEventListener('click',e=>{e.stopPropagation();_vcClose()});
  const r=anchorEl.getBoundingClientRect();
  el.style.visibility='hidden';el.classList.add('open'); // measure real size while rendered
  const pw=el.offsetWidth,ph=el.offsetHeight;
  let left=r.right-pw,top=r.bottom+8;
  if(left<8)left=Math.min(r.left,window.innerWidth-pw-8);
  if(top+ph>window.innerHeight-8)top=r.top-ph-8; // flip above the trigger if there's no room below
  el.style.left=Math.max(8,left)+'px';el.style.top=Math.max(8,top)+'px';
  el.style.visibility='';
  clearTimeout(_vcCloseTimer);
  _vcCloseTimer=setTimeout(_vcClose,6000);
}
function confirmDlg(msg,cb,label='Delete',anchorEl=null){
  if(anchorEl){vConfirmAt(anchorEl,msg,cb,label);return}
  openMdl('Confirm',`<p style="color:var(--td);line-height:1.65">${msg}</p>`,
    `<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-d" id="cfmY">${esc(label)}</button>`);
  document.getElementById('cfmY')?.addEventListener('click',()=>{closeMdl();cb()})}
window.closeMdl=closeMdl;window.confirmDlg=confirmDlg;window.vConfirmAt=vConfirmAt;

// Greeting
function getGreeting(){
  const name=SETTINGS.display_name||UNAME||'';
  const h=new Date().getHours(),seed=new Date().getMinutes();
  let pool;
  const dow=new Date().getDay();
  const dayHi=['Happy Sunday','Happy Monday','Happy Tuesday','Happy Wednesday','Happy Thursday','Happy Friday','Happy Saturday'][dow];
  if(h>=5&&h<12)pool=['Good morning','Rise and shine','Morning','Welcome back','Hello there',dayHi,'Fresh start'];
  else if(h>=12&&h<17)pool=['Good afternoon','Afternoon','Welcome back','Hello there','Hope the day is going well',dayHi];
  else if(h>=17&&h<21)pool=['Good evening','Evening','Welcome back','Hello there','Good to see you','Hope it was a good day'];
  else pool=['Ready when you are','Burning the midnight oil','Working late','Still up','Night owl mode','Welcome back'];
  const base=pool[seed%pool.length];
  return name?`${base}, ${name}`:base;
}
// The greeting split into its two styled parts: "Good afternoon," + the name.
function greetHtml(){
  const name=SETTINGS.display_name||UNAME||'';const g=getGreeting();
  if(!name||!g.endsWith(', '+name))return`<span class="greet-hi">${esc(g)}.</span>`;
  return`<span class="greet-hi">${esc(g.slice(0,-(name.length+2)))},</span> <span class="greet-name">${esc(name)}</span><span class="greet-hi">.</span>`;
}
// Keeps the dashboard greeting current (morning/afternoon/etc.) without a visible clock
function startGreetingRefresh(){
  setInterval(()=>{
    if(new Date().getSeconds()===0){const g=document.getElementById('greetTitle');if(g)g.innerHTML=greetHtml()}
  },1000);
}

// ── DASHBOARD ───────────────────────────────────────────────────
const NP=44;let netH={up:[],dn:[]};
// The clock format is the user's choice (Settings > Appearance): 12-hour by default, or 24-hour.
function _clockParts(now){
  const is24=(SETTINGS&&SETTINGS.time_format)==='24';
  const H=now.getHours(),pad=n=>String(n).padStart(2,'0');
  const h=is24?pad(H):pad(((H+11)%12)+1),m=pad(now.getMinutes()),p=is24?'':(H>=12?'PM':'AM');
  return {h,m,p,str:`${h}:${m}`,short:`${h}:${m}${p?' '+p:''}`};
}
// The big clock, split so the digits, the blinking colon and AM/PM can be styled separately.
function _bigClockHtml(now){
  const t=_clockParts(now);
  return `<span class="hs-time">${esc(t.h)}<i class="hs-colon">:</i>${esc(t.m)}</span>${t.p?`<span class="hs-ampm">${esc(t.p)}</span>`:''}`;
}
// ── Particle clock (home screen) ───────────────────────────────────────
// The time is drawn as a cloud of particles that flow into the shape of the digits and re-form
// every second. The idea comes from a public "particle clock" demo; this is a rewrite for Void OS:
// spring physics on requestAnimationFrame (no fixed 20 ms timer), digits sampled from the bundled
// Orbitron face, colours read from the current theme, pointer repulsion, and a burst on press.
const HsClock=(()=>{
  let cv=null,ctx=null,off=null,octx=null,W=0,H=0,dpr=1,raf=0,parts=[],text='',period='',pill=null,sr=null;
  let mx=-9999,my=-9999,palette=null,palT=0,lastCheck=0,laidOut=false,reduce=false;
  const MAXP=1700,K=0.075,DAMP=0.80,R=58;
  const FONT='Orbitron, Sora, sans-serif';
  const hex=c=>{                                   // any CSS colour -> [r,g,b]
    const t=document.createElement('canvas').getContext('2d');t.fillStyle='#888';t.fillStyle=c;
    const v=t.fillStyle;
    if(v[0]==='#')return [parseInt(v.slice(1,3),16),parseInt(v.slice(3,5),16),parseInt(v.slice(5,7),16)];
    const m=v.match(/[\d.]+/g)||[136,136,136];return [+m[0],+m[1],+m[2]];
  };
  function readPalette(){
    const cs=getComputedStyle(document.documentElement);
    const a=hex(cs.getPropertyValue('--tx').trim()||'#eee'),b=hex(cs.getPropertyValue('--ps').trim()||cs.getPropertyValue('--pu').trim()||'#8af');
    palette=[0,1,2,3].map(i=>{const t=i/3;return `rgb(${Math.round(a[0]+(b[0]-a[0])*t)},${Math.round(a[1]+(b[1]-a[1])*t)},${Math.round(a[2]+(b[2]-a[2])*t)})`});
    palT=performance.now();
  }
  function timeParts(){
    const t=_clockParts(new Date());
    return {str:t.str,period:t.p,short:t.short};
  }
  function layout(){
    if(!cv)return false;
    const w=cv.clientWidth,h=cv.clientHeight;
    if(w<40||h<20)return false;
    dpr=Math.min(2,window.devicePixelRatio||1);
    if(w!==W||h!==H||cv.width!==Math.round(w*dpr)){
      W=w;H=h;cv.width=Math.round(w*dpr);cv.height=Math.round(h*dpr);
      off.width=W;off.height=H;ctx.setTransform(dpr,0,0,dpr,0,0);
      text='';                                      // re-sample for the new size
    }
    laidOut=true;return true;
  }
  function sample(str){
    octx.clearRect(0,0,W,H);
    let fs=Math.min(H*0.8,74);
    octx.font=`600 ${fs}px ${FONT}`;
    const mw=octx.measureText('00:00').width;
    if(mw>W*0.97){fs*=W*0.97/mw;octx.font=`600 ${fs}px ${FONT}`}
    octx.textBaseline='middle';octx.textAlign='center';octx.fillStyle='#fff';
    octx.fillText(str,W/2,H/2+fs*0.04);
    const data=octx.getImageData(0,0,W,H).data;
    let step=2,pts=[];
    for(;;){
      pts=[];
      for(let y=0;y<H;y+=step)for(let x=0;x<W;x+=step)if(data[((y|0)*W+(x|0))*4+3]>120)pts.push(x,y);
      if(pts.length/2<=MAXP||step>=5)break;
      step++;
    }
    return {pts,step};
  }
  function retarget(str){
    const {pts,step}=sample(str);
    const n=pts.length/2;if(!n)return;
    const T=[];for(let i=0;i<n;i++)T.push([pts[i*2],pts[i*2+1]]);
    T.sort((a,b)=>a[0]-b[0]||a[1]-b[1]);
    while(parts.length<Math.min(n,MAXP)){
      const ang=Math.random()*Math.PI*2,r=Math.max(W,H)*(.4+Math.random()*.6);
      parts.push({x:W/2+Math.cos(ang)*r,y:H/2+Math.sin(ang)*r,vx:0,vy:0,tx:0,ty:0,c:0,s:1,ph:Math.random()*6.28});
    }
    parts.sort((a,b)=>a.x-b.x||a.y-b.y);
    const L=parts.length;
    for(let i=0;i<L;i++){
      const t=T[Math.min(n-1,Math.floor(i*n/L))],p=parts[i];
      p.tx=t[0]+(Math.random()-.5)*step*.6;p.ty=t[1]+(Math.random()-.5)*step*.6;
      p.c=Math.min(3,Math.floor(t[0]/W*4));
      p.s=step*0.42+Math.random()*0.3;
    }
  }
  function frame(now){
    if(!cv||!cv.isConnected){raf=0;cv=null;return}
    raf=requestAnimationFrame(frame);
    if(now-lastCheck>120){
      lastCheck=now;
      const tp=timeParts();
      if(layout()&&(tp.str!==text)){text=tp.str;retarget(text)}
      if(tp.period!==period){period=tp.period;if(pill){pill.textContent=period;pill.hidden=!period}}
      if(sr){const s=tp.short;if(sr.textContent!==s)sr.textContent=s}
      const de=document.getElementById('hsBigDate');
      if(de){const ds=new Date().toLocaleDateString([],{weekday:'long',month:'long',day:'numeric'});if(de.textContent!==ds)de.textContent=ds}
      if(now-palT>3000||!palette)readPalette();
    }
    if(!laidOut||!parts.length)return;
    ctx.clearRect(0,0,W,H);
    const k=reduce?1:K,d=reduce?0:DAMP,t=now*0.002;
    const buckets=[[],[],[],[]];
    for(let i=0;i<parts.length;i++){
      const p=parts[i];
      let ax=(p.tx-p.x)*k,ay=(p.ty-p.y)*k;
      if(!reduce){
        ax+=Math.sin(t*1.7+p.ph)*0.035;ay+=Math.cos(t*1.3+p.ph)*0.035;        // a faint shimmer
        const dx=p.x-mx,dy=p.y-my,d2=dx*dx+dy*dy;
        if(d2<R*R&&d2>0.01){const dist=Math.sqrt(d2),f=(1-dist/R)*2.4;ax+=dx/dist*f;ay+=dy/dist*f}
      }
      p.vx=(p.vx+ax)*d;p.vy=(p.vy+ay)*d;
      p.x+=p.vx;p.y+=p.vy;
      buckets[p.c].push(p);
    }
    for(let b=0;b<4;b++){
      const arr=buckets[b];if(!arr.length)continue;
      ctx.fillStyle=palette?palette[b]:'#fff';
      ctx.beginPath();
      for(let i=0;i<arr.length;i++){const p=arr[i];ctx.moveTo(p.x+p.s,p.y);ctx.arc(p.x,p.y,p.s,0,6.2832)}
      ctx.fill();
    }
  }
  function burst(){
    for(const p of parts){const a=Math.random()*6.2832,v=3+Math.random()*7;p.vx+=Math.cos(a)*v;p.vy+=Math.sin(a)*v}
  }
  function mount(box){
    if(raf){cancelAnimationFrame(raf);raf=0}
    parts=[];text='';period='';laidOut=false;palette=null;
    if(!box||!window.HTMLCanvasElement)return false;
    reduce=!!(window.matchMedia&&matchMedia('(prefers-reduced-motion: reduce)').matches);
    cv=box.querySelector('canvas');pill=box.querySelector('.hs-ampm');sr=box.querySelector('.hs-sr');
    if(!cv||!cv.getContext)return false;
    ctx=cv.getContext('2d');off=document.createElement('canvas');octx=off.getContext('2d',{willReadFrequently:true});
    const rel=e=>{const r=cv.getBoundingClientRect();return [e.clientX-r.left,e.clientY-r.top]};
    cv.addEventListener('pointermove',e=>{[mx,my]=rel(e)});
    cv.addEventListener('pointerleave',()=>{mx=my=-9999});
    // Easter egg: spam the clock 13 times (each press within a second and a half of the last) and
    // you get rickrolled; a notification then rickrolls you once more.
    let taps=0,lastTap=0;
    cv.addEventListener('pointerdown',e=>{
      [mx,my]=rel(e);burst();
      const now=Date.now();
      taps=now-lastTap<1500?taps+1:1;lastTap=now;
      if(taps>=13){taps=0;rickroll()}
    });
    const go=()=>{if(cv&&!raf)raf=requestAnimationFrame(frame)};
    if(document.fonts&&document.fonts.load){
      Promise.race([document.fonts.load('600 40px Orbitron','0123456789:'),new Promise(r=>setTimeout(r,1500))]).then(go,go);
    }else go();
    return true;
  }
  return {mount};
})();
function rDash(){
  _okSeed=Math.random();                  // a fresh status line each time the dashboard is opened
  const root=document.getElementById('vroot');
  const hs=!!document.querySelector('.shell.hs');
  _cpuHist=[];
  root.innerHTML=`
  <header class="dash-hero">
    <h1 class="dash-greet" id="greetTitle">${greetHtml()}</h1>
    <p class="dash-status pending" id="dashStatus" role="status"><span class="st-dot" aria-hidden="true"></span><span id="dashStatusText">Checking the server\u2026</span></p>
  </header>
  <div class="dash-body">
    <section class="dash-apps" aria-label="Apps">
      ${hs?`<div class="th hs-sec"><h2 class="th-lbl">System</h2></div>
      <div class="tgrid" id="hsSysGrid">${_systemTilesHtml()}</div>`:''}
      <div class="th"><h2 class="th-lbl">Apps</h2>
        ${IS_ADMIN?`<button class="tb" onclick="showAddTile()"><svg viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg> Add app</button>`:''}
      </div>
      <div class="tgrid" id="tgrid"><div class="loading"><div class="loader"></div></div></div>
    </section>
    <aside class="card dash-server" aria-label="Server">
      ${`<div class="hs-clock-big tnum" id="hsBigClock" role="timer" aria-live="off"><canvas aria-hidden="true"></canvas><span class="hs-ampm" hidden></span><span class="hs-sr"></span></div>
        <div class="hs-clock-date" id="hsBigDate">${new Date().toLocaleDateString([],{weekday:'long',month:'long',day:'numeric'})}</div>`}
      <h2 class="sec-h">Server</h2>
      <div class="gauges">
        ${_gaugeHtml('gCpu','Processor')}
        ${_gaugeHtml('gMem','Memory')}
      </div>
      <div class="net-row" aria-label="Network">
        <span class="net-k">Network</span>
        <span class="net-v"><span class="net-dir" aria-label="upload">\u2191</span><b class="tnum" id="nU">\u2014</b></span>
        <span class="net-v"><span class="net-dir" aria-label="download">\u2193</span><b class="tnum" id="nD">\u2014</b></span>
      </div>
      <div class="pwr-row" id="pwrRow" hidden aria-label="Power"></div>
      <div class="drives" id="diskSec" aria-label="Storage"></div>
      <div class="srv-foot tnum" id="upSub">\u00a0</div>
    </aside>
  </div>`;
  loadTiles();startStatsPoll();window.addEventListener('resize',drawNet);
  {
    const box=document.getElementById('hsBigClock');
    if(box&&!HsClock.mount(box))box.innerHTML=_bigClockHtml(new Date());
    window.removeEventListener('resize',_fitServerCard);window.addEventListener('resize',_fitServerCard);
  }
}
let _cpuHist=[];
// Ring gauge: an SVG circle whose stroke fills to the value. Colour follows
// load — the theme's success / warning / danger colours — and eases between
// them as the value changes (CSS transitions on stroke and stroke-dashoffset).
const _G_R=46,_G_C=2*Math.PI*_G_R;
function _gaugeHtml(id,label){
  return `<div class="gauge" id="${id}">
    <svg viewBox="0 0 120 120" aria-hidden="true"><circle class="g-track" cx="60" cy="60" r="${_G_R}"/>
      <circle class="g-fill" cx="60" cy="60" r="${_G_R}" stroke-dasharray="${_G_C.toFixed(2)}" stroke-dashoffset="${_G_C.toFixed(2)}"/></svg>
    <div class="g-mid"><span class="g-num tnum">\u2014</span><span class="g-lbl">${label}</span></div>
    <div class="g-sub tnum"></div></div>`;
}
function _loadLevel(p,warn=85,bad=95){return p>=bad?'bad':p>=warn?'warn':'ok'}
function _setGauge(id,pct,sub,warn,bad){
  const g=document.getElementById(id);if(!g)return;
  const v=Math.max(0,Math.min(100,pct||0));
  g.querySelector('.g-fill').style.strokeDashoffset=(_G_C*(1-v/100)).toFixed(2);
  g.dataset.level=_loadLevel(v,warn,bad);
  g.querySelector('.g-num').textContent=Math.round(v)+'%';
  g.setAttribute('aria-label',`${g.querySelector('.g-lbl').textContent} ${Math.round(v)}%`);
  if(sub!=null)g.querySelector('.g-sub').textContent=sub;
}
function _uptimeWords(u){
  // "2h 5m" / "4m 10s" from the server → "Running for 2 hours" etc.
  const h=+((u.match(/(\d+)h/)||[])[1]||0),m=+((u.match(/(\d+)m/)||[])[1]||0);
  if(h>=48)return`Running for ${Math.floor(h/24)} days`;
  if(h>=1)return`Running for ${h} hour${h===1?'':'s'}`;
  if(m>=1)return`Running for ${m} minute${m===1?'':'s'}`;
  return'Just started';
}
// The headline sentence: the single most useful thing to know right now.
function _dashStatus(d){
  const files=(d.disks||[]).find(x=>x.files)||(d.disks||[])[0];
  _cpuHist.push(d.cpu);if(_cpuHist.length>8)_cpuHist.shift();
  const cpuAvg=_cpuHist.reduce((a,b)=>a+b,0)/_cpuHist.length;
  const where=files?(files.label==='System drive'?'the system drive':files.label):'';
  if(files&&files.percent>=95)return['bad',`${files.label} is almost full. ${fmtB(files.free)} left \u2014 free up space before uploads start failing.`];
  if(IS_ADMIN&&!DOKR)return['warn','Docker isn\u2019t running, so App Store apps are unavailable. Everything else is working.'];
  if(files&&files.percent>=90)return['warn',`${files.label} is ${Math.round(files.percent)}% full. ${fmtB(files.free)} left.`];
  if(_cpuHist.length>=5&&cpuAvg>=90)return['warn',`The processor has been busy for a while (${Math.round(cpuAvg)}%). Things may feel slow.`];
  if(d.ram&&d.ram.percent>=95)return['warn',`Memory is nearly full (${Math.round(d.ram.percent)}%). Apps may slow down.`];
  if(d.temp!=null&&d.temp>=80)return['warn',`The server is running hot (${Math.round(d.temp)}\u00b0C). Check its cooling.`];
  const pw=_powerInfo(d.power);
  if(pw&&pw.lvl!=='ok')return[pw.lvl==='bad'?'bad':'warn',pw.sentence];
  return['ok',_okMessage(d,files,where)];
}
// The line shown when nothing needs attention. One is picked per visit to the dashboard (so it doesn't
// flicker on every refresh) from a pool; each entry is built from live numbers and returns null when
// the number it needs isn't available, in which case the plain message is used.
let _okSeed=Math.random();
const _OK_POOL=[
  (c)=>c.files?`Everything\u2019s running. ${fmtB(c.files.free)} free on ${c.where}.`:null,
  (c)=>c.up?`All quiet. ${c.up}, and not a single complaint.`:null,
  (c)=>c.cpu!=null&&c.cpu<50?`Smooth sailing \u2014 the processor is at ${Math.round(c.cpu)}% and has barely noticed you.`:null,
  (c)=>c.files&&c.files.percent<70?`${fmtB(c.files.free)} of breathing room on ${c.where}. Go wild.`:null,
  (c)=>c.temp!=null&&c.temp<65?`Cool and calm at ${Math.round(c.temp)}\u00b0C. Your server is a happy server.`:null,
  (c)=>c.apps?`${c.apps} app${c.apps===1?'':'s'} one click away, and everything behind ${c.apps===1?'it':'them'} is awake.`:null,
  (c)=>c.ram!=null&&c.ram<70?`Memory at ${Math.round(c.ram)}% \u2014 plenty of headroom, no drama.`:null,
  (c)=>c.hour<5?'Burning the midnight oil? The server is up too, and perfectly healthy.':null,
  (c)=>c.hour>=5&&c.hour<12?'Good morning. The server already had its coffee \u2014 all systems go.':null,
  (c)=>c.hour>=12&&c.hour<18?'Afternoon check-in: nothing is on fire. Carry on.':null,
  (c)=>c.hour>=18?'Winding down? Everything here is running fine \u2014 you can relax.':null,
  (c)=>c.up&&c.files?`${c.up} without a hiccup, with ${fmtB(c.files.free)} to spare.`:null,
  ()=>'Nothing needs your attention. Enjoy the quiet.',
  ()=>'Your files, your hardware, your rules \u2014 and everything is running.',
  (c)=>c.cpu!=null&&c.ram!=null&&c.cpu<80&&c.ram<85?`Healthy: CPU ${Math.round(c.cpu)}%, memory ${Math.round(c.ram)}%. Nothing to fix.`:null,
  // by day of the week
  (c)=>['Sunday mode: the server is relaxed, and so can you be.','New week, same rock-solid server.','Tuesday, and nothing is broken. A good one.',
        'Midweek check: all systems steady.','Almost Friday \u2014 the server is already there.','Friday vibes. The server has no plans to ruin them.',
        'Saturday: nothing to maintain, nothing to fix.'][c.day],
  // by the numbers
  (c)=>c.procs?`${c.procs} processes humming along, and none of them are misbehaving.`:null,
  (c)=>c.cores&&c.cpu!=null&&c.cpu<30?`${c.cores} core${c.cores===1?'':'s'} ready and mostly idle. Throw something at it.`:null,
  (c)=>c.up&&/days/.test(c.up)?`${c.up}. That\u2019s a proper streak.`:null,
  (c)=>c.ramUsed&&c.ramTotal&&c.ram<75?`${fmtB(c.ramUsed)} of ${fmtB(c.ramTotal)} in use. Plenty left for whatever comes next.`:null,
  (c)=>c.temp!=null&&c.temp<55?`${Math.round(c.temp)}\u00b0C \u2014 practically a spa day for the hardware.`:null,
  (c)=>c.down!=null&&c.up_!=null&&c.down<51200&&c.up_<51200?'The network is calm \u2014 barely a whisper going in or out.':null,
  (c)=>c.down>=1048576?`Pulling ${fmtSpd(c.down)} right now. The pipes are earning their keep.`:null,
  (c)=>c.where?`Everything on ${c.where} is right where you left it.`:null,
  (c)=>c.files&&c.files.percent<50?`Only ${Math.round(c.files.percent)}% of ${c.where} is used. Lots of room to grow.`:null,
  (c)=>c.apps?`Your ${c.apps} app${c.apps===1?' is':'s are'} lined up and waiting for a click.`:null,
  // by the hour
  (c)=>c.hour>=12&&c.hour<14?'Lunch break? The server will keep an eye on things.':null,
  (c)=>c.hour>=22?'Late one? The server keeps watch so you can log off.':null,
  (c)=>c.hour>=6&&c.hour<9?'Early start. The server was up before you, as usual.':null,
  // by name
  (c)=>c.name?`${c.name}, the server has your back. Everything is running.`:null,
  (c)=>c.name?`All green, ${c.name}. Take a breath.`:null,
  // just because
  ()=>'No alerts, no warnings, no surprises.',
  ()=>'Quiet logs, healthy disks, happy server.',
  ()=>'You own the box. The box is doing great.',
  ()=>'Zero alerts. Zero drama. Zero reason to worry.',
  ()=>'Powered by your hardware, answering to nobody else.',
  ()=>'Nothing to report \u2014 which is the best kind of report.',
  ()=>'Everything is exactly where it should be.',
  ()=>'All systems go. Whatever you were about to do, go do it.',
  ()=>'Your server, your data, your terms.',
  ()=>'Still here, still running, still yours.',
];
function _okMessage(d,files,where){
  const now=new Date();
  const c={files,where,cpu:d.cpu,ram:d.ram&&d.ram.percent,ramUsed:d.ram&&d.ram.used,ramTotal:d.ram&&d.ram.total,temp:d.temp,
           hour:now.getHours(),day:now.getDay(),procs:d.procs,cores:d.cores&&d.cores.length,
           down:d.net&&d.net.down,up_:d.net&&d.net.up,name:SETTINGS.display_name||'',
           up:d.uptime?_uptimeWords(d.uptime):'',apps:(typeof _hsApps!=='undefined'&&_hsApps||[]).length};
  if(c.up==='Just started')c.up='';
  const first=_OK_POOL[Math.floor(_okSeed*_OK_POOL.length)](c);
  return first||_OK_POOL[0](c)||'Everything\u2019s running.';
}
// Power: what the Pi's firmware says about its supply, or the laptop's battery. Null when the
// machine has neither (a desktop PC or a server on mains), and then the row stays hidden.
function _powerInfo(p){
  if(!p)return null;
  const rows=[];
  let lvl='ok',sentence='';
  const pi=p.pi,b=p.battery;
  if(pi){
    if(pi.undervolt_now){lvl='bad';sentence='The Raspberry Pi is under-voltage right now: its power supply or cable can\u2019t keep up. Use the official 5 V supply and a short, thick cable.'}
    else if(pi.throttled_now||pi.freq_capped){lvl='warn';sentence='The Raspberry Pi is slowing itself down'+(pi.temp_limit?' because it is hot.':' to protect itself.')}
    else if(pi.undervolt_past){lvl='warn';sentence='The Raspberry Pi has had a power dip since it started. Check its supply and cable before it corrupts something.'}
    const txt=pi.undervolt_now?'Under-voltage now':pi.throttled_now?'Throttled':pi.freq_capped?'Speed capped':pi.undervolt_past?'Dipped earlier':'Power OK';
    const sub=pi.undervolt_now?'Supply too weak':pi.undervolt_past?'Under-voltage since boot':(pi.throttled_past?'Throttled earlier':'No power problems since boot');
    rows.push({k:'Power',v:txt,sub,lvl:pi.undervolt_now?'bad':(pi.throttled_now||pi.freq_capped||pi.undervolt_past)?'warn':'ok'});
  }
  if(b){
    const low=b.plugged===false&&b.percent<=20,crit=b.plugged===false&&b.percent<=10;
    const l=crit?'bad':low?'warn':'ok';
    if(l!=='ok'&&(lvl==='ok'||(l==='bad'&&lvl!=='bad'))){lvl=l;sentence=crit?`The battery is at ${b.percent}% and not charging. The server will switch off soon \u2014 plug it in.`:`The battery is at ${b.percent}% and not charging. Plug the server in.`}
    let sub=b.plugged===true?(b.percent>=100?'Fully charged':'Charging'):(b.plugged===false?'On battery':'');
    if(b.plugged===false&&b.secs_left){const h=Math.floor(b.secs_left/3600),m=Math.round(b.secs_left%3600/60);sub+=` \u00b7 about ${h?h+' h ':''}${m} min left`}
    rows.push({k:'Battery',v:b.percent+'%',sub,lvl:l});
  }
  return {rows,lvl,sentence};
}
function _paintPower(p){
  const el=document.getElementById('pwrRow');if(!el)return;
  const info=_powerInfo(p);
  if(!info||!info.rows.length){el.hidden=true;return}
  el.hidden=false;
  const h=info.rows.map(r=>`<div class="pwr-item" data-level="${r.lvl}"><span class="pwr-k">${esc(r.k)}</span>
    <b class="pwr-v tnum">${esc(r.v)}</b><span class="pwr-s">${esc(r.sub||'')}</span></div>`).join('');
  if(el._h!==h){el.innerHTML=h;el._h=h}
}
function applyS(d){
  if(!d||d.error)return;
  _paintPower(d.power);
  const [lvl,msg]=_dashStatus(d);
  const st=document.getElementById('dashStatus');
  if(st){st.className='dash-status'+(lvl==='ok'?'':' '+lvl);const t=document.getElementById('dashStatusText');if(t&&t.textContent!==msg)t.textContent=msg}
  _setGauge('gCpu',d.cpu,`${d.cores?d.cores.length+(d.cores.length===1?' core':' cores'):''}${d.procs?(d.cores?' \u00b7 ':'')+d.procs+' processes':''}`);
  // Memory runs high normally (Windows and Linux both use spare RAM as cache), so it
  // turns amber/red later than the processor does.
  _setGauge('gMem',d.ram.percent,`${fmtB(d.ram.used)} of ${fmtB(d.ram.total)}`+(d.temp!=null?` \u00b7 ${d.temp.toFixed(0)}\u00b0C`:''),90,97);
  const us=document.getElementById('upSub');if(us)us.textContent=_uptimeWords(d.uptime||'');
  const nu=document.getElementById('nU');if(nu)nu.textContent=fmtSpd(d.net.up);
  const nd=document.getElementById('nD');if(nd)nd.textContent=fmtSpd(d.net.down);
  netH.up.push(d.net.up);netH.dn.push(d.net.down);
  if(netH.up.length>NP){netH.up.shift();netH.dn.shift()}
  drawNet();
  const ds=document.getElementById('diskSec');
  if(ds&&d.disks){
    // Storage reads differently from load: a calm capacity bar per drive,
    // led by how much is FREE (what people actually want to know).
    // The home screen's panel is a fixed size, so it lists the first four drives; the classic page just grows.
    const shown=(window.innerWidth>1000&&document.querySelector('.shell.hs'))?d.disks.slice(0,4):d.disks,hidden=d.disks.length-shown.length;
    ds.innerHTML=shown.map(dk=>{const lv=dk.percent>=95?'bad':dk.percent>=90?'warn':'ok';
      return `<div class="drive" data-level="${lv}">
        <div class="drive-top"><span class="drive-name">${esc(dk.label||dk.mount)}${dk.files?' <span class="srv-tag">Files</span>':''}</span>
          <span class="drive-pct tnum">${Math.round(dk.percent)}%</span></div>
        <div class="drive-bar" role="img" aria-label="${escA((dk.label||dk.mount)+': '+Math.round(dk.percent)+'% used')}"><i style="width:${dk.percent}%"></i></div>
        <div class="drive-sub tnum">${fmtB(dk.free)} free of ${fmtB(dk.total)}</div></div>`}).join('')
      +(hidden>0?`<div class="drive-sub">+ ${hidden} more drive${hidden===1?'':'s'} in Files</div>`:'');
  }
  _fitServerCard();
}
// The home screen's server panel never scrolls: if its content is taller than the room it has,
// the whole panel is scaled down to fit (never below 62%).
function _fitServerCard(){
  const el=document.querySelector('.dash-server');if(!el)return;
  el.style.zoom='';
  if(window.innerWidth<=1000)return;
  const over=el.scrollHeight-el.clientHeight;
  if(over>1)el.style.zoom=Math.max(.62,el.clientHeight/el.scrollHeight).toFixed(3);
}
function ring(rid,vid,lbl){
  const R=32,C=(2*Math.PI*R).toFixed(1);
  return `<div class="rw" style="flex-shrink:0">
    <svg width="74" height="74" viewBox="0 0 74 74"><circle class="ring-t" cx="37" cy="37" r="${R}" fill="none" stroke-width="6"/>
    <circle class="ring-p" id="${rid}" cx="37" cy="37" r="${R}" fill="none" stroke-width="6" stroke-dasharray="${C}" stroke-dashoffset="${C}"/></svg>
    <div class="rv" id="${vid}">\u2014<div class="rs-">%</div></div></div>${lbl?`<div class="slbl">${lbl}</div>`:''}`;
}
// Polls /api/stats every 2s. We use polling rather than the old EventSource-based
// /api/stats/stream because Cloudflare Quick Tunnels (trycloudflare.com) buffer
// text/event-stream responses at the edge and never deliver them to the client -
// SSE only worked over direct LAN access, not through the tunnel. Plain polling
// works identically over LAN and over the tunnel.
async function pollStats(){
  try{
    const r=await api('/api/stats');
    applyS(await r.json());
  }catch(_){/* transient network hiccup - next tick will retry */}
}
function startStatsPoll(){
  if(statsPoll)clearInterval(statsPoll);
  pollStats();
  statsPoll=setInterval(pollStats,2000);
}
function drawNet(){
  const c=document.getElementById('ncvs');if(!c)return;
  const W=c.offsetWidth||260,H=c.clientHeight||38;c.width=W;c.height=H;
  const ctx=c.getContext('2d');ctx.clearRect(0,0,W,H);
  const all=[...netH.up,...netH.dn],mv=Math.max(...all,1024);
  function line(data,col){
    if(data.length<2)return;const step=W/(NP-1);
    ctx.beginPath();data.forEach((v,i)=>{const x=i*step,y=H-(v/mv)*(H*.9)-2;i?ctx.lineTo(x,y):ctx.moveTo(x,y)});
    ctx.strokeStyle=col;ctx.lineWidth=1.5;ctx.stroke();
    ctx.lineTo((data.length-1)*step,H);ctx.lineTo(0,H);ctx.closePath();
    ctx.fillStyle=col.replace('rgb(','rgba(').replace(')',',0.1)');ctx.fill();
  }
  line(netH.dn,'rgb(91,196,255)');line(netH.up,'rgb(155,133,255)');
}
let _tilesPoll=null;
function _tileStatusLabel(a){
  if(!a.tunnel_enabled)return null;
  const s=a.tunnel_status;
  if(s==='connected')return{cls:'ts-connected',text:'Live'};
  if(s==='not_installed')return{cls:'ts-error',text:'cloudflared missing'};
  if(s==='at_cap')return{cls:'ts-error',text:'Tunnel limit hit'};
  if(s==='error')return{cls:'ts-error',text:'Tunnel error'};
  if(s==='reconnecting')return{cls:'ts-pending',text:'Reconnecting…'};
  return{cls:'ts-pending',text:'Connecting…'};
}
function _gateBadge(a){
  // Only meaningful to admins (gate_enabled is only ever present in the admin view of a tile)
  // and only worth showing once the tunnel itself is in the picture at all.
  if(!a.tunnel_enabled||!a.gate_enabled)return '';
  const bad=a.gate_status==='error'||a.gate_status==='not_installed';
  return `<div class="tgate ${bad?'tgate-err':''}" title="${bad?'Gate proxy problem — see Edit':'PIN gate is protecting this tunnel'}">\u{1F512}</div>`;
}
function _systemTilesHtml(){
  // home-cloud systems treat Files, App Store, Settings etc. as icons on the home
  // screen itself. Same list as the launcher/search (hsViews), minus Home.
  if(!document.querySelector('.shell.hs'))return '';
  return hsViews().filter(x=>x.v!=='dashboard').map(x=>
    `<a class="card tile ch" href="javascript:void(0)" onclick="go('${x.v}')" data-tile-name="${escA(x.n.toLowerCase())}">
      <div class="ticon">${x.i}</div><div class="tname">${esc(x.n)}</div></a>`).join('');
}
async function loadTiles(){
  const gen=_renderGen; // read-only: this only refreshes a scoped sub-element, not a new navigation
  const g=document.getElementById('tgrid');if(!g)return;
  try{
    let apps=await(await api('/api/apps')).json();
    if(gen!==_renderGen)return;
    if(!Array.isArray(apps))apps=[]; // defensive: backend should always send an array, but never trust it blindly
    _hsApps=apps;
    const sysG=document.getElementById('hsSysGrid');if(sysG)sysG.innerHTML=_systemTilesHtml();
    let anyPending=false;
    g.innerHTML=apps.map(a=>{
      const st=_tileStatusLabel(a);
      const pending=a.tunnel_enabled&&a.tunnel_status!=='connected';
      if(pending)anyPending=true;
      const tag=pending?'div':'a'; // pending tunnel tiles aren't clickable yet — no live URL to send them to
      const hrefAttr=pending?'':`href="${escA(a.url)}" target="_blank" rel="noopener"`;
      return `<${tag} ${hrefAttr} class="card tile ch${pending?' tunnel-pending':''}" data-id="${a.id}" data-tile-name="${escA((a.name||'').toLowerCase())}"
        data-app-tile="1" draggable="${IS_ADMIN?'true':'false'}"
        ${pending?`onclick="toast('Tunnel still connecting — try again shortly','info')"`:''}>
        <div class="ticon">${appIconHtml(a.icon)}</div><div class="tname">${esc(a.name)}</div><div class="tdesc">${esc(a.description||'')}</div>
        ${st?`<div class="tstatus ${st.cls}">${st.text}</div>`:''}
        ${_gateBadge(a)}
        ${IS_ADMIN?`<button class="tedit" onclick="event.preventDefault();event.stopPropagation();editTile('${a.id}')" title="Edit">\u270e</button>
        <button class="tdel" onclick="event.preventDefault();event.stopPropagation();delTile('${a.id}',event)" title="Remove">\u2715</button>`:''}</${tag}>`;
    }).join('')+
      (IS_ADMIN?`<button class="card tile tile-add ch" onclick="showAddTile()"><div class="ticon">+</div><div class="tname">Add app</div></button>`
        :(apps.length?'':`<div class="empty-dir compact" style="grid-column:1/-1">No apps have been added yet.</div>`));
    wireTileContextMenu(g);
    g.querySelectorAll('.tile').forEach(t=>{
      const d=t.querySelector('.tdel'),e=t.querySelector('.tedit');
      if(d)t.addEventListener('mouseenter',()=>{d.style.opacity='1';if(e)e.style.opacity='1'});
      if(d)t.addEventListener('mouseleave',()=>{d.style.opacity='0';if(e)e.style.opacity='0'});
    });
    if(IS_ADMIN)wireTileReorder(g);
    if(_tilesPoll){clearTimeout(_tilesPoll);_tilesPoll=null}
    if(anyPending)_tilesPoll=setTimeout(loadTiles,2500); // keep polling only while something's still connecting
  }catch(e){if(gen!==_renderGen)return;if(g)g.innerHTML=`<div style="color:var(--rd);padding:12px">${esc(e.message)}</div>`}
}
// Right-click (or long-press on touch) an installed app tile: Open / Copy
// link, plus Edit / Remove for admins — home-screen's per-app "\u22ef" menu.
function wireTileContextMenu(grid){
  grid.querySelectorAll('.tile[data-app-tile]').forEach(t=>{
    const open=(x,y)=>{
      const a=_hsApps.find(z=>z.id===t.dataset.id);if(!a)return;
      document.querySelectorAll('.ctx.tile-ctx').forEach(m=>m.remove());
      const m=document.createElement('div');m.className='ctx tile-ctx';
      const usable=a.url&&a.url!=='#'&&!(a.tunnel_enabled&&a.tunnel_status!=='connected');
      m.innerHTML=(usable?`<div class="ctx-item" data-a="open">\u2197\uFE0F Open</div><div class="ctx-item" data-a="copy">\u{1F517} Copy link</div>`:'')
        +(IS_ADMIN?`${usable?'<div class="ctx-sep"></div>':''}<div class="ctx-item" data-a="edit">\u270E Edit</div><div class="ctx-item danger" data-a="del">\u2715 Remove</div>`:'');
      if(!m.innerHTML)return;
      document.body.appendChild(m);
      m.style.left=Math.min(x,window.innerWidth-m.offsetWidth-8)+'px';
      m.style.top=Math.min(y,window.innerHeight-m.offsetHeight-8)+'px';
      m.addEventListener('click',ev=>{
        const it=ev.target.closest('[data-a]');if(!it)return;m.remove();
        if(it.dataset.a==='open')window.open(a.url,'_blank','noopener');
        else if(it.dataset.a==='copy')navigator.clipboard?.writeText(new URL(a.url,location.href).href).then(()=>toast('Link copied','success'),()=>toast('Copy failed','error'));
        else if(it.dataset.a==='edit')editTile(a.id);
        else if(it.dataset.a==='del')delTile(a.id,{currentTarget:t,stopPropagation(){},preventDefault(){}});
      });
      setTimeout(()=>document.addEventListener('click',()=>m.remove(),{once:true}),0);
    };
    t.addEventListener('contextmenu',e=>{e.preventDefault();open(e.clientX,e.clientY)});
    let lp=null;
    t.addEventListener('touchstart',e=>{const p=e.touches[0];lp=setTimeout(()=>{lp=null;open(p.clientX,p.clientY)},550)},{passive:true});
    ['touchend','touchmove','touchcancel'].forEach(ev=>t.addEventListener(ev,()=>{if(lp){clearTimeout(lp);lp=null}},{passive:true}));
  });
}
// Drag-and-drop reorder for installed app tiles (system tiles and the Add-app
// button are excluded via [data-app-tile] — they aren't real, reorderable
// apps.json entries). Reorders live in the DOM as you drag, one PUT to
// persist once you drop — same "optimistic then persist" shape as every
// other instant-feeling toggle in this app (theme, wallpaper, UI mode).
let _dragTile=null;
function wireTileReorder(grid){
  const tiles=()=>[...grid.querySelectorAll('.tile[data-app-tile]')];
  tiles().forEach(t=>{
    t.addEventListener('dragstart',e=>{_dragTile=t;t.classList.add('tile-dragging');e.dataTransfer.effectAllowed='move'});
    t.addEventListener('dragend',()=>{t.classList.remove('tile-dragging');persistTileOrder(grid)});
    t.addEventListener('dragover',e=>{
      if(!_dragTile||_dragTile===t)return;
      e.preventDefault();
      const r=t.getBoundingClientRect();
      const before=(e.clientX-r.left)<r.width/2;
      t.parentNode.insertBefore(_dragTile,before?t:t.nextSibling);
    });
    t.addEventListener('drop',e=>e.preventDefault());
  });
}
async function persistTileOrder(grid){
  const order=[...grid.querySelectorAll('.tile[data-app-tile]')].map(t=>t.dataset.id);
  if(!order.length)return;
  try{
    const r=await(await api('/api/apps/reorder',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({order})})).json();
    if(!r.ok)errToast(r,'Failed to save order');
  }catch(e){toast('Failed to save order','error')}
}
function _tileModalBody(a){
  a=a||{};
  return `
    <div class="fg"><label class="fgl">Name</label><input class="fgi" id="aN" placeholder="Jellyfin" value="${escA(a.name||'')}" autofocus/></div>
    <div class="fg"><label class="fgl">URL</label><input class="fgi" id="aU" type="url" placeholder="http://localhost:8096" value="${escA(a.url&&a.url!=='#'?a.url:'')}"/></div>
    <div class="fg"><label class="fgl">Icon (emoji, or an App Store icon URL)</label><input class="fgi" id="aI" placeholder="\u{1F3AC}" maxlength="512" style="font-size:${/^https?:/.test(a.icon||'')?'12px':'20px'}" value="${escA(a.icon||'')}"/></div>
    <div class="fg"><label class="fgl">Description</label><input class="fgi" id="aD" placeholder="Media server" value="${escA(a.description||'')}"/></div>
    <div class="fg" style="border-top:1px solid var(--bd);padding-top:12px;margin-top:4px">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
        ${vCheckbox('aTun',a.tunnel_enabled)}
        <span class="fgl" style="margin:0">Route through its own Cloudflare Tunnel</span>
      </label>
      <div class="sdesc" style="margin:4px 0 8px">Void opens a dedicated tunnel for this app's local port, so it gets a
        real public link — same mechanism as the dashboard's own Remote Access tunnel, just one per app. Best for
        apps you're fine bookmarking a link that changes if the tunnel restarts (e.g. Portainer, code-server).</div>
      <div id="aTunFields" style="display:${a.tunnel_enabled?'flex':'none'};flex-direction:column;gap:10px">
        <div style="display:flex;gap:8px;align-items:flex-end">
          <div class="fg" style="flex:1;margin:0"><label class="fgl">Local port</label>
            <input class="fgi" id="aPort" type="number" min="1" max="65535" placeholder="8443" value="${a.local_port||''}"/></div>
          <select class="fgi" id="aPortPick" style="flex:1;max-width:190px">
            <option value="">Pick a running container…</option>
          </select>
        </div>
        <div id="aDomBox" data-host="${escA(a.custom_hostname||'')}" style="border-top:1px solid var(--bd);padding-top:10px">
          <div class="sdesc">Checking custom domain…</div>
        </div>
        <div style="border-top:1px solid var(--bd);padding-top:10px">
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
            ${vCheckbox('aGate',a.gate_enabled)}
            <span class="fgl" style="margin:0">Require a PIN or password before allowing tunnel access</span>
          </label>
          <div class="sdesc" style="margin:4px 0 8px">A second layer in front of ${a.tunnel_enabled?"this app's":"the app's"} own
            login — not a replacement for it. Recommended for anything with real control over your system, like
            Portainer. This is a shared secret with a cookie, not real SSO — there's no per-user identity or audit
            trail on Cloudflare's side, just a lock on the door before the app's own sign-in.</div>
          <div id="aGateFields" style="display:${a.gate_enabled?'flex':'none'};flex-direction:column;gap:8px">
            <div class="seg" id="aGateModeSeg">
              <button class="seg-btn ${a.gate_is_pin!==false?'active':''}" data-mode="pin" type="button">PIN</button>
              <button class="seg-btn ${a.gate_is_pin===false?'active':''}" data-mode="password" type="button">Password</button>
            </div>
            <div style="display:flex;gap:8px;align-items:flex-end">
              <div class="fg" style="flex:1;margin:0"><label class="fgl" id="aGatePinLabel">PIN${a.gate_enabled?' (leave blank to keep current)':''}</label>
                <input class="fgi" id="aGatePin" type="password" inputmode="numeric" placeholder="${a.gate_enabled?'••••••':'6 digits'}" maxlength="6"/></div>
              <div class="fg" style="flex:1;margin:0;max-width:150px"><label class="fgl">Remember for</label>
                <select class="fgi" id="aGateTtl">
                  <option value="1">1 hour</option>
                  <option value="12" ${!a.gate_cookie_ttl_hours||a.gate_cookie_ttl_hours==12?'selected':''}>12 hours</option>
                  <option value="168" ${a.gate_cookie_ttl_hours==168?'selected':''}>7 days</option>
                  <option value="720" ${a.gate_cookie_ttl_hours==720?'selected':''}>30 days</option>
                </select></div>
            </div>
          </div>
        </div>
      </div>
    </div>`;
}
// ── custom domain for a tile (Cloudflare named tunnel; see modules/domains.py) ──
let _domState=null; // {connected, domain} once loaded, null when not connected / not loaded
function _tileDomainValue(){
  // undefined = leave the tile's address alone (domain not connected, so the field doesn't exist)
  const el=document.getElementById('aHost');
  return el?el.value.trim():undefined;
}
async function _wireTileDomain(){
  const box=document.getElementById('aDomBox'); if(!box)return;
  const current=box.dataset.host||'';
  let s={connected:false};
  try{s=await(await api('/api/admin/domain')).json()}catch(e){}
  _domState=s.connected?s:null;
  const paint=()=>{
    if(!document.getElementById('aDomBox'))return;
    if(_domState){
      const slug=(document.getElementById('aN')?.value||'app').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'')||'app';
      box.innerHTML=`<label class="fgl">Your own address</label>
        <input class="fgi" id="aHost" placeholder="${escA(slug+'.'+_domState.domain)}" value="${escA(current)}" autocomplete="off" spellcheck="false"/>
        <div class="sdesc" style="margin:4px 0 0">Under <b>${esc(_domState.domain)}</b>. Void sets up the Cloudflare tunnel and
          DNS record for you; HTTPS comes with it. Leave blank to use a temporary link that changes when the tunnel restarts.
          <a href="#" id="aDomOff">Disconnect domain</a></div>`;
      document.getElementById('aDomOff').addEventListener('click',async ev=>{
        ev.preventDefault();
        const r=await(await api('/api/admin/domain',{method:'DELETE'})).json();
        if(r.error){errToast(r);return}
        toast('Domain disconnected','info');_domState=null;paint();
      });
      const n=document.getElementById('aN'),h=document.getElementById('aHost');
      if(n&&h&&!n._domHook){n._domHook=1;n.addEventListener('input',()=>{
        const sl=(n.value||'app').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'')||'app';
        const hh=document.getElementById('aHost');if(hh&&_domState)hh.placeholder=sl+'.'+_domState.domain;
      })}
    }else{
      box.innerHTML=`<details>
        <summary class="fgl" style="margin:0;cursor:pointer">Use your own domain instead of a temporary link</summary>
        <div class="sdesc" style="margin:6px 0 8px">Needs a domain that's on Cloudflare (free plan is fine). Create an API token at
          <a href="https://dash.cloudflare.com/profile/api-tokens" target="_blank" rel="noopener">dash.cloudflare.com/profile/api-tokens</a>
          &rarr; <i>Create Custom Token</i> with: <b>Account &rsaquo; Cloudflare Tunnel &rsaquo; Edit</b>,
          <b>Zone &rsaquo; DNS &rsaquo; Edit</b>, <b>Zone &rsaquo; Zone &rsaquo; Read</b>, limited to your domain.
          Void keeps it encrypted and never shows it again. You only do this once.</div>
        <div class="fg" style="margin:0 0 8px"><label class="fgl">Domain</label>
          <input class="fgi" id="aDomName" placeholder="example.com" autocomplete="off" spellcheck="false"/></div>
        <div class="fg" style="margin:0 0 8px"><label class="fgl">Cloudflare API token</label>
          <input class="fgi" id="aDomTok" type="password" autocomplete="new-password"/></div>
        <button class="btn btn-g" id="aDomGo" type="button">Connect domain</button>
      </details>`;
      document.getElementById('aDomGo').addEventListener('click',async ev=>{
        const btn=ev.currentTarget;btn.disabled=true;btn.textContent='Checking…';
        try{
          const r=await(await api('/api/admin/domain',{method:'PUT',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({domain:document.getElementById('aDomName').value,api_token:document.getElementById('aDomTok').value})})).json();
          if(r.error){errToast(r);btn.disabled=false;btn.textContent='Connect domain';return}
          _domState={connected:true,domain:r.domain};toast('Connected '+r.domain,'success');paint();
        }catch(e){btn.disabled=false;btn.textContent='Connect domain';toast('Could not reach the server','error')}
      });
    }
  };
  paint();
}
async function _wireTileTunnelToggle(){
  _wireTileDomain();
  const chk=document.getElementById('aTun'),fields=document.getElementById('aTunFields');
  chk.addEventListener('change',()=>{fields.style.display=chk.checked?'flex':'none'});
  const gchk=document.getElementById('aGate'),gfields=document.getElementById('aGateFields');
  gchk.addEventListener('change',()=>{gfields.style.display=gchk.checked?'flex':'none'});
  const gatePin=document.getElementById('aGatePin'),gatePinLabel=document.getElementById('aGatePinLabel');
  const gateModeSeg=document.getElementById('aGateModeSeg');
  const keepCurrent=gatePinLabel.textContent.includes('leave blank');
  function updateGateModeUI(mode){
    if(mode==='pin'){
      gatePinLabel.textContent='PIN'+(keepCurrent?' (leave blank to keep current)':'');
      gatePin.inputMode='numeric'; gatePin.maxLength=6; gatePin.placeholder=keepCurrent?'••••••':'6 digits';
    }else{
      gatePinLabel.textContent='Password'+(keepCurrent?' (leave blank to keep current)':'');
      gatePin.removeAttribute('inputmode'); gatePin.maxLength=64; gatePin.placeholder=keepCurrent?'••••••':'6+ characters';
    }
  }
  updateGateModeUI(gateModeSeg.querySelector('.seg-btn.active')?.dataset.mode||'pin');
  gateModeSeg.addEventListener('click',e=>{
    const b=e.target.closest('.seg-btn'); if(!b)return;
    gateModeSeg.querySelectorAll('.seg-btn').forEach(x=>x.classList.toggle('active',x===b));
    updateGateModeUI(b.dataset.mode);
  });
  enhanceSelect(document.getElementById('aGateTtl'));
  const pick=document.getElementById('aPortPick');
  try{
    const d=await(await api('/api/admin/docker/ports')).json();
    (d.ports||[]).forEach(p=>{
      const o=document.createElement('option');o.value=p.port;o.textContent=`${p.container} (:${p.port})`;
      pick.appendChild(o);
    });
  }catch(e){/* docker not available — picker just stays empty, manual port entry still works */}
  enhanceSelect(pick);
  pick.addEventListener('change',()=>{if(pick.value)document.getElementById('aPort').value=pick.value});
}
function showAddTile(){
  openMdl('Add App Tile',_tileModalBody(),
    `<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-p" id="savT">Add tile</button>`);
  _wireTileTunnelToggle();
  document.getElementById('savT')?.addEventListener('click',async()=>{
    const n=document.getElementById('aN').value.trim(),u=document.getElementById('aU').value.trim();
    const tun=document.getElementById('aTun').checked,port=document.getElementById('aPort').value;
    const gate=document.getElementById('aGate').checked,pin=document.getElementById('aGatePin').value;
    const ttl=document.getElementById('aGateTtl').value;
    const gateMode=document.querySelector('#aGateModeSeg .seg-btn.active')?.dataset.mode||'pin';
    if(!n||(!u&&!tun)){toast('Name and URL required','error');return}
    if(tun&&!u&&!port){toast('Tunneling needs a local port','error');return}
    if(gate&&!pin){toast(gateMode==='pin'?'A PIN is required to enable the gate':'A password is required to enable the gate','error');return}
    if(gate&&pin&&gateMode==='pin'&&!/^\d{6}$/.test(pin)){toast('PIN must be exactly 6 digits','error');return}
    const r=await api('/api/apps',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:n,url:u,icon:document.getElementById('aI').value||'\u{1F517}',
        description:document.getElementById('aD').value,tunnel_enabled:tun,local_port:port?Number(port):undefined,
        custom_hostname:tun?_tileDomainValue():undefined,
        gate_enabled:gate,gate_pin:pin||undefined,gate_cookie_ttl_hours:Number(ttl),gate_mode:gate?gateMode:undefined})});
    const d=await r.json();
    if(d.error){errToast(d);return}
    toast(d.tunnel_error?`Tile added — ${d.tunnel_error}`:'Tile added','success');
    closeMdl();loadTiles();
  });
}
async function editTile(id){
  const apps=await(await api('/api/apps')).json();
  const a=apps.find(x=>x.id===id);
  if(!a){toast('Tile not found','error');return}
  openMdl('Edit App Tile',_tileModalBody(a),
    `<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-p" id="savT">Save</button>`);
  _wireTileTunnelToggle();
  document.getElementById('savT')?.addEventListener('click',async()=>{
    const n=document.getElementById('aN').value.trim(),u=document.getElementById('aU').value.trim();
    const tun=document.getElementById('aTun').checked,port=document.getElementById('aPort').value;
    const gate=document.getElementById('aGate').checked,pin=document.getElementById('aGatePin').value;
    const ttl=document.getElementById('aGateTtl').value;
    const gateMode=document.querySelector('#aGateModeSeg .seg-btn.active')?.dataset.mode||'pin';
    if(!n||(!u&&!tun)){toast('Name and URL required','error');return}
    if(tun&&!u&&!port){toast('Tunneling needs a local port','error');return}
    if(gate&&!pin&&!a.gate_enabled){toast(gateMode==='pin'?'A PIN is required to enable the gate':'A password is required to enable the gate','error');return}
    if(gate&&pin&&gateMode==='pin'&&!/^\d{6}$/.test(pin)){toast('PIN must be exactly 6 digits','error');return}
    const r=await api(`/api/apps/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:n,url:u,icon:document.getElementById('aI').value||'\u{1F517}',
        description:document.getElementById('aD').value,tunnel_enabled:tun,local_port:port?Number(port):undefined,
        custom_hostname:tun?_tileDomainValue():undefined,
        gate_enabled:gate,gate_pin:pin||undefined,gate_cookie_ttl_hours:Number(ttl),gate_mode:gate?gateMode:undefined})});
    const d=await r.json();
    if(d.error){errToast(d);return}
    toast(d.tunnel_error?`Saved — ${d.tunnel_error}`:'Tile saved','success');
    closeMdl();loadTiles();
  });
}
async function delTile(id,ev){
  confirmDlg('Remove this app tile?',async()=>{await api(`/api/apps/${id}`,{method:'DELETE'});toast('Removed','info');loadTiles()},'Remove',ev?.currentTarget);
}
window.showAddTile=showAddTile;window.delTile=delTile;window.editTile=editTile;

// ── CONTAINERS ──────────────────────────────────────────────────
async function rCtrs(){
  const gen=++_renderGen; // self-bumped: a manual Refresh click while a previous rCtrs fetch is still
                          // in flight must supersede it too, not just a cross-view navigation via go()
  const root=document.getElementById('vroot');
  root.innerHTML=`<div class="vh">
    <div><div class="vtitle">Containers</div><div class="vsub">${DOKR?'Docker':'Docker not connected'}</div></div>
    <button class="tb" onclick="rCtrs()"><svg viewBox="0 0 24 24"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 102.13-9.36L1 10"/></svg> Refresh</button>
  </div>
  ${!DOKR?`<div style="background:rgba(var(--yw-rgb),.07);border:1px solid rgba(var(--yw-rgb),.18);border-radius:var(--r);padding:16px 18px;color:var(--yw)">
    <div style="font-weight:700;margin-bottom:8px">\u{1F433} Docker not detected</div>
    <div style="font-family:var(--f-mono);font-size:12px;color:var(--yw);margin-bottom:10px;white-space:pre-wrap">${esc(DOCKERR||'Unknown error')}</div>
    <div style="font-size:12px">Linux/Pi: <code>sudo usermod -aG docker $USER</code> \u00b7 Windows/Mac: open Docker Desktop</div></div>`
    :'<div class="loading"><div class="loader"></div><span>Loading\u2026</span></div>'}`;
  if(!DOKR)return;
  try{
    const ctrs=await(await api('/api/containers')).json();
    if(gen!==_renderGen)return; // a newer view/refresh superseded this one — drop the stale result
    document.querySelector('.loading')?.remove();
    if(ctrs.error){root.innerHTML+=`<div style="color:var(--rd);padding:16px">${esc(ctrs.error)}</div>`;return}
    if(!ctrs.length){root.innerHTML+=`<div class="empty-dir" style="padding:52px">No containers found.</div>`;return}
    const g=document.createElement('div');g.className='cgrid';g.innerHTML=ctrs.map(cCard).join('');root.appendChild(g);
    g.querySelectorAll('.act[data-a]').forEach(b=>b.addEventListener('click',()=>cAct(b.dataset.id,b.dataset.a,b.dataset.n)));
    g.querySelectorAll('.act.log').forEach(b=>b.addEventListener('click',()=>showLogs(b.dataset.id,b.dataset.n)));
    g.querySelectorAll('.act.why').forEach(b=>b.addEventListener('click',()=>showWhy(b.dataset.id,b.dataset.n)));
    g.querySelectorAll('.act.rm').forEach(b=>b.addEventListener('click',()=>showCtrRemoveDlg(b.dataset.id,b.dataset.n,b.dataset.img)));
    g.querySelectorAll('.act.rebuild').forEach(b=>b.addEventListener('click',()=>ctrRebuild(b.dataset.id,b.dataset.n,b)));
  }catch(e){if(gen!==_renderGen)return;root.innerHTML+=`<div style="color:var(--rd);padding:16px">${esc(e.message)}</div>`}
}
function cCard(c){
  const run=c.status==='running';
  const bc={'running':'b-running','exited':'b-exited','stopped':'b-stopped','paused':'b-paused','restarting':'b-restarting','created':'b-created'}[c.status]||'b-exited';
  const ports=Object.entries(c.ports||{}).map(([k,v])=>`${v.join(',')}${k.split('/')[0]}`).join(' \u00b7 ')||'No ports';
  return `<div class="card ccard ch"><div class="ctop"><div class="cname" title="${escA(c.name)}">${esc(c.name)}</div><span class="badge ${bc}">${esc(c.status)}</span></div>
    <div class="cimg">\u{1F5BC} ${esc(c.image)}</div><div class="cports">\u{1F50C} ${esc(ports)}</div>
    <div class="cacts">${run?`<button class="act" data-a="stop" data-id="${c.id}" data-n="${escA(c.name)}">\u25a0 Stop</button>`
      :`<button class="act" data-a="start" data-id="${c.id}" data-n="${escA(c.name)}">\u25b6 Start</button>`}
    <button class="act" data-a="restart" data-id="${c.id}" data-n="${escA(c.name)}">\u21ba Restart</button>
    <button class="act log" data-id="${c.id}" data-n="${escA(c.name)}">\u{1F4CB} Logs</button>
    ${(c.status==='restarting'||(c.status!=='running'&&c.exit_code)||c.restarts>=3)?`<button class="act why" data-id="${c.id}" data-n="${escA(c.name)}" style="color:var(--yw)">\u26a0 Why?</button>`:''}
    ${c.compose?`<button class="act rebuild" data-id="${c.id}" data-n="${escA(c.name)}" title="Re-create this container from its compose file (after you edit it)">\u{1F528} Rebuild</button>`:''}
    <button class="act rm" data-id="${c.id}" data-n="${escA(c.name)}" data-img="${escA(c.image)}">\u{1F5D1} Remove</button></div></div>`;
}
// Rebuild: `docker compose up -d --build --force-recreate` for this container's service, so an edited
// compose file takes effect. The data (volumes, bind-mounted folders) is kept. Runs in the background.
function ctrRebuild(id,name,btn){
  confirmDlg(`Rebuild "${esc(name)}"? It is re-created from its compose file, so any change you made to the file takes effect. Its data is kept; the app is briefly offline.`,async()=>{
    try{
      const r=await api(`/api/containers/${id}/rebuild`,{method:'POST'});const d=await r.json();
      if(!r.ok){errToast(d,'Could not rebuild');return}
      toast(`Rebuilding ${name}\u2026`,'info');
      for(let i=0;i<400;i++){
        await new Promise(res=>setTimeout(res,3000));
        let s;try{s=await(await api('/api/containers/rebuild/'+encodeURIComponent(d.name))).json()}catch(e){continue}
        if(s.done){
          if(s.ok)toast(`${name} was rebuilt`,'success');
          else openMdl('Rebuild failed',`<div class="sdesc" style="margin-bottom:8px">Docker Compose reported a problem:</div><pre class="fv-rawjson" style="max-height:50vh;overflow:auto;background:var(--deep);border-radius:8px">${esc(s.log||'No output')}</pre>`,'<button class="btn btn-g" onclick="closeMdl()">Close</button>',true);
          if(curV==='containers')rCtrs();
          return;
        }
      }
      toast('Still rebuilding \u2014 check the Containers page in a moment','info');
    }catch(e){toast('Could not rebuild: '+e.message,'error')}
  },'Rebuild',btn);
}
async function cAct(id,act,name){
  toast(`${act[0].toUpperCase()+act.slice(1)}ing ${name}\u2026`,'info');
  try{const d=await(await api(`/api/containers/${id}/action`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:act})})).json();
    if(d.ok){toast(`${name}: ${d.status}`,'success');setTimeout(rCtrs,900)}else errToast(d,'Failed')}
  catch(e){toast(e.message,'error')}
}
async function showWhy(id,name){
  openMdl(`Why is ${esc(name)} having trouble?`,`<div class="loading"><div class="loader"></div><span>Looking at its exit code and logs\u2026</span></div>`,'',true);
  let d;
  try{d=await(await api(`/api/containers/${id}/diagnose`)).json()}
  catch(e){document.getElementById('mdlB').innerHTML=`<div style="color:var(--rd)">${esc(e.message)}</div>`;return}
  if(d.error){document.getElementById('mdlB').innerHTML=`<div style="color:var(--rd)">${esc(d.error)}</div>`;return}
  const h=d.host||{};
  const facts=[d.status?`Status: ${esc(d.status)}`:'',d.restart_count?`Restarted ${d.restart_count} time${d.restart_count===1?'':'s'}`:'',
    d.exit_code!=null&&d.exit_code!==0?`Last exit code ${d.exit_code}`:'',d.oom_killed?'Killed for memory':''].filter(Boolean).join(' \u00b7 ');
  const hostLine=[h.model||h.machine,h.ram_mb?`${(h.ram_mb/1024).toFixed(1)} GB RAM`:'',h.page_size?`${h.page_size/1024} KB pages`:'',
    h.docker_arch?`Docker: ${h.docker_arch}`:''].filter(Boolean).join(' \u00b7 ');
  const colour=d.severity==='info'?'var(--td)':d.severity==='warn'?'var(--yw)':'var(--rd)';
  document.getElementById('mdlB').innerHTML=`
    <div style="font-size:15px;font-weight:700;color:${colour}">${esc(d.title)}</div>
    <div class="sdesc" style="margin-top:6px">${esc(d.cause)}</div>
    ${(d.fix||[]).length?`<div style="margin-top:12px;font-weight:600;font-size:13px">What to do</div>
      <ol style="margin:6px 0 0 18px;font-size:13px;line-height:1.55">${d.fix.map(f=>`<li>${esc(f).replace(/`([^`]+)`/g,'<code>$1</code>')}</li>`).join('')}</ol>`:''}
    ${(d.evidence||[]).length?`<div style="margin-top:12px;font-weight:600;font-size:13px">What it printed</div>
      <div class="logbox" style="max-height:120px;margin-top:6px">${d.evidence.map(esc).join('\n')}</div>`:''}
    <div class="sdesc" style="margin-top:12px;font-size:12px">${esc(facts)}${hostLine?`<br>${esc(hostLine)}`:''}</div>
    ${d.log_tail?`<details style="margin-top:10px"><summary style="cursor:pointer;font-size:12px;color:var(--td)">Last log lines</summary>
      <div class="logbox" style="max-height:200px;margin-top:6px">${esc(d.log_tail)}</div></details>`:''}`;
}
async function showLogs(id,name){
  openMdl(`Logs \u2014 ${name}`,`<div class="loading"><div class="loader"></div></div>`,'',true);
  try{const d=await(await api(`/api/containers/${id}/logs`)).json();
    document.getElementById('mdlB').innerHTML=`<div class="logbox">${esc(d.logs||d.error||'No output')}</div>`;
    const l=document.querySelector('.logbox');if(l)l.scrollTop=l.scrollHeight}
  catch(e){document.getElementById('mdlB').innerHTML=`<div style="color:var(--rd)">${esc(e.message)}</div>`}
}
function showCtrRemoveDlg(id,name,image){
  openMdl(`Remove ${esc(name)}`,`
    <div class="sdesc">This stops (if running) and permanently removes this container. It can be recreated later from the same image, unless you also remove the image below.</div>
    <label style="display:flex;align-items:center;gap:8px;margin-top:14px;padding:10px;border:1px solid var(--rd);border-radius:8px;cursor:pointer">
      ${vCheckbox('ctrRemoveImage',false,'',true)}
      <span style="font-size:13px;color:var(--rd)">Also delete the image (${esc(image)}) \u2014 you'll need to pull or rebuild it to run this again</span>
    </label>`,
    `<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-d" id="ctrRemoveGoBtn">Remove</button>`);
  document.getElementById('ctrRemoveGoBtn')?.addEventListener('click',async()=>{
    const removeImage=document.getElementById('ctrRemoveImage').checked;
    closeMdl();
    toast(`Removing ${name}\u2026`,'info');
    try{
      const d=await(await api(`/api/containers/${id}/delete`,{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({remove_image:removeImage})})).json();
      if(d.error){errToast(d);return}
      if(removeImage&&!d.image_removed)toast(`${name} removed \u2014 image kept (${d.image_error||'still in use elsewhere'})`,'info');
      else toast(removeImage?`${name} and its image removed`:`${name} removed`,'success');
      rCtrs();
    }catch(e){toast(e.message,'error')}
  });
}
window.rCtrs=rCtrs;

// ── FILES ───────────────────────────────────────────────────────
let curP='',selF=new Set(),clip=null,dragIs=[],dragCp=false,ctxEl=null;
let marquee=null,marqueeStart=null,marqueeBaseSel=null;
const ficons={dir:'\u{1F4C1}',text:'\u{1F4C4}',image:'\u{1F5BC}\uFE0F',video:'\u{1F3AC}',audio:'\u{1F3B5}',archive:'\u{1F4E6}',pdf:'\u{1F4CB}',binary:'\u2699\uFE0F'};

// Your own folder (Users/<you>) is shown as "My Files"; everything else keeps its
// real name, which is already the friendly one (Shared, Users, Drives).
function _friendlyCrumbs(bc){
  const me=(typeof UNAME!=='undefined'?UNAME:'');
  const mine=FS_USERS+'/'+me;
  const i=bc.findIndex(b=>b.path===mine);
  // Inside your own folder the trail starts at "My Files" (not Server files > Users > you).
  if(i>=0&&FULL_FILES){
    const out=bc.slice(i).map((b,k)=>({name:k===0?'My Files':b.name,path:b.path}));
    out.rooted=true;return out;
  }
  return bc.map(b=>({name:b.path===mine?'My Files':b.name,path:b.path}));
}
let _places=null;                       // what the sidebar offers this account (/api/files/places)
function _tfCtx(){return{curP:curP,me:(_places&&_places.me)?_places.me.path:null}}   // for the transfers panel, which lives outside this block
async function loadPlaces(force){
  if(_places&&!force)return _places;
  try{_places=await apiData('/api/files/places')}catch(e){_places=null}
  return _places;
}
// Opening Files lands you in your own folder, like a home directory.
async function rFilesHome(){
  const pending=_pendingFilesPath;_pendingFilesPath=null;
  const pl=await loadPlaces();
  rFiles(pending!=null?pending:(pl&&pl.me?pl.me.path:''));
}
async function rFiles(path){
  const gen=++_renderGen; // self-bumped: navigating into a subfolder is a new "request" that must
                          // supersede a still-in-flight fetch for the previous folder, same view or not
  curP=path;selF.clear();
  const root=document.getElementById('vroot');
  // Moving between folders: leave what is on screen, dimmed, instead of flashing an empty page.
  const prevMain=root.querySelector('.files-shell .files-main');
  if(prevMain)prevMain.classList.add('fs-loading');
  else root.innerHTML=`<div class="loading"><div class="loader"></div></div>`;
  try{
    const d=await(await api('/api/files?path='+encodeURIComponent(path))).json();
    if(gen!==_renderGen)return; // a newer folder request or view switch superseded this one
    if(d.error){root.innerHTML=`<div style="color:var(--rd);padding:20px">${esc(d.error)}</div>`;return}
    if(d.virtual_root){renderVirtualRoot(d);return}
    const bc=d.breadcrumb||[];
    const hasUp=path!=='';
    const parentPath=bc.length>1?bc[bc.length-2].path:bc.length===1?'':'';
    const crumbs=_friendlyCrumbs(bc);
    const here=crumbs.length?crumbs[crumbs.length-1].name:'Files';
    const nItems=(d.entries||d.items||[]).length;
    root.innerHTML=`
    <div class="vh"><div><h1 class="vtitle">${esc(here)}</h1>
      <div class="vsub">${nItems===0?'Empty folder':nItems===1?'1 item':nItems+' items'}</div></div></div>
    <div class="files-shell">
      <div class="files-sidebar" id="filesSidebar">
        <div id="fsPlaces">${_placesHtml(_places)}</div>
        <div id="fsCloudSection"></div>
        <div id="fsDrives">${_drivesHtml(_places)}</div>
        <div class="fs-usage" id="fsUsage"></div>
      </div>
      <div class="files-main">
    <div class="bcrumb" id="bcrumbEl">
      ${crumbs.rooted?'':`<span class="bc ${!crumbs.length?'cur':''}" data-path="">${FULL_FILES?'Server files':'Files'}</span>`}
      ${crumbs.map((b,i)=>`${(crumbs.rooted&&i===0)?'':'<span class="bc-sep" aria-hidden="true">\u203A</span>'}
        <span class="bc ${i===crumbs.length-1?'cur':''}" data-path="${escA(b.path)}">${esc(b.name)}</span>`).join('')}
    </div>
    <div class="ftbar">
      ${hasUp?`<button class="tb" id="upBtn" data-path="${escA(parentPath)}"><svg viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg> Up</button>`:''}
      <button class="tb" id="mkdBtn"><svg viewBox="0 0 24 24"><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/><line x1="12" y1="11" x2="12" y2="17"/><line x1="9" y1="14" x2="15" y2="14"/></svg> New folder</button>
      <button class="tb" id="ulBtn"><svg viewBox="0 0 24 24"><polyline points="16 16 12 12 8 16"/><line x1="12" y1="12" x2="12" y2="21"/><path d="M20.39 18.39A5 5 0 0018 9h-1.26A8 8 0 103 16.3"/></svg> Upload</button>
      ${clip?`<button class="tb" id="pasteBtn"><svg viewBox="0 0 24 24"><path d="M16 4h2a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/></svg> Paste ${clip.paths.length}</button>`:''}
      <button class="tb" id="trashBtn"><svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg> Trash</button>
      <div class="view-toggle" id="viewToggle">
        <button data-mode="grid" title="Grid view"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg></button>
        <button data-mode="list" title="List view"><svg viewBox="0 0 24 24"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg></button>
      </div>
      <div style="margin-left:8px;position:relative;display:flex;align-items:center">
        <svg viewBox="0 0 24 24" width="14" height="14" style="position:absolute;left:9px;pointer-events:none;stroke:var(--td);fill:none;stroke-width:2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <input type="text" id="fSearchInp" placeholder="Search files &amp; content\u2026" spellcheck="false"
          style="padding:6px 10px 6px 28px;border-radius:var(--rs);border:1px solid var(--bd);background:var(--sf);color:var(--tx);font-size:12.5px;width:220px"/>
      </div>
      <input type="file" id="ulInp" multiple style="display:none"/>
    </div>
    <div class="sel-bar" id="selBar">
      <span id="selCnt" style="font-size:12px;color:var(--td)"></span>
      <button class="tb" id="copySelBtn"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg> Copy</button>
      <button class="tb" id="cutSelBtn"><svg viewBox="0 0 24 24"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><line x1="20" y1="4" x2="8.12" y2="15.88"/><line x1="14.47" y1="14.48" x2="20" y2="20"/><line x1="8.12" y1="8.12" x2="12" y2="12"/></svg> Cut</button>
      <button class="tb" id="dlSelBtn"><svg viewBox="0 0 24 24"><polyline points="8 17 12 21 16 17"/><line x1="12" y1="12" x2="12" y2="21"/><path d="M20.88 18.09A5 5 0 0018 9h-1.26A8 8 0 103 16.3"/></svg> Download</button>
      <button class="tb danger" id="delSelBtn"><svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg> Delete</button>
      <span style="width:1px;align-self:stretch;background:var(--bd2)"></span>
      <button class="tb" onclick="clearSel()">\u2715 Deselect</button>
    </div>
    <div class="files-area" id="filesArea">
      <div class="drop-overlay" id="dropOvr">Drop files to upload</div>
      <div class="fgrid" id="fgrid">
        ${d.entries.length?d.entries.map((e,i)=>fchip(e,d.entries.length<=400?i:null)).join(''):'<div class="empty-dir">This folder is empty</div>'}
      </div>
    </div>
    </div></div>`;
    initFilesSidebar(path);
    const fgridEl=document.getElementById('fgrid');
    wireGrid(fgridEl);
    if(fgridEl&&fgridEl.querySelector('.fitem[style]')){
      fgridEl.classList.add('nav');setTimeout(()=>fgridEl.classList.remove('nav'),900);
    }
    if(_pendingHighlight){
      const hn=_pendingHighlight;_pendingHighlight=null;
      const hel=[...document.querySelectorAll('.fitem')].find(x=>x.dataset.name===hn);
      if(hel){hel.scrollIntoView({block:'center'});hel.classList.add('flash');setTimeout(()=>hel.classList.remove('flash'),1800)}
    }
    document.getElementById('mkdBtn')?.addEventListener('click',()=>mkdirDlg(path));    const ulBtn=document.getElementById('ulBtn'),ulInp=document.getElementById('ulInp');
    ulBtn.addEventListener('click',()=>ulInp.click());
    ulInp.addEventListener('change',e=>doUpload(e.target.files,path));
    document.getElementById('pasteBtn')?.addEventListener('click',()=>doPaste(path));
    document.getElementById('trashBtn')?.addEventListener('click',()=>showTrash());
    applyFilesView();
    document.querySelectorAll('#viewToggle button').forEach(b=>b.addEventListener('click',()=>setFilesView(b.dataset.mode)));
    const searchInp=document.getElementById('fSearchInp');
    let searchTimer=null;
    searchInp.addEventListener('input',()=>{
      clearTimeout(searchTimer);
      const q=searchInp.value.trim();
      if(!q){rFiles(path);return}
      searchTimer=setTimeout(()=>doFileSearch(q,path),320);
    });
    searchInp.addEventListener('keydown',e=>{if(e.key==='Escape'){clearTimeout(searchTimer);searchInp.value='';rFiles(path)}});
    document.getElementById('copySelBtn')?.addEventListener('click',()=>toClip('copy'));
    document.getElementById('cutSelBtn')?.addEventListener('click',()=>toClip('cut'));
    document.getElementById('dlSelBtn')?.addEventListener('click',()=>downloadSel());
    document.getElementById('delSelBtn')?.addEventListener('click',e=>deleteSel(null,e.currentTarget));
    const area=document.getElementById('filesArea'),ovl=document.getElementById('dropOvr');
    area.addEventListener('dragover',e=>{if(e.dataTransfer.types.includes('Files')){e.preventDefault();ovl.classList.add('show')}});
    area.addEventListener('dragleave',e=>{if(!area.contains(e.relatedTarget))ovl.classList.remove('show')});
    area.addEventListener('drop',e=>{e.preventDefault();ovl.classList.remove('show');if(e.dataTransfer.files.length)doUpload(e.dataTransfer.files,path)});
    document.getElementById('bcrumbEl')?.addEventListener('click',e=>{
      const el=e.target.closest('.bc');
      if(el)rFiles(el.dataset.path);
    });
    document.getElementById('upBtn')?.addEventListener('click',function(){rFiles(this.dataset.path)});
    document.getElementById('fgrid')?.addEventListener('click',e=>{
      if(marqueeMoved){marqueeMoved=false;return}
      if(e.target.id==='fgrid'||e.target.classList.contains('empty-dir'))clearSel();
    });
  }catch(e){if(gen!==_renderGen)return;root.innerHTML=`<div style="color:var(--rd);padding:20px">Error: ${esc(e.message)}</div>`}
}
function renderVirtualRoot(d){
  const root=document.getElementById('vroot');
  root.innerHTML=`<div class="vh"><div><div class="vtitle">Files</div><div class="vsub">Your accessible folders</div></div></div>
  <div class="home-cards" id="vrootCards">
    ${d.entries.map(e=>`
    <div class="card home-card ch" data-path="${escA(e.path)}">
      <div class="home-card-icon">${e.name==='Shared'?'\u{1F465}':'\u{1F4C2}'}</div>
      <div class="home-card-name">${esc(e.name)}</div>
      <div class="home-card-desc">${e.name==='Shared'?'Accessible by all users':'Your private folder'}</div>
    </div>`).join('')}
  </div>`;
  document.getElementById('vrootCards')?.addEventListener('click',e=>{
    const el=e.target.closest('.home-card');
    if(el)rFiles(el.dataset.path);
  });
}
// Per-extension file icon. Falls back to the old generic category emoji
// (via .ficon's onerror swap) whenever the icon route 404s — a genuinely
// unsupported extension, a cold cache miss with no network reachable from
// the server, or anything else. Never a visibly broken image, never a
// blocked render. `size` lets callers ask for a smaller icon (e.g. inline
// search results) without a second copy of this function.
function fileIconHtml(e,size){
  const sz=size||40;
  if(e.type==='dir')return `<div class="ficon" style="font-size:${sz}px;flex-shrink:0">\u{1F4C1}</div>`;
  const ext=(e.ext||'').toLowerCase();
  const fallback=ficons[e.type]||'\u{1F4C4}';
  if(!ext)return `<div class="ficon" style="font-size:${sz}px;flex-shrink:0">${fallback}</div>`; // no extension at
                                                                                    // all — skip the fetch entirely
  return `<div class="ficon fic-wrap" style="width:${sz}px;height:${sz}px;flex-shrink:0">
    <img class="fic-img" src="/api/file-icon/${encodeURIComponent(ext)}"
         alt="${escA(ext)} file icon"
         onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'ficon',textContent:'${fallback}',style:'font-size:${sz}px;flex-shrink:0'}))"/>
  </div>`;
}
function applyFilesView(){
  const mode=(()=>{try{return localStorage.getItem('void_files_view')||'grid'}catch(e){return 'grid'}})();
  const grid=document.getElementById('fgrid');
  if(grid)grid.classList.toggle('list-view',mode==='list');
  document.querySelectorAll('#viewToggle button').forEach(b=>b.classList.toggle('active',b.dataset.mode===mode));
}
function setFilesView(mode){
  try{localStorage.setItem('void_files_view',mode)}catch(e){}
  const grid=document.getElementById('fgrid');
  const reduce=window.matchMedia&&matchMedia('(prefers-reduced-motion: reduce)').matches;
  if(!reduce&&grid&&document.startViewTransition){
    document.startViewTransition(()=>applyFilesView());   // the browser cross-fades old and new layout
  }else{
    applyFilesView();
    if(grid&&!reduce){grid.classList.remove('view-swap');void grid.offsetWidth;grid.classList.add('view-swap');
      setTimeout(()=>grid.classList.remove('view-swap'),320)}
  }
}
const _FS_ICO={
  home:'<svg viewBox="0 0 24 24"><path d="M3 11l9-8 9 8"/><path d="M5 10v10h5v-6h4v6h5V10"/></svg>',
  folder:'<svg viewBox="0 0 24 24"><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/></svg>',
  people:'<svg viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>',
  drive:'<svg viewBox="0 0 24 24"><rect x="2" y="13" width="20" height="8" rx="2"/><path d="M5 13l2-8h10l2 8"/><circle cx="6.5" cy="17" r=".8"/></svg>',
  server:'<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="6" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/><circle cx="7" cy="7" r=".8"/><circle cx="7" cy="17" r=".8"/></svg>',
  trash:'<svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>',
};
function _placeItem(path,icon,label,cls,extra){
  return `<div class="fs-item ${cls||''}" data-fs-path="${escA(path)}">${_FS_ICO[icon]}<span class="fs-lbl">${esc(label)}</span>${extra||''}</div>`;
}
// Admins and the Super User can leave a drive out of the sidebar for everyone (it stays mounted; this only tidies the list).
function _driveHide(d,canManage){
  if(!canManage)return '';
  return `<button class="fs-hide" type="button" data-name="${escA(d.name)}" title="Hide from the sidebar for everyone (it stays mounted)" aria-label="Hide ${escA(d.name)} from the sidebar">
    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17.9 17.9A10.6 10.6 0 0112 20C5 20 1 12 1 12a18.5 18.5 0 015.1-5.9M9.9 4.2A9.1 9.1 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.2 3.2M14.1 14.1a3 3 0 11-4.2-4.2"/><line x1="1" y1="1" x2="23" y2="23"/></svg></button>`;
}
async function setHiddenDrives(list){
  const r=await api('/api/storage/drives/visibility',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({hidden:list})});
  const d=await r.json();
  if(!r.ok||d.error){errToast(d,'Couldn\u2019t save that');return false}
  return true;
}
async function repaintPlaces(){
  const box=document.getElementById('fsPlaces'),dbox=document.getElementById('fsDrives');
  const pl=await loadPlaces(true);
  if(box&&pl){box.innerHTML=_placesHtml(pl);if(dbox)dbox.innerHTML=_drivesHtml(pl);wireDriveButtons(document.getElementById('filesSidebar'))}
}
function wireDriveButtons(root){
  const r=root||document;
  r.querySelectorAll('.fs-hide').forEach(b=>{
    b.onclick=async e=>{
      e.stopPropagation();e.preventDefault();
      const pl=await loadPlaces();
      const cur=((pl&&pl.drives&&pl.drives.hidden)||[]);
      if(await setHiddenDrives([...cur,b.dataset.name])){toast('Hidden from the sidebar for everyone. The drive is still mounted.','success');await repaintPlaces()}
    };
  });
  r.querySelectorAll('[data-unhide]').forEach(a=>{
    a.onclick=async e=>{
      e.preventDefault();
      const pl=await loadPlaces();
      const names=((pl&&pl.drives&&pl.drives.hidden)||[]);
      const which=a.dataset.unhide;
      if(await setHiddenDrives(which==='all'||!which?[]:names.filter(n=>n!==which)))await repaintPlaces();
    };
  });
  r.querySelectorAll('.fs-item[data-fs-path]').forEach(el=>{
    if(!el.onclick)el.onclick=()=>rFiles(el.dataset.fsPath);
  });
}
// The sidebar, in the order a person looks for things: yours, shared, then the admin views.
function _placesHtml(pl){
  if(!pl)return `<div class="fs-title">Locations</div>
    ${_placeItem(FS_SHARED,'people','Shared')}`;
  let h=`<div class="fs-title">Locations</div>`;
  h+=_placeItem(pl.me.path,'home','My Files');
  (pl.me.folders||[]).forEach(f=>{h+=_placeItem(f.path,'folder',f.name,'fs-sub')});
  h+=_placeItem(pl.shared.path,'people','Shared');
  if(pl.full){
    h+=_placeItem(pl.users.path,'people','Users');
    h+=`<div class="fs-title" style="margin-top:10px">Advanced</div>`;
    h+=_placeItem(pl.root.path,'server','Server files');
    if(pl.app_data)h+=_placeItem(pl.app_data.path,'server','App data');   // Super User only
  }
  return h;
}
// Connected drives sit at the very bottom of the sidebar, below Advanced and Cloud.
function _drivesHtml(pl){
  if(!pl||!pl.full)return '';
  const dr=pl.drives.items||[],hid=pl.drives.hidden||[],canManage=!!pl.drives.can_manage;
  let h=`<div class="fs-title" style="margin-top:10px">Drives</div>`;
  h+=dr.length?dr.map(d=>_placeItem(d.path,'drive',d.name,'',_driveHide(d,canManage))).join('')
    :(hid.length?`<div class="fs-hint">All drives are hidden.</div>`
      :`<div class="fs-hint">No drives connected.<br><a href="#" data-go="settings">Add a drive or network share</a></div>`);
  if(hid.length&&canManage)h+=`<div class="fs-hint"><a href="#" data-unhide="all">Show ${hid.length} hidden drive${hid.length===1?'':'s'}</a></div>`;
  return h;
}
async function initFilesSidebar(path){
  const sb=document.getElementById('filesSidebar');if(!sb)return;
  const wirePlaces=()=>{
    // The most specific place that contains this folder is the active one (My Files, not also Users).
    const items=[...sb.querySelectorAll('.fs-item[data-fs-path]')];
    let best=null;
    items.forEach(el=>{
      const fp=el.dataset.fsPath;
      if(path===fp||(fp&&path.startsWith(fp+'/'))){if(!best||fp.length>best.dataset.fsPath.length)best=el}
    });
    items.forEach(el=>{el.classList.toggle('active',el===best);el.onclick=()=>rFiles(el.dataset.fsPath)});
    wireDriveButtons(sb);
    sb.querySelectorAll('[data-go]').forEach(a=>a.addEventListener('click',e=>{e.preventDefault();go(a.dataset.go)}));
  };
  wirePlaces();
  loadPlaces(true).then(pl=>{
    const box=document.getElementById('fsPlaces'),dbox=document.getElementById('fsDrives');
    if(box&&pl){box.innerHTML=_placesHtml(pl);if(dbox)dbox.innerHTML=_drivesHtml(pl);wirePlaces()}
  });
  const cloudEl=document.getElementById('fsCloudSection');
  if(cloudEl)try{
    const providers=await apiData('/api/cloud/providers');
    const configured=providers.filter(p=>p.configured);
    cloudEl.innerHTML=configured.length?`<div class="fs-title" style="margin-top:10px">Cloud</div>`+configured.map(p=>
      `<div class="fs-item" data-cloud="${p.id}"><svg viewBox="0 0 24 24"><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/></svg>${esc(p.label)}${p.connected?'':' <span style="opacity:.55;font-size:11px;margin-left:auto">Connect</span>'}</div>`
    ).join(''):'';
    // A synced account opens its folder in My Files; the import browser lives in Settings > Storage & cloud.
    const synced={};
    await Promise.all(configured.filter(p=>p.connected).map(async p=>{
      try{const pairs=await apiData(`/api/cloud/${p.id}/sync`);const w=pairs.find(x=>x.entire&&x.enabled);if(w)synced[p.id]=w.local_path}catch(e){}
    }));
    cloudEl.querySelectorAll('[data-cloud]').forEach(el=>{
      const p=configured.find(x=>x.id===el.dataset.cloud);
      el.onclick=()=>synced[p.id]?rFiles(synced[p.id]):p.connected?go('settings'):connectCloud(p.id);
      if(synced[p.id]){el.dataset.syncPath=synced[p.id];if(path===synced[p.id]||path.startsWith(synced[p.id]+'/'))el.classList.add('active')}
    });
    cloudSyncPaint();cloudSyncPoll();
  }catch(e){}
  const usage=document.getElementById('fsUsage');
  if(usage)try{
    const s=await(await api('/api/stats')).json();
    const disk=(s.disks||[])[0];
    if(disk){
      usage.innerHTML=`<div class="fs-usage-txt">${fmtB(disk.used)} of ${fmtB(disk.total)} used</div>
        <div class="fs-usage-bar"><div class="fs-usage-fill" style="width:${disk.percent}%"></div></div>
        <div class="fs-usage-txt">${disk.percent}% full</div>`;
    }
  }catch(e){}
}
function fchip(e,i){
  return `<div class="fitem${e.type==='dir'&&CLOUD_SYNCING.has(e.path)?' syncing':''}" data-path="${escA(e.path)}" data-type="${e.type}" data-name="${escA(e.name)}"${e.protected?' data-prot="1"':' draggable="true"'}${i!=null?` style="--i:${Math.min(i,26)}"`:''}>
    ${fileIconHtml(e)}
    <div class="fname" title="${escA(e.name)}">${esc(e.name)}</div>
    ${e.size!=null?`<div class="fsize">${fmtB(e.size)}</div>`:'<div class="fsize"></div>'}
    <div class="fmod">${e.modified?new Date(e.modified*1000).toLocaleString([],{dateStyle:'medium',timeStyle:'short'}):''}</div>
  </div>`;
}
// One set of listeners for the whole grid (not six per file): a folder with thousands of entries renders
// and responds as fast as one with ten, and entries added later (an upload landing) need no wiring.
function wireGrid(grid){
  if(!grid||grid._wired)return;
  grid._wired=true;
  const itemOf=e=>{const el=e.target.closest&&e.target.closest('.fitem');return el&&grid.contains(el)?el:null};
  grid.addEventListener('click',e=>{
    const el=itemOf(e);if(!el)return;
    const path=el.dataset.path,type=el.dataset.type,name=el.dataset.name;
    if(e.ctrlKey||e.metaKey){e.preventDefault();e.stopPropagation();toggleSel(el,path)}
    else if(e.shiftKey){e.preventDefault();e.stopPropagation();rangeSel(el,path)}
    else{clearSel();if(type==='dir')rFiles(path);else viewFile(path,name)}
  });
  grid.addEventListener('contextmenu',e=>{
    const el=itemOf(e);if(!el)return;
    const path=el.dataset.path;
    e.preventDefault();
    if(!selF.has(path)){clearSel();toggleSel(el,path)}
    showCtx(e.clientX,e.clientY,{path,type:el.dataset.type,name:el.dataset.name,prot:!!el.dataset.prot});
  });
  grid.addEventListener('dragstart',e=>{
    const el=itemOf(e);if(!el)return;
    const path=el.dataset.path;
    dragCp=e.shiftKey;dragIs=selF.has(path)?[...selF]:[path];
    e.dataTransfer.effectAllowed=dragCp?'copy':'move';e.dataTransfer.setData('text/plain',path);
    setTimeout(()=>el.classList.add('dragging'),0);
  });
  grid.addEventListener('dragend',e=>{
    document.querySelectorAll('.fitem.dragging,.fitem.drop-tgt').forEach(x=>{x.classList.remove('dragging','drop-tgt');x.querySelector('.drop-indicator')?.remove()});
  });
  const clearTgt=()=>document.querySelectorAll('.fitem.drop-tgt').forEach(x=>{x.classList.remove('drop-tgt');x.querySelector('.drop-indicator')?.remove()});
  grid.addEventListener('dragover',e=>{
    const el=itemOf(e);
    if(!el||el.dataset.type!=='dir'||dragIs.includes(el.dataset.path))return;
    e.preventDefault();
    if(!el.classList.contains('drop-tgt')){
      clearTgt();el.classList.add('drop-tgt');
      const ind=document.createElement('div');ind.className='drop-indicator';
      ind.textContent=dragCp?'\u{1F4CB} Copy here':'\u2702\uFE0F Move here';el.appendChild(ind);
    }
  });
  grid.addEventListener('dragleave',e=>{
    const el=itemOf(e);if(!el||!el.classList.contains('drop-tgt'))return;
    if(e.relatedTarget&&el.contains(e.relatedTarget))return;
    el.classList.remove('drop-tgt');el.querySelector('.drop-indicator')?.remove();
  });
  grid.addEventListener('drop',async e=>{
    const el=itemOf(e);if(!el||el.dataset.type!=='dir')return;
    e.preventDefault();clearTgt();
    const items=dragIs.slice();
    await dropItems(items,el.dataset.path,dragCp);dragIs=[];rFiles(curP);
  });
}
function wireItem(){}   // kept for older callers: items are handled by the grid's own listeners now
function toggleSel(el,path){el.classList.toggle('sel');selF.has(path)?selF.delete(path):selF.add(path);updSel()}
function clearSel(){selF.clear();document.querySelectorAll('.fitem.sel').forEach(e=>e.classList.remove('sel'));updSel()}
function rangeSel(el,path){
  const all=[...document.querySelectorAll('.fitem')];const lastSel=[...document.querySelectorAll('.fitem.sel')].pop();
  if(!lastSel){toggleSel(el,path);return}
  const a=all.indexOf(lastSel),b=all.indexOf(el);const [lo,hi]=[Math.min(a,b),Math.max(a,b)];
  all.slice(lo,hi+1).forEach(e=>{e.classList.add('sel');selF.add(e.dataset.path)});updSel();
}
function updSel(){const n=selF.size,bar=document.getElementById('selBar');if(bar){bar.classList.toggle('show',n>0);const c=document.getElementById('selCnt');if(c)c.textContent=`${n} selected`}
  const ap=[...selF].some(_isProtPath);
  ['cutSelBtn','delSelBtn'].forEach(id=>{const b=document.getElementById(id);if(b){b.disabled=ap;b.title=ap?'Your selection includes a folder that is part of Void OS\u2019s structure, so it can\u2019t be moved or deleted':''}});
}
let marqueeMoved=false,_mq=null;
function _mqScroller(){return document.getElementById('mainEl')}
function startMarquee(e){
  if(e.button!==0||e.target.closest('.fitem'))return; // left-click on empty space only
  marqueeMoved=false;
  const additive=e.ctrlKey||e.metaKey;
  if(!additive)clearSel();
  marqueeBaseSel=additive?new Set(selF):new Set();
  marqueeStart={x:e.clientX,y:e.clientY};
  _mqBegin(e.clientX,e.clientY);
  marquee=document.createElement('div');marquee.className='marquee';
  Object.assign(marquee.style,{left:e.clientX+'px',top:e.clientY+'px',width:'0px',height:'0px'});
  document.body.appendChild(marquee);
  document.addEventListener('mousemove',dragMarquee);
  document.addEventListener('mouseup',stopMarquee);
  e.preventDefault(); // avoid native text-selection ghosting while dragging
}
// Geometry is measured once, in the scroller's own coordinates, so a drag over thousands of files only
// does arithmetic per frame (no layout reads) and keeps working while the page scrolls under it.
function _mqBegin(cx,cy){
  const sc=_mqScroller(),sr=sc.getBoundingClientRect();
  const items=[...document.querySelectorAll('.fitem')].map(el=>{
    const r=el.getBoundingClientRect();
    return{el,path:el.dataset.path,l:r.left-sr.left+sc.scrollLeft,r:r.right-sr.left+sc.scrollLeft,
           t:r.top-sr.top+sc.scrollTop,b:r.bottom-sr.top+sc.scrollTop,on:el.classList.contains('sel')};
  });
  _mq={sc,sr,items,sx:cx-sr.left+sc.scrollLeft,sy:cy-sr.top+sc.scrollTop,px:cx,py:cy,raf:0,sel:new Set(marqueeBaseSel||[])};
}
function dragMarquee(e){
  if(!marquee||!marqueeStart||!_mq)return;
  _mq.px=e.clientX;_mq.py=e.clientY;
  if(Math.abs(e.clientX-marqueeStart.x)>3||Math.abs(e.clientY-marqueeStart.y)>3)marqueeMoved=true;
  if(!_mq.raf)_mq.raf=requestAnimationFrame(_mqFrame);
}
function _mqFrame(){
  const q=_mq;if(!q||!marquee)return;
  q.raf=0;
  const sc=q.sc,sr=q.sr;
  // scroll while the pointer is near the top or bottom edge of the page area
  const edge=46,dy=q.py<sr.top+edge?-(sr.top+edge-q.py)/3:(q.py>sr.bottom-edge?(q.py-(sr.bottom-edge))/3:0);
  if(dy){sc.scrollTop+=Math.max(-26,Math.min(26,dy))}
  const cx=q.px-sr.left+sc.scrollLeft,cy=q.py-sr.top+sc.scrollTop;
  const x1=Math.min(q.sx,cx),x2=Math.max(q.sx,cx),y1=Math.min(q.sy,cy),y2=Math.max(q.sy,cy);
  Object.assign(marquee.style,{left:(x1-sc.scrollLeft+sr.left)+'px',top:(y1-sc.scrollTop+sr.top)+'px',
    width:(x2-x1)+'px',height:(y2-y1)+'px'});
  let changed=false;
  for(const it of q.items){
    const hit=it.l<x2&&it.r>x1&&it.t<y2&&it.b>y1;
    const want=hit||(marqueeBaseSel&&marqueeBaseSel.has(it.path));
    if(want!==it.on){it.on=want;it.el.classList.toggle('sel',want);changed=true}
    if(want)q.sel.add(it.path);else q.sel.delete(it.path);
  }
  if(changed){selF=new Set(q.sel);updSel()}
  if(dy)q.raf=requestAnimationFrame(_mqFrame);  // keep going while auto-scrolling
}
function stopMarquee(){
  document.removeEventListener('mousemove',dragMarquee);
  document.removeEventListener('mouseup',stopMarquee);
  if(_mq&&_mq.raf)cancelAnimationFrame(_mq.raf);
  _mq=null;
  if(marquee){marquee.remove();marquee=null}
  marqueeStart=null;marqueeBaseSel=null;
}
function selectAll(){
  document.querySelectorAll('.fitem').forEach(el=>{el.classList.add('sel');selF.add(el.dataset.path)});
  updSel();
}
let marqueeTouchTimer=null,marqueeTouchActive=false;
function touchPoint(e){
  const t=(e.touches&&e.touches[0])||(e.changedTouches&&e.changedTouches[0]);
  return t?{x:t.clientX,y:t.clientY}:null;
}
function startMarqueeTouch(e){
  if(e.target.closest('.fitem, .ftbar, .bcrumb, .sel-bar, .vh, button, input, a'))return;
  const pt=touchPoint(e);if(!pt)return;
  clearTimeout(marqueeTouchTimer);
  marqueeTouchActive=false;
  marqueeMoved=false;
  // Long-press before engaging select-mode — a quick touch+drag is a normal
  // scroll gesture and must not be hijacked; only a held-still press signals
  // "I want to box-select", same idea as long-press-to-select elsewhere.
  marqueeTouchTimer=setTimeout(()=>{
    marqueeTouchActive=true;
    clearSel();
    marqueeBaseSel=new Set();
    marqueeStart={x:pt.x,y:pt.y};
    _mqBegin(pt.x,pt.y);
    marquee=document.createElement('div');marquee.className='marquee';
    Object.assign(marquee.style,{left:pt.x+'px',top:pt.y+'px',width:'0px',height:'0px'});
    document.body.appendChild(marquee);
    if(navigator.vibrate)navigator.vibrate(12); // small haptic cue select-mode engaged, where supported
  },350);
}
function moveMarqueeTouch(e){
  if(!marqueeTouchActive){
    clearTimeout(marqueeTouchTimer); // real movement before the long-press fired = a scroll, not a select-drag
    return;
  }
  const pt=touchPoint(e);if(!pt)return;
  e.preventDefault(); // now genuinely dragging a selection box — stop the page scrolling under it
  dragMarquee({clientX:pt.x,clientY:pt.y});
}
function endMarqueeTouch(){
  clearTimeout(marqueeTouchTimer);
  if(marqueeTouchActive){stopMarquee();marqueeTouchActive=false}
}
function showBlankCtx(x,y,path){
  hideCtx();
  const icons={folder:'<path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/>',
    upload:'<polyline points="16 16 12 12 8 16"/><line x1="12" y1="12" x2="12" y2="21"/><path d="M20.39 18.39A5 5 0 0018 9h-1.26A8 8 0 103 16.3"/>',
    clipboard:'<path d="M16 4h2a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/>',
    all:'<polyline points="20 6 9 17 4 12"/>',
    refresh:'<polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 102.13-9.36L1 10"/>'};
  const hasItems=!!document.querySelector('.fitem');
  const items=[
    {l:'New Folder',ico:'folder',cb:()=>mkdirDlg(path)},
    {l:'Upload',ico:'upload',cb:()=>document.getElementById('ulInp')?.click()},
    clip?{l:`Paste ${clip.paths.length} item(s)`,ico:'clipboard',cb:()=>doPaste(path)}:null,
    {sep:true},
    hasItems?{l:'Select All',ico:'all',cb:()=>selectAll()}:null,
    {l:'Refresh',ico:'refresh',cb:()=>rFiles(path)},
  ].filter(Boolean);
  const menu=document.createElement('div');menu.className='ctx';
  items.forEach(it=>{
    if(it.sep){menu.appendChild(Object.assign(document.createElement('div'),{className:'ctx-sep'}));return}
    const el=document.createElement('div');el.className='ctx-item';
    el.innerHTML=`<svg class="ctx-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${icons[it.ico]||''}</svg>${it.l}`;
    el.addEventListener('click',()=>{hideCtx();it.cb()});menu.appendChild(el);
  });
  document.body.appendChild(menu);ctxEl=menu;
  const r=menu.getBoundingClientRect();
  if(r.right>window.innerWidth)menu.style.left=(x-r.width)+'px';else menu.style.left=x+'px';
  if(r.bottom>window.innerHeight)menu.style.top=(y-r.height)+'px';else menu.style.top=y+'px';
}
// A listed entry is "protected" when it's part of Void OS's own folder structure (Shared, a personal
// folder, a mounted drive...): the server refuses to rename, move or delete it, so the page doesn't offer to.
function _isProtPath(p){return !!document.querySelector('.fitem[data-path="'+CSS.escape(p)+'"]')?.dataset.prot}
function _cleanSeps(list){
  const out=[];
  for(const it of list){if(it.sep&&(!out.length||out[out.length-1].sep))continue;out.push(it)}
  while(out.length&&out[out.length-1].sep)out.pop();
  return out;
}
function showCtx(x,y,item){
  hideCtx();const isT=item.type==='text',isD=item.type==='dir',multi=selF.size>1;
  const icons={eye:'<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/>',edit:'<path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/>',type:'<polyline points="4 7 4 4 20 4 20 7"/><line x1="9" y1="20" x2="15" y2="20"/><line x1="12" y1="4" x2="12" y2="20"/>',copy:'<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/>',scissors:'<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><line x1="20" y1="4" x2="8.12" y2="15.88"/><line x1="14.47" y1="14.48" x2="20" y2="20"/><line x1="8.12" y1="8.12" x2="12" y2="12"/>',clipboard:'<path d="M16 4h2a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/>',download:'<polyline points="8 17 12 21 16 17"/><line x1="12" y1="12" x2="12" y2="21"/><path d="M20.88 18.09A5 5 0 0018 9h-1.26A8 8 0 103 16.3"/>',trash:'<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/>',share:'<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/>'};
  const anyProt=item.prot||[...selF].some(_isProtPath);
  let items=[
    !isD?{l:'View',ico:'eye',cb:()=>viewFile(item.path,item.name)}:null,
    isT?{l:'Edit',ico:'edit',cb:()=>editFile(item.path,item.name)}:null,
    {sep:true},!item.prot?{l:'Rename',ico:'type',cb:()=>renameDlg(item)}:null,
    {l:multi?`Copy ${selF.size} items`:'Copy',ico:'copy',cb:()=>toClip('copy')},
    !anyProt?{l:multi?`Cut ${selF.size} items`:'Cut',ico:'scissors',cb:()=>toClip('cut')}:null,
    clip?{l:`Paste ${clip.paths.length} item(s)`,ico:'clipboard',cb:()=>doPaste(isD?item.path:curP)}:null,
    {sep:true},{l:multi?`Download ${selF.size} items`:'Download',ico:'download',cb:()=>downloadSel(multi?[...selF]:[item.path])},
    (!multi&&(item.path===FS_SHARED||item.path.startsWith(FS_SHARED+'/')))?{l:'Get Share Link',ico:'share',cb:()=>showShareDlg(item)}:null,
    {sep:true},!anyProt?{l:multi?`Delete ${selF.size} items`:'Delete',ico:'trash',danger:true,cb:()=>deleteSel([item.path])}:null,
  ].filter(Boolean);
  items=_cleanSeps(items);
  const menu=document.createElement('div');menu.className='ctx';
  items.forEach(it=>{
    if(it.sep){menu.appendChild(Object.assign(document.createElement('div'),{className:'ctx-sep'}));return}
    const el=document.createElement('div');el.className='ctx-item'+(it.danger?' danger':'');
    el.innerHTML=`<svg class="ctx-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${icons[it.ico]||''}</svg>${it.l}`;
    el.addEventListener('click',()=>{hideCtx();it.cb()});menu.appendChild(el);
  });
  document.body.appendChild(menu);ctxEl=menu;
  const r=menu.getBoundingClientRect();
  if(r.right>window.innerWidth)menu.style.left=(x-r.width)+'px';else menu.style.left=x+'px';
  if(r.bottom>window.innerHeight)menu.style.top=(y-r.height)+'px';else menu.style.top=y+'px';
}
function hideCtx(){if(ctxEl){ctxEl.remove();ctxEl=null}}
document.addEventListener('click',hideCtx);
function filesViewActive(){return !!document.getElementById('fgrid')}
document.getElementById('mainEl')?.addEventListener('mousedown',e=>{
  if(!filesViewActive())return;
  if(e.target.closest('.fitem, .ftbar, .bcrumb, .sel-bar, .vh, button, input, a'))return;
  startMarquee(e);
});
document.getElementById('mainEl')?.addEventListener('touchstart',e=>{
  if(!filesViewActive())return;
  startMarqueeTouch(e);
},{passive:true});
document.getElementById('mainEl')?.addEventListener('touchmove',e=>{
  if(!filesViewActive())return;
  moveMarqueeTouch(e);
},{passive:false});
document.getElementById('mainEl')?.addEventListener('touchend',e=>{
  if(!filesViewActive())return;
  endMarqueeTouch();
});
document.getElementById('mainEl')?.addEventListener('touchcancel',e=>{
  if(!filesViewActive())return;
  endMarqueeTouch();
});
document.getElementById('mainEl')?.addEventListener('contextmenu',e=>{
  if(!filesViewActive())return;
  if(e.target.closest('input, textarea'))return; // let native cut/copy/paste work in text fields
  e.preventDefault();
  if(!e.target.closest('.fitem'))showBlankCtx(e.clientX,e.clientY,curP);
});
document.addEventListener('keydown',e=>{
  const tag=(e.target.tagName||'').toLowerCase();
  if(tag==='input'||tag==='textarea'||e.target.isContentEditable)return;
  const grid=document.getElementById('fgrid');
  if(!grid)return;
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='a'){e.preventDefault();selectAll()}
  else if(e.key==='Escape'&&selF.size){clearSel()}
});
function toClip(mode){clip={paths:selF.size>0?[...selF]:[],mode};toast(`${clip.paths.length} item(s) ${mode==='copy'?'copied':'cut'}`,'info');rFiles(curP)}
async function doPaste(destPath){
  if(!clip||!clip.paths.length)return;
  const items=clip.paths.map(src=>({src,dst:(destPath?destPath+'/':'')+src.split('/').pop()})).filter(i=>i.src!==i.dst);
  if(!items.length){toast('Those items are already in this folder','info');return}
  const job=await startFileOp({op:clip.mode==='copy'?'copy':'move',items});
  if(job&&clip.mode==='cut'){clip=null;rFiles(curP)}  // clear the "cut" highlight; items move when the job finishes
}
async function dropItems(items,destFolder,copy){
  const list=items.map(src=>({src,dst:(destFolder?destFolder+'/':'')+src.split('/').pop()})).filter(i=>i.src!==i.dst);
  if(list.length)await startFileOp({op:copy?'copy':'move',items:list});
}
async function deleteSel(paths,anchorEl){
  const ps=paths||(selF.size>0?[...selF]:[]);if(!ps.length)return;
  confirmDlg(`Move ${ps.length} item(s) to Trash?`,async()=>{
    const job=await startFileOp({op:'trash',paths:ps},anchorEl);
    if(job)clearSel();  // the view refreshes when the job finishes
  },'Move to Trash',anchorEl);
}
function zipPanel(){
  document.getElementById('zprogPanel')?.remove();
  const el=document.createElement('div');el.id='zprogPanel';el.className='zprog-panel';
  el.innerHTML=`
    <div class="zprog-panel-head">
      <span class="zprog-panel-title" id="zprogTitle">Compressing files\u2026</span>
      <button class="zprog-panel-x" id="zprogCloseBtn" title="Cancel" aria-label="Cancel">\u2715</button>
    </div>
    <div style="font-size:11.5px;color:var(--td)" id="zprogLabel"></div>
    <div class="zprog-track"><div class="zprog-fill indeterminate" id="zprogFill"></div></div>`;
  document.body.appendChild(el);
  requestAnimationFrame(()=>el.classList.add('show'));
  return el;
}
function closeZipPanel(el){
  el.classList.remove('show');
  setTimeout(()=>el.remove(),220);
}
async function downloadSel(paths){
  const ps=paths||(selF.size>0?[...selF]:[]);if(!ps.length)return;
  if(ps.length===1){
    const el=document.querySelector(`.fitem[data-path="${CSS.escape(ps[0])}"]`);
    const isDir=el?el.dataset.type==='dir':false;
    if(!isDir){
      const a=document.createElement('a');a.href='/api/file/download?path='+encodeURIComponent(ps[0]);a.download=ps[0].split('/').pop();a.click();return
    }
    // single folder — /api/file/download only serves single files (404s on
    // a directory), so fall through to the same zip-job flow multi-select
    // already uses below instead of trying to raw-download a folder.
  }
  let jobId=null,cancelled=false,pollTimer=null;
  const panel=zipPanel();
  document.getElementById('zprogLabel').textContent=`Preparing ${ps.length} items\u2026`;
  const stopPolling=()=>{if(pollTimer)clearTimeout(pollTimer)};
  const cancelJob=async()=>{
    cancelled=true;stopPolling();
    if(jobId)try{await api(`/api/file/zip/${jobId}`,{method:'DELETE'})}catch(e){}
  };
  document.getElementById('zprogCloseBtn')?.addEventListener('click',()=>{cancelJob();closeZipPanel(panel)});
  try{
    const start=await(await api('/api/file/zip',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:ps})})).json();
    if(start.error){errToast(start);closeZipPanel(panel);return}
    jobId=start.job_id;
    const poll=async()=>{
      if(cancelled)return;
      if(!document.body.contains(panel)){cancelJob();return} // panel was closed/removed
      const d=await(await api(`/api/file/zip/progress/${jobId}`)).json();
      if(d.error){errToast(d);closeZipPanel(panel);return}
      const fill=document.getElementById('zprogFill'),label=document.getElementById('zprogLabel');
      if(d.total>0){
        fill?.classList.remove('indeterminate');
        if(fill)fill.style.width=Math.round(100*d.processed/d.total)+'%';
        if(label)label.textContent=`Compressed ${d.processed} of ${d.total} items\u2026`;
      }
      if(d.done){
        if(fill){fill.classList.remove('indeterminate');fill.style.width='100%'}
        if(label)label.textContent='Starting download\u2026';
        if(d.skipped>0)toast(`${d.skipped} item${d.skipped===1?' was':'s were'} left out of the zip - unreadable, in use, or no longer there`,'warning',{duration:9000});
        const a=document.createElement('a');a.href=`/api/file/zip/download/${jobId}`;
        a.download=ps.length===1?(ps[0].split('/').pop()+'.zip'):'void-os-files.zip';
        a.click();
        setTimeout(()=>closeZipPanel(panel),700);
        return;
      }
      pollTimer=setTimeout(poll,350);
    };
    poll();
  }catch(e){toast('Failed to start compression','error');closeZipPanel(panel)}
}
// ── CONTENT SEARCH ────────────────────────────────────────────────
async function doFileSearch(q,scopePath){
  const grid=document.getElementById('fgrid');if(!grid)return;
  grid.innerHTML=`<div class="loading"><div class="loader"></div></div>`;
  try{
    const d=await(await api(`/api/files/search?q=${encodeURIComponent(q)}&path=${encodeURIComponent(scopePath)}`)).json();
    if(d.error){grid.innerHTML=`<div class="empty-dir">${esc(d.error)}</div>`;return}
    if(!d.results.length){grid.innerHTML='<div class="empty-dir">No matches found</div>';return}
    grid.innerHTML=d.results.map(searchResultChip).join('')+
      (d.truncated?'<div class="empty-dir" style="grid-column:1/-1;padding:12px">Showing first 150 matches \u2014 refine your search</div>':'');
    grid.querySelectorAll('.fitem').forEach(el=>{
      const p=el.dataset.path,type=el.dataset.type,name=el.dataset.name;
      el.addEventListener('click',()=>{type==='dir'?rFiles(p):viewFile(p,name)});
    });
  }catch(e){grid.innerHTML=`<div class="empty-dir">Search failed: ${esc(e.message)}</div>`}
}
function searchResultChip(e){
  return `<div class="fitem${e.type==='dir'&&CLOUD_SYNCING.has(e.path)?' syncing':''}" data-path="${escA(e.path)}" data-type="${e.type}" data-name="${escA(e.name)}"
      style="grid-column:1/-1;display:flex;align-items:center;gap:10px;text-align:left;padding:9px 10px">
    ${fileIconHtml(e,20)}
    <div style="flex:1;min-width:0">
      <div class="fname" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(e.name)}</div>
      <div style="font-size:11px;color:var(--td);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
        ${esc(e.path)}${e.snippet?' \u2014 \u2026'+esc(e.snippet)+'\u2026':''}</div>
    </div>
    ${e.size!=null?`<div class="fsize" style="flex-shrink:0">${fmtB(e.size)}</div>`:''}
  </div>`;
}
// ── RECYCLE BIN ───────────────────────────────────────────────────
async function showTrash(){
  openMdl('Recycle Bin','<div class="loading"><div class="loader"></div></div>',
    `<button class="btn btn-g" onclick="closeMdl()">Close</button><button class="btn btn-d" id="emptyTrashBtn">Empty Trash</button>`,true);
  document.getElementById('emptyTrashBtn')?.addEventListener('click',e=>{
    confirmDlg('Permanently delete everything in the Recycle Bin? This cannot be undone.',async()=>{
      await startFileOp({op:'empty_trash'});
    },'Empty Trash',e.currentTarget);
  });
  await loadTrash();
}
async function loadTrash(){
  try{
    const d=await apiData('/api/trash');
    const items=d.items||[];
    document.getElementById('mdlB').innerHTML = items.length
      ? `<div class="user-table">${items.map(trashRow).join('')}</div>`
      : '<div class="empty-dir">Recycle Bin is empty</div>';
    items.forEach(it=>{
      document.getElementById(`trRestore-${it.id}`)?.addEventListener('click',()=>restoreTrashItem(it.id));
      document.getElementById(`trDel-${it.id}`)?.addEventListener('click',e=>deleteTrashItemForever(it.id,it.name,e.currentTarget));
    });
  }catch(e){document.getElementById('mdlB').innerHTML=`<div style="color:var(--rd)">${esc(e.message)}</div>`}
}
window.loadTrash=loadTrash;     // xferFinished() lives outside this scope and refreshes the open Recycle Bin with it
function trashRow(it){
  return `<div class="user-row" id="trrow-${it.id}">
    <div class="user-avatar avatar-guest">${ficons[it.type]||'\u{1F4C4}'}</div>
    <div class="user-info">
      <div class="user-name">${esc(it.name)}</div>
      <div class="user-meta">was ${esc(it.original_path)} &nbsp;\u2022&nbsp; deleted ${it.deleted_at?new Date(it.deleted_at).toLocaleString():'\u2014'} by ${esc(it.deleted_by||'\u2014')}${it.size!=null?` &nbsp;\u2022&nbsp; ${fmtB(it.size)}`:''}</div>
    </div>
    <div class="user-actions">
      <button class="btn btn-g btn-sm" id="trRestore-${it.id}">\u21A9 Restore</button>
      <button class="btn btn-d btn-sm" id="trDel-${it.id}">\u{1F5D1} Delete Forever</button>
    </div>
  </div>`;
}
async function restoreTrashItem(id){
  const r=await(await api('/api/trash/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:[id]})})).json();
  const res=r.results?.[0];
  if(res?.ok){
    toast('Restored','success');document.getElementById(`trrow-${id}`)?.remove();
    if(curV==='files')rFiles(curP);
    if(!document.querySelectorAll('[id^="trrow-"]').length){const b=document.getElementById('mdlB');if(b)b.innerHTML='<div class="empty-dir">Recycle Bin is empty</div>'}
  }else errToast(res,'Restore failed');
}
function deleteTrashItemForever(id,name,anchorEl){
  confirmDlg(`Permanently delete "${esc(name)}"? This cannot be undone.`,async()=>{
    const r=await(await api('/api/trash/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:[id]})})).json();
    const res=r.results?.[0];
    if(res?.ok){
      toast('Deleted permanently','success');document.getElementById(`trrow-${id}`)?.remove();
      if(!document.querySelectorAll('[id^="trrow-"]').length){const b=document.getElementById('mdlB');if(b)b.innerHTML='<div class="empty-dir">Recycle Bin is empty</div>'}
    }else errToast(res,'That didn\u2019t work');
  },'Delete Forever',anchorEl);
}
window.showTrash=showTrash;
function renameDlg(item){
  openMdl('Rename',`<div class="fg"><label class="fgl">New name</label><input class="fgi" id="rnIn" value="${escA(item.name)}" autofocus spellcheck="false"/></div>`,
    `<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-p" id="rnOk">Rename</button>`);
  const inp=document.getElementById('rnIn');const dot=item.name.lastIndexOf('.');if(dot>0)inp.setSelectionRange(0,dot);
  document.getElementById('rnOk')?.addEventListener('click',async()=>{
    const n=inp.value.trim();if(!n)return;
    const d=await(await api('/api/file/rename',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:item.path,name:n})})).json();
    if(d.ok){toast('Renamed','success');closeMdl();const el=document.querySelector(`.fitem[data-path="${CSS.escape(item.path)}"]`);if(el){el.dataset.path=d.path;el.dataset.name=n;el.querySelector('.fname').textContent=n}}
    else errToast(d,'Failed');
  });
}
function mkdirDlg(path){
  openMdl('New Folder',`<div class="fg"><label class="fgl">Name</label><input class="fgi" id="mkIn" placeholder="my-folder" autofocus/></div>`,
    `<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-p" id="mkOk">Create</button>`);
  document.getElementById('mkOk')?.addEventListener('click',async()=>{
    const n=document.getElementById('mkIn').value.trim();if(!n)return;
    const np=path?(path+'/'+n):n;
    const d=await(await api('/api/file/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:np})})).json();
    if(d.ok){toast('Folder created','success');closeMdl();addFileEl({name:n,type:'dir',size:null,modified:Date.now()/1000,path:np})}else errToast(d,'Failed');
  });
}
// ══════════════════════════════════════════════════════════════════
// UPLOADS — a real, persistent progress panel (WeTransfer/Drive-style)
// backing both the original small-file path and new chunked/resumable
// uploads for files too large for a single request. See FEATURE_REQUEST_
// chunked_uploads.md for the full design.
// ══════════════════════════════════════════════════════════════════
const UP_CHUNK_BYTES = UP_CHUNK_MB*1024*1024;  // per-chunk size (VOID_UPLOAD_CHUNK_MB)
const UP_CHUNK_THRESHOLD = 50*1024*1024;  // files at/above this size use chunking
const UP_SMALL_BATCH_CAP = 16*1024*1024;  // small files are grouped into <=16MB requests, so several
                                           // requests can run side by side (and stay far under Cloudflare's 100MB cap)
const UP_PARALLEL = 3;                    // files/batches uploading at the same time
let _upActive=0;const _upQ=[];
function upEnqueue(fn){
  return new Promise(res=>{
    _upQ.push(async()=>{try{await fn()}catch(e){console.error(e)}finally{_upActive--;upPump();res()}});
    upPump();
  });
}
function upPump(){while(_upActive<UP_PARALLEL&&_upQ.length){_upActive++;_upQ.shift()()}}

function upResumeKey(filename,size,path){return `void-resume:${path}:${filename}:${size}`}

// A row is the upload's own bookkeeping; what the panel shows is derived from it by upPush().
function upPush(row){
  const st={queued:'queued',uploading:'running',done:'done',failed:'failed',skipped:'skipped'}[row.status]||'running';
  const where=tfFolderLabel(row._path);
  tpUpsert('u-'+row.id,{
    kind:'upload',title:row.file.name,badge:tfBadge(row.file.name),state:st,live:st==='running',
    finalizing:/finaliz|starting/i.test(row.label||''),
    loaded:row.loaded,total:row.total,pct:row.total?row.loaded/row.total*100:(st==='done'?100:0),
    detail:st==='queued'?'Waiting\u2026':st==='done'?`Uploaded to ${where}`:st==='failed'?(row.err||'Failed'):st==='skipped'?'Cancelled':(row.label||''),
    group:'up:'+(row._path||''),groupPrefix:'Uploading to',groupLabel:where,groupPath:row._path||'',
    cancel:(st==='queued'||st==='running')?()=>upCancelRow(row):null,
    action:st==='done'?{label:'Show in folder',fn:()=>tpOpenFolder(row._path||'',row.file.name)}
          :(st==='failed'&&row.retry)?{label:'Retry',fn:row.retry}:null,
  });
}
function upAddRow(file,path){
  const id='r'+Math.random().toString(36).slice(2,9);
  const row={id,file,loaded:0,total:file.size,status:'queued',label:'',err:null,retry:null,cancelled:false,xhr:null,uploadId:null,_path:path};
  TP.lastDest={path:path||''};
  upPush(row);
  return row;
}
function upSetProgress(row,loaded){row.loaded=Math.min(loaded,row.total);upPush(row)}
function upSetStatus(row,status,label){
  row.status=status;if(label!==undefined&&label!==null)row.label=label;
  if(status!=='failed')row.err=null;
  upPush(row);
}
function upSetFailed(row,msg,retryFn){
  row.status='failed';row.err=msg||'Failed';
  row.retry=retryFn?()=>{row.retry=null;row.cancelled=false;row.loaded=0;upSetStatus(row,'queued','');retryFn()}:null;
  upPush(row);
}
async function upCancelRow(row){
  if(row.status==='done'||row.status==='skipped')return;
  row.cancelled=true;
  if(row.xhr){try{row.xhr.abort()}catch(e){}}
  if(row.uploadId){
    try{await api('/api/file/upload/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({upload_id:row.uploadId})})}catch(e){}
    try{localStorage.removeItem(upResumeKey(row.file.name,row.file.size,row._path))}catch(e){}
  }
  upSetStatus(row,'skipped','Cancelled');
}
// A finished file shows up in the folder being looked at, if that is where it was uploaded to.
function upShowInView(item,path){
  if(App.curV==='files'&&(tfCtx().curP||'')===(path||''))addFileEl(item);
}

// ── Small-file path (single request per batch, real progress via XHR) ──
function upSendBatchXHR(fd,rows){
  return new Promise((resolve)=>{
    const xhr=new XMLHttpRequest();
    rows.forEach(r=>r.xhr=xhr);
    xhr.open('POST','/api/file/upload');
    xhr.withCredentials=true;
    xhr.upload.onprogress=(e)=>{
      if(!e.lengthComputable)return;
      // Spread this batch's overall byte progress over its files in proportion to size, so a tiny
      // file doesn't look finished before a large one in the same request has actually arrived.
      const batchTotal=rows.reduce((s,r)=>s+r.total,0);
      for(const r of rows){
        if(r.status==='failed'||r.cancelled)continue;
        const share=batchTotal?Math.floor(e.loaded*(r.total/batchTotal)):0;
        upSetProgress(r,Math.min(share,r.total));
      }
    };
    xhr.onload=()=>{
      let d=null;try{d=JSON.parse(xhr.responseText)}catch(e){}
      resolve({status:xhr.status,body:d});
    };
    xhr.onerror=()=>resolve({status:0,body:null});
    xhr.onabort=()=>resolve({status:0,body:null,aborted:true});
    xhr.send(fd);
  });
}

async function upRunSmallBatch(files,path,rows){
  const live=rows.map((r,i)=>[r,files[i]]).filter(([r])=>!r.cancelled);
  if(!live.length)return;
  rows=live.map(x=>x[0]);files=live.map(x=>x[1]);
  rows.forEach(r=>upSetStatus(r,'uploading','Uploading\u2026'));
  const fd=new FormData();fd.append('path',path);for(const f of files)fd.append('files',f);
  const res=await upSendBatchXHR(fd,rows);
  if(res.aborted&&rows.every(r=>r.cancelled))return;
  if(!res.body||res.status>=400){
    rows.forEach(r=>{if(!r.cancelled)upSetFailed(r,res.body?.error||'Upload failed',()=>upEnqueue(()=>upRunSmallBatch([r.file],path,[r])))});
    return;
  }
  const savedByName={};(res.body.saved||[]).forEach(it=>savedByName[it.name]=it);
  const failedByName={};(res.body.failed||[]).forEach(it=>failedByName[it.name]=it);
  for(const r of rows){
    if(r.cancelled)continue;
    if(savedByName[r.file.name]){upSetProgress(r,r.total);upSetStatus(r,'done','Done');upShowInView(savedByName[r.file.name],path)}
    else upSetFailed(r,failedByName[r.file.name]?.error||'Failed',()=>upEnqueue(()=>upRunSmallBatch([r.file],path,[r])));
  }
}

// ── Chunked path (sequential chunks, resumable) ─────────────────────
async function upSendChunkXHR(url,fd,onProgress,row){
  return new Promise((resolve)=>{
    const xhr=new XMLHttpRequest();
    if(row)row.xhr=xhr;
    xhr.open('POST',url);xhr.withCredentials=true;
    if(onProgress)xhr.upload.onprogress=onProgress;
    xhr.onload=()=>{let d=null;try{d=JSON.parse(xhr.responseText)}catch(e){}resolve({status:xhr.status,body:d,xhr})};
    xhr.onerror=()=>resolve({status:0,body:null,xhr});
    xhr.onabort=()=>resolve({status:0,body:null,xhr,aborted:true});
    xhr.send(fd);
  });
}

async function upRunChunkedFile(file,path,row){
  if(row.cancelled)return;
  const resumeKey=upResumeKey(file.name,file.size,path);
  row._path=path;
  let uploadId=null,startChunk=0;
  const totalChunks=Math.max(1,Math.ceil(file.size/UP_CHUNK_BYTES));
  const retry=()=>upEnqueue(()=>upRunChunkedFile(file,path,row));

  // Try to resume a previous session for this exact file (name+size+path).
  try{
    const saved=JSON.parse(localStorage.getItem(resumeKey)||'null');
    if(saved?.upload_id){
      const st=await(await api(`/api/file/upload/status/${saved.upload_id}`)).json();
      if(!st.error){uploadId=saved.upload_id;startChunk=st.received_chunks;upSetProgress(row,startChunk*UP_CHUNK_BYTES)}
    }
  }catch(e){/* no valid resume — start fresh below */}

  if(!uploadId){
    upSetStatus(row,'uploading','Starting\u2026');
    let r;
    try{
      r=await(await api('/api/file/upload/start',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({path,filename:file.name,size:file.size,total_chunks:totalChunks,chunk_size:UP_CHUNK_BYTES})})).json();
    }catch(e){upSetFailed(row,'Upload failed',retry);return}
    if(r.error){upSetFailed(row,r.error);return}
    uploadId=r.upload_id;startChunk=0;
    try{localStorage.setItem(resumeKey,JSON.stringify({upload_id:uploadId,filename:file.name,size:file.size,path}))}catch(e){}
  }
  row.uploadId=uploadId;
  upSetStatus(row,'uploading','Uploading\u2026');

  for(let i=startChunk;i<totalChunks;i++){
    if(row.cancelled)return;
    const start=i*UP_CHUNK_BYTES,end=Math.min(start+UP_CHUNK_BYTES,file.size);
    const blob=file.slice(start,end);
    let attempt=0,ok=false;
    while(attempt<3&&!ok){
      attempt++;
      const fd=new FormData();fd.append('upload_id',uploadId);fd.append('chunk_index',String(i));fd.append('chunk',blob,'chunk');
      const res=await upSendChunkXHR('/api/file/upload/chunk',fd,(e)=>{
        if(!e.lengthComputable)return;
        upSetProgress(row,start+e.loaded);
      },row);
      if(row.cancelled)return;
      if(res.status===200){ok=true;upSetProgress(row,end)}
      else if(res.status===409&&res.body?.expected_index!==undefined){
        // Our view of progress had drifted from the server's — resync and continue from there.
        i=res.body.expected_index-1;ok=true;
      }else if(attempt<3){
        await new Promise(res=>setTimeout(res,600*attempt));
      }
    }
    if(!ok){upSetFailed(row,'Chunk upload failed after retries',retry);return}
  }

  upSetStatus(row,'uploading','Finalizing\u2026');
  let fin;
  try{fin=await(await api('/api/file/upload/finish',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({upload_id:uploadId})})).json()}
  catch(e){upSetFailed(row,'Could not finalize upload',retry);return}
  if(fin.error){upSetFailed(row,fin.error,retry);return}
  try{localStorage.removeItem(resumeKey)}catch(e){}
  upSetProgress(row,row.total);upSetStatus(row,'done','Done');
  upShowInView(fin,path);
}

async function doUpload(files,path){
  if(!files||!files.length)return;
  files=[...files];
  const rows=files.map(f=>upAddRow(f,path));

  const small=[],chunked=[];
  files.forEach((f,i)=>(f.size>=UP_CHUNK_THRESHOLD?chunked:small).push({f,row:rows[i]}));

  // Small files are grouped into requests of at most UP_SMALL_BATCH_CAP, each request's real progress
  // driven by XHR; large files go in resumable chunks. UP_PARALLEL of those run at the same time.
  const batches=[];let cur=[],curBytes=0;
  for(const {f,row} of small){
    if(cur.length&&curBytes+f.size>UP_SMALL_BATCH_CAP){batches.push(cur);cur=[]; curBytes=0}
    cur.push({f,row});curBytes+=f.size;
  }
  if(cur.length)batches.push(cur);

  const jobs=[];
  for(const batch of batches)jobs.push(upEnqueue(()=>upRunSmallBatch(batch.map(b=>b.f),path,batch.map(b=>b.row))));
  for(const {f,row} of chunked)jobs.push(upEnqueue(()=>upRunChunkedFile(f,path,row)));
  await Promise.all(jobs);
}
function addFileEl(item){
  const g=document.getElementById('fgrid');if(!g)return;
  const empty=g.querySelector('.empty-dir');if(empty)empty.remove();
  const tmp=document.createElement('div');tmp.innerHTML=fchip(item);const newEl=tmp.firstElementChild;
  newEl.classList.add('entering');
  const all=[...g.querySelectorAll('.fitem')];
  const ins=all.find(e=>{const eD=e.dataset.type==='dir',nD=item.type==='dir';if(nD&&!eD)return false;if(!nD&&eD)return true;return e.dataset.name.toLowerCase()>item.name.toLowerCase()});
  ins?g.insertBefore(newEl,ins):g.appendChild(newEl);
  wireItem(newEl);newEl.addEventListener('animationend',()=>newEl.classList.remove('entering'),{once:true});
}
// ── File viewer helpers: JSON tree, CSV table, Office documents, music player ─────────────
const FV_OFFICE=new Set(['pptx','pptm','ppsx','docx','docm','xlsx','xlsm','ppt','doc','xls','odp','ods','odt']);
const FV_IMG_MAX=40*1024*1024;         // a bigger picture is offered as a download instead of decoded in the tab
function fvXl(on){const m=document.querySelector('.mdl');if(m)m.classList.toggle('xl',!!on)}
function fvEl(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}

// JSON as a collapsible tree (built with textContent, so nothing in the file is ever treated as HTML).
function fvJsonTree(value){
  let budget=4000;                      // nodes drawn; the raw view always shows everything
  const render=(v,key,depth)=>{
    if(--budget<0){return fvEl('div','jt-row jt-more','… more not shown')}
    const k=key==null?null:fvEl('span','jt-k',typeof key==='number'?key+':':JSON.stringify(key)+':');
    if(v!==null&&typeof v==='object'){
      const isArr=Array.isArray(v),keys=isArr?v.map((_,i)=>i):Object.keys(v);
      const d=document.createElement('details');d.className='jt-node';d.open=depth<2;
      const s=document.createElement('summary');
      if(k){s.appendChild(k);s.appendChild(document.createTextNode(' '))}
      s.appendChild(fvEl('span','jt-b',isArr?`[ ${keys.length} ]`:`{ ${keys.length} }`));
      d.appendChild(s);
      const kids=fvEl('div','jt-kids');
      keys.slice(0,500).forEach(kk=>kids.appendChild(render(v[kk],kk,depth+1)));
      if(keys.length>500)kids.appendChild(fvEl('div','jt-row jt-more',`… ${keys.length-500} more`));
      d.appendChild(kids);return d;
    }
    const row=fvEl('div','jt-row');
    if(k){row.appendChild(k);row.appendChild(document.createTextNode(' '))}
    const t=v===null?'null':typeof v;
    row.appendChild(fvEl('span','jt-v jt-'+t,t==='string'?JSON.stringify(v.length>2000?v.slice(0,2000)+'…':v):String(v)));
    return row;
  };
  const wrap=fvEl('div','jt');wrap.appendChild(render(value,null,0));return wrap;
}
// Small RFC-4180-ish CSV parser (quotes, doubled quotes, newlines inside quotes), bounded.
function fvParseCsv(text,delim,maxRows,maxCols){
  const rows=[];let row=[],cell='',q=false;
  for(let i=0;i<text.length&&rows.length<=maxRows;i++){
    const c=text[i];
    if(q){if(c==='"'){if(text[i+1]==='"'){cell+='"';i++}else q=false}else cell+=c}
    else if(c==='"')q=true;
    else if(c===delim){row.push(cell);cell=''}
    else if(c==='\n'||c==='\r'){if(c==='\r'&&text[i+1]==='\n')i++;row.push(cell);cell='';rows.push(row);row=[]}
    else cell+=c;
  }
  if(cell!==''||row.length){row.push(cell);rows.push(row)}
  return rows.slice(0,maxRows).map(r=>r.slice(0,maxCols));
}
function fvTable(rows,header){
  const t=fvEl('table','fv-tbl'),tb=document.createElement('tbody');
  rows.forEach((r,i)=>{const tr=document.createElement('tr');
    r.forEach(c=>{const td=document.createElement(header&&i===0?'th':'td');td.textContent=c;tr.appendChild(td)});tb.appendChild(tr)});
  t.appendChild(tb);const w=fvEl('div','fv-tblwrap');w.appendChild(t);return w;
}
// Raw / formatted switch used by JSON and CSV.
function fvModes(box,views){
  const bar=fvEl('div','fv-modes');box.appendChild(bar);
  const holder=fvEl('div');box.appendChild(holder);
  const show=i=>{holder.replaceChildren(views[i].node());[...bar.children].forEach((b,j)=>b.classList.toggle('on',j===i))};
  views.forEach((v,i)=>{const b=fvEl('button','fv-mode',v.label);b.type='button';b.onclick=()=>show(i);bar.appendChild(b)});
  show(0);
}

// Office documents: the server pulls out the text (bounded), the page draws it.
// Office documents: LibreOffice on the server draws the real thing (layout, fonts, colours) as a PDF; the first view of a file
// takes a few seconds. Without LibreOffice (or if it can't open the file) the server reads out the text and pictures instead.
async function fvOffice(path,name,box){
  const r=await api('/api/file/office?path='+encodeURIComponent(path));
  const d=await r.json();
  if(r.ok&&d.kind==='render'){
    const pu='/api/file/office/pdf?path='+encodeURIComponent(path);
    box.replaceChildren(fvEl('div','sdesc','Rendering with LibreOffice\u2026 the first time takes a few seconds.'));fvXl(true);
    const pr=await api(pu);
    if(pr.ok){
      box.replaceChildren();
      const f=document.createElement('iframe');f.className='fv-pdf';f.src=pu;box.appendChild(f);return;
    }
    const r2=await api('/api/file/office?plain=1&path='+encodeURIComponent(path));
    return fvOfficePlain(path,name,box,r2,await r2.json());
  }
  return fvOfficePlain(path,name,box,r,d);
}
async function fvOfficePlain(path,name,box,r,d){
  box.replaceChildren();
  if(!r.ok||d.error){box.appendChild(fvEl('div','sdesc',d.error||'Couldn’t preview this file'));return}
  if(['legacy','toolarge','broken'].includes(d.kind)){
    const n=fvEl('div','fv-note');n.appendChild(fvEl('div','fv-note-ico','\u{1F4C4}'));n.appendChild(fvEl('div',null,d.note));
    box.appendChild(n);return;
  }
  fvXl(true);
  const meta=fvEl('div','fv-meta');
  const chip=t=>meta.appendChild(fvEl('span','fv-chip',t));
  const media=nm=>`/api/file/office/media?path=${encodeURIComponent(path)}&name=${encodeURIComponent(nm)}`;
  if(d.kind==='pptx'){
    chip('presentation');chip(`${d.total} slide${d.total===1?'':'s'}`);
    if(d.truncated)chip('⚠ first '+d.slides.length+' shown');
    box.appendChild(meta);
    const list=fvEl('div','fv-slides');
    d.slides.forEach((s,i)=>{
      const c=fvEl('div','fv-slide');
      c.appendChild(fvEl('div','fv-slide-n','Slide '+(i+1)));
      if(s.title)c.appendChild(fvEl('div','fv-slide-t',s.title));
      if(s.images.length){const g=fvEl('div','fv-slide-imgs');
        s.images.forEach(nm=>{const im=document.createElement('img');im.loading='lazy';im.decoding='async';im.alt='';im.src=media(nm);
          im.onerror=()=>im.remove();g.appendChild(im)});c.appendChild(g)}
      if(s.text.length){const u=fvEl('ul','fv-slide-l');s.text.forEach(t=>u.appendChild(fvEl('li',null,t)));c.appendChild(u)}
      if(!s.title&&!s.text.length&&!s.images.length)c.appendChild(fvEl('div','fv-slide-e','(no text on this slide)'));
      if(s.notes)c.appendChild(fvEl('div','fv-slide-notes','Notes: '+s.notes));
      list.appendChild(c);
    });
    box.appendChild(list);
  }else if(d.kind==='docx'){
    chip('document');if(d.truncated)chip('⚠ shortened');box.appendChild(meta);
    const page=fvEl('div','fv-doc');
    d.blocks.forEach(b=>{
      if(b.t==='table')page.appendChild(fvTable(b.rows,true));
      else page.appendChild(fvEl(b.t==='h'?'h'+Math.min(6,b.level+1):'p',null,b.text));
    });
    if(!d.blocks.length)page.appendChild(fvEl('div','sdesc','This document has no text.'));
    box.appendChild(page);
  }else if(d.kind==='xlsx'){
    chip('workbook');chip(`${d.total} sheet${d.total===1?'':'s'}`);box.appendChild(meta);
    const bar=fvEl('div','fv-modes'),holder=fvEl('div');box.appendChild(bar);box.appendChild(holder);
    const show=i=>{const s=d.sheets[i];holder.replaceChildren();
      if(!s.rows.length)holder.appendChild(fvEl('div','sdesc','This sheet is empty.'));
      else holder.appendChild(fvTable(s.rows,true));
      if(s.truncated)holder.appendChild(fvEl('div','sdesc','Showing the first '+s.rows.length+' rows.'));
      [...bar.children].forEach((b,j)=>b.classList.toggle('on',j===i))};
    d.sheets.forEach((s,i)=>{const b=fvEl('button','fv-mode',s.name);b.type='button';b.onclick=()=>show(i);bar.appendChild(b)});
    if(d.sheets.length)show(0);
  }
}

// Music player with a flowing wave that moves with the sound (a canvas fed by an AnalyserNode).
function fvAudio(box,src,name){
  box.replaceChildren();
  const wrap=fvEl('div','fv-music');
  const cv=fvEl('canvas','fv-wave');
  const title=fvEl('div','fv-tname',name.replace(/\.[^.]+$/,''));
  const sub=fvEl('div','fv-tsub','Audio');
  const au=document.createElement('audio');au.className='fv-aud';au.controls=true;au.preload='metadata';au.src=src;
  wrap.append(cv,title,sub,au);box.appendChild(wrap);
  const cs=getComputedStyle(document.documentElement);
  const col=n=>(cs.getPropertyValue(n)||'').trim()||'#8b7bff';
  const colors=[col('--pu'),col('--ps'),col('--pg'),col('--pu')];
  let ctx=null,an=null,data=null,lvl=0,t=0,raf=0,W=0,H=0;
  const size=()=>{const dpr=Math.min(2,window.devicePixelRatio||1);W=Math.max(200,cv.clientWidth);H=cv.clientHeight||150;
    cv.width=Math.round(W*dpr);cv.height=Math.round(H*dpr);cv.getContext('2d').setTransform(dpr,0,0,dpr,0,0)};
  const wire=()=>{
    if(ctx)return;
    try{const C=window.AudioContext||window.webkitAudioContext;ctx=new C();const s=ctx.createMediaElementSource(au);
      an=ctx.createAnalyser();an.fftSize=128;an.smoothingTimeConstant=.82;s.connect(an);an.connect(ctx.destination);
      data=new Uint8Array(an.frequencyBinCount)}catch(e){ctx=null;an=null}
  };
  const g=cv.getContext('2d');
  const frame=()=>{
    if(!document.body.contains(cv)){cancelAnimationFrame(raf);try{au.pause();ctx&&ctx.close()}catch(e){}return}
    raf=requestAnimationFrame(frame);
    const playing=!au.paused&&!au.ended;
    let target=0.06;                                  // a gentle idle sway
    if(playing){
      target=0.28+0.1*Math.sin(t*2.1);                // moves even if the browser won't give us levels
      if(an){an.getByteFrequencyData(data);let s=0;const n=Math.min(40,data.length);for(let i=0;i<n;i++)s+=data[i];
        const l=s/n/255;if(l>0.001)target=Math.max(target*.35,l*1.35)}
    }
    lvl+=(target-lvl)*(target>lvl?.25:.06);
    t+=0.016+lvl*.03;
    g.clearRect(0,0,W,H);
    g.globalCompositeOperation='lighter';
    const layers=[{f:1.6,s:1.0,p:0,a:1.0,c:0},{f:2.3,s:-1.3,p:1.7,a:.75,c:1},{f:3.1,s:.8,p:3.1,a:.55,c:2},{f:1.1,s:-.6,p:4.6,a:.9,c:3}];
    layers.forEach((L,li)=>{
      const line=new Path2D();
      for(let x=0;x<=W;x+=6){
        const u=x/W,env=Math.sin(Math.PI*u);              // quiet at the edges, full in the middle
        const y=H*0.58-(Math.sin(u*Math.PI*2*L.f+t*L.s+L.p)*0.5+Math.sin(u*Math.PI*3.7*L.f-t*L.s*.7)*0.25)*H*(0.12+lvl*.5)*L.a*env-lvl*H*.12*env;
        x?line.lineTo(x,y):line.moveTo(x,y);
      }
      const area=new Path2D(line);area.lineTo(W,H);area.lineTo(0,H);area.closePath();   // the fill closes at the bottom; the stroke is only the wave
      const gr=g.createLinearGradient(0,0,0,H);gr.addColorStop(0,colors[L.c]);gr.addColorStop(1,'rgba(0,0,0,0)');
      g.globalAlpha=0.38;g.fillStyle=gr;g.fill(area);
      g.globalAlpha=0.85;g.lineWidth=1.6;g.strokeStyle=colors[L.c];g.stroke(line);
    });
    g.globalAlpha=1;g.globalCompositeOperation='source-over';
  };
  size();
  au.addEventListener('play',()=>{wire();if(ctx&&ctx.state==='suspended')ctx.resume();sub.textContent='Playing'});
  au.addEventListener('pause',()=>{sub.textContent='Paused'});
  au.addEventListener('ended',()=>{sub.textContent='Finished'});
  au.addEventListener('loadedmetadata',()=>{if(isFinite(au.duration)&&au.duration>0&&!sub.dataset.set){sub.dataset.set=1;const m=Math.floor(au.duration/60),s=Math.floor(au.duration%60);sub.textContent=`Audio · ${m}:${String(s).padStart(2,'0')}`}});
  window.addEventListener('resize',size);
  frame();
}

// Cloudflare tells us only the two-letter code; the browser knows the country's name.
function countryName(o){
  if(o.country)return o.country;
  try{return new Intl.DisplayNames(['en'],{type:'region'}).of(o.country_code)||o.country_code}catch(_){return o.country_code}
}
async function viewFile(path,name){
  const pp=path.includes('/')?path.substring(0,path.lastIndexOf('/')):'';
  openMdl(name,`<div class="loading"><div class="loader"></div></div>`,
    `<a class="btn btn-g" href="/api/file?path=${encodeURIComponent(path)}" download="${escA(name)}">\u{2B07} Download</a><button class="btn btn-d" id="fvDel">\u{1F5D1} Delete</button>`,true);
  document.getElementById('fvDel')?.addEventListener('click',e=>{confirmDlg(`Move "${esc(name)}" to Trash?`,async()=>{
    const job=await startFileOp({op:'trash',paths:[path]},e.currentTarget);
    if(job){closeMdl();const el=document.querySelector(`.fitem[data-path="${CSS.escape(path)}"]`);if(el){el.classList.add('leaving');setTimeout(()=>{el.remove();const g=document.getElementById('fgrid');if(g&&!g.querySelector('.fitem'))g.innerHTML='<div class="empty-dir">This folder is empty</div>'},220)}}
    },'Move to Trash',e.currentTarget)});
  try{
    const _ext=(name.includes('.')?name.split('.').pop():'').toLowerCase();
    if(FV_OFFICE.has(_ext)){await fvOffice(path,name,document.getElementById('mdlB'));return}
    // HEAD instead of GET for the initial content-type probe — Flask serves HEAD automatically
    // for any GET route, no server change needed. This only transfers headers, not the file body:
    // for image/video/audio/pdf below, the actual data transfer is the browser's own src=-triggered
    // GET, so a GET here would fetch the whole file a second time for nothing. For text/archive/
    // binary further down, a second request is unavoidable either way (their body IS the payload,
    // not just something to hand off to the browser's native renderer), so this costs nothing extra
    // there — it's already what happens today, just with an explicit HEAD in front of it now.
    const r=await api('/api/file?path='+encodeURIComponent(path),{method:'HEAD'});const ct=(r.headers.get('content-type')||'').split(';')[0];
    // Direct src= — not blob-fetching the whole file first — lets the browser's own network stack
    // handle the request, including Range requests for scrubbing/seeking and progressive rendering,
    // same as the PDF branch below already did. The old blob approach bought nothing for auth (the
    // api() helper here authenticates purely via the session cookie, which img/video/audio send
    // automatically on a same-origin request, same as the iframe below already relies on) — it just
    // meant nothing rendered until the whole file had downloaded, a real problem for a large video.
    const _src='/api/file?path='+encodeURIComponent(path),_size=Number(r.headers.get('content-length'))||0;
    if(ct.startsWith('image/')){
      if(_size>FV_IMG_MAX){document.getElementById('mdlB').innerHTML=`<div class="fv-note"><div class="fv-note-ico">\u{1F5BC}</div><div>This picture is ${fmtB(_size)} — too large to show here. Download it to open it.</div></div>`;return}
      fvXl(true);
      const box=document.getElementById('mdlB');
      box.innerHTML=`<div class="fv-meta"><span class="fv-chip" id="fvDim">image</span><span class="fv-chip">${fmtB(_size)}</span><span class="fv-chip">click to zoom</span></div><div class="fv-imgwrap"><img class="fv-img" decoding="async" src="${_src}" alt="${escA(name)}"/></div>`;
      const im=box.querySelector('.fv-img'),wrap=box.querySelector('.fv-imgwrap');
      im.addEventListener('load',()=>{const c=document.getElementById('fvDim');if(c)c.textContent=`${im.naturalWidth} × ${im.naturalHeight}`});
      im.addEventListener('error',()=>{wrap.innerHTML='<div class="fv-note"><div>This image couldn’t be shown.</div></div>'});
      im.addEventListener('click',()=>wrap.classList.toggle('zoom'));
      return}
    if(ct.startsWith('video/')){fvXl(true);document.getElementById('mdlB').innerHTML=`<video class="fv-vid" controls preload="metadata" src="${_src}"></video>`;return}
    if(ct.startsWith('audio/')){fvAudio(document.getElementById('mdlB'),_src,name);return}
    if(ct==='application/pdf'){fvXl(true);document.getElementById('mdlB').innerHTML=`<iframe class="fv-pdf" src="${_src}"></iframe>`;return}
    const d=await(await api('/api/file?path='+encodeURIComponent(path))).json();
    if(d.type==='text'){
      if(!d.truncated&&['json','geojson'].includes(d.ext||'')){
        let parsed,ok=true;try{parsed=JSON.parse(d.content)}catch(_){ok=false}
        if(ok){
          const box=document.getElementById('mdlB');box.replaceChildren();
          const meta=fvEl('div','fv-meta');meta.appendChild(fvEl('span','fv-chip','json'));meta.appendChild(fvEl('span','fv-chip',fmtB(d.size)));box.appendChild(meta);
          const raw=()=>{const pre=fvEl('pre','fv-rawjson');pre.textContent=JSON.stringify(parsed,null,2);const w=fvEl('div','fv-code');w.appendChild(pre);return w};
          fvModes(box,[{label:'Tree',node:()=>fvJsonTree(parsed)},{label:'Formatted',node:raw}]);
          const mf=document.getElementById('mdlF');const eb=document.createElement('button');eb.className='btn btn-g';eb.textContent='✏ Edit';eb.addEventListener('click',()=>editFile(path,name));mf.insertBefore(eb,mf.firstChild);return;
        }
      }
      if(!d.truncated&&['csv','tsv'].includes(d.ext||'')){
        const rows=fvParseCsv(d.content,d.ext==='tsv'?'\t':',',301,30);
        if(rows.length>1){
          const box=document.getElementById('mdlB');box.replaceChildren();
          const meta=fvEl('div','fv-meta');meta.appendChild(fvEl('span','fv-chip',d.ext));meta.appendChild(fvEl('span','fv-chip',fmtB(d.size)));
          if(rows.length>300)meta.appendChild(fvEl('span','fv-chip warn','⚠ first 300 rows'));box.appendChild(meta);
          const rawv=()=>{const pre=fvEl('pre','fv-rawjson');pre.textContent=d.content;const w=fvEl('div','fv-code');w.appendChild(pre);return w};
          fvModes(box,[{label:'Table',node:()=>fvTable(rows.slice(0,300),true)},{label:'Raw',node:rawv}]);
          const mf=document.getElementById('mdlF');const eb=document.createElement('button');eb.className='btn btn-g';eb.textContent='✏ Edit';eb.addEventListener('click',()=>editFile(path,name));mf.insertBefore(eb,mf.firstChild);return;
        }
      }
      const isMarkdown=['md','markdown'].includes(d.ext||'');let content='';
      if(isMarkdown&&typeof marked!=='undefined'){content=`<div style="padding:12px 2px;line-height:1.75;font-size:14px;color:var(--tx)">${marked.parse(d.content)}</div>`}
      else{let hi=esc(d.content);if(typeof hljs!=='undefined'){try{hi=hljs.highlight(d.content,{language:d.ext||'plaintext',ignoreIllegals:true}).value}catch(_){try{hi=hljs.highlightAuto(d.content).value}catch(_2){}}}
        content=`<div class="fv-code"><pre style="padding:14px;margin:0;overflow:auto"><code class="hljs">${hi}</code></pre></div>`}
      document.getElementById('mdlB').innerHTML=`<div class="fv-meta"><span class="fv-chip">${esc(d.ext||'text')}</span><span class="fv-chip">${fmtB(d.size)}</span>${d.truncated?'<span class="fv-chip warn">\u26a0 Truncated</span>':''}</div>${content}`;
      const mf=document.getElementById('mdlF');const eb=document.createElement('button');eb.className='btn btn-g';eb.textContent='\u270F Edit';eb.addEventListener('click',()=>editFile(path,name));mf.insertBefore(eb,mf.firstChild);return;
    }
    if(d.type==='archive'){document.getElementById('mdlB').innerHTML=`<div class="fv-meta"><span class="fv-chip">archive</span><span class="fv-chip">${fmtB(d.size)}</span><span class="fv-chip">${(d.contents||[]).length} files</span></div>${(d.contents||[]).length?`<ul class="fv-zip">${d.contents.map(f=>`<li>${esc(f)}</li>`).join('')}</ul>`:'<div style="color:var(--td)">Cannot list this archive type</div>'}`;return}
    if(d.type==='binary'){document.getElementById('mdlB').innerHTML=`<div class="fv-meta"><span class="fv-chip">binary</span><span class="fv-chip">${fmtB(d.size)}</span><span class="fv-chip">${esc(d.mime)}</span></div>${d.hex?`<div class="fv-hex">${esc(d.hex)}</div>`:''}`;return}
  }catch(e){document.getElementById('mdlB').innerHTML=`<div style="color:var(--rd)">Failed: ${esc(e.message)}</div>`}
}
async function editFile(path,name){
  openMdl(`Edit \u2014 ${name}`,`<div class="loading"><div class="loader"></div></div>`,'',true);
  try{
    const d=await(await api('/api/file?path='+encodeURIComponent(path))).json();
    if(d.error){document.getElementById('mdlB').innerHTML=`<div style="color:var(--rd)">${esc(d.error)}</div>`;return}
    document.getElementById('mdlB').innerHTML=`<div style="font-size:11px;color:var(--td);margin-bottom:8px">${fmtB(d.size)} \u00b7 ${esc(d.ext||'text')}</div>
      <textarea class="edit-ta" id="editTA" spellcheck="false">${esc(d.content)}</textarea>`;
    document.getElementById('mdlF').innerHTML=`<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-p" id="saveEdit">\u{1F4BE} Save</button>`;
    document.getElementById('saveEdit')?.addEventListener('click',async()=>{
      const r=await(await api('/api/file/edit',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({path,content:document.getElementById('editTA').value})})).json();
      if(r.ok){toast('Saved','success');closeMdl()}else errToast(r,'Save failed')});
  }catch(e){document.getElementById('mdlB').innerHTML=`<div style="color:var(--rd)">${esc(e.message)}</div>`}
}
function showShareDlg(item){
  openMdl('Get Share Link',`
    <div class="fg"><label class="fgl">Sharing</label>
      <div style="font-family:var(--f-mono);font-size:12.5px;color:var(--td);word-break:break-all">${esc(item.path)}</div></div>
    <div class="fg"><label class="fgl">Expires</label>
      <select class="fgi" id="shExp">
        <option value="1">1 hour</option>
        <option value="24" selected>1 day</option>
        <option value="168">7 days</option>
        <option value="720">30 days</option>
        <option value="0">Never</option>
      </select></div>
    <div class="fg"><label class="fgl">Protect with (optional)</label>
      <div class="seg" id="shModeSeg">
        <button class="seg-btn active" data-mode="password" type="button">Password</button>
        <button class="seg-btn" data-mode="pin" type="button">PIN</button>
      </div>
    </div>
    <div class="fg"><label class="fgl" id="shPwLabel">Password</label>
      <input class="fgi" id="shPw" type="password" placeholder="Leave blank for no password"/></div>
    <div class="sdesc">Anyone with this link can view or download this ${item.type==='dir'?'folder':'file'} — no Void OS
      account needed. Only items inside Shared can be linked this way.</div>
    <div id="shResult" style="display:none;margin-top:14px;padding-top:14px;border-top:1px solid var(--bd)">
      <label class="fgl">Link</label>
      <div style="display:flex;gap:8px">
        <input class="fgi" id="shUrl" readonly style="flex:1;font-family:var(--f-mono);font-size:12px" onclick="this.select()"/>
        <button class="btn btn-g btn-sm" id="shCopyBtn">Copy</button>
      </div>
    </div>`,
    `<button class="btn btn-g" onclick="closeMdl()">Close</button><button class="btn btn-p" id="shCreateBtn">Create Link</button>`);
  enhanceSelect(document.getElementById('shExp'));
  let shMode='password';
  const shPw=document.getElementById('shPw'),shPwLabel=document.getElementById('shPwLabel');
  document.getElementById('shModeSeg')?.addEventListener('click',e=>{
    const b=e.target.closest('.seg-btn'); if(!b)return;
    shMode=b.dataset.mode;
    document.querySelectorAll('#shModeSeg .seg-btn').forEach(x=>x.classList.toggle('active',x===b));
    if(shMode==='pin'){
      shPwLabel.textContent='PIN'; shPw.type='text'; shPw.inputMode='numeric'; shPw.maxLength=6;
      shPw.placeholder='Leave blank for no PIN (6 digits)';
    }else{
      shPwLabel.textContent='Password'; shPw.type='password'; shPw.removeAttribute('inputmode'); shPw.removeAttribute('maxlength');
      shPw.placeholder='Leave blank for no password';
    }
  });
  document.getElementById('shCreateBtn')?.addEventListener('click',async()=>{
    const hours=Number(document.getElementById('shExp').value),pw=shPw.value;
    if(shMode==='pin'&&pw&&!/^\d{6}$/.test(pw)){toast('A PIN must be exactly 6 digits','error');return}
    const r=await api('/api/shares',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path:item.path,expires_in_hours:hours||null,password:pw||undefined,mode:pw?shMode:undefined})});
    const d=await r.json();
    if(d.error){errToast(d);return}
    const full=`${location.origin}${d.url}`;
    document.getElementById('shUrl').value=full;
    document.getElementById('shResult').style.display='block';
    toast('Share link created','success');
  });
  document.getElementById('shCopyBtn')?.addEventListener('click',async()=>{
    const url=document.getElementById('shUrl').value;
    try{
      if(navigator.clipboard&&window.isSecureContext)await navigator.clipboard.writeText(url);
      else{const ta=document.createElement('textarea');ta.value=url;ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove()}
      toast('Link copied','success');
    }catch(e){toast('Could not copy — select and copy manually','error')}
  });
}
window.showShareDlg=showShareDlg;
window.rFiles=rFiles;window.clearSel=clearSel;window.doPaste=doPaste;window.downloadSel=downloadSel;window.deleteSel=deleteSel;

// ── TERMINAL ─────────────────────────────────────────────────────
async function rTerm(){
  const root=document.getElementById('vroot');
  root.innerHTML=`<div class="loading"><div class="loader"></div></div>`;
  let locked=false;
  try{
    const lr=await api('/api/terminal/lock-status');
    const st=await lr.json();
    if(!lr.ok){root.innerHTML=`<div class="empty-dir">Couldn’t open the Terminal: ${esc(st.error)}</div>`;return}
    locked=!!st.locked;
  }catch(e){}
  if(locked){renderTermLock();return}
  renderTermView();
}
function renderTermLock(){
  const root=document.getElementById('vroot');
  root.innerHTML=`<div class="vh"><div><div class="vtitle">Terminal</div><div class="vsub">Password-protected</div></div></div>
    <div class="term-lock-wrap"><div class="card term-lock">
      <div style="font-size:32px">\u{1F512}</div>
      <div style="font-weight:600;font-size:14px">This terminal is locked</div>
      <div class="sdesc">Enter the terminal password to open a shell. This is separate from your account password.</div>
      <input class="fgi" type="password" id="termUnlockPw" placeholder="Terminal password" style="width:100%" autofocus/>
      <div id="termUnlockErr" style="color:var(--rd);font-size:12px;min-height:16px"></div>
      <button class="btn btn-p" id="termUnlockBtn" style="width:100%">Unlock</button>
    </div></div>`;
  const pwInp=document.getElementById('termUnlockPw'),errEl=document.getElementById('termUnlockErr'),btn=document.getElementById('termUnlockBtn');
  async function tryUnlock(){
    const pw=pwInp.value;if(!pw)return;
    btn.disabled=true;errEl.textContent='';
    try{
      const r=await api('/api/terminal/unlock',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
      const d=await r.json();
      if(d.ok)renderTermView();
      else{errEl.textContent=d.error==='locked'?`Too many attempts. Try again in ${d.retry_after||30}s.`:'That password isn\u2019t right.';btn.disabled=false;pwInp.value='';pwInp.focus()}
    }catch(e){errEl.textContent='Can\u2019t reach the server. Check the connection and try again.';btn.disabled=false}
  }
  btn.addEventListener('click',tryUnlock);
  pwInp.addEventListener('keydown',e=>{if(e.key==='Enter')tryUnlock()});
}
function renderTermView(){
  const root=document.getElementById('vroot');
  _termAccessRevoked=false;
  root.innerHTML=`<div class="vh"><div><div class="vtitle">Terminal</div><div class="vsub" id="termSub">Loading\u2026</div></div>
    <div style="display:flex;gap:8px">
      <button class="tb" id="termClr"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg> Clear</button>
      <button class="tb" id="termRec"><svg viewBox="0 0 24 24"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 102.13-9.36L1 10"/></svg> Restart</button>
    </div></div>
  <div class="term-view"><div class="term-tabs" id="termTabs" role="tablist" aria-label="Terminal sessions"></div>
    <div class="term-wrap"><div class="term-stack" id="termStack"></div></div></div>`;
  const stack=document.getElementById('termStack');
  // Sessions outlive the view: coming back re-attaches their (still connected) terminals.
  TERMS.list.forEach(s=>stack.appendChild(s.el));
  document.getElementById('termTabs').addEventListener('click',e=>{
    const x=e.target.closest('[data-close]');
    if(x){e.stopPropagation();termClose(+x.dataset.close);return}
    if(e.target.closest('#termAdd')){termOpen();return}
    const t=e.target.closest('[data-tid]');if(t)termSelect(+t.dataset.tid);
  });
  document.getElementById('termTabs').addEventListener('dblclick',e=>{
    const t=e.target.closest('[data-tid]');if(!t)return;
    const s=TERMS.list.find(x=>x.id===+t.dataset.tid);if(!s)return;
    const n=prompt('Name this terminal',s.title);
    if(n&&n.trim()){s.title=n.trim().slice(0,24);termPaintTabs()}
  });
  document.getElementById('termClr')?.addEventListener('click',()=>{const s=termActive();if(s&&s.term)s.term.clear()});
  document.getElementById('termRec')?.addEventListener('click',()=>{const s=termActive();if(s)termConnect(s,true)});
  TERMS.obs=new ResizeObserver(()=>termFit(termActive()));
  TERMS.obs.observe(stack);
  if(TERMS.list.length){
    if(!termActive())TERMS.active=TERMS.list[0].id;
    termSelect(TERMS.active);
  }else termOpen();
}
// ── terminal sessions ───────────────────────────────────────────────────
// Several shells, one tab each. They keep running when you open another page and come back;
// closing a tab (or the browser) ends that shell. The server caps how many one account may have.
const TERMS={list:[],active:null,seq:0,obs:null};
function termActive(){return TERMS.list.find(s=>s.id===TERMS.active)||null}
function termStatus(s,text,state){
  s.status=text;s.state=state||'';termPaintTabs();
  if(TERMS.active===s.id){const el=document.getElementById('termSub');if(el)el.textContent=text}
}
function termPaintTabs(){
  const bar=document.getElementById('termTabs');if(!bar)return;
  bar.innerHTML=TERMS.list.map(s=>`<div class="term-tab${s.id===TERMS.active?' active':''}" role="tab" tabindex="0" data-tid="${s.id}" aria-selected="${s.id===TERMS.active}" title="Double-click to rename">
      <span class="tt-dot ${esc(s.state)}"></span><span class="tt-name">${esc(s.title)}</span>
      <button class="tt-x" data-close="${s.id}" aria-label="Close ${esc(s.title)}">\u00D7</button></div>`).join('')
    +`<button class="term-add" id="termAdd" title="New terminal" aria-label="New terminal">+</button>`;
  bar.querySelectorAll('.term-tab').forEach(t=>t.addEventListener('keydown',e=>{
    if(e.key==='Enter'||e.key===' '){e.preventDefault();termSelect(+t.dataset.tid)}}));
}
function termFit(s){
  if(!s||!s.fit||!s.el||s.el.style.display==='none')return;
  try{s.fit.fit();if(s.ws&&s.ws.readyState===WebSocket.OPEN)s.ws.send(JSON.stringify({t:'r',r:s.term.rows,c:s.term.cols}))}catch(_){}
}
function termSelect(id){
  TERMS.active=id;
  TERMS.list.forEach(s=>{s.el.style.display=s.id===id?'':'none'});
  termPaintTabs();
  const s=termActive();if(!s)return;
  const sub=document.getElementById('termSub');if(sub)sub.textContent=s.status||'';
  requestAnimationFrame(()=>{termFit(s);try{s.term.focus()}catch(_){}});
}
function termClose(id){
  const i=TERMS.list.findIndex(s=>s.id===id);if(i<0)return;
  const s=TERMS.list[i];
  s.closed=true;
  if(s.ws){try{s.ws.close()}catch(_){}}
  try{s.term&&s.term.dispose()}catch(_){}
  s.el.remove();TERMS.list.splice(i,1);
  if(!TERMS.list.length){TERMS.active=null;termOpen();return}
  if(TERMS.active===id)termSelect(TERMS.list[Math.min(i,TERMS.list.length-1)].id);else termPaintTabs();
}
function termConnect(s,fresh){
  if(s.ws){s.ws.onclose=null;s.ws.onerror=null;try{s.ws.close()}catch(_){}}
  if(fresh)s.term.reset();
  termStatus(s,'Connecting\u2026','');
  const proto=location.protocol==='https:'?'wss:':'ws:';
  // Same-origin connection: works through Cloudflare Tunnel, reverse proxies, and any other
  // single-port setup, since the terminal is served on the same host:port as the dashboard.
  const ws=new WebSocket(`${proto}//${location.host}/terminal`);
  s.ws=ws;
  ws.onopen=()=>{termStatus(s,'Connected','on');termFit(s);ws.send(JSON.stringify({t:'r',r:s.term.rows,c:s.term.cols}))};
  ws.onmessage=e=>s.term.write(e.data);
  ws.onclose=()=>{
    if(s.closed||s.ws!==ws)return;
    if(_termAccessRevoked){s.term.write('\r\n\x1b[31m[terminal access was revoked \u2014 session ended]\x1b[0m\r\n');termStatus(s,'Access revoked','off');return}
    s.term.write('\r\n\x1b[33m[session ended \u2014 click Restart to reconnect]\x1b[0m\r\n');termStatus(s,'Disconnected','off');
  };
  ws.onerror=()=>{if(s.closed||s.ws!==ws)return;s.term.write('\r\n\x1b[31m[WebSocket connection error]\x1b[0m\r\n');termStatus(s,'Error','off')};
}
async function termOpen(){
  const gen=_renderGen; // never set up a live terminal for a view the user already left
  const subEl=document.getElementById('termSub');
  if(!TERMEN){if(subEl)subEl.textContent='Terminal disabled';return}
  try{await loadScript('/static/vendor/terminal.js')}
  catch(e){if(subEl)subEl.textContent='Failed to load xterm.js';return}
  if(gen!==_renderGen)return;
  if(typeof Terminal==='undefined'){if(subEl)subEl.textContent='The terminal component didn\u2019t load \u2014 reload the page';return}
  const stack=document.getElementById('termStack');if(!stack)return;
  let n=1;const used=new Set(TERMS.list.map(s=>s.title));
  while(used.has('Shell '+n))n++;
  const s={id:++TERMS.seq,title:'Shell '+n,status:'Connecting\u2026',state:'',term:null,fit:null,ws:null,closed:false,el:document.createElement('div')};
  s.el.className='term-inner';
  stack.appendChild(s.el);
  TERMS.list.push(s);
  TERMS.active=s.id;
  TERMS.list.forEach(x=>{x.el.style.display=x.id===s.id?'':'none'});
  termPaintTabs();
  s.term=new Terminal({cursorBlink:true,cursorStyle:'block',fontSize:14,lineHeight:1.2,scrollback:5000,
    fontFamily:'"JetBrains Mono","Cascadia Code","Fira Code",ui-monospace,monospace',allowTransparency:false,
    theme:{background:'#0d1117',foreground:'#c9d1d9',cursor:'#58a6ff',cursorAccent:'#0d1117',
      selectionBackground:'rgba(110,118,129,0.4)',
      black:'#484f58',red:'#ff7b72',green:'#3fb950',yellow:'#d29922',blue:'#58a6ff',
      magenta:'#bc8cff',cyan:'#39c5cf',white:'#b1bac4',
      brightBlack:'#6e7681',brightRed:'#ffa198',brightGreen:'#56d364',brightYellow:'#e3b341',
      brightBlue:'#79c0ff',brightMagenta:'#d2a8ff',brightCyan:'#56d4dd',brightWhite:'#f0f6fc'}});
  s.fit=new FitAddon.FitAddon();s.term.loadAddon(s.fit);
  s.term.open(s.el);
  s.term.onData(d=>{if(s.ws&&s.ws.readyState===WebSocket.OPEN)s.ws.send(JSON.stringify({t:'i',d}))});
  await new Promise(r=>{requestAnimationFrame(()=>requestAnimationFrame(r));setTimeout(r,120)});   // the timer covers a hidden page, where frames don't run
  if(s.closed)return;
  try{s.fit.fit()}catch(_){}
  termConnect(s);
  try{s.term.focus()}catch(_){}
}
function cleanupTerm(){
  // Leaving the page: stop resizing, but keep the shells running for when you come back.
  if(TERMS.obs){TERMS.obs.disconnect();TERMS.obs=null}
}

// ══════════════════════════════════════════════════════════════════════
// INTERCOM — talk to, and listen through, the server's speakers and microphones, by room.
// Server side: modules/intercom.py (protocol in its docstring). Audio runs in the worklets in
// static/intercom-worklet.js: 24 kHz mono mu-law, 20 ms frames, a small jitter buffer.
// The page connects as soon as it opens; there is no "start a call" step. Rooms are chosen once
// by an admin, everyone else just presses Talk.
// ══════════════════════════════════════════════════════════════════════
const IX={ws:null,ctx:null,master:null,micStream:null,micSrc:null,micNode:null,micGain:null,
  players:new Map(),rooms:[],devices:[],admin:false,me:'',engine:null,health:null,btList:null,
  listen:new Set(),target:new Set(),mode:'hold',vol:1,
  talking:false,granted:[],force:false,micPerm:'idle',micLevel:0,
  retry:0,retryT:null,pingT:null,rtt:null,dead:true,live:false,fatal:'',meterRaf:0,lastPaint:0};
try{
  const p=JSON.parse(localStorage.getItem('void-ix')||'{}');
  if(Array.isArray(p.listen))IX.listen=new Set(p.listen);
  if(Array.isArray(p.target))IX.target=new Set(p.target);
  if(p.mode==='toggle')IX.mode='toggle';
  if(typeof p.vol==='number')IX.vol=Math.max(0,Math.min(1.5,p.vol));
}catch(_){}
function ixSavePrefs(){
  try{localStorage.setItem('void-ix',JSON.stringify({listen:[...IX.listen],target:[...IX.target],mode:IX.mode,vol:IX.vol}))}catch(_){}
}
const _IX_ICO={
  mic:'<svg viewBox="0 0 24 24"><path d="M12 2a3 3 0 00-3 3v7a3 3 0 006 0V5a3 3 0 00-3-3z"/><path d="M19 11v1a7 7 0 01-14 0v-1"/><path d="M12 19v3"/></svg>',
  ear:'<svg viewBox="0 0 24 24"><path d="M3 14v-2a9 9 0 0118 0v2"/><path d="M21 15a2 2 0 01-2 2h-1v-5h1a2 2 0 012 2zM3 15a2 2 0 002 2h1v-5H5a2 2 0 00-2 2z"/></svg>',
  dots:'<svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>',
  bt:'<svg viewBox="0 0 24 24"><path d="M6.5 7.5l11 9L12 21V3l5.5 4.5-11 9"/></svg>',
  plus:'<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  spk:'<svg viewBox="0 0 24 24"><path d="M11 5L6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 010 7"/></svg>',
};

// ── teardown ──────────────────────────────────────────────────────
function ixStopMic(){
  try{IX.micNode&&IX.micNode.disconnect()}catch(_){}
  try{IX.micSrc&&IX.micSrc.disconnect()}catch(_){}
  try{IX.micGain&&IX.micGain.disconnect()}catch(_){}
  if(IX.micStream)IX.micStream.getTracks().forEach(t=>t.stop());
  IX.micNode=IX.micSrc=IX.micGain=IX.micStream=null;
}
function cleanupIntercom(silent){
  IX.dead=true;IX.live=false;
  clearTimeout(IX.retryT);clearInterval(IX.pingT);IX.retryT=IX.pingT=null;
  if(IX.meterRaf){cancelAnimationFrame(IX.meterRaf);IX.meterRaf=0}
  if(IX.ws){try{IX.ws.onclose=null;IX.ws.onmessage=null;IX.ws.close()}catch(_){}IX.ws=null}
  ixStopMic();
  IX.players.forEach(p=>{try{p.node.disconnect();p.gain.disconnect()}catch(_){}});
  IX.players.clear();
  if(IX.ctx){try{IX.ctx.close()}catch(_){}IX.ctx=null;IX.master=null}
  IX.talking=false;IX.granted=[];IX.rtt=null;IX.micLevel=0;
  if(!silent)ixPaintStatus();
}

// ── audio graph ───────────────────────────────────────────────────
async function ixEnsureAudio(){
  if(IX.ctx&&IX.ctx.state!=='closed'){
    if(IX.ctx.state==='suspended')try{await IX.ctx.resume()}catch(_){}
    return IX.ctx;
  }
  const AC=window.AudioContext||window.webkitAudioContext;
  if(!AC||!window.AudioWorkletNode)throw new Error(window.isSecureContext?'This browser can’t play live audio (no AudioWorklet).':'Live audio needs HTTPS (or localhost). Open Void OS through its https address, such as your tunnel link.');
  const ctx=new AC({latencyHint:'interactive'});
  if(!ctx.audioWorklet){try{ctx.close()}catch(_){}throw new Error('Live audio needs HTTPS (or localhost). Open Void OS through its https address, such as your tunnel link.')}
  await ctx.audioWorklet.addModule('/static/intercom-worklet.js?v=3');
  IX.ctx=ctx;IX.master=ctx.createGain();IX.master.gain.value=IX.vol;IX.master.connect(ctx.destination);
  if(ctx.state==='suspended')try{await ctx.resume()}catch(_){}
  return ctx;
}
function ixPlayer(roomId){
  let p=IX.players.get(roomId);
  if(p||!IX.ctx)return p;
  const node=new AudioWorkletNode(IX.ctx,'ic-player',{numberOfInputs:0,numberOfOutputs:1,outputChannelCount:[1]});
  const gain=IX.ctx.createGain();
  node.connect(gain);gain.connect(IX.master);
  p={node,gain,level:0,buf:0,at:0};
  node.port.onmessage=e=>{if(e.data.buf!=null)p.buf=e.data.buf};
  IX.players.set(roomId,p);
  return p;
}
function ixDropPlayer(roomId){
  const p=IX.players.get(roomId);if(!p)return;
  try{p.node.disconnect();p.gain.disconnect()}catch(_){}
  IX.players.delete(roomId);
}
// Half duplex: while you talk into a room, its own microphone is muted for you (no echo of yourself).
function ixApplyMutes(){
  IX.players.forEach((p,id)=>{p.gain.gain.value=(IX.talking&&IX.granted.includes(id))?0:1});
}
function ixMicError(e){
  const n=e&&e.name;
  if(n==='NotAllowedError'||n==='PermissionDeniedError')
    return 'The microphone is blocked. Tap the lock icon next to the address, allow the microphone for this site, then try again. (On iPhone: Settings → Safari → Microphone.)';
  if(n==='NotFoundError'||n==='DevicesNotFoundError')return 'No microphone was found on this device. Plug one in or check that it is enabled.';
  if(n==='NotReadableError'||n==='TrackStartError')
    return 'Your microphone couldn’t be opened. Another app (a call, a recorder) may be using it, or it was just unplugged. Close the other app and try again.';
  if(n==='SecurityError')return 'The browser blocked microphone access because this page isn’t secure. Open Void OS through its https address.';
  if(n==='AbortError')return 'Starting the microphone was interrupted. Try again.';
  if(n==='TypeError')return 'This browser can’t capture a microphone on this page. Use an up-to-date Chrome, Safari or Firefox over https.';
  return 'Couldn’t start the microphone'+(n?` (${n})`:'')+(e&&e.message?': '+String(e.message).slice(0,120):'')+'.';
}
async function ixEnsureMic(){
  if(IX.micNode)return true;
  if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia||!window.isSecureContext){
    IX.micPerm='unsupported';ixPaintDock();
    toast(window.isSecureContext?'This browser can’t use a microphone here. Try an up-to-date Chrome, Safari or Firefox.':'The microphone only works on https pages (or localhost). Open Void OS through its https address.','error');
    return false;
  }
  try{await ixEnsureAudio()}catch(e){IX.micPerm='unsupported';toast(e.message,'error');ixPaintDock();return false}
  try{
    try{
      IX.micStream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
    }catch(e){
      if(e&&e.name==='OverconstrainedError')IX.micStream=await navigator.mediaDevices.getUserMedia({audio:true});   // some phones refuse the options
      else throw e;
    }
  }catch(e){
    IX.micPerm=(e&&(e.name==='NotAllowedError'||e.name==='PermissionDeniedError'))?'denied':'error';
    toast(ixMicError(e),'error');ixPaintDock();return false;
  }
  IX.micPerm='granted';
  try{
    IX.micSrc=IX.ctx.createMediaStreamSource(IX.micStream);
    IX.micNode=new AudioWorkletNode(IX.ctx,'ic-mic',{numberOfInputs:1,numberOfOutputs:1});
  }catch(e){
    ixStopMic();IX.micPerm='error';
    toast('The microphone started but the page couldn’t process its sound ('+(e&&e.name||'error')+'). Reload the page; if it keeps happening, try another browser.','error');
    ixPaintDock();return false;
  }
  IX.micGain=IX.ctx.createGain();IX.micGain.gain.value=0;       // keeps the graph running without feeding the speakers
  IX.micSrc.connect(IX.micNode);IX.micNode.connect(IX.micGain);IX.micGain.connect(IX.ctx.destination);
  IX.micNode.port.onmessage=e=>{
    const d=e.data;IX.micLevel=d.level||0;
    if(d.frame&&IX.ws&&IX.ws.readyState===1&&IX.ws.bufferedAmount<64000)IX.ws.send(d.frame);
  };
  ixPaintDock();
  return true;
}

// ── connection ────────────────────────────────────────────────────
function ixSend(o){if(IX.ws&&IX.ws.readyState===1)IX.ws.send(JSON.stringify(o))}
function ixConnect(){
  if(IX.dead||IX.ws)return;
  const proto=location.protocol==='https:'?'wss:':'ws:';
  const ws=new WebSocket(`${proto}//${location.host}/ws/intercom`);
  ws.binaryType='arraybuffer';
  IX.ws=ws;
  ws.onopen=()=>{IX.retry=0;IX.failTold=false;IX.live=true;ixPaintStatus();
    IX.pingT=setInterval(()=>ixSend({t:'ping',ts:performance.now()}),5000)};
  ws.onmessage=e=>{
    if(typeof e.data==='string'){let m;try{m=JSON.parse(e.data)}catch(_){return}ixOnJson(m);return}
    try{
      const u=new Uint8Array(e.data);
      if(u.length<3||u[0]!==2)return;
      const room=IX.rooms.find(r=>r.n===u[1]);if(!room)return;
      const p=IX.players.get(room.id);if(!p)return;
      let pk=0;for(let i=2;i<u.length;i++){const v=(~u[i])&0x7F;if(v>pk)pk=v}   // mu-law magnitude grows with level
      p.level=Math.max(pk/127,p.level*.85);p.at=performance.now();
      p.node.port.postMessage({ulaw:u.slice(2)});
    }catch(_){}
  };
  ws.onclose=()=>{
    if(IX.ws!==ws)return;
    IX.ws=null;IX.live=false;clearInterval(IX.pingT);IX.pingT=null;IX.rtt=null;
    if(IX.talking)ixTalkStop(true);
    if(IX.retry===3&&!IX.failTold){
      IX.failTold=true;
      toast(navigator.onLine===false
        ?'You appear to be offline. The intercom reconnects by itself when the network is back.'
        :'The live connection keeps failing. Check that Void OS is running and reachable. If you open it through another address that forwards to the computer, that address has to be listed in VOID_ALLOWED_ORIGINS in its .env file (the activity log shows “cross_site_blocked”).','error');
    }
    ixPaintStatus();
    if(IX.dead||IX.fatal)return;
    const wait=Math.min(10000,1000*Math.pow(2,IX.retry++));
    IX.retryT=setTimeout(ixConnect,wait);
  };
  ws.onerror=()=>{};
}
function ixOnJson(m){
  if(m.t==='hello'||m.t==='state'){
    IX.rooms=m.rooms||[];
    if(m.you){IX.me=m.you.username||'';IX.admin=!!m.you.admin||IX.admin}
    // listen to what this person was listening to, if those rooms still exist and have a microphone
    if(m.t==='hello'){
      [...IX.listen].forEach(id=>{if(!IX.rooms.some(r=>r.id===id&&r.has_input))IX.listen.delete(id)});
      [...IX.target].forEach(id=>{if(!IX.rooms.some(r=>r.id===id&&r.has_output))IX.target.delete(id)});
      if(!IX.target.size){const c=IX.rooms.filter(r=>r.has_output);const b=c.find(r=>r.builtin);if(b||c.length===1)IX.target.add((b||c[0]).id)}   // nothing chosen yet: This computer, or the only room
      ixSyncListen(true);
    }
    ixPaintRooms();ixPaintDock();
  }else if(m.t==='floor'){
    IX.granted=m.granted||[];
    ixApplyMutes();
    if((m.denied||[]).length){
      const d=m.denied[0],name=(IX.rooms.find(r=>r.id===d.room)||{}).name||'That room';
      toast(d.by?`${name} is in use: ${d.by} is talking.`:`${name}: ${d.reason||'can’t talk there'}`,'warning',
        IX.admin&&d.by?{action:{label:'Take over',fn:()=>{IX.force=true;ixTalkStart().finally(()=>{IX.force=false})}}}:{});
    }
    if(IX.talking&&!IX.granted.length)ixTalkStop(true);
    ixPaintRooms();ixPaintDock();
  }else if(m.t==='notice'){
    toast(m.message||'',m.level==='error'?'error':(m.level==='info'?'info':'warning'));
    if(m.fatal){IX.fatal=m.message||'Intercom unavailable';ixPaintStatus();ixLoadHealth()}
  }else if(m.t==='pong'){
    if(typeof m.ts==='number'){IX.rtt=Math.round(performance.now()-m.ts);ixPaintStatus()}
  }
}
function ixSyncListen(initial){
  // keep players in step with the listen set, then tell the server
  const ids=[...IX.listen];
  ixSend({t:'listen',rooms:ids});
  IX.players.forEach((_,id)=>{if(!IX.listen.has(id))ixDropPlayer(id)});
  if(ids.length)ixEnsureAudio().then(()=>{ids.forEach(id=>ixPlayer(id));ixApplyMutes()}).catch(e=>{if(!initial)toast(e.message,'error')});
}

// ── talking ───────────────────────────────────────────────────────
async function ixTalkStart(){
  try{await ixTalkStart2()}
  catch(e){
    IX.talking=false;IX.granted=[];ixPaintDock();ixPaintRooms();
    toast('Couldn’t start talking: '+((e&&e.message)||'unexpected problem')+'. Reload the page and try again.','error');
  }
}
async function ixTalkStart2(){
  if(IX.talking)return;
  if(!IX.target.size){toast('Pick a room to talk to first.','info');return}
  if(!IX.live){toast('Not connected yet.','warning');return}
  if(!await ixEnsureMic())return;
  IX.talking=true;
  IX.micNode.port.postMessage({on:true});          // frames are ignored by the server until the floor is granted
  ixSend({t:'talk',rooms:[...IX.target],force:!!IX.force});
  ixPaintDock();ixPaintRooms();
}
function ixTalkStop(local){
  if(!IX.talking&&!IX.granted.length)return;
  IX.talking=false;
  if(IX.micNode)IX.micNode.port.postMessage({on:false});
  if(!local)ixSend({t:'talk_end'});
  IX.granted=[];ixApplyMutes();
  ixPaintDock();ixPaintRooms();
}

// ── drawing ───────────────────────────────────────────────────────
function ixPaintStatus(){
  const el=document.getElementById('ixStatus');if(!el)return;
  let cls='off',txt='Connecting…';
  if(IX.fatal){cls='bad';txt='Unavailable'}
  else if(IX.live){cls='on';txt=IX.rtt!=null?`Live · ${IX.rtt} ms`:'Live'}
  else if(IX.retry>=3){cls='bad';txt='Can’t connect'}
  else if(IX.retry>0){cls='warn';txt='Reconnecting…'}
  el.className='ix-status '+cls;
  el.innerHTML=`<i></i>${esc(txt)}`;
}
function ixRoomHtml(r){
  const sel=IX.target.has(r.id),lis=IX.listen.has(r.id);
  const mine=IX.granted.includes(r.id)&&IX.talking;
  const other=r.talking_by&&r.talking_by!==IX.me&&!mine;
  const chips=[];
  if(mine)chips.push('<span class="ix-tag live">You are talking</span>');
  else if(r.talking_by)chips.push(`<span class="ix-tag live">${esc(r.talking_by)} is talking</span>`);
  if(r.has_output&&r.output_ok===false)chips.push('<span class="ix-tag bad">Speaker offline</span>');
  if(r.has_input&&r.input_ok===false)chips.push(`<span class="ix-tag bad">${r.builtin?'No microphone on the server':'Microphone offline'}</span>`);
  if(!r.has_output)chips.push('<span class="ix-tag">Listen only</span>');
  else if(!r.has_input)chips.push('<span class="ix-tag">Talk only</span>');
  if(r.listeners)chips.push(`<span class="ix-tag">${r.listeners} listening</span>`);
  return `<div class="ix-room${sel?' sel':''}${mine?' mine':''}${other?' busy':''}" data-id="${escA(r.id)}">
    <div class="ix-room-top"><span class="ix-emoji">${esc(r.icon||'\u{1F50A}')}</span>
      <div class="ix-room-name">${esc(r.name)}</div>
      ${IX.admin&&!r.builtin?`<button class="ix-ibtn" data-act="menu" title="Edit this room" aria-label="Edit ${escA(r.name)}">${_IX_ICO.dots}</button>`:''}</div>
    <div class="ix-tags">${chips.join('')||'<span class="ix-tag quiet">Quiet</span>'}</div>
    <div class="ix-meter" title="What this room's microphone hears"><i></i></div>
    <div class="ix-room-act">
      <button class="ix-chip${lis?' on':''}" data-act="listen" ${r.has_input?'':'disabled'}>${_IX_ICO.ear}<span>Listen</span></button>
      <button class="ix-chip${sel?' on':''}" data-act="target" ${r.has_output?'':'disabled'}>${_IX_ICO.mic}<span>Talk here</span></button>
    </div></div>`;
}
function ixPaintRooms(){
  const g=document.getElementById('ixRooms');if(!g)return;
  if(!IX.rooms.length){
    g.innerHTML=`<div class="ix-empty"><div class="ix-empty-ico">\u{1F399}️</div>
      <div class="ix-empty-t">No rooms yet</div>
      <div class="sdesc">${IX.admin?'A room is a speaker, a microphone, or both, with a name, such as “Kitchen”. Add one to start.'
        :'An admin needs to add a room before the intercom can be used.'}</div>
      ${IX.admin?`<button class="btn btn-p btn-sm" data-act="add">${_IX_ICO.plus} Add a room</button>`:''}</div>`;
    return;
  }
  g.innerHTML=IX.rooms.map(ixRoomHtml).join('')+(IX.admin?
    `<button class="ix-room ix-add" data-act="add"><span class="ix-add-ico">${_IX_ICO.plus}</span><span>Add a room</span></button>`:'');
}
function ixPaintDock(){
  const lbl=document.getElementById('ixTarget'),sub=document.getElementById('ixSub'),btn=document.getElementById('ixPtt');
  if(!lbl||!btn)return;
  const names=[...IX.target].map(id=>(IX.rooms.find(r=>r.id===id)||{}).name).filter(Boolean);
  lbl.textContent=names.length?`Talking to ${names.join(', ')}`:'Pick a room to talk to';
  let s=IX.mode==='hold'?'Hold the button, or hold Space':'Tap to start, tap again to stop';
  if(IX.micPerm==='denied')s='Microphone blocked: allow it in your browser’s site settings';
  else if(IX.micPerm==='unsupported')s='This page needs HTTPS (or localhost) and a browser with microphone access';
  else if(IX.talking&&!IX.granted.length)s='Getting the speaker…';
  else if(IX.talking)s='You are live';
  sub.textContent=s;
  btn.classList.toggle('live',IX.talking&&IX.granted.length>0);
  btn.classList.toggle('wait',IX.talking&&!IX.granted.length);
  btn.disabled=!IX.live;
  document.querySelectorAll('#ixMode .seg-btn').forEach(b=>b.classList.toggle('active',b.dataset.mode===IX.mode));
}
// level meters follow the audio: one animation loop, only while the page is open
function ixMeterLoop(){
  if(IX.dead)return;
  const now=performance.now();
  IX.players.forEach((p,id)=>{
    const el=document.querySelector(`.ix-room[data-id="${CSS.escape(id)}"] .ix-meter i`);
    if(el){const v=now-p.at>400?0:p.level;p.level*=.9;el.style.transform=`scaleX(${Math.min(1,v).toFixed(3)})`}
  });
  const mic=document.getElementById('ixMicLvl');
  if(mic){IX.micLevel*=.85;mic.style.transform=`scaleX(${Math.min(1,IX.micLevel*3).toFixed(3)})`}
  IX.meterRaf=requestAnimationFrame(ixMeterLoop);
}

// ── set-up (admins) ───────────────────────────────────────────────
async function ixLoadHealth(){
  const box=document.getElementById('ixHealth');if(!box)return;
  try{IX.health=await(await api('/api/intercom/health')).json()}catch(_){return}
  const h=IX.health;
  const bad=h.steps.filter(s=>!s.ok&&!s.optional);
  const warn=h.steps.filter(s=>!s.ok&&s.optional);
  const fix=s=>(s.fix||[]).map(c=>`<div class="ix-cmd"><code>${esc(c)}</code><button class="ix-copy" data-copy="${escA(c)}">Copy</button></div>`).join('');
  const row=s=>`<div class="ix-step ${s.ok?'ok':(s.optional?'warn':'bad')}"><span class="ix-step-dot"></span>
      <div><div class="ix-step-t">${esc(s.label)}</div><div class="ix-step-d">${esc(s.detail||'')}</div>${fix(s)}</div></div>`;
  box.innerHTML=(bad.length||(IX.admin&&warn.length&&!IX.btOk()))?
    `<div class="ix-card ix-health ${bad.length?'bad':''}"><div class="ix-card-t">${bad.length?'This server isn’t ready for the intercom':'Setup check'}</div>
      ${h.steps.map(row).join('')}<button class="btn btn-g btn-sm" id="ixRecheck">↻ Check again</button></div>`:'';
  document.getElementById('ixRecheck')?.addEventListener('click',ixLoadHealth);
  ixPaintSetup();
}
IX.btOk=()=>!!(IX.health&&IX.health.bluetooth);
function ixDeviceRows(kind){
  const list=IX.devices.filter(d=>d.kind===kind);
  if(!list.length)return `<div class="sdesc">None found${kind==='output'?'. Connect a speaker, plug in a headset, or add a Bluetooth device below.':'.'}</div>`;
  return list.map(d=>`<div class="ix-dev ${kind==='output'?'ix-dev-spk':''}"><div class="ix-dev-n">${esc(d.name)}${d.default?' <span class="ix-tag quiet">default</span>':''}
    ${kind==='output'?`<div class="ix-vol-row" data-vdev="${escA(d.id)}" hidden>
      <button class="ix-vol-mute" type="button" data-act="spkmute" data-dev="${escA(d.id)}" aria-label="Mute" title="Mute / unmute">\u{1F50A}</button>
      <input type="range" class="ix-spk-vol" min="0" max="100" step="1" value="50" data-dev="${escA(d.id)}" aria-label="Volume of ${escA(d.name)}"/>
      <span class="ix-vol-pct tnum">50%</span></div>`:''}</div>
    <button class="btn btn-g btn-sm" data-act="${kind==='output'?'beep':'level'}" data-dev="${escA(d.id)}">${kind==='output'?'Play a beep':'Check level'}</button></div>`).join('');
}
function ixPaintSetup(){
  const el=document.getElementById('ixSetup');if(!el||!IX.admin)return;
  const btAvail=IX.health?!!IX.health.bluetooth:null;
  el.innerHTML=`
    <div class="ix-card"><div class="ix-card-t">Speakers</div>${ixDeviceRows('output')}</div>
    <div class="ix-card"><div class="ix-card-t">Microphones</div>${ixDeviceRows('input')}</div>
    <div class="ix-card"><div class="ix-card-t">Bluetooth</div>
      <div class="sdesc">Add a speaker or headset: put it in pairing mode, then scan. It is paired, trusted and connected in one step, and then appears under Speakers.</div>
      ${btAvail===false?`<div class="ix-note">Bluetooth isn’t available on this server (see the check above).</div>`:
        `<div class="ix-bt-actions"><button class="btn btn-p btn-sm" id="ixScan">${_IX_ICO.bt} Scan for devices</button></div>
         <div id="ixBt">${ixBtHtml()}</div>`}
    </div>`;
  ixVolApply();
}
// ── live speaker volumes: the sliders follow the system volume (polled) and set it as they are dragged ──
IX.vols={};IX.volSupported=null;IX.volDrag=new Set();IX.volSetAt={};IX.volTimer=null;
function ixVolApply(){
  document.querySelectorAll('#ixSetup .ix-vol-row').forEach(row=>{
    const id=row.dataset.vdev,v=IX.vols[id];
    if(!v){row.hidden=true;return}
    row.hidden=false;
    const rng=row.querySelector('input'),pct=row.querySelector('.ix-vol-pct'),mute=row.querySelector('.ix-vol-mute');
    const recent=Date.now()-(IX.volSetAt[id]||0)<1200;                 // don't fight a change we just made
    if(!IX.volDrag.has(id)&&!recent){rng.value=Math.round(Math.min(1,v.volume)*100);pct.textContent=rng.value+'%'}
    mute.textContent=v.muted?'\u{1F507}':(v.volume<0.34?'\u{1F508}':'\u{1F50A}');
    row.classList.toggle('muted',!!v.muted);
  });
}
async function ixVolPoll(){
  clearTimeout(IX.volTimer);
  const open=document.getElementById('ixSetupWrap');
  if(!document.getElementById('ixSetup')||!open||!open.open||IX.volSupported===false)return;   // closed or gone: stop
  if(!document.hidden){
    try{
      const d=await(await api('/api/intercom/volumes')).json();
      if(d.supported===false){IX.volSupported=false;IX.vols={}}
      else if(d.volumes){IX.volSupported=true;IX.vols=d.volumes}
      ixVolApply();
    }catch(_){}
  }
  IX.volTimer=setTimeout(ixVolPoll,1500);
}
async function ixVolSend(id,body){
  IX.volSetAt[id]=Date.now();
  try{const r=await api('/api/intercom/volume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device:id,...body})});
    const d=await r.json();
    if(!r.ok||d.error)toast(d.error||'Couldn\u2019t change the volume','error');
    else{IX.vols[id]={volume:d.volume!=null?d.volume:(body.volume!=null?body.volume:(IX.vols[id]||{}).volume||0),muted:d.muted!=null?d.muted:(body.muted!=null?body.muted:!!(IX.vols[id]||{}).muted)};ixVolApply()}
  }catch(_){toast('Couldn\u2019t change the volume','error')}
}
// Bluetooth devices already known to this server (paired or connected) are listed without a scan.
async function ixLoadKnownBt(){
  if(IX.btList!=null)return;
  try{const l=await(await api('/api/intercom/bluetooth/devices')).json();
    if(Array.isArray(l)&&IX.btList==null){IX.btList=l.filter(d=>d.paired||d.connected);ixPaintSetup()}}catch(_){}
}
function ixBtHtml(){
  const l=IX.btList;
  if(l==null)return '';
  if(!l.length)return `<div class="sdesc" style="margin-top:10px">No Bluetooth devices found. Put the device in pairing mode, bring it close to the server, and scan again. For an Amazon Echo, say “Alexa, pair” first.</div>`;
  return `<div class="ix-btlist">${l.map(d=>`<div class="ix-dev"><div class="ix-dev-n">${esc(d.name)}
      <span class="ix-tag ${d.connected&&d.audio_ready!==false?'live':'quiet'}">${d.connected?(d.audio_ready===false?'Connected, no audio yet':'Connected'):(d.paired?'Paired':'Nearby')}</span>
      ${d.speaker_only?'<span class="ix-tag">Speaker only</span>':''}
      ${d.hint?`<div class="sdesc">${esc(d.hint)}</div>`:''}</div>
      <div class="ix-dev-a">${d.connected&&d.audio_ready===false?`<button class="btn btn-p btn-sm" data-act="btadd" data-mac="${escA(d.mac)}">Connect audio</button>`:''}${d.connected?`<button class="btn btn-g btn-sm" data-act="btdisc" data-mac="${escA(d.mac)}">Disconnect</button>`
          :`<button class="btn btn-p btn-sm" data-act="btadd" data-mac="${escA(d.mac)}">${d.paired?'Connect':'Add'}</button>`}
        ${d.paired||d.connected?`<button class="btn btn-g btn-sm" data-act="btforget" data-mac="${escA(d.mac)}">Forget</button>`:''}</div></div>`).join('')}</div>`;
}
async function ixOverview(){
  const d=await(await api('/api/intercom/overview')).json();
  if(d.error)throw new Error(d.error);
  IX.devices=d.devices||[];IX.admin=!!d.admin;
  if(!IX.live)IX.rooms=d.rooms||IX.rooms;
  IX.engine=d.engine;
}
async function ixRoomDlg(room){
  if(room){   // the live list only says whether a room has a speaker or microphone; the editor needs which
    try{const all=await(await api('/api/intercom/rooms')).json();room=(Array.isArray(all)&&all.find(r=>r.id===room.id))||room}catch(_){}
  }
  const outs=IX.devices.filter(d=>d.kind==='output'),ins=IX.devices.filter(d=>d.kind==='input');
  const opt=(list,cur)=>`<option value="">None</option>`+list.map(d=>`<option value="${escA(d.id)}" ${d.id===cur?'selected':''}>${esc(d.name)}</option>`).join('');
  const icons=['\u{1F3E0}','\u{1F373}','\u{1F6CB}️','\u{1F6CF}️','\u{1F6AA}','\u{1F6E0}️','\u{1F697}','\u{1F333}','\u{1F4BB}','\u{1F50A}'];
  openMdl(room?`Edit ${room.name}`:'New room',`
    <div class="fg"><label class="fgl">Name</label><input class="fgi" id="ixrName" maxlength="40" placeholder="Kitchen" value="${escA(room?room.name:'')}"/></div>
    <div class="fg"><label class="fgl">Icon</label><div class="ix-icons" id="ixrIcons">${icons.map(i=>
      `<button type="button" class="ix-icon-b${(room?room.icon:icons[9])===i?' on':''}" data-i="${escA(i)}">${i}</button>`).join('')}</div></div>
    <div class="fg"><label class="fgl">Speaker (what people talk to)</label><select class="fgi" id="ixrOut">${opt(outs,room&&room.output)}</select></div>
    <div class="fg"><label class="fgl">Microphone (what people listen to)</label><select class="fgi" id="ixrIn">${opt(ins,room&&room.input)}</select></div>
    <div class="sdesc">A room can have just a speaker (announcements), just a microphone (a baby monitor), or both.</div>`,
    `${room?`<button class="btn btn-d" id="ixrDel" style="margin-right:auto">Delete</button>`:''}<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-p" id="ixrSave">Save</button>`);
  enhanceSelect(document.getElementById('ixrOut'));enhanceSelect(document.getElementById('ixrIn'));
  let icon=room?room.icon:icons[9];
  document.getElementById('ixrIcons').addEventListener('click',e=>{
    const b=e.target.closest('.ix-icon-b');if(!b)return;icon=b.dataset.i;
    document.querySelectorAll('#ixrIcons .ix-icon-b').forEach(x=>x.classList.toggle('on',x===b));
  });
  document.getElementById('ixrSave').addEventListener('click',async()=>{
    const body={name:document.getElementById('ixrName').value,icon,output:document.getElementById('ixrOut').value||null,input:document.getElementById('ixrIn').value||null};
    const r=await api(room?`/api/intercom/rooms/${encodeURIComponent(room.id)}`:'/api/intercom/rooms',
      {method:room?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    if(d.error){errToast(d);return}
    closeMdl();toast(room?'Room saved':'Room added','success');
    try{await ixOverview()}catch(_){}
    ixPaintRooms();ixPaintDock();
  });
  document.getElementById('ixrDel')?.addEventListener('click',()=>{
    confirmDlg(`Delete the room “${esc(room.name)}”? The devices stay as they are.`,async()=>{
      const d=await(await api(`/api/intercom/rooms/${encodeURIComponent(room.id)}`,{method:'DELETE'})).json();
      if(d.error){errToast(d);return}
      IX.listen.delete(room.id);IX.target.delete(room.id);ixSavePrefs();
      closeMdl();toast('Room deleted','info');
      try{await ixOverview()}catch(_){}
      ixPaintRooms();ixPaintDock();
    },'Delete');
  });
}

// ── the page ──────────────────────────────────────────────────────
async function rIntercom(){
  const gen=_renderGen;
  cleanupIntercom(true);
  const root=document.getElementById('vroot');
  root.innerHTML=`<div class="ix"><div class="vh"><div><div class="vtitle">Intercom</div>
      <div class="vsub">Talk to the speakers in your home, or listen in, from anywhere.</div></div>
      <span class="ix-status off" id="ixStatus"><i></i>Connecting…</span></div>
    <div class="loading"><div class="loader"></div></div></div>`;
  try{await ixOverview()}catch(e){
    if(gen!==_renderGen)return;
    root.innerHTML=`<div class="ix"><div class="vh"><div><div class="vtitle">Intercom</div></div></div>
      <div class="ix-card bad"><div class="ix-card-t">Couldn’t load the intercom</div><div class="sdesc">${esc(e.message||'')}</div></div></div>`;
    return;
  }
  if(gen!==_renderGen)return;
  root.innerHTML=`<div class="ix">
    <div class="vh"><div><div class="vtitle">Intercom</div>
      <div class="vsub">Talk to the speakers in your home, or listen in, from anywhere.</div></div>
      <span class="ix-status off" id="ixStatus"><i></i>Connecting…</span></div>
    <div id="ixHealth"></div>
    <div class="ix-rooms" id="ixRooms"></div>
    <div class="ix-dock" id="ixDock">
      <div class="ix-dock-l"><div class="ix-dock-t" id="ixTarget"></div><div class="ix-dock-s" id="ixSub"></div></div>
      <button class="ix-ptt" id="ixPtt" aria-label="Talk">${_IX_ICO.mic}</button>
      <div class="ix-dock-r">
        <div class="seg" id="ixMode"><button class="seg-btn" data-mode="hold" type="button">Hold</button><button class="seg-btn" data-mode="toggle" type="button">Tap</button></div>
        <label class="ix-vol" title="Volume of what you hear">${_IX_ICO.spk}<input type="range" id="ixVol" min="0" max="1.5" step="0.05" value="${IX.vol}"/></label>
        <div class="ix-miclvl" title="Your microphone"><i id="ixMicLvl"></i></div>
      </div>
    </div>
    ${IX.admin?`<details class="ix-setup-wrap" id="ixSetupWrap"><summary>Set up devices and Bluetooth</summary><div id="ixSetup" class="ix-setup"></div></details>`:''}
  </div>`;
  ixPaintStatus();ixPaintRooms();ixPaintDock();ixPaintSetup();
  if(IX.admin){
    document.getElementById('ixSetupWrap').addEventListener('toggle',e=>{
      if(e.target.open){if(!IX.health)ixLoadHealth();ixLoadKnownBt();ixVolPoll()}});
  }
  ixLoadHealth();
  IX.dead=false;IX.fatal='';IX.retry=0;
  ixConnect();
  IX.meterRaf=requestAnimationFrame(ixMeterLoop);

  document.getElementById('ixRooms').addEventListener('click',e=>{
    const a=e.target.closest('[data-act]');if(!a)return;
    const card=a.closest('.ix-room'),id=card&&card.dataset.id,act=a.dataset.act;
    if(act==='add'){ixRoomDlg(null);return}
    const room=IX.rooms.find(r=>r.id===id);
    if(act==='menu'&&room){ixRoomDlg(room);return}
    if(act==='listen'&&room){
      if(IX.listen.has(id))IX.listen.delete(id);else IX.listen.add(id);
      ixSavePrefs();ixSyncListen();ixPaintRooms();
    }else if(act==='target'&&room){
      if(IX.talking)return;
      if(IX.target.has(id))IX.target.delete(id);else IX.target.add(id);
      ixSavePrefs();ixPaintRooms();ixPaintDock();
    }
  });
  document.getElementById('ixMode').addEventListener('click',e=>{
    const b=e.target.closest('.seg-btn');if(!b)return;
    if(IX.talking)ixTalkStop();
    IX.mode=b.dataset.mode;ixSavePrefs();ixPaintDock();
  });
  document.getElementById('ixVol').addEventListener('input',e=>{
    IX.vol=parseFloat(e.target.value);if(IX.master)IX.master.gain.value=IX.vol;ixSavePrefs();
  });
  // the talk button: hold, or tap to start and tap to stop
  const ptt=document.getElementById('ixPtt');
  let held=false;
  const down=async e=>{
    e.preventDefault();
    if(ptt.disabled)return;
    try{ptt.setPointerCapture&&e.pointerId!=null&&ptt.setPointerCapture(e.pointerId)}catch(_){}
    if(IX.mode==='toggle'){if(IX.talking)ixTalkStop();else await ixTalkStart();return}
    held=true;await ixTalkStart();
    if(!held)ixTalkStop();                       // released while the microphone was still being set up
  };
  const up=()=>{if(IX.mode==='hold'&&held){held=false;ixTalkStop()}};
  ptt.addEventListener('pointerdown',down);
  ptt.addEventListener('pointerup',up);
  ptt.addEventListener('pointercancel',up);
  ptt.addEventListener('lostpointercapture',up);
  ptt.addEventListener('contextmenu',e=>e.preventDefault());
  if(!window._ixKeys){
    window._ixKeys=true;
    document.addEventListener('keydown',e=>{
      if(curV!=='intercom'||IX.dead||e.repeat)return;
      const t=document.activeElement&&document.activeElement.tagName;
      if(['INPUT','TEXTAREA','SELECT','BUTTON'].includes(t)||e.ctrlKey||e.metaKey||e.altKey)return;
      if(e.code==='Space'){
        e.preventDefault();
        if(IX.mode==='toggle'){IX.talking?ixTalkStop():ixTalkStart()}
        else{held=true;ixTalkStart().then(()=>{if(!held)ixTalkStop()})}
      }
    });
    document.addEventListener('keyup',e=>{
      if(curV!=='intercom'||IX.dead)return;
      if(e.code==='Space'&&IX.mode==='hold'&&held){held=false;ixTalkStop()}
    });
  }
  // a click anywhere unlocks audio on browsers that start the page muted
  root.addEventListener('click',()=>{if(IX.ctx&&IX.ctx.state==='suspended')IX.ctx.resume().catch(()=>{})},{once:true});

  // set-up actions
  document.getElementById('ixHealth').addEventListener('click',e=>{
    const c=e.target.closest('[data-copy]');
    if(c)navigator.clipboard?.writeText(c.dataset.copy).then(()=>toast('Copied','success'),()=>{});
  });
  const setup=document.getElementById('ixSetup');
  const ixSetupClick=async e=>{
    const b=e.target.closest('[data-act],#ixScan');if(!b)return;
    const act=b.dataset.act||'scan',dev=b.dataset.dev,mac=b.dataset.mac;
    const post=(u,body)=>api(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})}).then(r=>r.json());
    if(act==='beep'){
      b.disabled=true;
      try{const d=await post('/api/intercom/devices/test-output',{device:dev});
        if(d.error)errToast(d);else toast('Did you hear it?','info')}finally{b.disabled=false}
    }else if(act==='level'){
      b.disabled=true;const old=b.textContent;b.textContent='Listening…';
      try{const d=await post('/api/intercom/devices/test-input',{device:dev});
        if(d.error)errToast(d);
        else toast(d.peak<0.01?'Almost silent: check the microphone isn’t muted.':`Working. Loudest sound: ${Math.round(d.peak*100)}%`,d.peak<0.01?'warning':'success')}
      finally{b.disabled=false;b.textContent=old}
    }else if(act==='scan'){
      b.disabled=true;b.textContent='Scanning… (up to 30 s)';
      try{const d=await post('/api/intercom/bluetooth/scan');
        if(d.error){errToast(d)}else{IX.btList=d.devices||[];if(d.warning)toast(d.warning,'warning');else if(d.tip)toast(d.tip,'info')}
      }catch(_){toast('Scan failed','error')}
      finally{ixPaintSetup()}
    }else if(act==='btadd'){
      b.disabled=true;b.textContent='Connecting…';
      try{const d=await post('/api/intercom/bluetooth/add',{mac});
        if(d.error){errToast(d)}
        else if(d.ok){toast('Connected. It now appears under Speakers.','success')}
        else toast(d.message||'That didn’t work','error');
        await ixOverview();
        const r=await api('/api/intercom/bluetooth/devices');IX.btList=await r.json();
      }catch(_){toast('That didn’t work','error')}
      finally{ixPaintSetup()}
    }else if(act==='btdisc'){
      b.disabled=true;b.textContent='Disconnecting\u2026';
      try{const d=await post('/api/intercom/bluetooth/disconnect',{mac});
        if(d.error||d.ok===false)toast(d.message||d.error||'Couldn\u2019t disconnect it','error');
        await ixOverview();IX.btList=await(await api('/api/intercom/bluetooth/devices')).json();
      }catch(_){toast('That didn\u2019t work','error')}
      finally{ixPaintSetup()}
    }else if(act==='btforget'){
      const d=await(await api(`/api/intercom/bluetooth/device/${encodeURIComponent(mac)}`,{method:'DELETE'})).json();
      if(d.ok===false||d.error)toast(d.message||d.error||'Couldn’t forget it','error');
      try{await ixOverview();IX.btList=await(await api('/api/intercom/bluetooth/devices')).json()}catch(_){}
      ixPaintSetup();
    }
  };
  if(setup){
    let volT=null;
    setup.addEventListener('input',e=>{
      const r=e.target.closest&&e.target.closest('.ix-spk-vol');if(!r)return;
      const id=r.dataset.dev;IX.volDrag.add(id);
      const pct=r.parentElement.querySelector('.ix-vol-pct');if(pct)pct.textContent=r.value+'%';
      clearTimeout(volT);volT=setTimeout(()=>ixVolSend(id,{volume:r.value/100}),90);
    });
    const endDrag=e=>{const r=e.target.closest&&e.target.closest('.ix-spk-vol');if(r){IX.volDrag.delete(r.dataset.dev);IX.volSetAt[r.dataset.dev]=Date.now()}};
    setup.addEventListener('change',endDrag);
    setup.addEventListener('pointerup',endDrag);
    setup.addEventListener('click',e=>{
      const b=e.target.closest&&e.target.closest('[data-act="spkmute"]');if(!b)return;
      const id=b.dataset.dev,cur=IX.vols[id];ixVolSend(id,{muted:!(cur&&cur.muted)});
    });
  }
  setup&&setup.addEventListener('click',e=>{
    ixSetupClick(e).catch(err=>{
      toast('That didn’t work: '+((err&&err.message)||'Void OS couldn’t be reached. Check your connection and try again.'),'error');
      try{ixPaintSetup()}catch(_){}
    });
  });
}

// ── LIVE PERMISSION ENFORCEMENT ──────────────────────────────────────
// A normal HTTP request always sees fresh permissions (Flask re-checks
// every time), so the only real gap was a persistent connection (the
// terminal) plus the *already-rendered UI* not knowing anything changed
// until a reload. /ws/control is a push-only channel the server uses to
// tell this exact open tab the instant an admin changes what it's
// allowed to do — see _notify_permissions_changed in modules/web.py.
const _SIDEBAR_BUTTONS = {
  containers:`<button class="sbtn" data-view="containers"><span class="sb-icon"><svg viewBox="0 0 24 24"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg></span><span class="sb-lbl">Containers</span></button>`,
  appstore:`<button class="sbtn" data-view="appstore"><span class="sb-icon"><svg viewBox="0 0 24 24"><path d="M20 7h-3V5a3 3 0 00-3-3H10a3 3 0 00-3 3v2H4a1 1 0 00-1 1v3a3 3 0 003 3 3 3 0 003-3 3 3 0 006 0 3 3 0 003 3 3 3 0 003-3V8a1 1 0 00-1-1z"/><path d="M5 14v5a1 1 0 001 1h12a1 1 0 001-1v-5"/></svg></span><span class="sb-lbl">App Store</span></button>`,
  terminal:`<button class="sbtn" data-view="terminal"><span class="sb-icon"><svg viewBox="0 0 24 24"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg></span><span class="sb-lbl">Terminal</span></button>`,
  intercom:`<button class="sbtn" data-view="intercom"><span class="sb-icon"><svg viewBox="0 0 24 24"><path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z"/><path d="M19 10v2a7 7 0 01-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg></span><span class="sb-lbl">Intercom</span></button>`,
  access:`<button class="sbtn" data-view="access"><span class="sb-icon"><svg viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg></span><span class="sb-lbl">Access Control</span></button>`,
};
// Insertion order matches the server-rendered template exactly, so a
// freshly-granted button lands where it would have if the page had just
// been loaded with that permission already in place.
const _SIDEBAR_ORDER = ['dashboard','containers','appstore','files','terminal','intercom','access','settings','sessions'];

function _wireNavButton(el){el.addEventListener('click',()=>go(el.dataset.view))}

function syncSidebarForPermissions(){
  const nav=document.querySelector('.sb-nav');
  if(!nav)return; // login page, or vroot not mounted — nothing to sync
  const want={containers:CAN_DOCK,appstore:CAN_DOCK,terminal:CAN_TERM,intercom:CAN_INTERCOM,access:IS_ADMIN};
  for(const key of Object.keys(want)){
    const existing=nav.querySelector(`.sbtn[data-view="${key}"]`);
    if(want[key]&&!existing){
      const idx=_SIDEBAR_ORDER.indexOf(key);
      let anchor=null;
      for(let i=idx+1;i<_SIDEBAR_ORDER.length&&!anchor;i++)anchor=nav.querySelector(`.sbtn[data-view="${_SIDEBAR_ORDER[i]}"]`);
      if(!_SIDEBAR_BUTTONS[key])continue;   // no button for this view (e.g. a removed page) — skip, don't crash
      const tmp=document.createElement('div');tmp.innerHTML=_SIDEBAR_BUTTONS[key];const btn=tmp.firstElementChild;
      if(anchor)nav.insertBefore(btn,anchor);else nav.appendChild(btn);
      _wireNavButton(btn);
    }else if(!want[key]&&existing){
      existing.remove();
    }
  }
}

function applyPermissionsUpdate(msg){
  const prev={role:ROLE,term:CAN_TERM,dock:CAN_DOCK,intc:CAN_INTERCOM,files:FULL_FILES};
  const prevTerm=CAN_TERM;
  ROLE=msg.role??ROLE; IS_SUPERUSER=!!msg.is_superuser; IS_ADMIN=!!msg.can_admin;
  CAN_TERM=!!msg.can_terminal; CAN_DOCK=!!msg.can_docker; CAN_INTERCOM=!!msg.can_intercom; FULL_FILES=!!msg.full_files;
  syncSidebarForPermissions();
  // Targeted by its own container: Access Control also contains .role-badge
  // elements, so "the first .role-badge on the page" isn't always the sidebar's.
  document.querySelectorAll('.sb-userinfo .role-badge').forEach(badge=>{
    badge.className='role-badge role-'+ROLE;
    const sym=badge.querySelector('.role-sym'),lbl=badge.querySelector('.role-lbl');
    if(sym)sym.textContent=ROLE==='superuser'?'\u2605':(ROLE==='admin'?'\u25b2':'\u25cf');
    if(lbl)lbl.textContent=ROLE==='superuser'?'Super User':ROLE.charAt(0).toUpperCase()+ROLE.slice(1);
  });
  const stillAllowed={containers:CAN_DOCK,appstore:CAN_DOCK,terminal:CAN_TERM,intercom:CAN_INTERCOM,admin:IS_ADMIN,access:IS_ADMIN};
  if(curV in stillAllowed&&!stillAllowed[curV]){
    if(curV==='terminal'&&prevTerm&&!CAN_TERM)_termAccessRevoked=true; // let ws.onclose show the specific message
    if(curV==='intercom')cleanupIntercom(true); // ends the WS/mic cleanly before redirecting away
    toast('Your access to this changed — redirected to Dashboard','info');
    go('dashboard');
    return;
  }
  // Still allowed to be on this page, but it was drawn for the old rank
  // (Settings sections, Add-app tile, tile edit buttons, home-screen home icons,
  // Access Control buttons...). Redraw it so nothing stale is left to click.
  // Terminal and Intercom hold a live connection, so they're left alone
  // (they're closed above when the access is gone).
  const changed=prev.role!==ROLE||prev.term!==CAN_TERM||prev.dock!==CAN_DOCK||prev.intc!==CAN_INTERCOM||prev.files!==FULL_FILES;
  if(changed&&curV&&curV!=='terminal'&&curV!=='intercom'){
    const v=curV;curV='';go(v);
  }
}

// The page re-checks what this account may do whenever its live channel (re)connects, when the tab comes
// back into view, and every few seconds while open. The server pushes changes instantly; this is what
// guarantees a lost push (a dropped tunnel, a sleeping laptop) is still caught within seconds.
let _endingSession=false;
function endSessionNow(reason){
  if(_endingSession)return;
  _endingSession=true;
  const msg=({account_deleted:'Your account was deleted',session_revoked:'This session was ended by an administrator',
    signed_out:'You were signed out',password_reset:'Your password was changed — sign in again',
    '2fa_reset':'Two-factor authentication was reset — sign in again'})[reason]||'You were signed out';
  toast(msg,'error');
  // end live shells and calls on this page straight away rather than waiting for the redirect
  try{if(typeof TERMS!=='undefined')TERMS.list.forEach(s=>{s.closed=true;try{s.ws&&s.ws.close()}catch(_){}})}catch(_){}
  try{cleanupIntercom(true)}catch(_){}
  setTimeout(()=>{location.href='/login'},900);
}
// Something another person (or the server) changed that this page is showing: redraw just that part.
const _liveTimers={};
function liveRefresh(kind){
  clearTimeout(_liveTimers[kind]);
  _liveTimers[kind]=setTimeout(async()=>{
    try{
      const modalOpen=()=>!document.getElementById('ovr')?.classList.contains('hidden');
      if(kind==='apps_changed'){
        if(curV==='dashboard'){document.getElementById('tgrid')?loadTiles():rDash()}
        else if(curV==='appstore'&&!modalOpen()&&typeof asReload==='function')asReload();
      }else if(kind==='shares_changed'){
        if(curV==='shares'&&!modalOpen()&&!document.activeElement?.closest?.('#sharesListSection'))loadShares(_renderGen,(document.getElementById('sharesUserF')?.value||'').trim());
      }else if(kind==='audit_changed'){
        if(document.getElementById('auditSection')&&!modalOpen()&&!document.activeElement?.closest?.('#auditSection'))
          loadAuditLog(document.getElementById('auditActionF')?.value||'',(document.getElementById('auditUserF')?.value||'').trim());
      }else if(kind==='errors_changed'){
        if(document.getElementById('errorLogSection')&&!modalOpen()&&!document.activeElement?.closest?.('#errorLogSection'))
          loadErrorLog((document.getElementById('errRefF')?.value||'').trim(),document.getElementById('errSrcF')?.value||'');
      }else if(kind==='storage_changed'){
        _places=null;
        if(document.getElementById('usbList')&&!document.activeElement?.matches?.('input,select')&&typeof loadUsbList==='function')loadUsbList();
        if(document.getElementById('networkShareList')&&typeof loadNetworkShares==='function')loadNetworkShares();
        if(curV==='files'&&typeof curP==='string'&&!document.querySelector('.fitem.dragging'))rFiles(curP);
      }else if(kind==='sessions_changed'){
        if(curV==='sessions'&&document.getElementById('sessionsSection')&&document.getElementById('ovr')?.classList.contains('hidden'))loadSessions();
      }else if(kind==='users_changed'){
        const open=!document.getElementById('ovr')?.classList.contains('hidden');
        if((curV==='access'||curV==='admin'||curV==='sessions')&&!open){const v=curV;curV='';go(v)}
      }else if(kind==='server_settings_changed'){
        const me=await(await api('/api/me')).json();
        if(me&&me.settings){Object.assign(SETTINGS,me.settings);applyUiMode()}
      }
    }catch(_){}
  },250);
}
// A red bar across the top while the storage drive (VOID_FILE_ROOT) isn't present; it goes away by itself.
function showRootBanner(root){
  let b=document.getElementById('rootBanner');
  if(!root||root.ok!==false){if(b)b.remove();return}
  if(!b){b=document.createElement('div');b.id='rootBanner';b.setAttribute('role','alert');document.body.appendChild(b)}
  const msg=`The storage drive isn\u2019t present (${root.path||'the files folder'}). Files, uploads, drives and cloud sync are paused until it is back.`;
  if(b.textContent!==msg)b.textContent=msg;
}
let _accessBusy=false;
async function accessCheck(){
  if(_accessBusy||_endingSession)return;
  _accessBusy=true;
  try{
    const r=await fetch('/api/me/access',{credentials:'same-origin',cache:'no-store'});
    if(r.status===401){endSessionNow('session_revoked');return}
    if(!r.ok)return;
    const a=await r.json();
    showRootBanner(a.root);
    const diff=a.role!==ROLE||!!a.can_terminal!==CAN_TERM||!!a.can_docker!==CAN_DOCK||!!a.can_intercom!==CAN_INTERCOM||!!a.full_files!==FULL_FILES||!!a.can_admin!==IS_ADMIN;
    if(diff)applyPermissionsUpdate(a);
  }catch(_){}finally{_accessBusy=false}
}
setInterval(()=>{if(!document.hidden)accessCheck()},10000);
// Containers also change outside Void OS (the docker CLI, a crash, a restart policy): while that tab is open, look every few
// seconds and redraw only when something actually differs.
let _ctrsSig=null,_ctrsSigGen=-1,_ctrsBusy=false;
setInterval(async()=>{
  if(document.hidden||curV!=='containers'||_ctrsBusy||!DOKR||!document.getElementById('ovr')?.classList.contains('hidden'))return;
  _ctrsBusy=true;
  try{
    const r=await api('/api/containers');if(!r.ok)return;
    const sig=JSON.stringify(await r.json());
    if(_ctrsSigGen!==_renderGen){_ctrsSigGen=_renderGen;_ctrsSig=sig;return}     // first look at this render: just remember it
    if(sig!==_ctrsSig){_ctrsSig=sig;rCtrs()}
  }catch(_){}finally{_ctrsBusy=false}
},4000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)accessCheck()});

let _ctrlGaveUp=false;
function initControlSocket(){
  if(_ctrlReconnectTimer){clearTimeout(_ctrlReconnectTimer);_ctrlReconnectTimer=null}
  const proto=location.protocol==='https:'?'wss:':'ws:';
  const ws=new WebSocket(`${proto}//${location.host}/ws/control`);
  _ctrlSock=ws;
  ws.onopen=()=>{_ctrlReconnectDelay=1000;xferResume();notifRefresh();appJobsResume();accessCheck()};  // resync anything missed while disconnected
  ws.onmessage=e=>{
    let msg;try{msg=JSON.parse(e.data)}catch(_){return}
    if(msg.type==='too_many'){_ctrlGaveUp=true;toast('Too many Void OS tabs are open for this account, so this one won\u2019t get live updates (permission changes, transfer progress). Close another tab and reload.','info',{key:'ctrl-limit'});return}
    if(msg.type==='file_op'){_xferLastPush=Date.now();xferUpdate(msg.job);return}
    if(msg.type==='notification'){notifIncoming(msg.item);return}
    if(msg.type==='notification_update'){notifUpdate(msg.item);return}
    if(msg.type==='permissions_changed')applyPermissionsUpdate(msg);
    else if(msg.type==='force_logout')endSessionNow(msg.reason);
    else if(msg.type==='apps_changed'||msg.type==='storage_changed'||msg.type==='users_changed'||msg.type==='server_settings_changed'||msg.type==='sessions_changed'||msg.type==='shares_changed'||msg.type==='audit_changed'||msg.type==='errors_changed')
      liveRefresh(msg.type);
  };
  ws.onclose=()=>{
    // Reconnect with backoff — a dropped control channel just means this
    // tab stops getting *instant* permission pushes until it's back (the
    // normal per-request checks are unaffected either way), so this isn't
    // urgent, but it should quietly heal itself. (Unless the server said this
    // account has too many tabs open: retrying would just be turned away again.)
    if(_ctrlGaveUp)return;
    _ctrlReconnectTimer=setTimeout(initControlSocket,Math.min(_ctrlReconnectDelay*=1.5,30000));
    if(!Conn._unloading)Conn.lost('socket');
  };
  ws.onerror=()=>{try{ws.close()}catch(_){}};
}

// ── ADMIN PANEL ──────────────────────────────────────────────────
// ── APP STORE ─────────────────────────────────────────────────────────────
// Installs and updates run on the server as jobs. Every job is tracked from the moment
// it starts (_appJobs), not just while its window is open, so closing the window never
// stops anything and progress keeps showing in the corner tray, the notification bell,
// the app's card and the browser tab's title. Updates arrive over the live channel
// (notification_update); polling /api/appstore/jobs only fills in when pushes go quiet.
const _appJobs=new Map();          // job id -> {id,kind,catalog_id,tile_id,label,progress,stage,done,error}
const _sleep=ms=>new Promise(r=>setTimeout(r,ms));
let _appJobTimer=null,_appJobLastPush=0,_jobDlg=null;
const _secretsShown=new Set();
const _asState={tab:'discover',cat:'',q:''};
let _updateInfo=null;              // tile_id -> {update_available, current_tags, latest_tags}, from checkForUpdates()
let _fullCatalog=[],_installedList=[];

function _jobActive(j){return j&&!j.done}
function appJobApply(j){
  const prev=_appJobs.get(j.id);
  const job={...(prev||{}),...j};
  _appJobs.set(job.id,job);
  appJobTray(job);
  asJobPaint(job);
  if(_jobDlg===job.id)jobDlgPaint(job);
  if(job.done&&!(prev&&prev.done))appJobFinished(job);
  else if(!job.done)appJobPollEnsure();
}
// A bell notification that belongs to a job carries its numbers in item.data.
function appJobFromNotif(item){
  const d=item&&item.data;if(!d||!d.job_id)return;
  _appJobLastPush=Date.now();
  appJobApply({id:d.job_id,kind:d.kind||'install',catalog_id:d.catalog_id||null,label:d.label,progress:d.progress==null?null:d.progress,
    stage:d.stage||'',done:!d.ongoing,error:(!d.ongoing&&item.level==='error')?(item.body||'Failed'):null});
}
// The corner tray (shared with file transfers) shows each running job as a row.
function appJobTray(job){
  const id='app-'+job.id,ended=!!job.done;
  const verbNow=job.kind==='update'?'Updating':'Installing',verbDone=job.kind==='update'?'Updated':'Installed';
  const x={id,appjob:true,job_id:job.id,state:ended?(job.error?'error':'done'):'running',
    label:`${ended&&!job.error?verbDone:verbNow} ${job.label||'app'}`,
    total_items:100,done_items:job.progress||0,total_bytes:0,done_bytes:0,total_files:0,done_files:0,
    errors:job.error?[{name:job.label||'app',error:job.error}]:[],
    status_text:ended?(job.error||'Done'):(job.stage||'Starting…'),pct:job.progress,indet:job.progress==null&&!ended};
  _xfers.set(id,x);xferRender(x);
}
function appJobPollEnsure(){
  if(_appJobTimer)return;
  _appJobTimer=setInterval(async()=>{
    const active=[..._appJobs.values()].filter(_jobActive);
    if(!active.length){clearInterval(_appJobTimer);_appJobTimer=null;return}
    if(Date.now()-_appJobLastPush<3500)return;        // pushes are arriving; no need to ask
    try{
      const r=await api('/api/appstore/jobs');
      if(!r.ok){if(r.status===403||r.status===404){clearInterval(_appJobTimer);_appJobTimer=null}return}
      for(const j of (await r.json()).jobs||[])if(_appJobs.has(j.id))appJobApply(j);
    }catch(_){}
  },2000);
}
async function appJobsResume(){          // after a reload or a dropped connection
  try{
    const r=await api('/api/appstore/jobs');if(!r.ok)return;
    for(const j of (await r.json()).jobs||[]){
      if(!j.done)appJobApply(j);
      else if(j.has_secrets&&j.kind==='install'&&!j.error&&!_secretsShown.has(j.id)&&!_appJobs.get(j.id)?.done){
        _appJobs.set(j.id,{...j});appSecretsDlg(j.id);
      }
    }
  }catch(_){}
}
async function appJobFinished(job){
  const ok=!job.error,upd=job.kind==='update',name=job.label||'The app';
  toast(ok?`${name} ${upd?'updated':'installed'}`:`Couldn’t ${upd?'update':'install'} ${name}`,ok?'success':'error',
    {hint:ok?undefined:job.error,duration:ok?7000:12000,action:ok&&!upd?{label:'Go to dashboard',fn:()=>go('dashboard')}:undefined});
  titleFlash(ok?`✓ ${name} ${upd?'updated':'installed'}`:`✕ ${name} failed`);
  if(App.curV==='appstore')asReload();
  if(typeof loadTiles==='function'&&App.curV==='dashboard')loadTiles();
  if(ok)setTimeout(()=>xferRemove('app-'+job.id),7000);
  if(ok&&!upd&&!jobDlgOpen(job.id)){          // generated passwords are shown once: don't lose them because the window was closed
    try{const r=await api(`/api/appstore/install/${job.id}`);const d=await r.json();
      if(r.ok&&d.secrets_generated&&!_secretsShown.has(job.id))appSecretsDlg(job.id,d.secrets_generated)}catch(_){}
  }
}
function xferRemove(id){
  _xfers.delete(id);tpRemove('x-'+id);
}

// ── tab title: progress of anything running, and a short "done" flash ──────
const _baseTitle=document.title.replace(/^\(\d+\+?\)\s*/,'');
let _titleFlash='',_titleFlashTimer=null;
function titleFlash(msg){
  _titleFlash=msg;clearTimeout(_titleFlashTimer);titleRefresh();
  _titleFlashTimer=setTimeout(()=>{_titleFlash='';titleRefresh()},12000);
}
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&_titleFlash){setTimeout(()=>{_titleFlash='';titleRefresh()},2500)}});
function titleRefresh(){
  const active=[...TP.items.values()].filter(_tfActive);
  let t=_baseTitle;
  if(active.length){
    const known=active.map(j=>j.state==='queued'?0:(j.pct==null?null:j.pct)).filter(p=>p!=null);
    const pct=known.length?Math.floor(known.reduce((a,b)=>a+b,0)/known.length):null;
    const what=active.length===1?String(active[0].title||'').replace(/…$/,''):
      `${active.length} ${active.every(j=>j.kind==='upload')?'uploads':'tasks'}`;
    t=`${pct!=null?pct+'% · ':''}${what} — ${_baseTitle}`;
  }else if(_titleFlash)t=`${_titleFlash} — ${_baseTitle}`;
  document.title=(_notifUnread?`(${_notifUnread>99?'99+':_notifUnread}) `:'')+t;
}

// ── progress dialog (shown after Install / Update, and when you click a running app) ───
function jobDlgOpen(id){return _jobDlg===id&&!ovr.classList.contains('hidden')&&!!document.getElementById('jobBar')}
function jobDlgPaint(j){
  const bar=document.getElementById('jobBar');if(!bar)return;
  const pct=j.progress,ended=!!j.done;
  bar.classList.toggle('indet',pct==null&&!ended);
  bar.classList.toggle('err',ended&&!!j.error);bar.classList.toggle('ok',ended&&!j.error);
  bar.firstElementChild.style.width=(ended&&!j.error?100:(pct||0))+'%';
  document.getElementById('jobStage').textContent=ended?(j.error?'Failed':'Done'):(j.stage||'Working…');
  document.getElementById('jobPct').textContent=ended?'':(pct==null?'':Math.floor(pct)+'%');
}
function showJobDlg(jobId){
  const j=_appJobs.get(jobId)||{id:jobId,label:'app',kind:'install'};
  _jobDlg=jobId;
  openMdl(`${j.kind==='update'?'Updating':'Installing'} ${j.label||'app'}`,`
    <div class="as-jobdlg">
      <div class="as-prog big indet" id="jobBar"><i></i></div>
      <div class="as-job-line"><span id="jobStage">Starting…</span><b id="jobPct"></b></div>
      <div class="sdesc">You can close this window — it keeps going in the background. Progress shows in your
        notifications and in this tab’s title.</div>
      <details class="as-details"><summary>Show details</summary><pre id="installLog"></pre></details>
      <div id="installResult" style="display:none"></div>
    </div>`,`<button class="btn btn-p" id="jobCloseBtn" onclick="closeMdl()">Run in background</button>`);
  jobDlgPaint(j);
  jobDlgPoll(jobId);
}
async function jobDlgPoll(jobId){
  while(jobDlgOpen(jobId)){
    let r,d;
    try{r=await api(`/api/appstore/install/${jobId}`);d=await r.json()}catch(e){await _sleep(2500);continue}
    if(!jobDlgOpen(jobId))return;
    if(!r.ok){
      if(r.status===0){await _sleep(2500);continue}
      document.getElementById('jobStage').textContent='Lost track of this job';
      const el=document.getElementById('installResult');el.style.display='block';
      el.innerHTML=`<div style="color:var(--rd)">${esc(d.error||'Not found')}</div>`;return;
    }
    const logEl=document.getElementById('installLog');
    if(logEl){logEl.textContent=(d.log||[]).join('\n');logEl.scrollTop=logEl.scrollHeight}
    appJobApply({id:jobId,kind:d.kind,label:d.label||undefined,progress:d.progress==null?null:d.progress,stage:d.stage,
      done:d.done,error:d.error||null,tile_id:d.tile_id});
    if(d.done){jobDlgResult(jobId,d);return}
    await _sleep(1200);
  }
}
function appSecretsHtml(secrets){
  return `<div class="sdesc" style="margin-top:12px;color:var(--yw)">Generated credentials — shown once, save them now:</div>`+
    Object.entries(secrets).map(([k,v])=>`<div style="display:flex;gap:8px;margin-top:6px"><input class="fgi" readonly value="${escA(k)}: ${escA(v)}" style="flex:1;font-family:var(--f-mono);font-size:11.5px" onclick="this.select()"/></div>`).join('');
}
function jobDlgResult(jobId,d){
  const el=document.getElementById('installResult');if(!el)return;
  el.style.display='block';
  const btn=document.getElementById('jobCloseBtn');if(btn)btn.textContent='Close';
  if(d.error){el.innerHTML=`<div style="color:var(--rd);white-space:pre-wrap">${esc(d.error)}</div>`;return}
  let html=d.kind==='update'?`<div style="color:var(--gr)">Updated — running the latest version now.</div>`
    :`<div style="color:var(--gr)">Installed — a tile has been added to your dashboard.</div>`;
  if(d.secrets_generated){html+=appSecretsHtml(d.secrets_generated);_secretsShown.add(jobId);api(`/api/appstore/install/${jobId}/ack`,{method:'POST'})}
  el.innerHTML=html;
}
async function appSecretsDlg(jobId,secrets){
  _secretsShown.add(jobId);
  if(!secrets){
    try{const r=await api(`/api/appstore/install/${jobId}`);const d=await r.json();secrets=r.ok?d.secrets_generated:null}catch(_){}
  }
  if(!secrets)return;
  const j=_appJobs.get(jobId)||{};
  openMdl(`${j.label||'App'} is ready`,`<div style="color:var(--gr)">Installed — a tile has been added to your dashboard.</div>${appSecretsHtml(secrets)}`,
    `<button class="btn btn-p" onclick="closeMdl()">Done</button>`);
  api(`/api/appstore/install/${jobId}/ack`,{method:'POST'});
}

// ── page ─────────────────────────────────────────────────────────────────────
const _SEARCH_SVG='<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>';
const _AS_PAGE=30;                       // rows added per step when a long list is shown
let _asShown=_AS_PAGE,_asSearchT=null,_asMoreObs=null,_asAnim=true,_asList=null;
async function rAppStore(){
  if(!CAN_DOCK){go('dashboard');return}
  _asShown=_AS_PAGE;_asAnim=true;
  const root=document.getElementById('vroot');
  root.innerHTML=`
    <div class="as-hero">
      <div><div class="vtitle">App Store</div>
        <div class="vsub">Install self-hosted apps in a click. Each one runs in its own Docker container.</div></div>
      <label class="as-search">${_SEARCH_SVG}<input type="search" id="asSearchInp" placeholder="Search apps…" spellcheck="false" autocomplete="off" value="${escA(_asState.q)}"/></label>
    </div>
    <div class="as-bar">
      <div class="as-tabs" id="asTabs" role="tablist">
        <button class="as-tab" role="tab" data-tab="discover">Discover</button>
        <button class="as-tab" role="tab" data-tab="installed">Installed<span class="as-count" id="asInstCount" style="display:none"></span></button>
      </div>
      <div class="as-bar-r">
        <span class="as-listinfo" id="asListInfo"></span>
        <button class="btn btn-g btn-sm" id="asListBtn" title="Look for new apps in the app list">↻ New apps</button>
        <button class="btn btn-g btn-sm" id="asCheckUpdatesBtn" title="Check installed apps for newer versions">↑ App updates</button>
      </div>
    </div>
    <div id="asBanner"></div>
    <div class="as-chips" id="asChips"></div>
    <div id="asBody">${asSkeleton()}</div>
    <div class="as-note">Apps are community-maintained and not vetted by Void OS. Every install is previewed first, and
      anything asking for elevated host access (privileged mode, host networking, the Docker socket) is flagged before you confirm.</div>`;
  document.getElementById('asCheckUpdatesBtn').addEventListener('click',checkForUpdates);
  document.getElementById('asListBtn').addEventListener('click',()=>asListCheck(true));
  document.getElementById('asTabs').addEventListener('click',e=>{const b=e.target.closest('[data-tab]');if(!b)return;_asState.tab=b.dataset.tab;_asAnim=true;asRender()});
  document.getElementById('asSearchInp').addEventListener('input',e=>{
    _asState.q=e.target.value;if(_asState.tab!=='discover')_asState.tab='discover';
    clearTimeout(_asSearchT);_asSearchT=setTimeout(()=>{_asShown=_AS_PAGE;_asAnim=false;asRender()},110);
  });
  document.getElementById('asChips').addEventListener('click',e=>{const b=e.target.closest('[data-cat]');if(!b)return;
    _asState.cat=b.dataset.cat;_asShown=_AS_PAGE;_asAnim=true;asRender()});
  document.getElementById('asBody').addEventListener('click',asBodyClick);
  document.getElementById('asBody').addEventListener('keydown',e=>{
    if(e.key==='Enter'&&e.target.classList?.contains('as-row')){showInstallDlg(e.target.dataset.id)}});
  await asReload(true);
  asLoadListStatus();
}
function asSkeleton(){
  return `<div class="as-list">${Array.from({length:9},()=>`<div class="as-row as-skel"><div class="as-ico"></div>
    <div class="as-meta"><div class="sk-line w60"></div><div class="sk-line w80"></div></div></div>`).join('')}</div>`;
}
async function asReload(first){
  const gen=_renderGen;
  try{
    const [inst,cat]=await Promise.all([apiData('/api/appstore/installed'),apiData('/api/appstore/catalog')]);
    if(gen!==_renderGen)return;
    _installedList=inst;_fullCatalog=cat;
    cat.forEach(c=>{if(c.installing&&!_appJobs.has(c.installing.job_id))
      appJobApply({id:c.installing.job_id,kind:'install',catalog_id:c.id,label:c.name,progress:c.installing.progress,stage:c.installing.stage,done:false})});
    _asAnim=!!first;
    asRender();
  }catch(e){
    const body=document.getElementById('asBody');
    if(body)body.innerHTML=`<div class="empty-dir">Couldn’t load the App Store: ${esc(e.message||'unknown error')}<br><button class="btn btn-g btn-sm" style="margin-top:12px" id="asRetry">Try again</button></div>`;
    document.getElementById('asRetry')?.addEventListener('click',()=>{document.getElementById('asBody').innerHTML=asSkeleton();asReload(true)});
  }
}
async function loadInstalled(){await asReload()}   // kept: other code refreshes the page through this name

// ── the app list itself (updating it without updating Void OS) ──
async function asLoadListStatus(){
  try{_asList=await apiData('/api/appstore/catalog/status')}catch(e){_asList=null}
  asPaintList();
}
function asPaintList(){
  const info=document.getElementById('asListInfo'),banner=document.getElementById('asBanner');
  if(!info||!banner)return;
  const L=_asList;
  info.textContent=L&&L.count?`${L.count} apps`:'';
  info.title=L?`App list ${L.version||''}${L.checked_at?' · checked '+new Date(L.checked_at*1000).toLocaleString():''}`:'';
  const r=L&&L.remote;
  if(!L||!L.update_available||!r){banner.innerHTML='';return}
  const bits=[r.new.length?`${r.new.length} new`:'',r.changed.length?`${r.changed.length} updated`:'',r.removed.length?`${r.removed.length} removed`:''].filter(Boolean).join(' · ');
  const names=[...r.new,...r.changed].slice(0,6).map(esc).join(', ');
  banner.innerHTML=`<div class="as-banner"><div class="as-banner-t"><b>The app list has an update</b>
      <span>${bits}${names?' — '+names+((r.new.length+r.changed.length)>6?'…':''):''}</span></div>
    ${IS_ADMIN?`<button class="btn btn-p btn-sm" id="asListUpdate">Update app list</button>`:`<span class="sdesc">Ask an admin to update it.</span>`}</div>`;
  document.getElementById('asListUpdate')?.addEventListener('click',asListUpdate);
}
async function asListCheck(manual){
  const btn=document.getElementById('asListBtn');
  if(btn){btn.disabled=true;btn.textContent='Checking…'}
  try{
    if(IS_ADMIN){_asList=await(await api('/api/appstore/catalog/check',{method:'POST'})).json()}
    else _asList=await apiData('/api/appstore/catalog/status');
    asPaintList();
    if(manual){
      if(_asList.error)toast(_asList.error,'info',{key:'as-list'});
      else if(_asList.update_available)toast('New apps are available','info',{key:'as-list'});
      else toast('The app list is up to date','success',{key:'as-list'});
    }
  }catch(e){if(manual)toast('Could not check for new apps','error',{hint:e.message})}
  finally{if(btn){btn.disabled=false;btn.textContent='↻ New apps'}}
}
async function asListUpdate(){
  const btn=document.getElementById('asListUpdate');
  if(btn){btn.disabled=true;btn.textContent='Updating…'}
  try{
    const r=await(await api('/api/appstore/catalog/update',{method:'POST'})).json();
    if(r.error){errToast(r);if(btn){btn.disabled=false;btn.textContent='Update app list'}return}
    _asList=r;
    toast(`App list updated: ${r.new.length} new, ${r.changed.length} updated`,'success');
    asPaintList();
    await asReload(false);
  }catch(e){toast('Could not update the app list','error',{hint:e.message});if(btn){btn.disabled=false;btn.textContent='Update app list'}}
}

// ── page body ──
function asScore(c,q){
  let total=0;
  for(const t of q.split(/\s+/).filter(Boolean)){
    const n=c.name.toLowerCase();let s=0;
    if(n===t)s=100;else if(n.startsWith(t))s=80;else if(n.includes(t))s=60;
    else if((c.category||'').toLowerCase().includes(t))s=30;
    else if((c.developer||'').toLowerCase().includes(t))s=25;
    else if((c.description||'').toLowerCase().includes(t))s=20;
    if(!s)return 0;total+=s;
  }
  return total;
}
function asRender(){
  const body=document.getElementById('asBody');if(!body)return;
  if(_asMoreObs){_asMoreObs.disconnect();_asMoreObs=null}
  document.querySelectorAll('#asTabs .as-tab').forEach(b=>{const on=b.dataset.tab===_asState.tab;b.classList.toggle('active',on);b.setAttribute('aria-selected',on)});
  const cnt=document.getElementById('asInstCount');
  if(cnt){cnt.textContent=_installedList.length;cnt.style.display=_installedList.length?'':'none'}
  const chips=document.getElementById('asChips');
  const sb=document.getElementById('asSearchInp');if(sb)sb.placeholder=`Search ${_fullCatalog.length||''} apps…`.replace('  ',' ');
  body.classList.toggle('as-anim',_asAnim);
  if(_asState.tab==='installed'){
    chips.style.display='none';body.innerHTML=asInstalledHtml();
    document.getElementById('asGoDiscover')?.addEventListener('click',()=>{_asState.tab='discover';_asAnim=true;asRender()});
    return;
  }
  chips.style.display='';
  const cats={};_fullCatalog.forEach(c=>{if(c.category)cats[c.category]=(cats[c.category]||0)+1});
  const names=Object.keys(cats).sort();
  chips.innerHTML=[['', 'All',_fullCatalog.length],...names.map(n=>[n,n,cats[n]])].map(([v,l,n])=>
    `<button class="as-chip${_asState.cat===v?' active':''}" data-cat="${escA(v)}">${esc(l)}<span>${n}</span></button>`).join('');
  const q=_asState.q.trim().toLowerCase();
  if(!q&&!_asState.cat){body.innerHTML=asHomeHtml();return}
  let list=_fullCatalog.filter(c=>!_asState.cat||c.category===_asState.cat);
  if(q){
    list=list.map(c=>[asScore(c,q),c]).filter(x=>x[0]>0).sort((x,y)=>y[0]-x[0]||x[1].name.localeCompare(y[1].name,undefined,{sensitivity:'base'})).map(x=>x[1]);
  }else list.sort((a,b)=>(b.featured?1:0)-(a.featured?1:0)||a.name.localeCompare(b.name,undefined,{sensitivity:'base'}));
  if(!list.length){
    body.innerHTML=`<div class="as-empty"><div class="as-empty-ico">\u{1F50D}</div><div class="as-empty-t">Nothing matches “${esc(_asState.q)}”</div>
      <div class="sdesc">Try a shorter word${_asState.cat?`, or look in all categories`:''}.</div>
      ${_asState.cat?`<button class="btn btn-g btn-sm" data-cat="">Show all categories</button>`:''}</div>`;
    return;
  }
  const shown=list.slice(0,_asShown);
  body.innerHTML=`<div class="as-count-line">${list.length} app${list.length===1?'':'s'}${_asState.cat?` in ${esc(_asState.cat)}`:''}${q?` for “${esc(_asState.q.trim())}”`:''}</div>
    <div class="as-list">${shown.map(asRowHtml).join('')}</div>
    ${list.length>shown.length?`<div class="as-more-wrap"><button class="btn btn-g btn-sm" id="asMoreBtn">Show more</button>
      <span class="sdesc">${shown.length} of ${list.length}</span></div>`:''}`;
  const more=document.getElementById('asMoreBtn');
  if(more){
    more.addEventListener('click',()=>{_asShown+=_AS_PAGE;_asAnim=false;asRender()});
    if('IntersectionObserver' in window){   // keep scrolling without pressing anything
      _asMoreObs=new IntersectionObserver(es=>{if(es[0].isIntersecting){_asMoreObs.disconnect();_asMoreObs=null;_asShown+=_AS_PAGE;_asAnim=false;asRender()}},{rootMargin:'400px'});
      _asMoreObs.observe(more);
    }
  }
}
function asHomeHtml(){
  const feat=_fullCatalog.filter(c=>c.featured).slice(0,10);
  const by={};_fullCatalog.forEach(c=>{const k=c.category||'Other';(by[k]=by[k]||[]).push(c)});
  const order=Object.keys(by).sort((a,b)=>by[b].length-by[a].length);
  const rank=(a,b)=>(b.featured?1:0)-(a.featured?1:0)||a.name.localeCompare(b.name,undefined,{sensitivity:'base'});
  return `${feat.length?`<section class="as-sec"><h3 class="as-h">Popular picks</h3><div class="as-featrow">${feat.map(asFeatHtml).join('')}</div></section>`:''}
    ${order.map(k=>`<section class="as-sec"><div class="as-sech"><h3 class="as-h">${esc(k)}</h3>
      <button class="as-seeall" data-cat="${escA(k)}">See all ${by[k].length} →</button></div>
      <div class="as-list">${by[k].slice().sort(rank).slice(0,6).map(asRowHtml).join('')}</div></section>`).join('')}`;
}
function asJobFor(catalogId){return [..._appJobs.values()].find(j=>j.kind==='install'&&j.catalog_id===catalogId&&!j.done)}
function asActHtml(c){
  const run=asJobFor(c.id);
  if(run){
    const pct=run.progress;
    return `<button class="as-run" data-job="${escA(run.id)}" title="Show progress">
      <span class="as-prog${pct==null?' indet':''}"><i style="width:${pct==null?40:Math.floor(pct)}%"></i></span>
      <span class="as-run-line"><span>${esc(run.stage||'Installing…')}</span><b>${pct==null?'':Math.floor(pct)+'%'}</b></span></button>`;
  }
  if(c.installed){
    const tile=_installedList.find(s=>s.catalog_id===c.id);
    return `<span class="as-installed">✓ Installed</span>
      ${tile&&tile.tile_exists&&tile.url?`<a class="btn btn-g btn-sm" href="${escA(tile.url)}" target="_blank" rel="noopener">Open</a>`:''}
      <button class="as-more" data-install="${escA(c.id)}" title="Install another copy" aria-label="Install another copy of ${escA(c.name)}">+</button>`;
  }
  return `<button class="btn btn-p btn-sm" data-install="${escA(c.id)}">Install</button>`;
}
function asRowHtml(c){
  const run=asJobFor(c.id);
  return `<div class="as-row${c.installed?' installed':''}${run?' installing':''}" data-id="${escA(c.id)}" tabindex="0">
    <div class="as-ico">${appIconHtml(c.icon)}</div>
    <div class="as-meta"><div class="as-name">${esc(c.name)}</div>
      <div class="as-sub">${esc(c.description||c.category||'')}</div></div>
    <div class="as-act">${asActHtml(c)}</div></div>`;
}
function asFeatHtml(c){
  const run=asJobFor(c.id);
  return `<div class="as-feat${c.installed?' installed':''}${run?' installing':''}" data-id="${escA(c.id)}" tabindex="0">
    <div class="as-ico">${appIconHtml(c.icon)}</div>
    <div class="as-name">${esc(c.name)}</div>
    <div class="as-desc">${esc(c.description||'')}</div>
    <div class="as-act">${asActHtml(c)}</div></div>`;
}
// Live update of one app (progress bar, "Installed") wherever it is on screen, without redrawing the page.
function asJobPaint(job){
  if(App.curV!=='appstore')return;
  if(job.kind==='update'){asInstalledPaint(job.tile_id);return}
  const c=_fullCatalog.find(x=>x.id===job.catalog_id);if(!c)return;
  const run=asJobFor(c.id);
  document.querySelectorAll(`.as-row[data-id="${CSS.escape(c.id)}"],.as-feat[data-id="${CSS.escape(c.id)}"]`).forEach(el=>{
    const act=el.querySelector('.as-act');if(act)act.innerHTML=asActHtml(c);
    el.classList.toggle('installing',!!run);el.classList.toggle('installed',!!c.installed&&!run);
  });
}
function asBodyClick(e){
  const job=e.target.closest('[data-job]');
  if(job){showJobDlg(job.dataset.job);return}
  const up=e.target.closest('[data-update]');
  if(up){showUpdateDlg(up.dataset.update,up.dataset.name);return}
  const un=e.target.closest('[data-uninstall]');
  if(un){showUninstallDlg(un.dataset.uninstall,un.dataset.name);return}
  const cat=e.target.closest('[data-cat]');
  if(cat){_asState.cat=cat.dataset.cat;_asShown=_AS_PAGE;_asAnim=true;asRender();window.scrollTo?.({top:0});document.getElementById('vroot')?.scrollTo?.({top:0,behavior:'smooth'});return}
  if(e.target.closest('a'))return;                 // "Open" goes to the app, not to the install dialog
  const inst=e.target.closest('[data-install]');
  if(inst){showInstallDlg(inst.dataset.install);return}
  const row=e.target.closest('.as-row[data-id],.as-feat[data-id]');
  if(row&&!row.classList.contains('as-skel'))showInstallDlg(row.dataset.id);
}
function asInstalledHtml(){
  if(!_installedList.length)return `<div class="as-empty"><div class="as-empty-ico">\u{1F4E6}</div><div class="as-empty-t">Nothing installed yet</div>
    <div class="sdesc">Apps you install from the App Store show up here, ready to open, update or remove.</div>
    <button class="btn btn-p btn-sm" id="asGoDiscover">Browse apps</button></div>`;
  return `<div class="as-instgrid">${_installedList.map(asInstalledCard).join('')}</div>`;
}
function asUpdateJob(tileId){return [..._appJobs.values()].find(j=>j.kind==='update'&&j.tile_id===tileId&&!j.done)}
function asInstalledCard(s){
  const info=_updateInfo?.[s.tile_id];
  const tags=Object.keys(s.image_tags||{}).length?Object.entries(s.image_tags).map(([svc,t])=>`${esc(svc)}: ${esc(t)}`).join(', '):'';
  const badge=info?(info.update_available?'<span class="as-tag warn">↑ Update available</span>':'<span class="as-tag ok">Up to date</span>'):'';
  const run=asUpdateJob(s.tile_id);
  return `<div class="as-card2 installed" data-tile="${escA(s.tile_id)}">
    <div class="as-top"><div class="as-ico">${appIconHtml(s.icon)}</div>${badge}</div>
    <div><div class="as-name">${esc(s.name)}${s.edited?' <span class="as-sub">(compose edited)</span>':''}</div>
      <div class="as-sub">${s.tile_exists?esc(s.url||''):'Tile missing — was it deleted manually?'}</div></div>
    ${tags?`<div class="as-tags">${tags}</div>`:''}
    <div class="as-foot as-inst-foot">${asInstalledActs(s,run)}</div></div>`;
}
function asInstalledActs(s,run){
  if(run){const p=run.progress;
    return `<button class="as-run" data-job="${escA(run.id)}"><span class="as-prog${p==null?' indet':''}"><i style="width:${p==null?40:Math.floor(p)}%"></i></span>
      <span class="as-run-line"><span>${esc(run.stage||'Updating…')}</span><b>${p==null?'':Math.floor(p)+'%'}</b></span></button>`}
  return `${s.tile_exists&&s.url?`<a class="btn btn-p btn-sm" href="${escA(s.url)}" target="_blank" rel="noopener">Open</a>`:''}
    <button class="btn btn-g btn-sm" data-update="${escA(s.tile_id)}" data-name="${escA(s.name)}">↑ Update</button>
    <button class="btn btn-d btn-sm" data-uninstall="${escA(s.tile_id)}" data-name="${escA(s.name)}">Uninstall</button>`;
}
function asInstalledPaint(tileId){
  const s=_installedList.find(x=>x.tile_id===tileId);if(!s)return;
  const card=document.querySelector(`.as-card2[data-tile="${CSS.escape(tileId)}"]`);if(!card)return;
  card.querySelector('.as-inst-foot').innerHTML=asInstalledActs(s,asUpdateJob(tileId));
}
async function checkForUpdates(){
  const btn=document.getElementById('asCheckUpdatesBtn');
  if(btn){btn.disabled=true;btn.textContent='Checking…'}
  try{
    const list=await apiData('/api/appstore/updates');
    _updateInfo={};
    list.forEach(u=>{_updateInfo[u.tile_id]=u});
    const n=list.filter(u=>u.update_available).length;
    _asState.tab='installed';asRender();
    toast(n?`${n} update${n===1?'':'s'} available`:'Everything is up to date','info');
  }catch(e){toast('Could not check for updates','error',{hint:e.message})}
  finally{if(btn){btn.disabled=false;btn.textContent='↻ Check for updates'}}
}
async function showInstallDlg(catalogId){
  const entry=_fullCatalog.find(c=>c.id===catalogId);const name=entry?.name||'app';
  const catWarnings=(entry?.warnings||[]).map(w=>`<li>${esc(w)}</li>`).join('');
  openMdl(`Install ${name}`,`
    <div class="as-dlg-head"><div class="as-ico">${appIconHtml(entry?.icon)}</div>
      <div><div class="as-name">${esc(name)}</div><div class="as-sub">${esc(entry?.category||'')}${entry?.min_ram_mb?` · about ${entry.min_ram_mb} MB RAM`:''}</div></div></div>
    <div class="sdesc" style="margin-top:12px">${esc(entry?.description||'')}</div>
    <div class="sdesc" style="margin-top:8px">Void OS generates a docker-compose stack, starts it, and adds a tile once it’s up — usually a few
      seconds, longer the first time while images download. You can close the window after you press Install; it keeps going.</div>
    ${entry?.installed?`<div class="as-already">✓ Already installed — this adds a second, separate copy.</div>`:''}
    ${catWarnings?`<ul style="margin:10px 0 0 18px;font-size:12.5px;color:var(--yw)">${catWarnings}</ul>`:''}
    <div id="asPreviewWrap" style="margin-top:14px"><div class="loading"><div class="loader"></div><span>Preparing preview…</span></div></div>`,
    `<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-p" id="installGoBtn" disabled>Install</button>`,
    true);
  const loadPreview=async(opts)=>{
    let preview=null;
    try{
      preview=await(await api('/api/appstore/preview',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({catalog_id:catalogId,options:opts})})).json();
    }catch(e){/* handled below via preview===null */}
    const wrap=document.getElementById('asPreviewWrap');
    if(!wrap)return; // modal already closed
    if(!preview||preview.error){
      wrap.innerHTML=`<div style="color:var(--rd)">${esc(preview?.error||'Could not load a preview')}</div>`;
      return;
    }
    const tagNotes=(preview.tag_notes||[]).map(n=>`<div>${esc(n)}</div>`).join('');
    const composeWarnings=(preview.warnings||[]).map(w=>`<li>${esc(w)}</li>`).join('');
    const checks=preview.preflight||[];
    const blocked=checks.some(c=>c.level==='block');
    const col={block:'var(--rd)',warn:'var(--yw)',info:'var(--td)'},ico={block:'\u26d4',warn:'\u26a0',info:'\u2139'};
    const checksHtml=checks.length?`<div style="margin-bottom:10px;display:flex;flex-direction:column;gap:6px">${checks.map(c=>
      `<div style="font-size:12.5px;color:${col[c.level]||'var(--td)'};line-height:1.45">${ico[c.level]||''} ${esc(c.text)}</div>`).join('')}</div>`:'';
    const optsHtml=(preview.options||[]).map(o=>`
      <label class="as-opt" style="display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border:1px solid var(--bd);border-radius:10px;margin-bottom:8px;cursor:pointer">
        ${vCheckbox('asOpt_'+o.id,o.enabled,'',true)}
        <span><span style="font-weight:600;font-size:13px">${esc(o.label)}</span>
          ${o.recommended_off?`<span class="as-tag warn" style="margin-left:6px">Off is recommended on this device</span>`:''}
          <span class="sdesc" style="display:block;margin-top:2px">${esc(o.description||'')}</span></span>
      </label>`).join('');
    wrap.innerHTML=`
      ${checksHtml}
      ${optsHtml}
      ${tagNotes?`<div class="sdesc" style="margin-bottom:8px">${tagNotes}</div>`:''}
      ${composeWarnings?`<div style="background:rgba(var(--yw-rgb),.07);border:1px solid rgba(var(--yw-rgb),.18);border-radius:8px;padding:10px 12px;margin-bottom:8px">
        <div style="font-weight:600;color:var(--yw);font-size:12px;margin-bottom:4px">\u26a0 Worth a look before installing</div>
        <ul style="margin:0 0 0 16px;font-size:12px;color:var(--yw)">${composeWarnings}</ul></div>`:''}
      <details class="as-details" ${preview.can_edit?'':''}><summary>docker-compose.yml ${preview.can_edit?'\u2014 you can edit it before installing':''}</summary>
        <textarea class="fgi" id="asComposeText" ${preview.can_edit?'':'readonly'} spellcheck="false"
          style="font-family:var(--f-mono);font-size:11.5px;min-height:260px;white-space:pre;resize:vertical;margin-top:8px">${esc(preview.compose)}</textarea>
        ${!preview.can_edit?'<div class="sdesc" style="margin-top:4px">Editing the compose file before install requires Terminal access as well as Docker access.</div>':''}
      </details>`;
    const state={};(preview.options||[]).forEach(o=>{state[o.id]=o.enabled});
    (preview.options||[]).forEach(o=>{
      document.getElementById('asOpt_'+o.id)?.addEventListener('change',e=>{
        // a different option set means a different compose file: preview again so the editor shows the real thing
        const next={...state,[o.id]:e.target.checked};
        document.getElementById('installGoBtn').disabled=true;
        wrap.innerHTML='<div class="loading"><div class="loader"></div><span>Updating the preview\u2026</span></div>';
        loadPreview(next);
      });
    });
    const goBtn=document.getElementById('installGoBtn');
    goBtn.disabled=blocked;
    goBtn.onclick=()=>{preview._options=state;doInstall(catalogId,preview,false)};
  };
  loadPreview(undefined);
}
async function doInstall(catalogId,preview,confirmRisky){
  const goBtn=document.getElementById('installGoBtn');
  const composeEl=document.getElementById('asComposeText');
  const editedText=composeEl?composeEl.value:null;
  const wasEdited=preview.can_edit&&editedText!==null&&editedText!==preview.compose;
  const body={catalog_id:catalogId,instance_id:preview.instance_id,options:preview._options};
  if(wasEdited){body.compose_override=editedText;body.secrets_generated=preview.secrets_generated}
  if(confirmRisky)body.confirm_risky=true;
  goBtn.disabled=true;goBtn.textContent='Starting…';
  const r=await api('/api/appstore/install',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  if(d.error==='risky_yaml'){
    // Warn-and-confirm, not a silent block — show exactly what was flagged and let the admin proceed if they mean to.
    goBtn.disabled=false;goBtn.textContent='Install Anyway';goBtn.className='btn btn-d';
    const warn=(d.warnings||[]).map(w=>`<li>${esc(w)}</li>`).join('');
    document.getElementById('asPreviewWrap').insertAdjacentHTML('afterbegin',
      `<div style="background:rgba(var(--rd-rgb),.08);border:1px solid rgba(var(--rd-rgb),.25);border-radius:8px;padding:10px 12px;margin-bottom:10px">
        <div style="font-weight:600;color:var(--rd);font-size:12px;margin-bottom:4px">This compose file requests elevated host access</div>
        <ul style="margin:0 0 8px 16px;font-size:12px;color:var(--rd)">${warn}</ul>
        <div style="font-size:12px;color:var(--td)">If you understand why and still want to proceed:</div></div>`);
    goBtn.onclick=()=>doInstall(catalogId,preview,true);
    return;
  }
  if(d.error){goBtn.disabled=false;goBtn.textContent='Install';errToast(d);return}
  const c=_fullCatalog.find(x=>x.id===catalogId);
  appJobApply({id:d.job_id,kind:'install',catalog_id:catalogId,label:c?.name||'app',progress:null,stage:'Starting…',done:false});
  showJobDlg(d.job_id);
}
function showUpdateDlg(tileId,name){
  const info=_updateInfo?.[tileId];
  const tagDiff=info?Object.entries(info.latest_tags||{}).map(([svc,tag])=>{
    const cur=info.current_tags?.[svc];
    return cur&&cur!==tag?`${esc(svc)}: ${esc(cur)} → ${esc(tag)}`:`${esc(svc)}: ${esc(tag)} (already latest)`;
  }).join('<br>'):'';
  openMdl(`Update ${name}`,`
    <div class="sdesc">Pulls the latest image tag(s) and recreates only the containers that changed. Your data and
      configuration are untouched — this never regenerates secrets or touches the app's data folder.</div>
    ${tagDiff?`<div class="sdesc" style="margin-top:10px;font-family:var(--f-mono);font-size:11.5px">${tagDiff}</div>`:''}`,
    `<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-p" id="installGoBtn">Update</button>`);
  document.getElementById('installGoBtn')?.addEventListener('click',async()=>{
    const b=document.getElementById('installGoBtn');b.disabled=true;b.textContent='Starting…';
    const r=await api(`/api/appstore/update/${tileId}`,{method:'POST'});
    const d=await r.json();
    if(d.error){b.disabled=false;b.textContent='Update';errToast(d);return}
    appJobApply({id:d.job_id,kind:'update',tile_id:tileId,label:name,progress:null,stage:'Starting…',done:false});
    showJobDlg(d.job_id);
  });
}
function showUninstallDlg(tileId,name){
  openMdl(`Uninstall ${esc(name)}`,`
    <div class="sdesc">This stops and removes the containers and the tile. App data is kept by default.</div>
    <label style="display:flex;align-items:center;gap:8px;margin-top:14px;padding:10px;border:1px solid var(--rd);border-radius:8px;cursor:pointer">
      ${vCheckbox('uninstallRemoveData',false,'',true)}
      <span style="font-size:13px;color:var(--rd)">Also permanently delete all its data (photos, vault, config \u2014 everything)</span>
    </label>
    <div id="uninstallConfirmWrap" style="display:none;margin-top:10px">
      <label class="fgl">Type "${esc(name)}" to confirm permanent data deletion</label>
      <input class="fgi" id="uninstallConfirmText" placeholder="${escA(name)}"/>
    </div>`,
    `<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-d" id="uninstallGoBtn">Uninstall</button>`);
  const removeCb=document.getElementById('uninstallRemoveData'),confirmWrap=document.getElementById('uninstallConfirmWrap');
  removeCb.addEventListener('change',()=>{confirmWrap.style.display=removeCb.checked?'block':'none'});
  document.getElementById('uninstallGoBtn')?.addEventListener('click',async()=>{
    const removeData=removeCb.checked;
    if(removeData&&document.getElementById('uninstallConfirmText').value.trim()!==name){
      toast('Type the app name exactly to confirm data deletion','error');return;
    }
    const r=await api(`/api/appstore/uninstall/${tileId}`,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({remove_data:removeData})});
    const d=await r.json();
    if(d.error){errToast(d);return}
    toast(removeData?'Uninstalled, data deleted':'Uninstalled, data kept','info');
    closeMdl();loadInstalled();loadTiles?.();
  });
}
window.rAppStore=rAppStore;

// Builds a toggle using the same gh-switch design used in the Admin panel (Uiverse.io
// gharsh11032000). extraAttrs lets callers attach data-* attributes (e.g. data-vis/data-id)
// to the actual <input>, so existing querySelectorAll/getElementById wiring keeps working.
function ghSwitch(id,checked,extraAttrs,extraClass){
  return `<label class="gh-switch${extraClass?' '+extraClass:''}"><input type="checkbox" id="${id}" ${checked?'checked':''} ${extraAttrs||''}/><span class="gh-slider"></span></label>`;
}
// Builds a checkbox using the themed .vcbx design (see CSS above). extraAttrs lets
// callers attach data-*/other attributes to the actual <input>; pass danger=true for
// the red variant used on destructive-action checkboxes (mirrors ghSwitch's gh-red).
function vCheckbox(id,checked,extraAttrs,danger){
  return `<span class="vcbx${danger?' vcbx-danger':''}"><input type="checkbox" id="${id}" ${checked?'checked':''} ${extraAttrs||''}/>
    <svg viewBox="0 0 22 22"><path d="M5.5,11.3L9,14.8L20.2,3.3"/></svg></span>`;
}


async function rAccess(){
  if(!IS_ADMIN){go('dashboard');return}
  const root=document.getElementById('vroot');
  root.innerHTML=`<div class="vh"><div><h1 class="vtitle">Access Control</h1><div class="vsub">People, permissions and protection</div></div>
    <button class="btn btn-p btn-sm" onclick="showAddUser()">\u002B New user</button></div>
  <div id="usersSection"><div class="loading"><div class="loader"></div><span>Loading users\u2026</span></div></div>
  <div class="ac-grid">
    <div class="admin-section">
      <div class="admin-section-title">App visibility</div>
      <div class="sdesc" style="margin-bottom:12px">Control which app tiles each rank can see. Hiding a tile from
        Admins is Super-User-only — an Admin can't see or affect a tile hidden from them, same as any other
        Super-User-restricted setting.</div>
      <div id="visTable"><div class="loading"><div class="loader"></div><span>Loading tiles\u2026</span></div></div>
    </div>
    ${IS_SUPERUSER?`<div class="admin-section">
      <div class="admin-section-title">Terminal password</div>
      <div class="sdesc" style="margin-bottom:12px">A second password required to open the Terminal tab, on top of normal account
        login \u2014 anyone signed in still needs this to get a shell. Configured during first-boot setup; change or remove it here.
        Takes a restart to apply, like every other .env-backed setting.</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end">
        <div class="fg" style="flex:1;min-width:200px;margin-bottom:0"><label class="fgl" id="termPwLbl">Loading\u2026</label>
          <input class="fgi" type="password" id="termPwInp" placeholder="New terminal password"/></div>
        <button class="btn btn-p btn-sm" id="termPwSaveBtn">Save</button>
        <button class="btn btn-d btn-sm" id="termPwClearBtn">Remove password</button>
      </div>
      <div id="termPwStat" style="font-size:12px;color:var(--td);margin-top:8px"></div>
      <button class="btn btn-g btn-sm" id="termPwRestartBtn" style="margin-top:8px">Restart Void OS now</button>
    </div>`:''}
    <div class="admin-section">
      <div class="admin-section-title">External storage</div>
      <div class="sdesc" id="storageDesc" style="margin-bottom:12px">Mount USB drives and SMB/NFS network shares into Void OS's own
        Files view, under <code>${FS_DRIVES}/</code> \u2014 matches home-screen-LocalStorage's job. USB mounting needs
        <code>udisksctl</code> (no root); SMB/NFS need root or passwordless sudo for <code>mount</code>/<code>umount</code>.</div>
      <div id="storageCapStatus" style="font-size:11.5px;color:var(--td);margin-bottom:12px"></div>
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <span style="font-size:12.5px;font-weight:600">USB Drives</span>
        <div style="display:flex;align-items:center;gap:8px;font-size:12px">
          ${ghSwitch('usbAutoMount',false)}<label for="usbAutoMount" style="cursor:pointer">Auto-mount on insert</label>
          ${ghSwitch('usbAutoRepair',true)}<label for="usbAutoRepair" style="cursor:pointer" title="If the server lost power and a drive was left dirty, check and fix it before mounting">Repair after a power cut</label>
        </div>
      </div>
      <div id="usbMountRow" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px">
        <label class="fgl" for="usbMountDir" style="margin:0;white-space:nowrap">Mount folder</label>
        <input class="fgi" id="usbMountDir" placeholder="default" spellcheck="false" autocomplete="off" style="flex:1;min-width:220px;font-size:12px"/>
        <button class="btn btn-g btn-sm" id="usbMountDirSave" type="button">Save</button>
        <button class="btn btn-g btn-sm" id="usbMountDirReset" type="button" title="Mount drives under the Drives folder in Files again">Use default</button>
      </div>
      <div class="sdesc" id="usbMountNote" style="margin-bottom:12px"></div>
      <div id="usbList"><div class="loading"><div class="loader"></div></div></div>
      <div style="display:flex;align-items:center;justify-content:space-between;margin:18px 0 8px">
        <span style="font-size:12.5px;font-weight:600">Network Shares</span>
        <button class="btn btn-p btn-sm" onclick="showAddNetworkShare()">Add Share</button>
      </div>
      <div id="networkShareList"><div class="loading"><div class="loader"></div></div></div>
    </div>
    ${IS_SUPERUSER?`    <div class="admin-section">
      <div class="admin-section-title">Panic button</div>
      <div class="sdesc" style="margin-bottom:12px">Immediately stops every app tile's Cloudflare tunnel — never the
        dashboard's own tunnel, so triggering this can't lock you out of undoing it. Tier 2 additionally stops every
        running Docker container.</div>
      ${CAN_DOCK?`<div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
        ${ghSwitch('panicTier2',false,'','gh-red')}<label for="panicTier2" style="font-size:13px;cursor:pointer">Also stop all Docker containers (Tier 2)</label>
      </div>`:''}
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
        <button class="btn btn-sm" id="revealPanicBtn">Show Panic Button</button>
        <div id="panicBtnWrap" style="display:none;gap:10px;flex-wrap:wrap;align-items:center">
          <input type="checkbox" id="panicBtnCb" class="panic-switch-input"/>
          <label for="panicBtnCb" id="panicBtn" class="panic-switch">
            Trigger Panic
            <svg viewBox="0 0 512 512" height="1em" xmlns="http://www.w3.org/2000/svg">
              <path d="M288 32c0-17.7-14.3-32-32-32s-32 14.3-32 32V256c0 17.7 14.3 32 32 32s32-14.3 32-32V32zM143.5 120.6c13.6-11.3 15.4-31.5 4.1-45.1s-31.5-15.4-45.1-4.1C49.7 115.4 16 181.8 16 256c0 132.5 107.5 240 240 240s240-107.5 240-240c0-74.2-33.8-140.6-86.6-184.6c-13.6-11.3-33.8-9.4-45.1 4.1s-9.4 33.8 4.1 45.1c38.9 32.3 63.5 81 63.5 135.4c0 97.2-78.8 176-176 176s-176-78.8-176-176c0-54.4 24.7-103.1 63.5-135.4z"></path>
            </svg>
          </label>
          <button class="btn btn-g" id="panicUndoBtn" style="display:none">Undo Last Panic</button>
        </div>
      </div>
      <div id="panicResult" style="margin-top:10px;font-size:13px;color:var(--td)"></div>
    </div>`:''}
  </div>
  <div id="auditSection" style="margin-top:24px"><div class="loading"><div class="loader"></div><span>Loading activity log\u2026</span></div></div>
  <div id="errorLogSection" style="margin-top:24px"></div>`;
  document.getElementById('revealPanicBtn')?.addEventListener('click',e=>{
    document.getElementById('panicBtnWrap').style.display='flex';
    e.target.style.display='none';
  });
  document.getElementById('panicBtn')?.addEventListener('click',triggerPanic);
  document.getElementById('panicBtn')?.addEventListener('click',()=>{
    const cb=document.getElementById('panicBtnCb');
    if(cb)setTimeout(()=>{cb.checked=false},900); // it's a one-shot action, not a persisted state — flash then reset
  });
  document.getElementById('panicUndoBtn')?.addEventListener('click',undoPanic);
  await loadUsers();
  await loadVisTable();
  loadAuditLog();                       // not queued behind the drive scans below, which can take a while
  loadErrorLog();
  await loadStorageCapabilities();
  await loadUsbList();
  await loadNetworkShares();
  if(IS_SUPERUSER)initTermPwControls();
  document.getElementById('usbAutoMount')?.addEventListener('change',async e=>{
    const checked=e.target.checked;
    const r=await(await api('/api/storage/usb/auto-mount',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:checked})})).json();
    if(r.ok)toast(checked?'USB auto-mount enabled':'USB auto-mount disabled','success');
    else{errToast(r,'Couldn\u2019t change USB auto-mount');e.target.checked=!checked}
  });
  const saveMountDir=async(value)=>{
    const r=await(await api('/api/storage/usb/auto-mount',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({mount_dir:value})})).json();
    if(r.ok){toast(value?'Drives will be mounted in '+r.mount_base:'Drives will be mounted in the Drives folder again','success');loadUsbList()}
    else errToast(r,'Couldn\u2019t use that folder');
  };
  document.getElementById('usbMountDirSave')?.addEventListener('click',()=>saveMountDir((document.getElementById('usbMountDir').value||'').trim()));
  document.getElementById('usbMountDirReset')?.addEventListener('click',()=>{document.getElementById('usbMountDir').value='';saveMountDir('')});
  document.getElementById('usbMountDir')?.addEventListener('keydown',e=>{if(e.key==='Enter')document.getElementById('usbMountDirSave').click()});
  document.getElementById('usbAutoRepair')?.addEventListener('change',async e=>{
    const checked=e.target.checked;
    const r=await(await api('/api/storage/usb/auto-mount',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({auto_repair:checked})})).json();
    if(r.ok)toast(checked?'Drives will be repaired after a power cut':'Automatic drive repair is off','success');
    else{errToast(r,'Couldn\u2019t change automatic repair');e.target.checked=!checked}
  });
}
let _storPlat='linux';
async function loadStorageCapabilities(){
  const el=document.getElementById('storageCapStatus');if(!el)return;
  try{
    const c=await(await api('/api/storage/capabilities')).json();
    _storPlat=c.platform||'linux';
    const desc=document.getElementById('storageDesc');
    if(desc&&_storPlat==='windows')desc.innerHTML=`Link USB drives and SMB/NFS network shares into Void OS's own Files view, under
      <code>${FS_DRIVES}/</code>. Windows already gives USB drives a letter \u2014 <b>Link to Files</b> makes one browsable here
      (an NTFS junction, no admin needed) and <b>Eject</b> safely removes it. Network shares are linked with a directory
      symlink, which needs Developer Mode or running Void OS as Administrator; NFS also needs Windows' <i>Client for NFS</i> feature.`;
    if(_storPlat==='windows'){loadUsbList();const mr=document.getElementById('usbMountRow'),mn=document.getElementById('usbMountNote');if(mr)mr.style.display='none';if(mn)mn.style.display='none'}
    const line=(label,cap)=>`<span style="color:${cap.ok?'var(--gr)':'var(--yw)'}">${cap.ok?'\u2713':'\u26a0'} ${label}: ${esc(cap.message)}</span>`;
    el.innerHTML=[line('USB',c.usb),line('SMB',c.smb),line('NFS',c.nfs)].join('<br>');
  }catch(e){el.textContent='Failed to check capabilities'}
}
async function loadUsbList(){
  const el=document.getElementById('usbList');if(!el)return;
  try{
    const d=await apiData('/api/storage/usb');
    document.getElementById('usbAutoMount').checked=!!d.auto_mount;
    const mdi=document.getElementById('usbMountDir');
    if(mdi){if(document.activeElement!==mdi)mdi.value=d.mount_dir||'';mdi.placeholder='default ('+(d.default_mount_dir||'')+')'}
    const mnote=document.getElementById('usbMountNote');
    if(mnote)mnote.innerHTML=`Drives are mounted in <code>${esc(d.mount_base||'')}</code>${d.mount_dir?'':' (the default)'} and appear in Files under <b>${FS_DRIVES}</b>.`
      +(d.can_mount===false?` <span style="color:var(--yw)">Automatic mounting needs passwordless sudo for <code>mount</code> and <code>umount</code> (the installer sets it up; re-run it if this server was installed before), otherwise Void OS has to fall back to the desktop's own mounting, which usually refuses a background service.</span>`:'');
    const rp=document.getElementById('usbAutoRepair');if(rp)rp.checked=d.auto_repair!==false;
    if((d.repairing||[]).length&&!window._usbRepairPoll)window._usbRepairPoll=setTimeout(()=>{window._usbRepairPoll=null;loadUsbList()},5000);
    if(!d.devices.length){el.innerHTML='<div class="sdesc">No USB storage devices detected</div>';return}
    el.innerHTML=`<div class="user-table">${d.devices.map(dev=>`
      <div class="user-row">
        <div class="user-avatar avatar-guest">\u{1F4BE}</div>
        <div class="user-info"><div class="user-name">${esc(dev.label||dev.device)}</div>
          <div class="user-meta">${esc(dev.device)} \u00b7 ${fmtB(dev.size||0)} \u00b7 ${dev.mountpoint?('Mounted at '+esc(dev.mountpoint)+(dev.bound_at?' \u2192 browsable at '+esc(dev.bound_at):(_storPlat==='windows'?' (not linked into Files yet)':' (not browsable in Files \u2014 needs mount privilege)'))):'Not mounted'}</div></div>
        ${dev.error?`<div class="user-meta" style="color:var(--rd);white-space:normal;flex-basis:100%">Couldn\u2019t mount: ${esc(dev.error)}</div>`:''}
        <div class="user-actions">
          ${dev.holds_files?`<span class="ix-tag quiet" title="Void OS keeps its Files folder on this drive">In use by Void OS</span>`:''}
          ${!dev.holds_files&&dev.mountpoint&&!dev.bound_at
            ?`<button class="btn btn-g btn-sm" onclick="mountUsb(${jsA(dev.device)})" title="Make this drive browsable in Files under ${FS_DRIVES}/">Link to Files</button>`:''}
          ${dev.holds_files?''
            :dev.mountpoint
            ?`<button class="btn btn-d btn-sm" onclick="unmountUsb(${jsA(dev.device)})">${_storPlat==='windows'?'Eject':'Unmount'}</button>`
            :((d.repairing||[]).includes(dev.device)
              ?`<button class="btn btn-g btn-sm" disabled title="Checking and repairing the file system">Repairing…</button>`
              :`<button class="btn btn-g btn-sm" onclick="mountUsb(${jsA(dev.device)})">Mount</button>`)}
        </div>
      </div>`).join('')}</div>`;
  }catch(e){el.innerHTML=`<div class="sdesc" style="color:var(--rd)">Couldn’t load: ${esc(e.message)}</div>`}
}
async function mountUsb(device){
  const r=await(await api('/api/storage/usb/mount',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device})})).json();
  if(r.ok&&r.repairing){toast(r.message||'Repairing the drive first…','info');loadUsbList()}
  else if(r.ok){toast('Mounted','success');loadUsbList()}
  else errToast(r,'Mount failed');
}
async function unmountUsb(device){
  const r=await(await api('/api/storage/usb/unmount',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device})})).json();
  if(r.ok){toast('Unmounted','success');loadUsbList()}
  else errToast(r,'Unmount failed');
}
async function loadNetworkShares(){
  const el=document.getElementById('networkShareList');if(!el)return;
  try{
    const shares=await apiData('/api/storage/network');
    if(!shares.length){el.innerHTML='<div class="sdesc">No network shares configured</div>';return}
    el.innerHTML=`<div class="user-table">${shares.map(s=>`
      <div class="user-row">
        <div class="user-avatar avatar-admin">${s.type==='smb'?'\u{1F5A5}\uFE0F':'\u{1F5C4}\uFE0F'}</div>
        <div class="user-info"><div class="user-name">${esc(s.name)} <span class="role-badge role-admin">${s.type.toUpperCase()}</span></div>
          <div class="user-meta">${esc(s.server)}/${esc(s.share)} \u2192 ${FS_DRIVES}/${esc(s.slug)} ${s.mounted?'':' \u00b7 <span style="color:var(--yw)">not currently mounted</span>'}</div></div>
        <div class="user-actions"><button class="btn btn-d btn-sm" onclick="deleteNetworkShare('${s.id}')">Remove</button></div>
      </div>`).join('')}</div>`;
  }catch(e){el.innerHTML=`<div class="sdesc" style="color:var(--rd)">Couldn’t load: ${esc(e.message)}</div>`}
}
async function deleteNetworkShare(id){
  const r=await(await api(`/api/storage/network/${id}`,{method:'DELETE'})).json();
  if(r.ok){toast('Share removed','success');loadNetworkShares()}
  else errToast(r,'Failed to remove');
}
function showAddNetworkShare(){
  openMdl('Add Network Share',`
    <div class="fg"><label class="fgl">Type</label>
      <div class="seg" id="nsType"><button class="seg-btn active" data-type="smb">SMB / CIFS</button><button class="seg-btn" data-type="nfs">NFS</button></div>
    </div>
    <div class="fg"><label class="fgl">Name</label><input class="fgi" id="nsName" placeholder="My NAS"/></div>
    <div class="fg"><label class="fgl">Server</label><input class="fgi" id="nsServer" placeholder="192.168.1.10"/></div>
    <div class="fg"><label class="fgl" id="nsShareLbl">Share name</label><input class="fgi" id="nsShare" placeholder="media"/></div>
    <div id="nsSmbFields">
      <div class="fg"><label class="fgl">Username (blank = guest)</label><input class="fgi" id="nsUser"/></div>
      <div class="fg"><label class="fgl">Password</label><input class="fgi" id="nsPass" type="password"/></div>
      <div class="fg"><label class="fgl">Domain (optional)</label><input class="fgi" id="nsDomain"/></div>
    </div>`,
    `<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-p" id="nsSaveBtn">Mount</button>`);
  document.querySelectorAll('#nsType .seg-btn').forEach(b=>b.addEventListener('click',()=>{
    document.querySelectorAll('#nsType .seg-btn').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    const nfs=b.dataset.type==='nfs';
    document.getElementById('nsSmbFields').style.display=nfs?'none':'';
    document.getElementById('nsShareLbl').textContent=nfs?'Export path':'Share name';
    document.getElementById('nsShare').placeholder=nfs?'export/media':'media';
  }));
  document.getElementById('nsSaveBtn')?.addEventListener('click',async()=>{
    const type=document.querySelector('#nsType .seg-btn.active').dataset.type;
    const body={
      type, name:document.getElementById('nsName').value.trim(),
      server:document.getElementById('nsServer').value.trim(),
      share:document.getElementById('nsShare').value.trim(),
    };
    if(type==='smb'){
      body.username=document.getElementById('nsUser').value.trim();
      body.password=document.getElementById('nsPass').value;
      body.domain=document.getElementById('nsDomain').value.trim();
    }
    if(!body.server||!body.share){toast('Server and share/export are required','error');return}
    const r=await(await api('/api/storage/network',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
    if(r.ok){toast('Share mounted','success');closeMdl();loadNetworkShares()}
    else errToast(r,'Mount failed');
  });
}
window.mountUsb=mountUsb;window.unmountUsb=unmountUsb;
window.deleteNetworkShare=deleteNetworkShare;window.showAddNetworkShare=showAddNetworkShare;

// ── Terminal password (Access Control) ───────────────────────────────────
async function initTermPwControls(){
  const lbl=document.getElementById('termPwLbl'),inp=document.getElementById('termPwInp');
  const saveBtn=document.getElementById('termPwSaveBtn'),clearBtn=document.getElementById('termPwClearBtn'),stat=document.getElementById('termPwStat');
  if(!lbl)return;
  try{
    const st=await apiData('/api/setup/status');
    const isSet=!!st.defaults?.terminal_password_set;
    lbl.textContent=isSet?'Change password (currently set)':'Set a terminal password';
    inp.placeholder=isSet?'\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022':'New terminal password';
    clearBtn.style.display=isSet?'':'none';
  }catch(e){lbl.textContent='Terminal password'}
  document.getElementById('termPwRestartBtn')?.addEventListener('click',()=>{
    confirmDlg('Restart Void OS now to apply any pending .env changes? If it\'s running as a systemd/Docker-managed service it\'ll come back up automatically; if you started it manually you\'ll need to restart it yourself.',async()=>{
      const r=await api('/api/admin/restart',{method:'POST'});
      if(!r.ok&&r.status!==0){errToast(await r.json(),'Couldn’t restart');return}
      Conn.restarting();  // dims the page, waits for the server, then reloads
    },'Restart');
  });
  saveBtn.addEventListener('click',async()=>{
    const pw=inp.value;
    if(!pw||pw.length<6){stat.textContent='Password must be at least 6 characters';stat.style.color='var(--rd)';return}
    saveBtn.disabled=true;
    const r=await(await api('/api/setup/configure',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({terminal_password:pw})})).json();
    saveBtn.disabled=false;
    if(r.ok){stat.textContent='Saved \u2014 restart Void OS to apply';stat.style.color='var(--gr)';inp.value='';initTermPwControls()}
    else{stat.textContent=r.error||'Failed to save';stat.style.color='var(--rd)'}
  });
  clearBtn.addEventListener('click',()=>{
    confirmDlg('Remove the terminal password? Anyone with account access will be able to open a shell without it (after a restart).',async()=>{
      const r=await(await api('/api/setup/configure',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({terminal_password_clear:true})})).json();
      if(r.ok){stat.textContent='Removed \u2014 restart Void OS to apply';stat.style.color='var(--gr)';initTermPwControls()}
      else{stat.textContent=r.error||'Failed to remove';stat.style.color='var(--rd)'}
    },'Remove');
  });
}
async function loadVisTable(){
  const gen=_renderGen;
  const el=document.getElementById('visTable');
  if(!el)return;
  try{
    const apps=await apiData('/api/apps');
    if(gen!==_renderGen)return;
    const anyPanicked=apps.some(a=>a.tunnel_enabled_before_panic);
    const undoBtn=document.getElementById('panicUndoBtn');
    if(undoBtn)undoBtn.style.display=anyPanicked?'inline-flex':'none';
    el.innerHTML=`<div class="user-table">${apps.map(a=>`
      <div class="user-row">
        <div class="user-avatar avatar-guest app-ico">${appIconHtml(a.icon)}</div>
        <div class="user-info"><div class="user-name">${esc(a.name)}</div>
          <div class="user-meta">${a.tunnel_enabled_before_panic?'<span style="color:var(--yw)">Stopped by Panic</span> &nbsp;\u2022&nbsp; ':''}${esc(a.description||'')||'&mdash;'}</div></div>
        <div class="user-actions" style="gap:20px">
          <div style="display:flex;align-items:center;gap:8px;font-size:12px">
            ${ghSwitch(`vis-guests-${a.id}`,a.hidden_from_guests,`data-vis="hidden_from_guests" data-id="${a.id}"`)}
            <label for="vis-guests-${a.id}" style="cursor:pointer">Hidden from Guests</label>
          </div>
          <div style="display:flex;align-items:center;gap:8px;font-size:12px;${IS_SUPERUSER?'':'opacity:.45;pointer-events:none'}">
            ${ghSwitch(`vis-admins-${a.id}`,a.hidden_from_admins,`data-vis="hidden_from_admins" data-id="${a.id}" ${IS_SUPERUSER?'':'disabled'}`)}
            <label for="vis-admins-${a.id}" style="cursor:pointer">Hidden from Admins</label>
          </div>
        </div>
      </div>`).join('')}</div>`;
    el.querySelectorAll('input[data-vis]').forEach(cb=>{
      cb.addEventListener('change',async()=>{
        const r=await api(`/api/apps/${cb.dataset.id}/visibility`,{method:'PUT',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({[cb.dataset.vis]:cb.checked})});
        const d=await r.json();
        if(d.error){errToast(d);cb.checked=!cb.checked;return}
        toast('Visibility updated','success');
      });
    });
  }catch(e){el.innerHTML=`<div style="color:var(--rd)">${esc(e.message)}</div>`}
}
async function triggerPanic(){
  const tier2=document.getElementById('panicTier2')?.checked;
  const r=await api('/api/admin/panic',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({tier2:!!tier2})});
  const d=await r.json();
  if(d.error){errToast(d);return}
  const parts=[`${d.tunnels_stopped} tunnel${d.tunnels_stopped===1?'':'s'} stopped`];
  if(d.containers_stopped!==null)parts.push(`${d.containers_stopped} container${d.containers_stopped===1?'':'s'} stopped`);
  document.getElementById('panicResult').textContent=parts.join(', ')+'.';
  toast('Panic triggered','info');
  loadVisTable();loadTiles?.();
}
async function undoPanic(){
  const r=await api('/api/admin/panic/undo',{method:'POST'});
  const d=await r.json();
  if(d.error){errToast(d);return}
  document.getElementById('panicResult').textContent=`${d.tunnels_restored} tunnel${d.tunnels_restored===1?'':'s'} restored.`;
  toast('Panic undone','success');
  loadVisTable();loadTiles?.();
}
window.rAccess=rAccess;

async function loadUsers(){
  const gen=_renderGen;
  const container=document.getElementById('usersSection');
  if(!container)return;
  try{
    const users=await apiData('/api/users');
    if(gen!==_renderGen)return;
    container.innerHTML=`<div class="admin-section">
      <div class="admin-section-title">All users (${users.length})</div>
      <div class="user-table" id="userTable">
        ${users.map(u=>userRow(u)).join('')}
      </div></div>`;
    document.getElementById('userTable')?.addEventListener('click',e=>{
      const btn=e.target.closest('button[data-act]');
      if(!btn)return;
      const target=users.find(u=>u.id===btn.dataset.uid);
      if(!target)return;
      switch(btn.dataset.act){
        case 'pwd':      showChangePwd(target.id,target.username); break;
        case 'tfareset': resetUserTfa(target.id,target.username,btn); break;
        case 'promote':  promoteUser(target.id,target.username,btn.dataset.role,btn); break;
        case 'transfer': transferSuperuser(target.id,target.username,btn); break;
        case 'delete':   deleteUser(target.id,target.username,btn); break;
      }
    });
    // Wire toggles and buttons
    users.filter(u=>u.role==='guest').forEach(u=>{
      const tog=document.getElementById(`tog-${u.id}`);
      if(tog)tog.addEventListener('change',async()=>{
        const d=await(await api(`/api/users/${u.id}/permissions`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({terminal:tog.checked})})).json();
        if(d.ok)toast(`Terminal ${tog.checked?'enabled':'disabled'} for ${u.username}`,'success');
        else{errToast(d,'Failed');tog.checked=!tog.checked}
      });
      const togIntc=document.getElementById(`tog-intc-${u.id}`);
      if(togIntc)togIntc.addEventListener('change',async()=>{
        const d=await(await api(`/api/users/${u.id}/permissions`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({intercom:togIntc.checked})})).json();
        if(d.ok)toast(`Intercom ${togIntc.checked?'enabled':'disabled'} for ${u.username}`,'success');
        else{errToast(d,'Failed');togIntc.checked=!togIntc.checked}
      });
    });
    if(IS_SUPERUSER){
      users.filter(u=>u.role==='admin').forEach(u=>{
        [['term','terminal'],['dock','docker'],['gfv','guest_file_view'],['intc','intercom']].forEach(([elKey,permKey])=>{
          const tog=document.getElementById(`tog-${elKey}-${u.id}`);
          if(tog)tog.addEventListener('change',async()=>{
            const d=await(await api(`/api/users/${u.id}/permissions`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({[permKey]:tog.checked})})).json();
            if(d.ok)toast(`${permKey} ${tog.checked?'enabled':'disabled'} for ${u.username}`,'success');
            else{errToast(d,'Failed');tog.checked=!tog.checked}
          });
        });
      });
    }
  }catch(e){if(gen!==_renderGen)return;container.innerHTML=`<div style="color:var(--rd)">${esc(e.message)}</div>`}
}
const AUDIT_ICONS={login:'\u{1F513}',login_failed:'\u26A0\uFE0F',logout:'\u{1F6AA}',setup_completed:'\u2728',
  trash:'\u{1F5D1}',restore:'\u21A9',delete_permanent:'\u274C',empty_trash:'\u{1F9F9}',
  mkdir:'\u{1F4C1}',rename:'\u270F\uFE0F',move:'\u27A1\uFE0F',copy:'\u{1F4CB}',upload:'\u2B06',
  user_created:'\u{1F464}',user_deleted:'\u{1F5D1}',permission_changed:'\u{1F511}',
  password_changed:'\u{1F510}',password_reset:'\u{1F510}','2fa_enabled':'\u{1F6E1}\uFE0F','2fa_disabled':'\u26A0\uFE0F','2fa_reset':'\u26A0\uFE0F','2fa_backup_used':'\u{1F511}','2fa_backup_regenerated':'\u{1F511}',
  session_revoked:'\u{1F6AB}',sessions_revoked_others:'\u{1F6AB}',
  share_create:'\u{1F517}',share_revoke:'\u{1F6AB}',share_edit:'\u270F\uFE0F',
  audit_log_cleared:'\u{1F5D1}',backup_exported:'\u2B07',backup_imported:'\u2B06'};
const AUDIT_DANGER=new Set(['login_failed','delete_permanent','user_deleted','session_revoked','sessions_revoked_others','empty_trash','audit_log_cleared',
  'cross_site_blocked','disk_space_low','app_install_failed','cloud_sync_failed','error_log_cleared']);
Object.assign(AUDIT_ICONS,{cross_site_blocked:'\u{1F6E1}\uFE0F',disk_space_low:'\u{1F4BD}',app_install_failed:'\u{1F4E6}',
  cloud_sync_failed:'\u2601\uFE0F',error_log_cleared:'\u{1F9F9}',cloud_import:'\u2B07\uFE0F',move:'\u27A1\uFE0F',copy:'\u{1F4CB}'});
// ── Admin → Error Log ───────────────────────────────────────────────
// Every unexpected server error, failed background job, failed outbound
// notification and browser-side crash, each with the reference code the
// user was shown. Paste a code into the box to jump straight to it.
async function loadErrorLog(refFilter='',sourceFilter=''){
  const gen=_renderGen;
  const box=document.getElementById('errorLogSection');if(!box)return;
  const qs=new URLSearchParams();if(refFilter)qs.set('ref',refFilter);if(sourceFilter)qs.set('source',sourceFilter);
  const r=await api('/api/admin/errors?'+qs);const d=await r.json();
  if(gen!==_renderGen)return;
  if(!r.ok){box.innerHTML=`<div class="admin-section"><div class="sdesc" style="color:var(--rd)">${esc(d.error)}</div></div>`;return}
  const items=d.items||[];
  const SRC={server:'Server',browser:'Browser',job:'Background job',notification:'Notification'};
  box.innerHTML=`<div class="admin-section">
    <div class="admin-section-title" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span style="flex:1">Error Log (${d.total})</span>
      <input class="fgi" id="errRefF" placeholder="Reference code\u2026" value="${escA(refFilter)}" style="width:150px;font-family:var(--f-mono)"/>
      <select class="fgi" id="errSrcF"><option value="">All sources</option>${Object.entries(SRC).map(([k,v])=>
        `<option value="${k}" ${k===sourceFilter?'selected':''}>${v}</option>`).join('')}</select>
      <button class="btn btn-g btn-sm" id="errRefresh">\u21BB Refresh</button>
      ${IS_SUPERUSER?'<button class="btn btn-d btn-sm" id="errClear">Clear</button>':''}
    </div>
    <div class="sdesc" style="margin-bottom:10px">Users see a short reference code with every unexpected error \u2014 look it up here.
      ${IS_SUPERUSER?'':'Full technical traces are visible to the Super User only.'}</div>
    ${items.length?items.map(x=>`<div class="user-row err-row" style="align-items:flex-start">
      <div class="user-avatar ${x.source==='browser'?'avatar-guest':'avatar-admin'}" style="font-size:14px">${x.source==='browser'?'\u{1F310}':x.source==='job'?'\u2699\uFE0F':x.source==='notification'?'\u{1F514}':'\u26A0\uFE0F'}</div>
      <div class="user-info" style="min-width:0">
        <div class="user-name">${esc(x.message||x.code)} <code style="font-size:11px;opacity:.8">ref ${esc(x.ref)}</code>
          ${x.category==='stale_view'||x.category==='benign'||x.severity==='info'?'<span class="role-badge role-guest" style="margin-left:6px" title="Nothing is broken">harmless</span>':''}</div>
        ${x.reason?`<div class="user-meta" style="white-space:normal;color:var(--tx);opacity:.9">${esc(x.reason)}</div>`:''}
        ${x.hint&&x.source!=='browser'?`<div class="user-meta" style="white-space:normal">What to do: ${esc(x.hint)}</div>`:''}
        ${x.where?`<div class="user-meta" style="white-space:normal;font-family:var(--f-mono);font-size:11px">at ${esc(x.where)}</div>`:''}
        <div class="user-meta" style="white-space:normal">${esc(SRC[x.source]||x.source)} \u00b7 ${new Date(x.ts*1000).toLocaleString()}
          ${x.user?` \u00b7 ${esc(x.user)}`:''}${x.route?` \u00b7 <code>${esc(x.route)}</code>`:''}${x.context?` \u00b7 ${esc(x.context)}`:''}</div>
        ${x.exception?`<div class="user-meta" style="white-space:normal;font-family:var(--f-mono);font-size:11px">${esc(x.exception)}</div>`:''}
        ${(x.recent||[]).length?`<details style="margin-top:6px"><summary style="cursor:pointer;font-size:11.5px;color:var(--td)">What the page was doing (last requests)</summary>
          <pre style="white-space:pre-wrap;font-size:11px;background:rgba(var(--ov-rgb),.05);padding:8px;border-radius:8px;margin-top:6px">${esc(x.recent.join('\n'))}</pre></details>`:''}
        ${x.trace?`<details style="margin-top:6px"><summary style="cursor:pointer;font-size:11.5px;color:var(--td)">Technical details${x.build?` \u00b7 build ${esc(x.build)}`:''}${x.ui?` \u00b7 ${esc(x.ui)} mode`:''}</summary>
          <pre style="white-space:pre-wrap;font-size:11px;max-height:260px;overflow:auto;background:rgba(var(--ov-rgb),.05);padding:8px;border-radius:8px;margin-top:6px">${esc(x.trace)}</pre></details>`:''}
      </div></div>`).join(''):`<div class="empty-dir">${refFilter?'No error with that reference code (it may have been cleared, or the server restarted before it was saved)':'No errors recorded \u2014 good news'}</div>`}
  </div>`;
  const _errRows=[...box.querySelectorAll('.err-row')];
  if(_errRows.length){const lb=document.createElement('div');lb.className='log-box';_errRows[0].before(lb);_errRows.forEach(r=>lb.appendChild(r))}
  const go_=()=>loadErrorLog(document.getElementById('errRefF').value.trim().toLowerCase(),document.getElementById('errSrcF').value);
  document.getElementById('errRefF')?.addEventListener('keydown',e=>{if(e.key==='Enter')go_()});
  document.getElementById('errSrcF')?.addEventListener('change',go_);
  document.getElementById('errRefresh')?.addEventListener('click',go_);
  document.getElementById('errClear')?.addEventListener('click',e=>confirmDlg('Clear the whole Error Log?',async()=>{
    const r=await api('/api/admin/errors',{method:'DELETE'});if(r.ok){toast('Error Log cleared','success');loadErrorLog()}else errToast(await r.json())},'Clear',e.currentTarget));
}
async function loadAuditLog(actionFilter='',userFilter=''){
  const gen=_renderGen;
  const container=document.getElementById('auditSection');
  if(!container)return;
  try{
    const qs=new URLSearchParams({limit:'200'});
    if(actionFilter)qs.set('action',actionFilter);
    if(userFilter)qs.set('username',userFilter);
    const d=await(await api('/api/audit?'+qs.toString())).json();
    if(gen!==_renderGen)return;
    const entries=d.entries||[],actions=d.actions||[];
    container.innerHTML=`<div class="admin-section">
      <div class="admin-section-title" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span style="flex:1">Activity Log (${entries.length}${entries.length>=200?'+':''})</span>
        <select class="fgi" id="auditActionF" style="min-width:190px">
          <option value="">All actions</option>
          ${actions.map(a=>`<option value="${escA(a)}" ${a===actionFilter?'selected':''}>${esc(a)}</option>`).join('')}
        </select>
        <input class="fgi" id="auditUserF" placeholder="Filter by username\u2026" value="${escA(userFilter)}" style="width:180px"/>
        <button class="btn btn-g btn-sm" id="auditRefreshBtn">\u21BB Refresh</button>
        ${IS_SUPERUSER?'<button class="btn btn-d btn-sm" id="auditClearBtn">\u{1F5D1} Clear log</button>':''}
      </div>
      <div id="auditTable" class="log-box">
        ${entries.length?entries.map(auditRow).join(''):'<div class="empty-dir">No activity recorded yet</div>'}
      </div></div>`;
    const doFilter=()=>loadAuditLog(document.getElementById('auditActionF').value,document.getElementById('auditUserF').value.trim());
    document.getElementById('auditActionF')?.addEventListener('change',doFilter);
    enhanceSelect(document.getElementById('auditActionF'));
    document.getElementById('auditUserF')?.addEventListener('keydown',e=>{if(e.key==='Enter')doFilter()});
    document.getElementById('auditRefreshBtn')?.addEventListener('click',doFilter);
    document.getElementById('auditClearBtn')?.addEventListener('click',showClearAuditDlg);
  }catch(e){if(gen!==_renderGen)return;container.innerHTML=`<div style="color:var(--rd)">${esc(e.message)}</div>`}
}
function auditRow(e){
  const icon=AUDIT_ICONS[e.action]||'\u2022';
  const danger=AUDIT_DANGER.has(e.action);
  const meta=[e.target,e.detail,e.ts?new Date(e.ts).toLocaleString():'',e.ip].filter(Boolean).map(esc).join(' &nbsp; \u2022 &nbsp; ');
  return `<div class="user-row audit-row">
    <div class="user-avatar ${danger?'avatar-guest':'avatar-admin'}" style="font-size:16px">${icon}</div>
    <div class="user-info">
      <div class="user-name">${esc(e.username||'system')} <span style="font-size:11px;color:var(--td);font-weight:400">${esc(e.action)}</span></div>
      <div class="user-meta">${meta}</div>
    </div>
  </div>`;
}
const CLEAR_AUDIT_PHRASE='CLEAR AUDIT LOG';
function showClearAuditDlg(){
  openMdl('Clear Audit Log',`
    <div class="fg"><label class="fgl">Scope</label>
      <div class="seg" id="caScopeSeg">
        <button class="seg-btn active" data-scope="all" type="button">Everything</button>
        <button class="seg-btn" data-scope="partial" type="button">Only older entries\u2026</button>
      </div>
    </div>
    <div class="fg" id="caWindowRow" style="display:none"><label class="fgl">Keep entries from the last</label>
      <select class="fgi" id="caWindow">
        <option value="1">1 day</option>
        <option value="7">7 days</option>
        <option value="30" selected>30 days</option>
        <option value="90">90 days</option>
      </select></div>
    <div class="sdesc" style="color:var(--yw)">This cannot be undone. Whatever's removed is gone \u2014 but clearing the
      log is itself recorded as the next entry, so there's always a trace of who cleared it and when.</div>
    <div class="fg"><label class="fgl">Type <code>${CLEAR_AUDIT_PHRASE}</code> to confirm</label>
      <input class="fgi" id="caConfirm" placeholder="${CLEAR_AUDIT_PHRASE}" autocomplete="off" spellcheck="false"/></div>`,
    `<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-d" id="caGoBtn" disabled>Clear log</button>`);
  enhanceSelect(document.getElementById('caWindow'));
  let scope='all';
  const goBtn=document.getElementById('caGoBtn'),confirmInp=document.getElementById('caConfirm'),windowRow=document.getElementById('caWindowRow');
  confirmInp.addEventListener('input',()=>{goBtn.disabled=confirmInp.value!==CLEAR_AUDIT_PHRASE});
  document.getElementById('caScopeSeg')?.addEventListener('click',e=>{
    const b=e.target.closest('.seg-btn'); if(!b)return;
    scope=b.dataset.scope;
    document.querySelectorAll('#caScopeSeg .seg-btn').forEach(x=>x.classList.toggle('active',x===b));
    windowRow.style.display=scope==='all'?'none':'';
  });
  goBtn.addEventListener('click',async()=>{
    if(confirmInp.value!==CLEAR_AUDIT_PHRASE)return;
    const body={confirm_text:confirmInp.value};
    if(scope==='partial'){
      const days=Number(document.getElementById('caWindow').value);
      body.before_ts=new Date(Date.now()-days*86400000).toISOString();
    }
    const r=await(await api('/api/audit/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
    if(r.error){errToast(r);return}
    closeMdl();
    if(r.removed===0)toast('Nothing was old enough to remove. Choose \u201cEverything\u201d to clear the whole log.','warning');
    else toast(`Cleared ${r.removed} entr${r.removed===1?'y':'ies'}`,'success');
    loadAuditLog();
  });
}
function userRow(u){
  const isMe=u.username===UNAME;
  const isGuest=u.role==='guest', isAdminRole=u.role==='admin', isSuperRole=u.role==='superuser';
  const termOn = u.permissions?.terminal ?? (isGuest?false:true);
  const dockOn = u.permissions?.docker ?? true;
  const gfvOn  = u.permissions?.guest_file_view ?? true;
  const intcOn = u.permissions?.intercom ?? false;
  // For someone who lost their phone and backup codes (an Admin can reset a Guest; only the Super User an Admin).
  const tfaBtn = (u.twofa_enabled && !isMe) ? `<button class="btn btn-g btn-sm" data-act="tfareset" data-uid="${u.id}" title="Turn off their two-factor sign-in">\u{1F6E1} Reset 2FA</button>` : '';

  let permsHtml;
  if(isGuest){
    permsHtml=`<div class="perm-item"><label for="tog-${u.id}" style="font-size:12px;cursor:pointer">Terminal</label>
      <label class="gh-switch" ${(!termOn&&!CAN_TERM)?'title="You need Terminal access yourself to grant it"':''}><input type="checkbox" id="tog-${u.id}" ${termOn?'checked':''} ${(!termOn&&!CAN_TERM)?'disabled':''}/><span class="gh-slider"></span></label></div>
      <div class="perm-item"><label for="tog-intc-${u.id}" style="font-size:12px;cursor:pointer">Intercom</label>
      <label class="gh-switch" ${(!intcOn&&!CAN_INTERCOM)?'title="You need Intercom access yourself to grant it"':''}><input type="checkbox" id="tog-intc-${u.id}" ${intcOn?'checked':''} ${(!intcOn&&!CAN_INTERCOM)?'disabled':''}/><span class="gh-slider"></span></label></div>`;
  }else if(isAdminRole && IS_SUPERUSER){
    permsHtml=`
      <div class="perm-item"><label for="tog-term-${u.id}" style="font-size:12px;cursor:pointer">Terminal</label>
        <label class="gh-switch"><input type="checkbox" id="tog-term-${u.id}" ${termOn?'checked':''}/><span class="gh-slider"></span></label></div>
      <div class="perm-item"><label for="tog-dock-${u.id}" style="font-size:12px;cursor:pointer">Docker</label>
        <label class="gh-switch"><input type="checkbox" id="tog-dock-${u.id}" ${dockOn?'checked':''}/><span class="gh-slider"></span></label></div>
      <div class="perm-item"><label for="tog-gfv-${u.id}" style="font-size:12px;cursor:pointer">Guest Files</label>
        <label class="gh-switch"><input type="checkbox" id="tog-gfv-${u.id}" ${gfvOn?'checked':''}/><span class="gh-slider"></span></label></div>
      <div class="perm-item"><label for="tog-intc-${u.id}" style="font-size:12px;cursor:pointer">Intercom</label>
        <label class="gh-switch"><input type="checkbox" id="tog-intc-${u.id}" ${intcOn?'checked':''}/><span class="gh-slider"></span></label></div>`;
  }else{
    permsHtml=`<div class="perm-item" style="color:var(--ps);font-size:12px">\u2713 Full access</div>`;
  }

  let actionsHtml;
  if(isSuperRole){
    actionsHtml=IS_SUPERUSER
      ?`<button class="btn btn-g btn-sm" data-act="pwd" data-uid="${u.id}">\u{1F511} Password</button>`
      :`<span style="font-size:11px;color:var(--tm)" title="Only the Super User can change this account">\u{1F512} Super User</span>`;
  }else if(isAdminRole){
    if(IS_SUPERUSER){
      actionsHtml=`
        <button class="btn btn-g btn-sm" data-act="pwd" data-uid="${u.id}">\u{1F511} Password</button>
        ${tfaBtn}
        <button class="btn btn-g btn-sm" data-act="promote" data-uid="${u.id}" data-role="guest">\u2193 Demote</button>
        <button class="btn btn-p btn-sm" data-act="transfer" data-uid="${u.id}">\u2605 Transfer Super User</button>
        ${!isMe?`<button class="btn btn-d btn-sm" data-act="delete" data-uid="${u.id}">\u{1F5D1} Delete</button>`:''}`;
    }else{
      actionsHtml=isMe
        ?`<button class="btn btn-p btn-sm" onclick="goToPasswordSettings()">\u{1F511} Change Password</button>`
        :`<span style="font-size:11px;color:var(--tm)" title="Only the Super User can manage Admin accounts">\u{1F512} Managed by Super User</span>`;
    }
  }else{ // guest
    actionsHtml=`
      <button class="btn btn-g btn-sm" data-act="pwd" data-uid="${u.id}">\u{1F511} Password</button>
      ${tfaBtn}
      ${IS_SUPERUSER?`<button class="btn btn-g btn-sm" data-act="promote" data-uid="${u.id}" data-role="admin">\u2191 Promote</button>`:''}
      <button class="btn btn-d btn-sm" data-act="delete" data-uid="${u.id}">\u{1F5D1} Delete</button>`;
  }

  const rankLabel=isSuperRole?'\u2605 Super User':({admin:'Admin',guest:'Guest'}[u.role]||u.role);
  return `<div class="user-row" id="urow-${u.id}">
    <div class="user-avatar ${isGuest?'avatar-guest':'avatar-admin'}">${u.username[0].toUpperCase()}</div>
    <div class="user-info">
      <div class="user-name">${esc(u.username)} ${isMe?'<span style="font-size:10px;color:var(--td)">(you)</span>':''}</div>
      <div class="user-meta"><span class="role-badge role-${u.role}">${rankLabel}</span>${u.twofa_enabled?' <span title="Two-factor sign-in is on" style="font-size:10.5px;color:var(--gr)">&#128737; 2FA</span>':''} &nbsp; Joined ${u.created_at?new Date(u.created_at).toLocaleDateString():'\u2014'}</div>
    </div>
    <div class="user-perms">${permsHtml}</div>
    <div class="user-actions">${actionsHtml}</div>
  </div>`;
}
function showAddUser(){
  const roleField=IS_SUPERUSER
    ?`<div class="fg"><label class="fgl">Role</label>
        <select class="fgi" id="nuRole"><option value="guest">Guest</option><option value="admin">Admin</option></select></div>`
    :`<input type="hidden" id="nuRole" value="guest"/>`;
  openMdl('New User',`
    <div class="fg"><label class="fgl">Username</label><input class="fgi" id="nuName" placeholder="username" autofocus spellcheck="false"/></div>
    <div class="fg"><label class="fgl">Password</label><input class="fgi" id="nuPass" type="password" placeholder="min. 8 characters"/></div>
    ${roleField}
    <div id="addUserNote" style="font-size:12px;color:var(--td);margin-top:4px">Guests get access to Shared and their own personal folder.</div>`,
    `<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-p" id="addUserBtn">Create User</button>`);
  enhanceSelect(document.getElementById('nuRole'));
  document.getElementById('addUserBtn')?.addEventListener('click',async()=>{
    const uname=document.getElementById('nuName').value.trim().toLowerCase();
    const pass=document.getElementById('nuPass').value;
    const role=document.getElementById('nuRole').value;
    if(!uname||!pass){toast('Username and password required','error');return}
    const r=await(await api('/api/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:uname,password:pass,role})})).json();
    if(r.id){toast(`User ${uname} created`,'success');closeMdl();await loadUsers()}
    else errToast(r,'Failed to create user');
  });
}
async function resetUserTfa(uid,uname,anchorEl){
  confirmDlg(`Turn off two-factor sign-in for <strong>${esc(uname)}</strong>? They\u2019ll be signed out everywhere and can sign in with just their password. Only do this if they\u2019ve lost their phone and their backup codes.`,async()=>{
    const r=await(await api(`/api/users/${uid}/2fa/reset`,{method:'POST'})).json();
    if(r.ok){toast(`Two-factor sign-in reset for ${uname}`,'success');await loadUsers()}
    else errToast(r,'Couldn\u2019t reset two-factor sign-in');
  },'Reset',anchorEl);
}

// ── TWO-FACTOR SIGN-IN (Settings -> Security) ─────────────────────────────
async function loadTfa(){
  const box=document.getElementById('tfaBox');if(!box)return;
  let d;
  try{d=await apiData('/api/me/2fa')}
  catch(e){if(box.isConnected)box.innerHTML=`<div style="font-size:12px;color:var(--yw)">Couldn\u2019t load: ${esc(e.message||'unknown error')}</div>`;return}
  if(!box.isConnected)return;          // Settings was closed while this was loading
  if(!d.enabled){
    box.innerHTML=`<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <button class="btn btn-p btn-sm" id="tfaOn">Enable two-factor auth</button>
      <span style="font-size:11.5px;color:var(--td)">Not enabled</span></div>`;
    box.querySelector('#tfaOn')?.addEventListener('click',tfaStart);
    return;
  }
  const low=d.backup_remaining<=2;
  box.innerHTML=`<div style="display:flex;flex-direction:column;gap:10px">
    <div style="font-size:13px;color:var(--gr)">\u2713 Two-factor authentication is on</div>
    <div style="font-size:12px;color:${low?'var(--yw)':'var(--td)'}">${d.backup_remaining} backup code${d.backup_remaining===1?'':'s'} left${low?' \u2014 generate new ones soon':''}</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn btn-g btn-sm" id="tfaCodes">Regenerate backup codes</button>
      <button class="btn btn-d btn-sm" id="tfaOff">Remove 2FA</button></div>
    <div style="font-size:11.5px;color:var(--td)">WebDAV (mounting your files as a network drive) is unavailable while this is on, because those apps can only send a password.</div></div>`;
  box.querySelector('#tfaCodes')?.addEventListener('click',tfaRegenDlg);
  box.querySelector('#tfaOff')?.addEventListener('click',tfaOffDlg);
}
function tfaStart(){
  openMdl('Enable two-factor authentication',`
    <div style="font-size:13px;color:var(--tm);margin-bottom:12px">Enter your password to continue.</div>
    <div class="fg"><label class="fgl">Password</label><input class="fgi" id="tfaPw" type="password" autocomplete="current-password" autofocus/></div>`,
    `<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-p" id="tfaNext">Continue</button>`);
  const go=async()=>{
    const pw=document.getElementById('tfaPw').value;if(!pw){toast('Enter your password','error');return}
    const b=document.getElementById('tfaNext');b.disabled=true;
    const r=await(await api('/api/me/2fa/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})})).json();
    b.disabled=false;
    if(r.ok)tfaScan(r);else errToast(r,'Couldn\u2019t start setup');
  };
  document.getElementById('tfaNext').addEventListener('click',go);
  document.getElementById('tfaPw').addEventListener('keydown',e=>{if(e.key==='Enter')go()});
}
function tfaScan(s){
  openMdl('Set up your authenticator app',`
    <div style="display:flex;gap:18px;flex-wrap:wrap;align-items:flex-start">
      ${s.qr_svg?`<div id="tfaQr" style="background:#fff;padding:8px;border-radius:10px;line-height:0;width:176px;height:176px;flex:none">${s.qr_svg}</div>`:''}
      <ol style="flex:1;min-width:210px;margin:0;padding-left:18px;font-size:13px;color:var(--tm);line-height:1.6">
        <li>Install an authenticator app (Google Authenticator, Aegis, Authy, 1Password\u2026).</li>
        <li>${s.qr_svg?'Scan the QR code, or enter':'Add a new account and enter'} this key:<br><code style="font-size:12.5px;user-select:all;word-break:break-all">${esc(s.key)}</code></li>
        <li>Enter the 6-digit code the app shows.</li>
      </ol></div>
    <div class="fg" style="margin-top:14px"><label class="fgl">Authentication code</label>
      <input class="fgi" id="tfaCode" inputmode="numeric" autocomplete="one-time-code" maxlength="7" placeholder="000000" style="letter-spacing:.2em;text-align:center;max-width:180px"/></div>`,
    `<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-p" id="tfaGo">Activate</button>`);
  document.querySelector('#tfaQr svg')?.setAttribute('style','width:100%;height:100%;display:block');
  const go=async()=>{
    const code=document.getElementById('tfaCode').value.trim();if(!code){toast('Enter the code from your app','error');return}
    const b=document.getElementById('tfaGo');b.disabled=true;
    const r=await(await api('/api/me/2fa/enable',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})})).json();
    b.disabled=false;
    if(r.ok){tfaShowCodes(r.backup_codes,'Two-factor authentication is on',r.other_sessions_signed_out);loadTfa()}
    else errToast(r,'That code didn\u2019t work');
  };
  document.getElementById('tfaGo').addEventListener('click',go);
  document.getElementById('tfaCode').addEventListener('keydown',e=>{if(e.key==='Enter')go()});
  setTimeout(()=>document.getElementById('tfaCode')?.focus(),60);
}
function tfaShowCodes(codes,title,signedOut){
  openMdl(title,`
    <div style="font-size:13px;color:var(--tm);margin-bottom:10px">Save these backup codes somewhere safe. Each one works <strong>once</strong> in place of an authentication code \u2014 they\u2019re the way back in if you lose your phone. They won\u2019t be shown again.</div>
    <div id="tfaCodeGrid" style="display:grid;grid-template-columns:1fr 1fr;gap:6px 18px;background:var(--bg3,rgba(255,255,255,.04));border-radius:10px;padding:14px 16px;font-family:var(--mono,monospace);font-size:14px;letter-spacing:.06em;user-select:all">
      ${codes.map(c=>`<span>${esc(c)}</span>`).join('')}</div>
    ${signedOut?`<div style="font-size:12px;color:var(--td);margin-top:10px">${signedOut} other signed-in device${signedOut===1?' was':'s were'} signed out.</div>`:''}`,
    `<button class="btn btn-g" id="tfaCopy">Copy</button><button class="btn btn-g" id="tfaDl">Download</button><button class="btn btn-p" id="tfaDone">Done</button>`);
  const text=`${OSNAME||'Void OS'} backup codes for ${UNAME}\n(each code works once)\n\n`+codes.join('\n')+'\n';
  document.getElementById('tfaCopy').addEventListener('click',()=>{
    (navigator.clipboard?navigator.clipboard.writeText(text):Promise.reject()).then(()=>toast('Copied','success'),()=>toast('Couldn\u2019t copy \u2014 select the codes and copy them by hand','error'));
  });
  document.getElementById('tfaDl').addEventListener('click',()=>{
    const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([text],{type:'text/plain'}));a.download='voidos-backup-codes.txt';
    document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(a.href),4000);
  });
  document.getElementById('tfaDone').addEventListener('click',closeMdl);
}
function tfaRegenDlg(){
  openMdl('Regenerate backup codes',`
    <div style="font-size:13px;color:var(--tm);margin-bottom:12px">This replaces your current backup codes \u2014 the old ones stop working. Enter your password to continue.</div>
    <div class="fg"><label class="fgl">Password</label><input class="fgi" id="tfaPw" type="password" autocomplete="current-password" autofocus/></div>`,
    `<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-p" id="tfaRg">Generate</button>`);
  const go=async()=>{
    const pw=document.getElementById('tfaPw').value;if(!pw){toast('Enter your password','error');return}
    const r=await(await api('/api/me/2fa/backup-codes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})})).json();
    if(r.ok){tfaShowCodes(r.backup_codes,'New backup codes');loadTfa()}else errToast(r,'Couldn\u2019t generate codes');
  };
  document.getElementById('tfaRg').addEventListener('click',go);
  document.getElementById('tfaPw').addEventListener('keydown',e=>{if(e.key==='Enter')go()});
}
function tfaOffDlg(){
  openMdl('Remove two-factor authentication',`
    <div style="font-size:13px;color:var(--tm);margin-bottom:12px">Your account will go back to password-only sign-in. Enter your password and a current authentication code (or a backup code).</div>
    <div class="fg"><label class="fgl">Password</label><input class="fgi" id="tfaPw" type="password" autocomplete="current-password" autofocus/></div>
    <div class="fg"><label class="fgl">Authentication or backup code</label><input class="fgi" id="tfaCode" autocomplete="one-time-code" maxlength="12" spellcheck="false" placeholder="000000"/></div>`,
    `<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-d" id="tfaRm">Remove 2FA</button>`);
  const go=async()=>{
    const pw=document.getElementById('tfaPw').value,code=document.getElementById('tfaCode').value.trim();
    if(!pw||!code){toast('Enter your password and a code','error');return}
    const r=await(await api('/api/me/2fa/disable',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw,code})})).json();
    if(r.ok){closeMdl();toast('Two-factor authentication removed','success');loadTfa()}else errToast(r,'Couldn\u2019t remove it');
  };
  document.getElementById('tfaRm').addEventListener('click',go);
  document.getElementById('tfaCode').addEventListener('keydown',e=>{if(e.key==='Enter')go()});
}
function showChangePwd(uid,uname){
  openMdl(`Change Password \u2014 ${uname}`,`
    <div class="fg"><label class="fgl">New Password</label><input class="fgi" id="npPass" type="password" placeholder="min. 8 characters" autofocus/></div>
    <div class="fg"><label class="fgl">Confirm</label><input class="fgi" id="npConf" type="password" placeholder="Repeat password"/></div>`,
    `<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-p" id="changePwdBtn">Save</button>`);
  document.getElementById('changePwdBtn')?.addEventListener('click',async()=>{
    const p=document.getElementById('npPass').value,c=document.getElementById('npConf').value;
    if(p!==c){toast('Passwords do not match','error');return}
    if(p.length<8){toast('Min. 8 characters','error');return}
    const r=await(await api(`/api/users/${uid}/password`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:p})})).json();
    if(r.ok){toast('Password changed','success');closeMdl()}else errToast(r,'Failed');
  });
}
async function deleteUser(uid,uname,anchorEl){
  confirmDlg(`Delete user <strong>${esc(uname)}</strong>? This cannot be undone.`,async()=>{
    const r=await(await api(`/api/users/${uid}`,{method:'DELETE'})).json();
    if(r.ok){toast(`${uname} deleted`,'success');document.getElementById(`urow-${uid}`)?.remove()}
    else errToast(r,'Failed');
  },'Delete',anchorEl);
}
async function promoteUser(uid,uname,newRole,anchorEl){
  const msg = newRole==='admin'
    ? `Promote <strong>${esc(uname)}</strong> to Admin? They'll get full file, Docker, and terminal access by default \u2014 you can restrict individual permissions afterward.`
    : `Demote <strong>${esc(uname)}</strong> to Guest? They'll lose Admin-level access immediately.`;
  confirmDlg(msg,async()=>{
    const r=await(await api(`/api/users/${uid}/promote`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({role:newRole})})).json();
    if(r.id){toast(`${uname} is now ${newRole}`,'success');await loadUsers()}
    else errToast(r,'Failed');
  }, newRole==='admin'?'Promote':'Demote', anchorEl);
}
async function transferSuperuser(uid,uname){
  // Deliberately kept on the full modal rather than the anchored popover: this
  // is the single most consequential action in this panel (immediately
  // surrenders your own Super User status), so it gets the version that
  // doesn't auto-dismiss and demands full attention rather than a small
  // transient popover.
  confirmDlg(`Transfer Super User to <strong>${esc(uname)}</strong>? You will become an Admin immediately as part of this swap \u2014 this cannot be undone by yourself alone.`,async()=>{
    const r=await(await api(`/api/users/${uid}/transfer-superuser`,{method:'POST'})).json();
    if(r.ok){toast(`Super User transferred to ${uname}`,'success');await loadUsers()}
    else errToast(r,'Failed');
  },'Transfer Super User');
}
window.showAddUser=showAddUser;window.showChangePwd=showChangePwd;window.deleteUser=deleteUser;
window.promoteUser=promoteUser;window.transferSuperuser=transferSuperuser;

// ── SESSIONS ─────────────────────────────────────────────────────
function fmtLastSeen(ts){
  if(!ts)return'\u2014';
  const s=Math.max(0,Math.floor(Date.now()/1000-ts));
  if(s<60)return'Active now';
  if(s<3600)return`${Math.floor(s/60)}m ago`;
  if(s<86400)return`${Math.floor(s/3600)}h ago`;
  return`${Math.floor(s/86400)}d ago`;
}
async function rSessions(){
  const root=document.getElementById('vroot');
  root.innerHTML=`<div class="vh"><div><div class="vtitle">${IS_ADMIN?'All Sessions':'Sessions'}</div>
    <div class="vsub">${IS_ADMIN?'Every device signed in, across all users':`Devices currently signed in as ${esc(UNAME)}`}</div></div>
    <button class="btn btn-d btn-sm" id="revokeOthersBtn">Log out my other devices</button></div>
    <div id="sessionsSection"><div class="loading"><div class="loader"></div><span>Loading sessions\u2026</span></div></div>`;
  document.getElementById('revokeOthersBtn')?.addEventListener('click',e=>revokeOtherSessions(e.currentTarget));
  await loadSessions();
}
async function loadSessions(){
  const gen=_renderGen;
  const container=document.getElementById('sessionsSection');
  if(!container)return;
  try{
    const d=await apiData('/api/sessions');
    if(gen!==_renderGen)return;
    const sessions=d.sessions||[];
    container.innerHTML=`<div class="admin-section">
      <div class="admin-section-title">${d.is_admin?'All Active Sessions':'Active Sessions'} (${sessions.length})</div>
      <div class="user-table" id="sessTable">
        ${sessions.length?sessions.map(s=>sessionRow(s,d.is_admin)).join(''):'<div class="empty-dir">No active sessions</div>'}
      </div></div>`;
    sessions.filter(s=>!s.is_current).forEach(s=>{
      document.getElementById(`sessRev-${s.id}`)?.addEventListener('click',e=>revokeSession(s.id,e.currentTarget));
    });
    sessions.forEach(s=>{
      document.getElementById(`sessDet-${s.id}`)?.addEventListener('click',e=>showSessionDetails(s.id,e.currentTarget));
    });
    wireSpoilers(container);
  }catch(e){if(gen!==_renderGen)return;container.innerHTML=`<div style="color:var(--rd)">${esc(e.message)}</div>`}
}
function sessionRow(s,showUser){
  const who=s.is_current?'<span style="font-size:10px;color:var(--ps)">(this device)</span>'
    :(s.is_mine?'<span style="font-size:10px;color:var(--td)">(you)</span>':'');
  const canViewDetails=IS_SUPERUSER||(IS_ADMIN&&(s.is_mine||s.owner_role==='guest'));
  const canRevoke=s.is_mine||IS_SUPERUSER||(IS_ADMIN&&s.owner_role==='guest'); // same rule the server enforces
  return `<div class="user-row" id="srow-${s.id}">
    <div class="user-avatar ${s.is_mine?'avatar-admin':'avatar-guest'}">\u{1F4BB}</div>
    <div class="user-info">
      <div class="user-name">${showUser?`${esc(s.username)} \u2014 `:''}${esc(s.label)} ${who}</div>
      <div class="user-meta"><span class="ip-spoiler" data-sid="${s.id}" title="Click to reveal/hide"><span class="ip-spoiler-text">${esc(s.ip||'Unknown IP')}</span><canvas class="ip-spoiler-canvas"></canvas></span>${(s.country||s.country_code)?` (${flagEmoji(s.country_code)} ${esc(countryName(s))})`:''} &nbsp; \u2022 &nbsp; ${fmtLastSeen(s.last_seen_ts)} &nbsp; \u2022 &nbsp; Signed in ${s.created_at?new Date(s.created_at).toLocaleString():'\u2014'}</div>
    </div>
    <div class="user-actions">
      ${canViewDetails?`<button class="btn btn-g btn-sm" id="sessDet-${s.id}"><svg viewBox="0 0 24 24" style="width:13px;height:13px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;vertical-align:-2px;margin-right:2px"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg> Details</button>`:''}
      ${(s.is_current||!canRevoke)?'':`<button class="btn btn-d btn-sm" id="sessRev-${s.id}">\u{1F5D1} Revoke</button>`}
    </div>
  </div>`;
}
async function revokeSession(sid,anchorEl){
  confirmDlg('Sign out this device? It will need to log in again.',async()=>{
    const r=await(await api(`/api/sessions/${sid}`,{method:'DELETE'})).json();
    if(r.ok){toast('Session revoked','success');document.getElementById(`srow-${sid}`)?.remove()}
    else errToast(r,'Failed');
  },'Sign out',anchorEl);
}
function closeSessionDetails(){document.getElementById('sessDetailsPanel')?.remove()}
async function showSessionDetails(sid,anchorEl){
  closeSessionDetails();
  let d;
  try{
    const r=await api(`/api/sessions/${sid}/details`);
    if(!r.ok){toast('Could not load details','error');return}
    d=await r.json();
  }catch(e){toast('Could not load details','error');return}

  const ci=d.client_info||{};
  const row=(label,val)=>val?`<div class="sess-details-row"><span class="sess-details-label">${esc(label)}</span><span class="sess-details-val">${esc(val)}</span></div>`:'';
  const panel=document.createElement('div');
  panel.className='sess-details';panel.id='sessDetailsPanel';
  panel.innerHTML=`
    <div class="sess-details-title">
      <span>\u{1F4BB} ${esc(d.username)} \u2014 ${esc((d.role||'').replace(/^\w/,c=>c.toUpperCase()))}</span>
      <button class="mx" onclick="closeSessionDetails()">&#x2715;</button>
    </div>
    <div class="sess-details-body">
      ${row('IP address',d.ip)}
      ${row('Country',(d.country||d.country_code)?`${flagEmoji(d.country_code)} ${countryName(d)}`:'')}
      ${row('Browser',d.browser?`${d.browser} ${d.browser_version||''}`.trim():'')}
      ${row('Operating system',d.os?`${d.os} ${d.os_version||''}`.trim():'')}
      ${row('Engine',d.engine)}
      ${row('Device type',d.device_type)}
      ${row('Screen',ci.screen)}
      ${row('Viewport',ci.viewport)}
      ${row('Pixel ratio',ci.pixel_ratio)}
      ${row('Color depth',ci.color_depth?`${ci.color_depth}-bit`:'')}
      ${row('Touch screen',ci.touch?(ci.touch==='true'?'Yes':'No'):'')}
      ${row('Timezone',ci.timezone)}
      ${row('Language',ci.language)}
      ${row('Platform',ci.platform)}
      ${row('Session created',d.created_at?new Date(d.created_at).toLocaleString():'')}
      ${row('Last active',d.last_seen_ts?fmtLastSeen(d.last_seen_ts):'')}
      ${d.user_agent?`<div class="sess-details-ua">${esc(d.user_agent)}</div>`:''}
    </div>
  `;
  document.body.appendChild(panel);

  const r=anchorEl.getBoundingClientRect();
  const pr=panel.getBoundingClientRect();
  let left=r.left, top=r.bottom+8;
  if(left+pr.width>window.innerWidth-12) left=window.innerWidth-pr.width-12;
  if(left<12) left=12;
  if(top+pr.height>window.innerHeight-12) top=r.top-pr.height-8;
  if(top<12) top=12;
  panel.style.left=left+'px';panel.style.top=top+'px';
}
async function revokeOtherSessions(anchorEl){
  confirmDlg('Log out all other devices? Only this device will stay signed in.',async()=>{
    const r=await(await api('/api/sessions/revoke-others',{method:'POST'})).json();
    if(r.ok){toast(`Signed out ${r.revoked} other device(s)`,'success');await loadSessions()}
    else errToast(r,'Failed');
  },'Sign out others',anchorEl);
}
window.rSessions=rSessions;

// ── SETTINGS ─────────────────────────────────────────────────────
let _setPage='account';   // which Settings category is open (survives the re-renders after saving something)
function rSettings(){
  const blobsOn=SETTINGS.show_blobs!==false;
  const blobSpeed=SETTINGS.blob_speed||'slow';
  const pinOn=SETTINGS.sidebar_pinned===true;
  const uname=SETTINGS.display_name||UNAME||'';
  const _ico=p=>`<svg viewBox="0 0 24 24" aria-hidden="true">${p}</svg>`;
  const _SET_ICONS={
    account:_ico('<circle cx="12" cy="8" r="4"/><path d="M4 21v-1a6 6 0 016-6h4a6 6 0 016 6v1"/>'),
    appearance:_ico('<circle cx="13.5" cy="6.5" r="1.5"/><circle cx="17.5" cy="10.5" r="1.5"/><circle cx="8.5" cy="7.5" r="1.5"/><circle cx="6.5" cy="12.5" r="1.5"/><path d="M12 22a10 10 0 110-20c5.5 0 10 3.6 10 8 0 3-2.5 4-4.5 4H15a2 2 0 00-1.4 3.4c.6.6.5 1.6-.1 2.2-.4.3-.9.4-1.5.4z"/>'),
    security:_ico('<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z"/><path d="M9 12l2 2 4-4"/>'),
    storage:_ico('<path d="M18 10h-1.26A8 8 0 109 20h9a5 5 0 000-10z"/>'),
    notifications:_ico('<path d="M18 8a6 6 0 10-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 01-3.46 0"/>'),
    remote:_ico('<circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15 15 0 010 20 15 15 0 010-20z"/>'),
    addresses:_ico('<path d="M10 13a5 5 0 007.07 0l3-3a5 5 0 00-7.07-7.07l-1.5 1.5"/><path d="M14 11a5 5 0 00-7.07 0l-3 3a5 5 0 007.07 7.07l1.5-1.5"/>'),
    server:_ico('<rect x="3" y="4" width="18" height="6" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/><circle cx="7" cy="7" r=".8"/><circle cx="7" cy="17" r=".8"/>'),
  };
  const _navBtn=(id,label)=>`<button class="set-nav-btn${_setPage===id?' active':''}" data-page="${id}" type="button">${_SET_ICONS[id]}<span>${label}</span></button>`;
  const _page=(id,title,sub,body)=>`<section class="set-page${_setPage===id?' active':''}" id="setp-${id}" data-page="${id}">
      <div class="set-ph"><h2>${title}</h2><p>${sub}</p></div>${body}</section>`;
  document.getElementById('vroot').innerHTML=`
  <div class="vh"><div><div class="vtitle">Settings</div><div class="vsub">Your account, how Void OS looks${IS_ADMIN?', and how the server is set up':''}</div></div></div>
  <div class="set-shell">
    <nav class="set-nav" id="setNav" aria-label="Settings sections">
      <div class="set-nav-grp">Your account</div>
      ${_navBtn('account','Account')}
      ${_navBtn('appearance','Appearance')}
      ${_navBtn('security','Security')}
      ${_navBtn('storage','Storage &amp; cloud')}
      ${IS_ADMIN?`<div class="set-nav-grp">Administration</div>
      ${_navBtn('notifications','Notifications')}
      ${_navBtn('remote','Remote access')}
      ${_navBtn('addresses','App addresses')}`:''}
      ${IS_SUPERUSER?`<div class="set-nav-grp">Server</div>
      ${_navBtn('server','Updates &amp; backup')}`:''}
    </nav>
    <div class="set-pages">

    ${_page('account','Account','Who you are on this server.',`
      <div class="scard">
        <div class="ssep">Profile</div>
        <div class="srow col">
          <div><div class="slabel">Display name</div><div class="sdesc">Used in the greeting on the dashboard</div></div>
          <div style="display:flex;gap:8px;width:100%">
            <input class="fgi" id="nameInp" placeholder="e.g. Ishaan" value="${escA(uname)}" style="flex:1;max-width:280px" spellcheck="false"/>
            <button class="btn btn-p btn-sm" id="saveNameBtn">Save</button>
          </div>
          ${uname?`<div style="font-size:12px;color:var(--td)">Preview: <em style="color:var(--ps)">${esc(getGreeting())}</em></div>`:''}
        </div>
      </div>
      <div class="scard">
        <div class="ssep">This account</div>
        <div class="set-kv">
          <span>Username</span><code>${esc(UNAME)}</code>
          <span>Rank</span><code>${esc(ROLE==='superuser'?'Super User':ROLE.charAt(0).toUpperCase()+ROLE.slice(1))}</code>
          <span>Files folder</span><code>${esc(FROOT)}</code>
          <span>Docker</span><code>${DOKR?'connected':'not available'}</code>
          <span>Terminal</span><code>${CAN_TERM?'enabled':'no access'}</code>
        </div>
      </div>`)}

    ${_page('appearance','Appearance','Colours, background and layout. Changes apply right away.',`
      <div class="scard">
        <div class="ssep">Color theme</div>
        <div class="srow col">
          <div><div class="sdesc" style="margin-top:0">Pick a look for the whole interface</div></div>
          <div class="theme-grid">${THEMES.map(t=>themeSwatch(t,SETTINGS.theme||'horizon')).join('')}</div>
        </div>
      </div>
      <div class="scard">
        <div class="ssep">Background</div>
        <div class="srow col">
          <div><div class="sdesc" style="margin-top:0">A wallpaper, shown behind the home-screen-mode home screen. Choose one of ours or upload your own.</div></div>
          <div class="wallpaper-grid" id="wallpaperGrid">${wallpaperSwatches(SETTINGS.wallpaper||'')}</div>
          <div style="display:flex;gap:8px;align-items:center">
            <input type="file" id="wallpaperFileInp" accept="image/png,image/jpeg,image/webp" style="display:none"/>
            <button class="btn btn-g btn-sm" id="wallpaperUploadBtn">Upload image…</button>
            <span id="wallpaperFileName" style="font-size:12px;color:var(--td)"></span>
          </div>
        </div>
      </div>
      <div class="scard">
        <div class="ssep">Layout</div>
        <div class="srow col">
          <div><div class="slabel">Interface</div><div class="sdesc">Classic is the original Void OS sidebar layout. Home screen mode drops the sidebar for a grid of apps and a clock, like a phone home screen.</div></div>
          <div class="seg" id="uiModeSeg">
            <button class="seg-btn ${(SETTINGS.ui_mode||'auto')==='auto'?'active':''}" data-mode="auto">Auto (server default)</button>
            <button class="seg-btn ${(SETTINGS.ui_mode||'auto')==='classic'?'active':''}" data-mode="classic">Classic</button>
            <button class="seg-btn ${(SETTINGS.ui_mode||'auto')==='home'?'active':''}" data-mode="home">Home screen</button>
          </div>
        </div>
        <div class="srow"><div><div class="slabel"><label for="pinT" style="cursor:pointer">Keep sidebar open</label></div><div class="sdesc">Pin the sidebar expanded instead of expand-on-hover (Classic mode only)</div></div>
          <label class="toggle"><input type="checkbox" id="pinT" ${pinOn?'checked':''}/><span class="toggle-sl"></span></label></div>
        ${IS_SUPERUSER?`
        <div class="srow col">
          <div><div class="slabel">Server-wide default interface</div><div class="sdesc">What accounts set to "Auto" get. Super User only.</div></div>
          <div class="seg" id="uiModeDefaultSeg">
            <button class="seg-btn" data-mode="classic">Classic</button>
            <button class="seg-btn" data-mode="home">Home screen</button>
          </div>
        </div>`:''}
      </div>
      <div class="scard">
        <div class="ssep">Effects</div>
        <div class="srow"><div><div class="slabel"><label for="blobT" style="cursor:pointer">Background blobs</label></div><div class="sdesc">Animated blobs behind the interface</div></div>
          <label class="toggle"><input type="checkbox" id="blobT" ${blobsOn?'checked':''}/><span class="toggle-sl"></span></label></div>
        <div class="srow"><div><div class="slabel">Clock</div><div class="sdesc">12-hour (4:30 PM) or 24-hour (16:30) time on the home screen</div></div>
          <div class="seg" id="timeFmtSeg">
            <button class="seg-btn ${(SETTINGS.time_format||'12')==='12'?'active':''}" data-tf="12">12-hour</button>
            <button class="seg-btn ${SETTINGS.time_format==='24'?'active':''}" data-tf="24">24-hour</button>
          </div></div>
        <div class="srow"><div><div class="slabel">Blob speed</div><div class="sdesc">How fast the background blobs drift</div></div>
          <div class="seg" id="blobSpeedSeg">
            <button class="seg-btn ${blobSpeed==='static'?'active':''}" data-speed="static">Static</button>
            <button class="seg-btn ${blobSpeed==='slow'?'active':''}" data-speed="slow">Slow</button>
            <button class="seg-btn ${blobSpeed==='fast'?'active':''}" data-speed="fast">Fast</button>
          </div></div>
      </div>`)}

    ${_page('security','Security','Keep your account safe.',`
      <div class="scard">
        <div class="ssep">Password</div>
        <div class="srow col" id="securitySection">
          <div><div class="slabel">Change password</div><div class="sdesc">Enter your current password to set a new one. Your other signed-in devices are signed out.</div></div>
          <div style="display:flex;flex-direction:column;gap:8px;width:100%">
            <input class="fgi" id="cpOld" type="password" placeholder="Current password" style="max-width:320px" autocomplete="current-password"/>
            <input class="fgi" id="cpNew" type="password" placeholder="New password (min. 8 chars)" style="max-width:320px" autocomplete="new-password"/>
            <input class="fgi" id="cpConf" type="password" placeholder="Confirm new password" style="max-width:320px" autocomplete="new-password"/>
            <div><button class="btn btn-p btn-sm" id="cpBtn">Update Password</button></div>
          </div>
        </div>
      </div>
      <div class="scard">
        <div class="ssep">Two-factor authentication</div>
        <div class="srow col" id="tfaSection">
          <div><div class="sdesc" style="margin-top:0">Add an extra layer of security. Once it’s on, you’ll need your password and a code from an authenticator app on your phone to sign in.</div></div>
          <div id="tfaBox" style="width:100%"><div class="loading"><div class="loader"></div></div></div>
        </div>
      </div>`)}

    ${_page('storage','Storage &amp; cloud','Your space, network drives and linked cloud accounts.',`
      ${!IS_ADMIN?`
      <div class="scard">
        <div class="ssep">Your storage</div>
        <div class="srow col" id="quotaSection">
          <div style="width:100%"><div class="loading"><div class="loader"></div><span>Loading storage usage…</span></div></div>
        </div>
      </div>`:''}
      <div class="scard">
        <div class="ssep">Cloud accounts</div>
        <div class="srow col">
          <div><div class="sdesc" style="margin-top:0">Browse, import/export, and continuously sync with Dropbox or Google Drive. Requires the server admin to configure API credentials in .env first.</div></div>
          <div id="rcloneStatus" style="font-size:11.5px;color:var(--td);width:100%"></div>
          <div id="cloudProvidersList" style="width:100%"><div class="loading"><div class="loader"></div></div></div>
        </div>
      </div>
      <div class="scard">
        <div class="ssep">Network drive (WebDAV)</div>
        <div class="srow col">
          <div><div class="slabel">Mount as a network drive</div>
            <div class="sdesc">Connect Finder, Explorer, or any WebDAV app — sign in with your Void OS username and password</div></div>
          <div style="display:flex;gap:8px;width:100%;max-width:460px">
            <input class="fgi" id="webdavUrl" readonly value="" style="flex:1;font-family:var(--f-mono);font-size:12px" onclick="this.select()"/>
            <button class="btn btn-g btn-sm" id="webdavCopyBtn">Copy</button>
          </div>
        </div>
      </div>`)}

    ${IS_ADMIN?_page('notifications','Notifications','Choose what you hear about, and where it goes.',`
      <div class="scard">
        <div class="ssep">Major events</div>
        <div class="srow col">
          <div><div class="slabel">Get notified of major events</div><div class="sdesc">Logins, failed login attempts, user changes, server setting changes, panic button, and more — sent to email, Discord, and/or Telegram. ${IS_SUPERUSER?'':'Super User only to edit — shown read-only.'}</div></div>
          <div id="notifyPanel" style="width:100%"><div class="loading"><div class="loader"></div></div></div>
        </div>
      </div>`):''}

    ${IS_ADMIN?_page('remote','Remote access','Reach Void OS from outside your network.',`
      <div class="scard">
        <div class="ssep">Cloudflare Quick Tunnel</div>
        <div class="srow col">
          <div><div class="sdesc" style="margin-top:0">Drag the strip open to reveal the public link, then copy it</div></div>
          <div class="tunnel-row" style="width:100%">
            <div class="vt-strip" id="vtStrip" data-locked="true">
              <div class="vt-content" aria-hidden="true"><span id="vtContentText">Checking…</span></div>
              <span class="vt-shadow"></span>
              <div class="vt-cover">
                <span class="vt-icon">
                  <svg viewBox="0 0 24 24"><path d="M13.5 4.5L21 12m0 0l-7.5 7.5M21 12H3"/></svg>
                </span>
                <div class="vt-back">
                  <div class="vt-back-shadow"></div>
                  <div class="vt-backing"></div>
                </div>
                <span class="vt-cover-text" id="vtCoverText"><span>Checking…</span></span>
              </div>
              <div class="vt-handle"></div>
              <input type="range" min="0" max="100" step="1" value="0" id="vtRange"
                aria-label="Drag to reveal the Cloudflare tunnel link" disabled/>
            </div>
            <button class="tunnel-copy" id="tunnelCopyBtn" title="Copy link" disabled>
              <svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
            </button>
          </div>
          <div id="tunnelProbe" class="sdesc" style="display:none;width:100%;margin-top:8px"></div>
          <div id="kvSyncRow" style="display:none;width:100%;margin-top:10px;padding-top:10px;border-top:1px solid var(--bd);align-items:center;justify-content:space-between;gap:10px">
            <div style="font-size:12px;color:var(--td)" id="kvSyncStatus">Workers KV sync: —</div>
            <button class="btn btn-g btn-sm" id="kvSyncBtn">Sync Now</button>
          </div>
          <details id="tunnelLogDetails" style="display:none;width:100%;margin-top:10px">
            <summary style="cursor:pointer;font-size:12px;color:var(--td)">cloudflared output (for troubleshooting)</summary>
            <pre id="tunnelLogPre" style="margin-top:8px;padding:10px;background:var(--deep);border:1px solid var(--bd);border-radius:8px;font-size:11px;line-height:1.5;max-height:220px;overflow:auto;white-space:pre-wrap;word-break:break-all;color:var(--td)"></pre>
          </details>
        </div>
      </div>`):''}

    ${IS_ADMIN?_page('addresses','App addresses','Where each app on the dashboard can be reached, and whether it is online.',`
      <div class="scard">
        <div class="ssep">Your domain</div>
        <div class="srow col" id="addrDomain"><div class="sdesc">Checking\u2026</div></div>
      </div>
      <div class="scard">
        <div class="ssep" style="display:flex;align-items:center;justify-content:space-between">
          <span>Apps</span><button class="btn btn-g btn-sm" id="addrRefresh" type="button">Refresh</button></div>
        <div id="addrList"><div class="loading"><div class="loader"></div></div></div>
      </div>`):''}

    ${IS_SUPERUSER?_page('server','Updates &amp; backup','Keep Void OS current and safe.',`
      <div class="scard" id="updCard">
        <div class="ssep">Updates</div>
        <div id="updBody"><div class="loading"><div class="loader"></div></div></div>
      </div>
      <div class="scard">
        <div class="ssep">Backup &amp; restore</div>
        <div class="srow col">
          <div><div class="slabel">Export backup</div>
            <div class="sdesc">Downloads users, tiles, sessions, share links, installed-app records, and the
              activity log as a single zip. Doesn't include your actual files or app data — back those up
              separately.</div></div>
          <button class="btn btn-g" id="backupExportBtn">⬇ Export Backup</button>
        </div>
        <div class="srow col">
          <div><div class="slabel">Restore from backup</div>
            <div class="sdesc">Restores from a zip made by Export Backup above — works across machines, e.g.
              moving to new hardware. Existing store files are snapshotted first, so this can be undone by hand
              if needed. This cannot be undone from within the app itself.</div></div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <input type="file" id="backupFileInp" accept=".zip" style="display:none"/>
            <button class="btn btn-g" id="backupChooseBtn">Choose File…</button>
            <span id="backupFileName" style="font-size:12px;color:var(--td)">No file chosen</span>
            <button class="btn btn-d" id="backupImportBtn" disabled>⬆ Restore</button>
          </div>
          <div id="backupResult" style="font-size:12px;color:var(--td);width:100%"></div>
        </div>
      </div>`):''}
    </div>
  </div>`;
  // Category navigation: every page stays in the document (their loaders and handlers are
  // wired below), only the chosen one is shown.
  const _showSetPage=id=>{
    _setPage=id;
    document.querySelectorAll('#setNav .set-nav-btn').forEach(b=>{
      const on=b.dataset.page===id;b.classList.toggle('active',on);
      if(on)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');
    });
    document.querySelectorAll('.set-page').forEach(p=>p.classList.toggle('active',p.dataset.page===id));
  };
  document.getElementById('setNav').addEventListener('click',e=>{
    const b=e.target.closest('.set-nav-btn');if(b)_showSetPage(b.dataset.page);
  });
  _showSetPage(document.getElementById('setp-'+_setPage)?_setPage:'account');
  document.getElementById('saveNameBtn')?.addEventListener('click',async()=>{
    const n=document.getElementById('nameInp').value.trim();
    const ok=await saveSettings({display_name:n});
    if(ok){toast('Display name saved','success');setTimeout(()=>rSettings(),150)}
    else toast('Failed to save','error');
  });
  document.getElementById('blobT')?.addEventListener('change',async e=>{
    const checked=e.target.checked;
    document.body.classList.toggle('blobs-hidden',!checked); // optimistic UI
    const ok=await saveSettings({show_blobs:checked});
    if(!ok){toast('Failed to save setting','error');document.body.classList.toggle('blobs-hidden',!e.target.checked)}
  });
  document.querySelectorAll('#blobSpeedSeg .seg-btn').forEach(btn=>{
    btn.addEventListener('click',()=>setBlobSpeed(btn.dataset.speed));
  });
  document.querySelectorAll('#timeFmtSeg .seg-btn').forEach(btn=>{
    btn.addEventListener('click',async()=>{
      const tf=btn.dataset.tf,prev=SETTINGS.time_format||'12';
      if(tf===prev)return;
      SETTINGS.time_format=tf;                                   // instant preview
      document.querySelectorAll('#timeFmtSeg .seg-btn').forEach(b=>b.classList.toggle('active',b.dataset.tf===tf));
      const c=document.getElementById('hsClock');if(c)c.textContent=_clockParts(new Date()).short;
      const ok=await saveSettings({time_format:tf});
      if(!ok){
        SETTINGS.time_format=prev;
        document.querySelectorAll('#timeFmtSeg .seg-btn').forEach(b=>b.classList.toggle('active',b.dataset.tf===prev));
        errToast(_lastSaveErr,'Couldn\u2019t save the clock format');
      }
    });
  });
  document.getElementById('pinT')?.addEventListener('change',e=>{
    if(!isMobile())toggleSidebar(e.target.checked); // mirrors clicking the sidebar's own pin button
    else saveSettings({sidebar_pinned:e.target.checked}); // mobile: just remember the preference for next desktop visit
  });
  document.querySelectorAll('#uiModeSeg .seg-btn').forEach(btn=>{
    btn.addEventListener('click',()=>setUiMode(btn.dataset.mode));
  });
  document.querySelectorAll('.wallpaper-swatch').forEach(el=>{
    el.addEventListener('click',()=>setWallpaper(el.dataset.wallpaperId));
  });
  const wallpaperFileInp=document.getElementById('wallpaperFileInp');
  document.getElementById('wallpaperUploadBtn')?.addEventListener('click',()=>wallpaperFileInp.click());
  wallpaperFileInp?.addEventListener('change',async()=>{
    const f=wallpaperFileInp.files[0];if(!f)return;
    document.getElementById('wallpaperFileName').textContent='Uploading\u2026';
    const fd=new FormData();fd.append('file',f);
    try{
      const r=await(await api('/api/me/wallpaper/upload',{method:'POST',body:fd})).json();
      if(r.ok){SETTINGS.wallpaper=r.wallpaper;SETTINGS.wallpaper_url=r.url;applyWallpaper();toast('Wallpaper updated','success');rSettings()}
      else errToast(r,'Upload failed');
    }catch(e){toast('Upload failed','error')}
    finally{document.getElementById('wallpaperFileName')&&(document.getElementById('wallpaperFileName').textContent='')}
  });
  if(IS_SUPERUSER){
    api('/api/server-settings').then(r=>r.json()).then(d=>{
      document.querySelectorAll('#uiModeDefaultSeg .seg-btn').forEach(b=>{
        b.classList.toggle('active',b.dataset.mode===d.ui_mode_default);
        b.addEventListener('click',()=>setUiModeDefault(b.dataset.mode));
      });
    }).catch(()=>toast('Couldn’t load the server-wide default interface','error',{key:'srv-default'}));
  }
  loadCloudProviders();
  if(IS_ADMIN){loadNotifyPanel();loadAddresses();document.getElementById('addrRefresh')?.addEventListener('click',()=>loadAddresses())}
  api('/api/rclone/check').then(r=>r.json()).then(d=>{
    const el=document.getElementById('rcloneStatus');if(!el)return;
    el.textContent=d.ok?('rclone detected \u2713 ('+d.message+') \u2014 continuous sync is available'):d.message;
    el.style.color=d.ok?'var(--gr)':'var(--yw)';
  }).catch(()=>{const el=document.getElementById('rcloneStatus');if(el){el.textContent='Couldn’t check whether continuous sync is available.';el.style.color='var(--yw)'}});
  document.querySelectorAll('.theme-swatch').forEach(el=>{
    el.addEventListener('click',()=>setTheme(el.dataset.themeId));
  });
  document.getElementById('cpBtn')?.addEventListener('click',async()=>{
    const op=document.getElementById('cpOld').value,np=document.getElementById('cpNew').value,cf=document.getElementById('cpConf').value;
    if(!op||!np||!cf){toast('Fill in all fields','error');return}
    if(np!==cf){toast('New passwords do not match','error');return}
    if(np.length<8){toast('Min. 8 characters','error');return}
    const r=await(await api('/api/me/password',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({old_password:op,new_password:np})})).json();
    if(r.ok){toast('Password changed','success');document.getElementById('cpOld').value='';document.getElementById('cpNew').value='';document.getElementById('cpConf').value=''}
    else errToast(r,'Failed');
  });
  loadTfa();
  if(IS_SUPERUSER)loadUpdates();
  document.getElementById('backupExportBtn')?.addEventListener('click',async()=>{
    const btn=document.getElementById('backupExportBtn');
    btn.disabled=true;const origLbl=btn.textContent;btn.textContent='Exporting\u2026';
    try{
      const r=await api('/api/backup/export');
      if(!r.ok){const d=await r.json().catch(()=>({}));errToast(d,'Export failed');return}
      const blob=await r.blob();
      const cd=r.headers.get('Content-Disposition')||'';
      const m=/filename="?([^"]+)"?/.exec(cd);
      const fname=m?m[1]:`voidos-backup-${Date.now()}.zip`;
      const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=fname;
      document.body.appendChild(a);a.click();a.remove();
      setTimeout(()=>URL.revokeObjectURL(a.href),4000);
      toast('Backup downloaded','success');
    }catch(e){toast('Export failed','error')}
    finally{btn.disabled=false;btn.textContent=origLbl}
  });
  const backupFileInp=document.getElementById('backupFileInp');
  document.getElementById('backupChooseBtn')?.addEventListener('click',()=>backupFileInp.click());
  backupFileInp?.addEventListener('change',()=>{
    const f=backupFileInp.files[0];
    document.getElementById('backupFileName').textContent=f?f.name:'No file chosen';
    document.getElementById('backupImportBtn').disabled=!f;
  });
  document.getElementById('backupImportBtn')?.addEventListener('click',()=>{
    const f=backupFileInp.files[0];
    if(!f)return;
    confirmDlg('Restore from this backup? Current users, tiles, sessions, share links, and installed-app '+
      'records will be overwritten (each is snapshotted first, so this can be undone by hand). This cannot '+
      'be undone from within the app itself.',async()=>{
      const resultEl=document.getElementById('backupResult');
      resultEl.textContent='Restoring\u2026';
      const fd=new FormData();fd.append('file',f);
      try{
        const r=await api('/api/backup/import',{method:'POST',body:fd});
        const d=await r.json();
        if(d.error){errToast(d);resultEl.textContent='';return}
        toast('Backup restored','success');
        resultEl.textContent=`Restored: ${d.restored.join(', ')}`+(d.snapshots.length?` \u2014 previous files saved as ${d.snapshots.map(s=>s.split(/[\\/]/).pop()).join(', ')}`:'');
        backupFileInp.value='';document.getElementById('backupFileName').textContent='No file chosen';
        document.getElementById('backupImportBtn').disabled=true;
      }catch(e){toast('Restore failed','error');resultEl.textContent=''}
    },'Restore');
  });
  const webdavUrl=`${location.origin}/webdav/`;
  const webdavInp=document.getElementById('webdavUrl');
  if(webdavInp)webdavInp.value=webdavUrl;
  document.getElementById('webdavCopyBtn')?.addEventListener('click',async()=>{
    try{
      if(navigator.clipboard&&window.isSecureContext)await navigator.clipboard.writeText(webdavUrl);
      else{const ta=document.createElement('textarea');ta.value=webdavUrl;ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove()}
      toast('WebDAV URL copied','success');
    }catch(e){toast('Could not copy — select and copy manually','error')}
  });
  if(IS_ADMIN)initTunnelBar();
  if(!IS_ADMIN)loadQuota();
}
window.rSettings=rSettings;

async function loadQuota(){
  const gen=_renderGen;
  const el=document.getElementById('quotaSection');
  if(!el)return;
  try{
    const me=await(await api('/api/me')).json();
    if(gen!==_renderGen)return;
    const q=me.quota;
    if(!q){el.innerHTML=`<div><div class="slabel">Personal folder</div><div class="sdesc">No storage limit configured</div></div>`;return}
    const usedMB=q.used_bytes/(1024*1024);
    const pct=Math.min(100,(usedMB/q.cap_mb)*100);
    const barColor=pct>90?'var(--rd)':pct>70?'var(--yw)':'var(--pu)';
    el.innerHTML=`<div style="width:100%">
      <div class="slabel">Personal folder</div>
      <div class="sdesc">${usedMB.toFixed(1)} MB of ${q.cap_mb} MB used</div>
      <div style="background:var(--sf2);border-radius:99px;height:8px;overflow:hidden;margin-top:8px;width:100%;max-width:320px">
        <div style="height:100%;width:${pct}%;background:${barColor};border-radius:99px;transition:width .3s"></div>
      </div>
    </div>`;
  }catch(e){el.innerHTML=`<div style="color:var(--rd)">${esc(e.message)}</div>`}
}

// ── SHARE LINKS ────────────────────────────────────────────────────
async function rShares(userFilter=''){
  const gen=++_renderGen; // self-bumped: the filter input re-renders this same view, same reasoning as rFiles
  const root=document.getElementById('vroot');
  root.innerHTML=`<div class="vh"><div><div class="vtitle">Share Links</div>
    <div class="vsub">Public links created from Shared \u2014 anyone with a link can view it, no account needed</div></div>
    ${IS_SUPERUSER?`<input class="fgi" id="sharesUserF" placeholder="Filter by username\u2026" value="${escA(userFilter)}" style="width:180px;font-size:12px;padding:6px 10px"/>`:''}</div>
    <div id="sharesListSection"><div class="loading"><div class="loader"></div><span>Loading\u2026</span></div></div>`;
  const filterInp=document.getElementById('sharesUserF');
  if(filterInp){
    filterInp.addEventListener('keydown',e=>{if(e.key==='Enter')rShares(filterInp.value.trim())});
    filterInp.addEventListener('blur',()=>{if(filterInp.value.trim()!==userFilter)rShares(filterInp.value.trim())});
  }
  await loadShares(gen,userFilter);
}
async function loadShares(gen,userFilter=''){
  const el=document.getElementById('sharesListSection');
  if(!el)return;
  try{
    let shares=await apiData('/api/shares');
    if(gen!==_renderGen)return;
    if(userFilter)shares=shares.filter(s=>(s.owner_username||'').toLowerCase().includes(userFilter.toLowerCase()));
    // Superuser view can span every account's links — group by owner so a long list stays scannable,
    // reusing the same "filter, don't invent a new UI" approach loadAuditLog already takes.
    const groups=IS_SUPERUSER
      ? Object.entries(shares.reduce((acc,s)=>{(acc[s.owner_username||'?']=acc[s.owner_username||'?']||[]).push(s);return acc},{}))
          .sort(([a],[b])=>a.localeCompare(b))
      : [[null,shares]];
    el.innerHTML=`<div class="admin-section">
      <div class="admin-section-title">${IS_ADMIN?'All Share Links':'Your Share Links'} (${shares.length})</div>
      ${!shares.length?'<div class="empty-dir">No share links yet \u2014 right-click a file or folder in Shared and choose "Get Share Link"</div>':
        groups.map(([owner,rows])=>`
          ${owner!==null?`<div style="font-size:11px;font-weight:600;color:var(--td);text-transform:uppercase;letter-spacing:.06em;margin:16px 0 6px">${esc(owner)}</div>`:''}
          <div class="user-table">${rows.map(shareRow).join('')}</div>
        `).join('')}
      </div>`;
    shares.forEach(s=>{
      document.getElementById(`shRev-${s.id}`)?.addEventListener('click',e=>revokeShare(s.id,e.currentTarget));
      document.getElementById(`shEdit-${s.id}`)?.addEventListener('click',()=>showEditShareDlg(s));
      document.getElementById(`shCopy-${s.id}`)?.addEventListener('click',()=>copyShareLink(s.id));
    });
  }catch(e){if(gen!==_renderGen)return;el.innerHTML=`<div style="color:var(--rd)">${esc(e.message)}</div>`}
}
function shareRow(s){
  const expiry=s.expired?'<span style="color:var(--rd)">Expired</span>'
    :s.expires_at?`Expires ${new Date(s.expires_at*1000).toLocaleString()}`:'Never expires';
  const roleTag=!s.is_mine&&s.owner_role?` <span style="font-size:10px;color:var(--td)">(${esc(s.owner_role)})</span>`:'';
  return `<div class="user-row" id="shrow-${s.id}">
    <div class="user-avatar ${s.is_dir?'avatar-admin':'avatar-guest'}">${s.is_dir?'📁':'📄'}</div>
    <div class="user-info">
      <div class="user-name">${esc(s.name)}${!s.is_mine?` <span style="font-size:10px;color:var(--td)">— ${esc(s.owner_username)}</span>${roleTag}`:''}
        ${s.has_password?'<span style="font-size:10px;color:var(--yw)">🔒</span>':''}</div>
      <div class="user-meta">${expiry} &nbsp;•&nbsp; ${s.hits} download${s.hits===1?'':'s'} &nbsp;•&nbsp;
        <span style="font-family:var(--f-mono);font-size:10.5px">${esc(s.path)}</span></div>
    </div>
    <div class="user-actions">
      <button class="btn btn-g btn-sm" id="shCopy-${s.id}">Copy Link</button>
      <button class="btn btn-g btn-sm" id="shEdit-${s.id}">Edit Duration</button>
      <button class="btn btn-d btn-sm" id="shRev-${s.id}">🗑 Revoke</button>
    </div>
  </div>`;
}
async function copyShareLink(sid){
  try{
    const d=await(await api(`/api/shares/${sid}/link`)).json();
    if(d.error){errToast(d);return}
    const full=`${location.origin}${d.url}`;
    if(navigator.clipboard&&window.isSecureContext)await navigator.clipboard.writeText(full);
    else{const ta=document.createElement('textarea');ta.value=full;ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove()}
    toast('Link copied','success');
  }catch(e){toast('Could not copy \u2014 select and copy manually','error')}
}
function showEditShareDlg(s){
  openMdl(`Edit Duration \u2014 ${esc(s.name)}`,`
    <div class="fg"><label class="fgl">Expires</label>
      <select class="fgi" id="shEditExp">
        <option value="1">1 hour</option>
        <option value="24">1 day</option>
        <option value="168">7 days</option>
        <option value="720">30 days</option>
        <option value="0" ${!s.expires_at?'selected':''}>Never</option>
      </select></div>`,
    `<button class="btn btn-g" onclick="closeMdl()">Cancel</button><button class="btn btn-p" id="shEditGoBtn">Save</button>`);
  enhanceSelect(document.getElementById('shEditExp'));
  document.getElementById('shEditGoBtn')?.addEventListener('click',async()=>{
    const hours=Number(document.getElementById('shEditExp').value);
    const r=await api(`/api/shares/${s.id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({expires_in_hours:hours||null})});
    const d=await r.json();
    if(d.error){errToast(d);return}
    closeMdl();toast('Share updated','success');loadShares(_renderGen);
  });
}
async function revokeShare(sid,anchorEl){
  confirmDlg('Revoke this share link? Anyone using it will immediately lose access.',async()=>{
    const r=await(await api(`/api/shares/${sid}`,{method:'DELETE'})).json();
    if(r.error){errToast(r);return}
    toast('Share link revoked','info');loadShares(_renderGen);
  },'Revoke',anchorEl);
}
window.rShares=rShares;
function _vtSetValue(strip,range,v){
  v=Math.max(0,Math.min(100,v));
  range.value=v;
  strip.style.setProperty('--value',v);
}
async function initTunnelBar(){
  if(_tunnelPoll){clearTimeout(_tunnelPoll);_tunnelPoll=null}
  const strip=document.getElementById('vtStrip'),range=document.getElementById('vtRange');
  if(strip&&range&&!strip.dataset.wired){
    strip.dataset.wired='1';
    // Live drag feedback — mirror the slider's value straight into the CSS var
    range.addEventListener('input',()=>strip.style.setProperty('--value',range.value));
    // On release: snap fully open (reveal) past the halfway point, otherwise snap closed
    range.addEventListener('change',()=>{
      const open=Number(range.value)>=50;
      _vtSetValue(strip,range,open?100:0);
      strip.classList.toggle('vt-strip--open',open);
      const copyBtn=document.getElementById('tunnelCopyBtn');
      if(copyBtn&&copyBtn.dataset.url)copyBtn.disabled=!open;
    });
  }
  // refreshTunnel() says how long to wait before asking again (0 = stop). It keeps
  // checking even after "connected": a restart or a drop gives the tunnel a NEW address,
  // and a page that stopped looking would keep offering the old, dead one to copy.
  const tick=async()=>{
    _tunnelPoll=null;
    const wait=await refreshTunnel();
    if(wait&&document.getElementById('vtStrip'))_tunnelPoll=setTimeout(tick,wait);
  };
  tick();
  const copyBtn=document.getElementById('tunnelCopyBtn');
  if(copyBtn&&!copyBtn.dataset.wired){
    copyBtn.dataset.wired='1';
    copyBtn.addEventListener('click',async()=>{
      const url=copyBtn.dataset.url;if(!url)return;
      try{
        if(navigator.clipboard&&window.isSecureContext)await navigator.clipboard.writeText(url);
        else{const ta=document.createElement('textarea');ta.value=url;ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove()}
        copyBtn.classList.add('copied');toast('Link copied','success');
        setTimeout(()=>copyBtn.classList.remove('copied'),1400);
      }catch(e){toast('Could not copy — select and copy manually','error')}
    });
  }
  const kvBtn=document.getElementById('kvSyncBtn');
  if(kvBtn&&!kvBtn.dataset.wired){
    kvBtn.dataset.wired='1';
    kvBtn.addEventListener('click',async()=>{
      kvBtn.disabled=true;const orig=kvBtn.textContent;kvBtn.textContent='Syncing…';
      try{
        const d=await(await api('/api/admin/tunnel/sync-kv',{method:'POST'})).json();
        if(d.ok){toast('Workers KV synced','success')}else{errToast(d,'Sync failed')}
      }catch(e){toast('Sync failed','error')}
      finally{kvBtn.disabled=false;kvBtn.textContent=orig;refreshTunnel()}
    });
  }
}
// Returns how many ms to wait before checking again (0 = stop polling)
async function refreshTunnel(){
  const strip=document.getElementById('vtStrip'),range=document.getElementById('vtRange'),
        contentEl=document.getElementById('vtContentText'),coverEl=document.getElementById('vtCoverText'),
        copyBtn=document.getElementById('tunnelCopyBtn'),kvRow=document.getElementById('kvSyncRow'),
        kvStatus=document.getElementById('kvSyncStatus'),kvBtn=document.getElementById('kvSyncBtn'),
        logDetails=document.getElementById('tunnelLogDetails'),logPre=document.getElementById('tunnelLogPre');
  if(!strip)return 0; // navigated away
  try{
    const resp=await api('/api/admin/tunnel');
    const d=await resp.json();
    if(!resp.ok){
      copyBtn.disabled=true;delete copyBtn.dataset.url;
      if(resp.status===0){coverEl.innerHTML='<span>Offline</span>';contentEl.textContent='Can\u2019t reach Void OS right now';return 5000}
      coverEl.innerHTML='<span>Unavailable</span>';contentEl.textContent=d.error||'Couldn\u2019t read the tunnel status';
      return 0;
    }
    // Raw cloudflared output — the only real way to tell "still handshaking"
    // apart from "cloudflared is failing for a reason nobody can see" (a bad
    // token env var it doesn't need, a blocked outbound connection, DNS,
    // etc). Only worth showing once there's something to show, and once the
    // tunnel isn't already connected (nobody needs this once it's working).
    if(logDetails){
      if(d.status!=='connected'&&d.status!=='disabled'&&Array.isArray(d.recent_output)&&d.recent_output.length){
        logDetails.style.display='block';
        logPre.textContent=d.recent_output.join('\n');
      }else{
        logDetails.style.display='none';
      }
    }
    if(kvRow){
      if(d.kv&&d.kv.configured){
        kvRow.style.display='flex';
        const when=d.kv.last_synced_at?new Date(d.kv.last_synced_at*1000).toLocaleTimeString():null;
        kvStatus.textContent=
          d.kv.status==='synced'  ? `Workers KV sync: \u2713 synced${when?' at '+when:''}` :
          d.kv.status==='syncing' ? 'Workers KV sync: syncing\u2026' :
          d.kv.status==='error'   ? `Workers KV sync: \u26a0 ${d.kv.error||'failed'}` :
          'Workers KV sync: not yet synced';
        if(kvBtn)kvBtn.disabled=!d.url;
      }else{
        kvRow.style.display='none';
      }
    }
    if(d.status==='connected'&&d.url){
      contentEl.textContent=d.url;
      coverEl.innerHTML='<span>Tear</span>&nbsp;<span>open</span>';
      strip.dataset.locked='false';range.disabled=false;
      copyBtn.dataset.url=d.url;
      // Only re-enable copy if the strip is currently torn open; stays disabled while covered (privacy)
      copyBtn.disabled=Number(range.value)<50;
      const pr=document.getElementById('tunnelProbe');
      if(pr){
        const pb=d.probe;
        if(!pb){pr.style.display='block';pr.style.color='var(--td)';pr.textContent='Checking that the public link answers\u2026'}
        else{
          const t=new Date(pb.at*1000).toLocaleTimeString();
          pr.style.display='block';
          if(pb.ok){pr.style.color='var(--gr)';pr.textContent=`\u2713 The public link answers (checked ${t}).`}
          else{pr.style.color='var(--yw)';pr.textContent=`\u26a0 Connected to Cloudflare, but this server couldn\u2019t open the public link (checked ${t}). `+
            `A brand-new link can take a minute to start working, and this machine\u2019s own DNS or firewall can block the check \u2014 try opening the link from your phone. ${pb.error?'('+pb.error+')':''}`}
        }
      }
      return 10000; // keep watching, gently: the link changes if the tunnel restarts
    }
    // Not connected yet — keep the strip locked shut and show status as the cover label
    strip.dataset.locked='true';range.disabled=true;
    _vtSetValue(strip,range,0);
    copyBtn.disabled=true;delete copyBtn.dataset.url;
    const pr2=document.getElementById('tunnelProbe');if(pr2)pr2.style.display='none';
    const label=
      d.status==='disabled'      ? 'Not enabled' :
      d.status==='not_installed' ? 'cloudflared not found' :
      d.status==='error'         ? 'Retrying\u2026' :
      d.status==='reconnecting'  ? 'Reconnecting\u2026' :
      'Starting\u2026';
    contentEl.textContent=
      d.status==='disabled'      ? 'Set VOID_CLOUDFLARE_TUNNEL=true in .env, then restart Void OS' :
      d.status==='not_installed' ? 'Install cloudflared, then restart Void OS' :
      d.status==='error'         ? 'cloudflared failed to connect \u2014 retrying automatically' :
      d.status==='reconnecting'  ? 'The tunnel dropped \u2014 reconnecting. The link will change when it\u2019s back.' :
      'Connecting to Cloudflare\u2026';
    coverEl.innerHTML=`<span>${label}</span>`;
    // 'error' is NOT a stable end state here — the supervisor backing this
    // always retries automatically a few seconds later (see CF_RESTART_DELAY),
    // it never just gives up. Treating it as terminal (the old behavior) meant
    // that if a single poll ever landed during that brief error window, this
    // interval stopped for good and the strip froze on whatever it last
    // fetched — including "Starting…" itself, since a fresh page load's very
    // first poll could just as easily land back in "starting" before ever
    // showing the error, making the tunnel look permanently stuck rather
    // than mid-retry. Only 'disabled' and 'not_installed' are genuinely
    // stable — nothing is running in the background that could change them
    // without an admin action (editing .env, installing cloudflared) — so
    // those are the only two that should stop polling.
    return (d.status==='disabled'||d.status==='not_installed')?0:2500;
  }catch(e){
    coverEl.innerHTML='<span>Offline</span>';
    contentEl.textContent='Could not reach server';
    copyBtn.disabled=true;
    return 5000;
  }
}

// Boot
startGreetingRefresh();
initControlSocket();
notifMountBells();notifRefresh();
loadScript('/static/vendor/viewer.js').catch(()=>{});
applyUiMode();
(function(){
  const qs=new URLSearchParams(window.location.search);
  const err=qs.get('cloud_error'),ok=qs.get('cloud_connected');
  if(err||ok){
    if(ok)toast(ok+' connected','success');
    if(err)toast('Cloud connect failed: '+err,'error');
    history.replaceState(null,'',window.location.pathname+window.location.hash);
  }
})();
go('dashboard');
}
