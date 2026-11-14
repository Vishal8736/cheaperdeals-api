/* Cheape's little AI helper ("Chhoti"): an avatar that lives on every page, talks (voice), listens (mic), notices problems by itself and can open pages.
   It can only READ the project code and shop data through /assistant/ask; it cannot change anything. Voice = the browser's own speech (free, nothing is sent to a third party by us). */
(function(){
  if(window.__companion||document.getElementById('ai-companion'))return; window.__companion=1;
  var csrf=document.body.getAttribute('hx-headers'); try{csrf=JSON.parse(csrf)['X-CSRF-Token'];}catch(e){csrf='';}
  var PAGES={orders:'/orders',order:'/orders',products:'/products',product:'/products',catalog:'/catalog',alerts:'/alerts',alert:'/alerts',agents:'/agents',agent:'/agents',strategy:'/strategy',finance:'/finance',paisa:'/finance',diagnostics:'/diagnostics',diagnostic:'/diagnostics',error:'/diagnostics',keys:'/keys','api key':'/keys',settings:'/settings',guide:'/guide',connect:'/website/connect',website:'/website/connect',dashboard:'/'};
  var GO_OK=/^\/(orders|products|catalog|alerts|agents|strategy|finance|diagnostics|website\/connect|keys|assistant|settings|guide)?$/;
  var st={voice:get('cc_voice','1')==='1',chat:get('cc_chat','1')==='1',open:false,after:-1,hist:[],unlocked:false,talking:false,lastAct:Date.now()};
  function get(k,d){try{var v=localStorage.getItem(k);return v===null?d:v;}catch(e){return d;}}
  function put(k,v){try{localStorage.setItem(k,v);}catch(e){}}
  var el=document.createElement('div'); el.id='ai-companion';
  el.innerHTML=
   '<div id="cc-panel" hidden><div id="cc-head"><b>Chhoti</b> <span id="cc-sub">tumhari AI saathi</span><button id="cc-x" aria-label="Band karo">&times;</button></div>'+
   '<div id="cc-log" aria-live="polite"></div>'+
   '<form id="cc-form"><button type="button" id="cc-mic" title="Bolkar poochho" aria-label="Mic">&#127908;</button><input id="cc-in" autocomplete="off" placeholder="Kuch bhi poochho: code, error, orders..." aria-label="Sawal"><button id="cc-send" aria-label="Bhejo">&#10148;</button></form>'+
   '<div id="cc-opts"><label><input type="checkbox" id="cc-v"> Awaaz (voice)</label><label><input type="checkbox" id="cc-c"> Khud se baat karo</label><label>Awaaz: <select id="cc-vs" aria-label="Awaaz chuno"></select></label><span>Sirf padh sakti hun, kuch badal/delete nahi.</span></div></div>'+
   '<div id="cc-bubble" hidden></div>'+
   '<button id="cc-av" aria-label="AI helper Chhoti kholo" title="Chhoti se baat karo">'+
   '<svg viewBox="0 0 100 100" width="64" height="64" aria-hidden="true"><defs><radialGradient id="ccs" cx="50%" cy="40%"><stop offset="0" stop-color="#FFE2CC"/><stop offset="1" stop-color="#F8C9A8"/></radialGradient></defs>'+
   '<path d="M14 58c-4-34 18-48 36-48s40 14 36 48c-2 14-8 26-8 26l-12-14H34L22 84s-6-12-8-26z" fill="#2B1B3D"/>'+
   '<circle cx="19" cy="30" r="9" fill="#2B1B3D"/><circle cx="81" cy="30" r="9" fill="#2B1B3D"/><circle cx="19" cy="30" r="4" fill="#FF6FA5"/><circle cx="81" cy="30" r="4" fill="#FF6FA5"/>'+
   '<ellipse cx="50" cy="56" rx="29" ry="30" fill="url(#ccs)"/>'+
   '<path d="M22 46c6-20 20-24 28-24s24 4 28 24c-10-8-18-10-28-10s-18 2-28 10z" fill="#2B1B3D"/>'+
   '<g id="cc-eyes"><ellipse cx="39" cy="56" rx="5" ry="6.5" fill="#2B1B3D"/><ellipse cx="61" cy="56" rx="5" ry="6.5" fill="#2B1B3D"/><circle cx="40.8" cy="54" r="1.8" fill="#fff"/><circle cx="62.8" cy="54" r="1.8" fill="#fff"/></g>'+
   '<circle cx="30" cy="67" r="5" fill="#FF8FB1" opacity=".55"/><circle cx="70" cy="67" r="5" fill="#FF8FB1" opacity=".55"/>'+
   '<circle cx="50" cy="63" r="1.2" fill="#E8A98B"/>'+
   '<ellipse id="cc-mouth" cx="50" cy="72" rx="5" ry="2.2" fill="#C9506F"/>'+
   '<circle cx="50" cy="31" r="3.2" fill="#FF3D6E"/></svg><span id="cc-dot" hidden></span></button>';
  document.body.appendChild(el);
  var $=function(i){return document.getElementById(i);}, panel=$('cc-panel'), log=$('cc-log'), bub=$('cc-bubble'), mouth=$('cc-mouth'), av=$('cc-av');
  $('cc-v').checked=st.voice; $('cc-c').checked=st.chat;
  var hideT=null;
  function bubble(txt,lvl,ms){ if(st.open)return; bub.textContent=txt; bub.className=lvl||''; bub.hidden=false; $('cc-dot').hidden=false; clearTimeout(hideT); hideT=setTimeout(function(){bub.hidden=true;},ms||9000); }
  function add(who,txt){ var d=document.createElement('div'); d.className='cc-m cc-'+who; d.textContent=txt; log.appendChild(d); log.scrollTop=log.scrollHeight; return d; }
  function open(v){ st.open=v; panel.hidden=!v; if(v){bub.hidden=true; $('cc-dot').hidden=true; if(!log.children.length) add('ai','Namaste! Main Chhoti hun. Project ke code, orders, errors, kuch bhi poochho. Main sirf padh sakti hun, kuch badalti ya delete nahi karti.'); setTimeout(function(){$('cc-in').focus();},50);} }
  av.addEventListener('click',function(){unlock(); open(!st.open);}); $('cc-x').addEventListener('click',function(){open(false);});
  $('cc-v').addEventListener('change',function(){st.voice=this.checked; put('cc_voice',st.voice?'1':'0'); if(!st.voice&&window.speechSynthesis)speechSynthesis.cancel(); else speak('Ab main bolungi.');});
  $('cc-c').addEventListener('change',function(){st.chat=this.checked; put('cc_chat',st.chat?'1':'0');});
  ['click','keydown','touchstart'].forEach(function(e){document.addEventListener(e,function(){st.lastAct=Date.now();unlock();},{passive:true,capture:true});});
  // ---- voice out: the browser's own voice. Humanised: the text is spoken phrase by phrase with small pauses, the pitch rises on questions, lifts on
  // exclamations and settles at the end of a sentence, and it drifts gently up and down between phrases so it sounds sing-song and warm, not flat. ----
  var voice=null, voices=[], tok=0;
  function rank(v){var n=(v.name+' '+v.lang).toLowerCase(),s=0; if(/hi[-_]in/.test(n))s+=6; else if(/en[-_]in/.test(n))s+=4; else if(/^en/.test(v.lang||''))s+=1;
    if(/female|heera|swara|kalpana|veena|lekha|neerja|google हिन्दी|google hindi|priya|aditi|raveena|zira|samantha/.test(n))s+=3; if(/natural|online|neural/.test(n))s+=3; if(/male|ravi|hemant|prabhat/.test(n)&&!/female/.test(n))s-=4; return s;}
  function pick(){ if(!window.speechSynthesis)return; var vs=speechSynthesis.getVoices(); if(!vs.length)return;
    voices=vs.slice().sort(function(a,b){return rank(b)-rank(a);}).filter(function(v){return rank(v)>0;}).slice(0,12);
    var sel=$('cc-vs'); sel.innerHTML='<option value="">Apne aap (best)</option>'+voices.map(function(v,i){return '<option value="'+i+'">'+v.name.replace(/Microsoft |Google /,'')+' ('+v.lang+')</option>';}).join('');
    var saved=get('cc_voicename',''), k=voices.findIndex(function(v){return v.name===saved;});
    if(k>=0){sel.value=String(k); voice=voices[k];} else voice=voices[0]||null; }
  if(window.speechSynthesis){speechSynthesis.onvoiceschanged=pick; setTimeout(pick,0);} else {$('cc-vs').style.display='none';}
  $('cc-vs').addEventListener('change',function(){ voice=this.value===''?(voices[0]||null):voices[+this.value]; put('cc_voicename',this.value===''?'':voice.name); unlock(); speak('Namaste! Main Chhoti hun. Kaisi lag rahi hai meri awaaz?'); });
  function unlock(){ if(st.unlocked)return; st.unlocked=true; }
  function mouthAnim(on){ st.talking=on; av.classList.toggle('talk',on); if(!on)mouth.setAttribute('ry','2.2'); }
  setInterval(function(){ if(st.talking)mouth.setAttribute('ry',String(2+Math.random()*5)); },110);
  function clean(t){ return String(t).replace(/\[\[go:[^\]]*\]\]/g,'').replace(/```[\s\S]*?```/g,' code ').replace(/https?:\/\/\S+/g,' link ').replace(/\.{2,}/g,'.').replace(/[`*#_>|]+/g,' ').replace(/\s-\s/g,', ').replace(/\s+/g,' ').trim().slice(0,420); }
  function phrases(t){ var m=clean(t).match(/[^.!?\u0964,;:]+[.!?\u0964,;:]*/g)||[], out=[], buf=''; // merge very short bits so the voice does not stutter
    m.forEach(function(s){ buf=(buf+' '+s).trim(); if(buf.replace(/[^\w\u0900-\u097F]/g,'').length>=14||/[.!?\u0964]$/.test(buf)){out.push(buf);buf='';} }); if(buf)out.push(buf); return out; }
  function speak(t){ if(!st.voice||!st.unlocked||!window.speechSynthesis||!t)return; var my=++tok, q=phrases(t), i=0;
    try{ speechSynthesis.cancel(); }catch(e){}
    (function next(){ if(my!==tok)return; if(i>=q.length){mouthAnim(false);return;}
      var s=q[i], end=s.slice(-1), u=new SpeechSynthesisUtterance(s), wave=Math.sin((i+1)*1.35);
      var pitch=1.5+0.16*wave+(end==='?'?0.22:end==='!'?0.12:end===','||end===';'?0.05:-0.06), rate=0.94+0.03*Math.cos(i*1.1)+(end==='!'?0.04:end==='?'?-0.03:0);
      u.lang=voice?voice.lang:'hi-IN'; if(voice)u.voice=voice; u.pitch=Math.max(0.5,Math.min(2,pitch)); u.rate=Math.max(0.7,Math.min(1.2,rate)); u.volume=1;
      u.onstart=function(){mouthAnim(true);}; var done=false, go=function(){ if(done)return; done=true; i++; if(my!==tok)return; mouthAnim(false); setTimeout(next,/[.!?\u0964]$/.test(s)?280:120); };
      u.onend=go; u.onerror=go; try{speechSynthesis.speak(u);}catch(e){mouthAnim(false);} })(); }
  // ---- voice in ----
  var SR=window.SpeechRecognition||window.webkitSpeechRecognition, rec=null, listening=false;
  if(!SR){ $('cc-mic').style.display='none'; }
  $('cc-mic').addEventListener('click',function(){ if(!SR)return; unlock(); if(listening){rec&&rec.stop();return;}
    try{ rec=new SR(); rec.lang='hi-IN'; rec.interimResults=false; rec.maxAlternatives=1; }catch(e){return;}
    rec.onstart=function(){listening=true; $('cc-mic').classList.add('on'); if(window.speechSynthesis)speechSynthesis.cancel();};
    rec.onend=function(){listening=false; $('cc-mic').classList.remove('on');};
    rec.onerror=function(e){add('ai','Mic nahi chal raha ('+(e&&e.error||'error')+'). Browser mein mic ki permission do, ya Chrome/Edge use karo.');};
    rec.onresult=function(e){ var t=e.results[0][0].transcript; $('cc-in').value=t; send(t); };
    try{rec.start();}catch(e){} });
  // ---- talking to the AI (read-only) ----
  function go(path){ if(GO_OK.test(path)&&location.pathname!==path){ setTimeout(function(){location.href=path;},900); } }
  function intent(q){ var m=q.toLowerCase().match(/(?:kholo|khol do|open|dikhao|le chalo|jao|chalo)\b/); if(!m)return null; var low=q.toLowerCase();
    for(var k in PAGES){ if(low.indexOf(k)>=0)return PAGES[k]; } return null; }
  function send(q){ q=(q||'').trim(); if(!q)return; $('cc-in').value=''; add('me',q); var to=intent(q);
    if(to){ var r='Theek hai, kholti hun.'; add('ai',r); speak(r); go(to); return; }
    var w=add('ai','Soch rahi hun...'); w.classList.add('wait');
    fetch('/assistant/ask',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({message:q,history:st.hist.slice(-8)})})
     .then(function(r){return r.json();}).then(function(d){ var a=(d&&d.answer)||'Samajh nahi aaya, dobara poochho.'; var m=a.match(/\[\[go:([^\]]+)\]\]/); w.classList.remove('wait'); w.textContent=a.replace(/\s*\[\[go:[^\]]*\]\]/g,'');
       st.hist.push({role:'user',content:q},{role:'assistant',content:w.textContent}); speak(w.textContent); if(m){var b=document.createElement('a'); b.href=m[1].trim(); b.textContent='Kholo: '+m[1].trim(); b.className='cc-go'; if(GO_OK.test(m[1].trim()))w.appendChild(document.createElement('br')),w.appendChild(b);} log.scrollTop=log.scrollHeight; })
     .catch(function(){w.classList.remove('wait'); w.textContent='Net ya server se baat nahi ho pa rahi. Thodi der baad try karo.';}); }
  $('cc-form').addEventListener('submit',function(e){e.preventDefault(); send($('cc-in').value);});
  // ---- speaking up by itself: new problems (real alerts/errors) and, now and then, a true line about the shop ----
  function poll(){ if(document.hidden)return;
    fetch('/ai/notices?after='+st.after,{credentials:'same-origin',cache:'no-store'}).then(function(r){return r.ok?r.json():null;}).then(function(d){ if(!d)return; st.after=d.latest;
      d.notices.slice(-2).forEach(function(n){ bubble(n.say,n.level,12000); if(st.open)add('ai',n.text); speak(n.say); }); }).catch(function(){}); }
  function chat(){ if(!st.chat||document.hidden||st.open||Date.now()-st.lastAct<180000)return; st.lastAct=Date.now();
    fetch('/ai/chatter',{credentials:'same-origin',cache:'no-store'}).then(function(r){return r.ok?r.json():null;}).then(function(d){ if(d&&d.line){bubble(d.line,'info',14000); speak(d.line);} }).catch(function(){}); }
  poll(); setInterval(poll,20000); setInterval(chat,60000);
  window.addEventListener('beforeunload',function(){ if(window.speechSynthesis)speechSynthesis.cancel(); });
})();
