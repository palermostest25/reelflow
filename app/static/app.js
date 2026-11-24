'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths={camera:'M3 7h3l2-3h8l2 3h3v13H3zM12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8',list:'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',film:'M4 3h16v18H4zM4 7h16M4 17h16M8 3v4M16 3v4M8 17v4M16 17v4',check:'m5 12 4 4L19 6',chart:'M4 20V4M4 20h17M8 16v-4M13 16V8M18 16V5',activity:'M2 12h4l3-8 5 16 3-8h5',settings:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M10 2h4l1 3 3 1 3 2-1 4 1 4-3 2-3 1-1 3h-4l-1-3-3-1-3-2 1-4-1-4 3-2 3-1z',pause:'M8 5v14M16 5v14',play:'m8 4 12 8-12 8z',plus:'M12 4v16M4 12h16',upload:'M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5',arrow:'M4 12h16m-6-6 6 6-6 6',refresh:'M20 7a9 9 0 1 0 1 8M20 2v5h-5',x:'M6 6l12 12M18 6 6 18',edit:'m4 16 12-12 4 4L8 20H4zM13 7l4 4',instagram:'M7 3h10a4 4 0 0 1 4 4v10a4 4 0 0 1-4 4H7a4 4 0 0 1-4-4V7a4 4 0 0 1 4-4M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M17 7h.01',clock:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M12 7v5l3 2',spark:'m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z',external:'M14 3h7v7M21 3 10 14M10 3H3v18h18v-7'};
const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name]||paths.film}"/></svg>`;
function icons(){document.querySelectorAll('[data-icon]').forEach(el=>el.innerHTML=icon(el.dataset.icon));}
let state, currentPage='', filter='all', libraryView='all', setsOpen=false, offset=0, toastTimer, toastTarget, records=[], renderVersion=0, queueView='schedule', scheduleDraft=null;
const selectedClips=new Map();
let guidanceDraft=null, guidanceOpen=false, batchGuidance='', captionBusy=false, captionProgress='', captionFailed=false;
let youtubeData, youtubeView='queue', youtubeOffset=0, youtubeFocus=null;
const titles={queue:'Instagram',youtube:'YouTube',multicam:'Multicam',library:'Footage library',history:'Published',insights:'Audience & voice',activity:'Activity log',settings:'Settings'};
async function api(path, method='GET', data){
 const headers={}; if(state?.csrf)headers['X-CSRF-Token']=state.csrf;
 if(data && !(data instanceof FormData))headers['Content-Type']='application/json';
 const response=await fetch(path,{method,headers,body:data?(data instanceof FormData?data:JSON.stringify(data)):undefined});
 let body;try{body=await response.json();}catch{body={detail:response.status===524||response.status===504?`The request timed out on the way to Reelflow (HTTP ${response.status}).`:`The server returned an unexpected response (HTTP ${response.status}).`};}
 if(!response.ok){if(response.status===401&&path!='/api/login')showLogin();throw new Error(typeof body.detail==='string'?body.detail:'Check the values and try again.');}return body;
}
async function rewriteCaption(id,instructions){
 let result=await api('/api/clips/'+encodeURIComponent(id)+'/regenerate','POST',instructions?{instructions}:undefined);
 // Slow models finish in the background so a proxy timeout cannot cut the request off.
 for(let waited=0;result.pending;waited+=3){
  if(waited>600)throw new Error('The caption model is taking longer than 10 minutes. Check Activity, then retry.');
  await new Promise(resolve=>setTimeout(resolve,3000));
  result=await api('/api/rewrites/'+encodeURIComponent(result.token));
 }
 if(typeof result.caption!=='string')throw new Error('The server did not return a caption.');
 return result.caption;
}
function toast(message,error=false){const el=$('#editor').open?$('#editor-feedback'):$('#toast');if(toastTarget&&toastTarget!==el)toastTarget.hidden=true;toastTarget=el;el.textContent=message;el.className=error?'error':'';el.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.hidden=true,error?9000:5000);}
function date(ts,options={}){return ts?new Intl.DateTimeFormat('en-AU',{timeZone:state?.settings.timezone||'Australia/Perth',...options}).format(new Date(ts*1000)):'Not scheduled';}
function day(ts){return date(ts,{weekday:'short',day:'numeric',month:'short'});}
function clock(ts){return date(ts,{hour:'2-digit',minute:'2-digit',hour12:false});}
const badge=(text,cls='')=>`<span class="badge ${cls}">${esc(text)}</span>`;
const laneTag=lane=>`<span class="lane ${lane}">${lane==='main'?'Grid Reel':'Trial Reel'}</span>`;
const header=(title,desc,actions='')=>`<div class="page-heading"><div><h1>${title}</h1><p>${desc}</p></div><div class="heading-actions">${actions}</div></div>`;
const uploadButton=`<button class="primary" data-action="upload">${icon('plus')}Add footage</button>`;
const empty=(title,body,action='',ico='film')=>`<div class="empty-state">${icon(ico)}<h2>${title}</h2><p>${body}</p>${action}</div>`;
const pager=length=>`<div class="pager"><button class="button small" data-action="previous" ${offset===0?'disabled':''}>Previous</button><span>${offset+1}–${offset+length}</span><button class="button small" data-action="next" ${length<50?'disabled':''}>Next</button></div>`;
function showLogin(){$('#shell').hidden=true;$('#login').hidden=false;$('#login input').focus();}
async function loadState(){state=await api('/api/state');$('#brand-label').textContent=state.settings.brand_name;$('.avatar').textContent=state.settings.brand_name.slice(0,1).toUpperCase();const accountLabel=$('#account-label');accountLabel.textContent=state.account?'@'+state.account.username:'Your workspace';accountLabel.title=accountLabel.textContent;$('#mode-badge').textContent=state.demo?'Local preview':state.settings.dry_run?'Dry run':'Live publishing';$('#mode-badge').className='badge '+(state.settings.dry_run?'':'green');$('#pause-button').innerHTML=icon(state.settings.paused?'play':'pause')+(state.settings.paused?'Resume':'Pause');$('#timezone-label').textContent=state.settings.timezone;$('#queue-count').textContent=state.main_remaining;$('#nav-multicam').hidden=!state.settings.multicam_enabled;const alive=state.heartbeat&&Date.now()/1000-Number(state.heartbeat)<180;$('#worker-label').textContent=alive?'Worker is running':'Worker is offline';$('.worker-indicator').className='worker-indicator'+(alive?' online':'');}
async function refresh(){await loadState();if(!$('#editor').open)await render();}
async function render(){
 const version=++renderVersion;const page=location.hash.slice(1)||'queue';currentPage=titles[page]?page:'queue';
 document.querySelectorAll('nav a').forEach(a=>{a.classList.toggle('active',a.dataset.page===currentPage);if(a.dataset.page===currentPage)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
 $('#breadcrumb').textContent=titles[currentPage];
 const youtubePage=currentPage==='youtube';
 $('#mode-badge').hidden=youtubePage;$('#pause-button').hidden=youtubePage;document.title=titles[currentPage]+' — Reelflow';
 let html='';const s=state.settings;
 if(currentPage==='queue'){
  let calendar;
  if(queueView==='schedule'){
   calendar=await api('/api/schedule');
   records=calendar.days.flatMap(d=>[...d.main,...d.trial]);
  }else records=await api(`/api/jobs?lane=${filter==='all'?'':filter}&offset=${offset}&limit=50`);
  html=header('Instagram schedule','See what goes out each day, before you let it run.',uploadButton);
  if(state.demo)html+=`<div class="notice"><p>Local preview. No Instagram account is connected and nothing can publish here.</p></div>`;
  else if(s.paused)html+=`<div class="notice"><p><strong>Publishing is paused.</strong> Build and review your schedule here, then use Resume when you’re ready.${s.dry_run?' Dry run is also on.':''}</p></div>`;
  else if(s.dry_run)html+=`<div class="notice"><p><strong>Dry run is on.</strong> Footage and captions are prepared, but nothing is published.</p><a href="#settings">Review setup →</a></div>`;
  html+=scheduleControls();
  if(s.trials_blocked)html+=`<div class="notice trial-blocked"><p><strong>Trial Reels are paused.</strong> Instagram refused to post a Trial for this account, so Reelflow stopped sending them. Regular Reels keep posting.</p><p class="help">${esc(s.trials_blocked)}</p><p class="help">Check in the Instagram app that this account can share a Reel as a Trial. Then try again.</p><button type="button" class="button small" data-action="unblock-trials">Try Trial Reels again</button></div>`;
  html+=`<div class="run-strip"><div><strong>${s.paused?'Posting is paused':s.dry_run?'Preparing your queue':'Autopilot is running'}</strong><span>${s.require_approval?'Approval required for new posts':'New posts are automatically approved'}</span></div><div><strong>${s.main_per_day?`${s.main_per_day} grid Reel${s.main_per_day===1?'':'s'} / day`:'Grid posts are off'}</strong><span>${state.interval>1?`Conservation: up to ${Math.min(s.main_per_day,s.conserve_main_per_day)} every ${state.interval} days`:'Shared to your grid, Reels and feed'}</span></div><div><strong>${s.trials_enabled&&s.trials_per_day?`Up to ${s.trials_per_day} Trials / day`:'Trials are off'}</strong><span>${state.interval>1&&s.trials_enabled?`Conservation: up to ${Math.min(s.trials_per_day,s.conserve_trials_per_day)} every ${state.interval} days`:'Non-followers first · off the grid'}</span></div><div><strong>${state.account?'@'+esc(state.account.username):'Instagram not connected'}</strong><span>${esc(s.timezone)} · ${s.learn_timing?'Learning posting times':'Your posting times'}</span></div></div>`;
  html+=`<div class="two-column"><section><div class="section-head"><h2>${queueView==='schedule'?'The daily schedule':'The running order'}</h2><div class="tabs" role="group" aria-label="Queue view"><button class="tab ${queueView==='schedule'?'active':''}" data-queue-view="schedule" aria-pressed="${queueView==='schedule'}">By day</button><button class="tab ${queueView==='list'?'active':''}" data-queue-view="list" aria-pressed="${queueView==='list'}">Post list</button></div></div>`;
  if(calendar)html+=scheduleView(calendar);
  else{
   html+=`<div class="list-filters tabs" role="group" aria-label="Filter destination">${['all','main','trial'].map(x=>`<button class="tab ${filter===x?'active':''}" data-filter="${x}">${{all:'All Reels',main:'Grid',trial:'Trials'}[x]}</button>`).join('')}</div>`;
   html+=records.length?records.map(jobRow).join('')+pager(records.length):setupStages();
  }
  html+=`<p class="queue-note">New footage mixes into flexible slots. Pinned times and edited captions stay in place. Trials stay Trials; automatic sharing to followers is off.</p></section><aside class="operating-summary">${queueView==='schedule'?scheduleTray():''}<section class="side-section"><h3>Footage runway</h3><div class="runway-figure">${state.runway_days}<span>days, approximately</span></div><p>${s.main_per_day?state.main_remaining:state.trial_remaining} unposted ${s.main_per_day?'grid':'Trial'} Reels. ${state.interval>1?'The conservation pace is active.':'At your configured posting pace.'}</p><div class="stock-track">${Array.from({length:14},(_,i)=>`<b class="${i<state.runway_days?'':'empty'}"></b>`).join('')}</div><a href="#settings" class="button small">Change the posting rhythm ${icon('arrow')}</a></section><section class="side-section"><h3>Two different audiences</h3><p><strong>Trials:</strong> Instagram tests these with non-followers first. They stay off your profile grid unless you share them to everyone in Instagram.</p><p class="help"><strong>Grid:</strong> regular Reels shared to your profile and feed, visible to followers and discovery audiences.</p><p class="help">A follower can still encounter a Trial through a direct share or an audio page.</p></section><section class="side-section"><h3>Make the captions yours</h3><p>Set tone, phrases to avoid, hashtags and other instructions for your caption model.</p><a href="#settings" class="button small">Edit caption instructions ${icon('arrow')}</a></section></aside></div>`;
 }
 if(currentPage==='library'){
  const [clips,sets]=await Promise.all([api(`/api/clips?offset=${offset}&limit=50&view=${libraryView}`),api('/api/batches')]);
  if(version!==renderVersion)return;
  records=clips;html=header('Footage library','Drop footage in your watched folder, or upload it here. We’ll take it from there.',`<button class="button" data-action="scan">${icon('refresh')}Scan folder</button>${uploadButton}`);
  html+=`<div class="notice"><p>Watching <code>${esc(state.footage_path)}</code>. Files are deduplicated by content. Originals in this folder are never modified.</p></div>`;
  html+=`<div class="rebuild-tools"><button type="button" class="button" data-action="rebuild-clips">${icon('refresh')}Rebuild all videos</button><p class="help">${state.rebuilding?`${state.rebuilding} video${state.rebuilding===1?'':'s'} waiting to rebuild or being rebuilt. Check Activity for results.`:'Recreate the exports from the originals. Captions and posting history stay in place.'}</p></div>`;
  html+=captionTools();
  html+=setsPanel(sets);
  html+=`<div class="list-filters tabs" role="group" aria-label="Filter footage">${[['all','All footage'],['unposted','Not posted yet'],['posted','Already posted'],['same_song','Same song'],['dated','Dated & windows'],['other','Talking & extras']].map(([x,label])=>`<button class="tab ${libraryView===x?'active':''}" data-library-view="${x}" aria-pressed="${libraryView===x}">${label}</button>`).join('')}</div>`;
  html+=records.length?`<div class="footage-grid">${records.map(clipCard).join('')}</div>`+pager(records.length):libraryView!=='all'?`<p class="empty-filter">${{unposted:'Every clip on this page has been posted at least once.',posted:'Nothing from the library has been posted yet.',same_song:'No repeated songs found yet. Reelflow listens to each prepared clip; matches appear here as they are found.',dated:'No clips have a date or posting window. Open a clip to set one.',other:'No talking, behind-the-scenes or promo clips yet. Choose the type when you upload, or open a clip to change it.'}[libraryView]}</p>`:empty('Start with the good stuff.','Set your watched folder in Docker, then drop in your clips. Or upload a few here to try the workflow.',uploadButton);
  html+=`<div id="drop-zone" class="drop-zone">${icon('upload')}<strong>Drop footage here</strong><p>MP4, MOV, M4V, MKV or WebM · up to 5 GB per input</p><button class="button small" data-action="upload">Browse files</button><p id="upload-progress" role="status"></p></div>`;
 }
 if(currentPage==='history'){
  records=await api(`/api/jobs?status=${filter==='rejected'?'rejected':'published'}&offset=${offset}&limit=50`);html=header('Published Reels','A lasting record of what went out, when, and where.',`<div class="tabs"><button class="tab ${filter!=='rejected'?'active':''}" data-filter="all">Published</button><button class="tab ${filter==='rejected'?'active':''}" data-filter="rejected">Rejected</button></div>`);
  html+=records.length?records.map(jobRow).join('')+pager(records.length):empty(filter==='rejected'?'Nothing rejected.':'Your first Reel starts here.',filter==='rejected'?'Clips you reject from the queue will stay here for reference or restoration.':'Once a Reel publishes, its caption, destination and posting time will appear here.','<a class="button" href="#queue">See upcoming Reels</a>','check');
 }
 if(currentPage==='activity'){
  records=await api(`/api/events?offset=${offset}&limit=50`);html=header('Activity log','Intake, captions, queue changes, notifications, and publishing outcomes.');
  html+=records.length?records.map(r=>`<div class="log-row"><time>${day(r.at)}<br>${clock(r.at)}</time><span>${badge(r.level,r.level==='error'?'rust':r.level==='warning'?'rust':'')}</span><p>${esc(r.message)}${r.job_id?' · Post #'+r.job_id:''}</p></div>`).join('')+pager(records.length):empty('Quiet for now.','Your activity log fills automatically as footage arrives and the worker runs.','','activity');
 }
 if(currentPage==='insights'){
  const data=await api('/api/insights');records=data.posts;html=header('Audience & captions','Your account’s voice and audience, learned from actual posts.');
  html+=`<div class="insights-summary">${icon('spark')}<div><strong>${records.length?records.length+' recent posts in your voice reference':'Your account sets the tone.'}</strong><p>${esc(data.note)} Captions use recent posts, available engagement, three silent frames and your notes. No invented crowd, track or venue claims.</p></div>${badge(state.metric_samples+' mature samples',state.metric_samples?'green':'')}</div>`;
  html+=records.length?`<div class="table-wrap"><table><thead><tr><th>Post / caption</th><th>Date</th><th>Views</th><th>Reach</th><th>Likes</th><th>Saves</th><th>Shares</th></tr></thead><tbody>${records.map(r=>`<tr><td><span class="caption-cell">${esc(r.caption||'No caption')}${r.permalink?` <a href="${safeLink(r.permalink)}" target="_blank" rel="noopener noreferrer" aria-label="Open Instagram post">↗</a>`:''}</span>${r.metrics_error?'<span class="help">Insights unavailable; check permissions.</span>':''}</td><td>${day(r.timestamp)}</td>${['views','reach','likes','saves','shares'].map(k=>`<td>${r[k]===null?'—':Number(r[k]).toLocaleString()}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`:empty('Let your past posts do the talking.','Connect Instagram in Settings. The next worker pass imports recent captions and available performance, so your next post sounds like you.','<a class="button" href="#settings">Connect Instagram</a>','instagram');
 }
 if(youtubePage){
  const data=await api(`/api/youtube?view=${youtubeView}&offset=${youtubeOffset}`);
  if(version!==renderVersion)return;
  youtubeData=data;
  html=youtubeWorkspace(data);
 }
 if(currentPage==='settings')html=settingsView();
 if(currentPage==='multicam'){
  if(!s.multicam_enabled)html=header('Multicam editor','Turn it on in Settings to sync camera angles to a mix and cut a full edit plus social clips.')+empty('The multicam editor is off.','Turn it on under Settings → Multicam editor.',`<a href="#settings" class="button">Open Settings ${icon('arrow')}</a>`,'camera');
  else{const data=await api('/api/multicam');if(version!==renderVersion)return;mcData=data;html=multicamView(data);}
 }
 if(version!==renderVersion)return;
 $('#content').innerHTML=html;icons();bindForms();
 if(youtubePage){
  bindYoutube();
  if(youtubeFocus){const id=youtubeFocus;youtubeFocus=null;await openYoutubeJob(id);}
 }
}
const youtubeOptions={
 visibility:[['private','Private'],['unlisted','Unlisted'],['public','Public']],
 category:[['22','People & Blogs'],['10','Music'],['24','Entertainment']],
};
const youtubeStates={draft:'Needs review',queued:'Scheduled',uploading:'Uploading',uploaded:'Uploaded',skipped:'Skipped',failed:'Needs attention',uncertain:'Check YouTube Studio',invalid:'File needs attention'};
const youtubeKind=kind=>kind==='short'?'Short':'Full set';
function youtubeSelect(name,label,options,current){
 return `<label class="field">${label}<select name="${name}">${options.map(([value,text])=>`<option value="${value}" ${current===value?'selected':''}>${text}</option>`).join('')}</select></label>`;
}
function youtubeToggle(name,label,checked,help=''){
 return `<label class="youtube-toggle"><span><strong>${label}</strong>${help?`<small>${help}</small>`:''}</span><input type="checkbox" name="${name}" ${checked?'checked':''}></label>`;
}
function youtubeNumber(name,label,value,max){
 return `<label class="field">${label}<input type="number" name="${name}" min="0" max="${max}" value="${value}" required></label>`;
}
function youtubeSetup(data){
 const account=data.account;
 return `<section class="youtube-panel youtube-channel" aria-labelledby="youtube-channel-title">
   <div class="youtube-panel-heading"><div class="youtube-platform-icon">${icon('play')}</div><div><h2 id="youtube-channel-title">${account?esc(account.title):'Connect YouTube'}</h2><p>${data.connected?'Uploads go to this channel.':account?'Reconnect to resume uploads.':'Link the channel that should receive your uploads.'}</p></div>
   ${badge(data.connected?'Connected':'Setup needed',data.connected?'green':'')}</div>
   ${account?.error?`<p class="editor-error">${esc(account.error)}</p>`:''}
   <details id="youtube-connection" ${!data.connected?'open':''}><summary>${data.connected?'Connection settings':'Set up Google sign-in'}</summary>
    <ol class="youtube-steps">
     <li>In <a href="https://console.cloud.google.com/apis/library/youtube.googleapis.com" target="_blank" rel="noopener noreferrer">Google Cloud</a>, select or create a project and enable <strong>YouTube Data API v3</strong>.</li>
     <li>Set up the <a href="https://console.cloud.google.com/auth/overview" target="_blank" rel="noopener noreferrer">Google Auth Platform</a>. If the app is in Testing, add your Google account under <strong>Audience → Test users</strong>.</li>
     <li>Under <strong>Clients</strong>, create a <strong>Web application</strong> and add this authorized redirect URI.</li>
    </ol>
    <label class="field">Authorized redirect URI<input id="youtube-callback" value="${esc(data.callback)}" readonly></label>
    <button type="button" class="button small" data-yt-control="copy">Copy redirect URI</button>
    <form id="youtube-credentials">
     <label class="field">Client ID<input name="client_id" value="${esc(data.client_id)}" required maxlength="300" autocomplete="off" placeholder="….apps.googleusercontent.com"></label>
     <label class="field">Client secret<input name="client_secret" type="password" maxlength="1000" autocomplete="new-password" ${data.client_id?'':'required'} placeholder="${data.client_id?'Leave blank to keep the saved secret':'Paste the secret from Google Cloud'}"></label>
     <div class="inline-actions"><button class="primary" type="submit">${data.connected?'Reconnect Google':'Save & connect Google'} ${icon('arrow')}</button>
     ${data.connected?`<button class="text-button" type="button" data-yt-control="disconnect">Disconnect</button>`:''}</div>
    </form>
   </details>
  </section>`;
}
function youtubePreferences(data){
 const p=data.preferences;
 return `<section class="youtube-panel" aria-labelledby="youtube-preferences-title">
 <details id="youtube-preferences"><summary id="youtube-preferences-title">Automation & defaults</summary>
 <form id="youtube-preferences-form">
  ${youtubeToggle('mirror_grid','Post every grid Reel as a Short too',p.mirror_grid,'Each Instagram grid Reel also goes out as a YouTube Short at the same time, with its caption. Move the Reel and the Short follows. Vertical and square clips up to 3 minutes only.')}
  ${youtubeToggle('auto_shorts','Create Short drafts automatically',p.auto_shorts,'Adds eligible square and vertical clips from the Footage library, including existing clips.')}
  ${youtubeToggle('auto_approve','Schedule new drafts automatically',p.auto_approve,'Applies to new folder sets and automatically added Shorts. Existing drafts still need review.')}
  <h3>Upload schedule</h3>
  <p class="help">Times use ${esc(data.timezone)}. Set a frequency to 0 to stop automatic uploads of that type.</p>
  <div class="form-row">${youtubeNumber('shorts_per_day','Shorts per day',p.shorts_per_day,10)}${youtubeNumber('short_hour','First Short hour',p.short_hour,23)}</div>
  <div class="form-row">${youtubeNumber('sets_every_days','Days between sets',p.sets_every_days,90)}${youtubeNumber('set_hour','Set upload hour',p.set_hour,23)}</div>
  <h3>Defaults for new drafts</h3>
  <div class="form-row">${youtubeSelect('visibility','Visibility',youtubeOptions.visibility,p.visibility)}${youtubeSelect('category','Category',youtubeOptions.category,p.category)}</div>
  ${youtubeToggle('made_for_kids','Made for kids',p.made_for_kids)}
  <label class="field">Description footer<textarea name="description_footer" rows="3" maxlength="2000" placeholder="Booking email, links or credits">${esc(p.description_footer)}</textarea></label>
  <button class="primary" type="submit">Save preferences</button>
 </form></details></section>`;
}
function youtubeQueueRow(job){
 const when=job.uploaded_at||job.scheduled_at;
 const visibility=job.actual_visibility||job.visibility;
 const status=job.status==='queued'&&!job.scheduled_at?'Awaiting a slot':job.manual&&job.status==='queued'?'Upload requested':youtubeStates[job.status];
 const percent=job.total_bytes?Math.round(100*job.uploaded_bytes/job.total_bytes):0;
 return `<article class="youtube-row">
  <div class="youtube-format">${icon(job.kind==='short'?'play':'film')}<span>${youtubeKind(job.kind)}</span></div>
  <div class="youtube-row-title"><h3>${esc(job.title)}</h3><p title="${esc(job.source_name)}">${esc(job.source_name)}${job.duration?` · ${Math.floor(job.duration/60)}:${String(Math.floor(job.duration)%60).padStart(2,'0')}`:''}</p>${job.status==='uploading'&&job.total_bytes?`<progress max="100" value="${percent}" aria-label="Upload progress"></progress>`:''}</div>
  <div class="youtube-row-time">${when?`<strong>${day(when)}</strong><span>${clock(when)}${job.pinned?' · Pinned':''}</span>`:'<span>Unscheduled</span>'}</div>
  <div class="youtube-row-state">${badge(status,job.status==='uploaded'?'green':job.error?'rust':'')}<span>${esc(visibility)}</span></div>
  <button type="button" class="button small" data-youtube-job="${job.id}" aria-label="Review ${esc(job.title)}">${job.status==='uploaded'?'Details':'Review'}</button>
 </article>`;
}
function youtubeWorkspace(data){
 const controls=`<button class="button" type="button" data-yt-control="scan">${icon('refresh')}Scan sets</button><button class="primary" type="button" data-yt-control="upload">${icon('plus')}Add full set</button>`;
 const tabs=[['queue','All'],['drafts','Review'],['scheduled','Scheduled'],['uploaded','Uploaded'],['skipped','Skipped']];
 const emptyCopy={
  queue:['No uploads in the queue','Add a finished set or open a square or vertical clip in the Footage library to create a Short.'],
  drafts:['Nothing to review','New drafts and uploads that need attention will appear here.'],
  scheduled:['No scheduled uploads','Approve a draft to give it a slot.'],
  uploaded:['No uploads yet','Finished uploads will appear here with their visibility and a link to YouTube Studio.'],
  skipped:['No skipped uploads','You can restore skipped drafts when you’re ready.'],
 }[youtubeView];
 return header('YouTube','Schedule Shorts and finished 20–30 minute sets.',controls)+`
  <div class="youtube-status-strip"><div><span class="worker-indicator ${data.connected&&!data.preferences.paused?'online':''}"></span><div>
    <strong>${data.preferences.paused?'Automatic uploads paused':data.connected?'Automatic uploads enabled':'Waiting for channel connection'}</strong>
    <p>${data.preferences.auto_approve?'New automatic drafts are scheduled.':'Review new drafts before scheduling.'} ${data.queue_count} in queue.</p>
  </div></div><button type="button" class="button small" data-yt-control="${data.connected?'pause':'setup'}">${!data.connected?'Connect channel':data.preferences.paused?'Resume automatic uploads':'Pause automatic uploads'}</button></div>
  <div class="youtube-layout">
   <section class="youtube-queue" aria-labelledby="youtube-queue-title"><div class="section-head"><h2 id="youtube-queue-title">Upload queue</h2>
    <div class="tabs" role="group" aria-label="YouTube queue view">${tabs.map(([key,text])=>`<button type="button" class="tab ${youtubeView===key?'active':''}" data-youtube-view="${key}" aria-pressed="${youtubeView===key}">${text}</button>`).join('')}</div></div>
    ${data.jobs.length?data.jobs.map(youtubeQueueRow).join(''):empty(emptyCopy[0],emptyCopy[1],youtubeView==='queue'?'<a class="button" href="#library">Open footage library</a>':'','film')}
    ${youtubeOffset||data.more?`<div class="pager"><button class="button small" data-yt-control="previous" ${youtubeOffset?'':'disabled'}>Previous</button><span>${youtubeOffset+1}–${youtubeOffset+data.jobs.length}</span><button class="button small" data-yt-control="next" ${data.more?'':'disabled'}>Next</button></div>`:''}
   </section>
   <aside aria-label="YouTube setup">
    ${youtubeSetup(data)}
    <section class="youtube-panel youtube-folder"><h2>Watched set folder</h2><p>Drop a finished 20–30 minute video here. Reelflow waits for the copy to finish and adds it to the queue.</p><code>${esc(data.folder)}</code><p class="help">For large files, use this folder or open Reelflow directly on your network. Your tunnel may limit browser uploads.</p></section>
    ${youtubePreferences(data)}
   </aside>
  </div>`;
}
function youtubeEditForm(job){
 const timezone=Intl.DateTimeFormat().resolvedOptions().timeZone;
 const localTime=job.pinned&&job.scheduled_at?new Date((job.scheduled_at*1000)-new Date(job.scheduled_at*1000).getTimezoneOffset()*60000).toISOString().slice(0,16):'';
 return `<form id="youtube-edit">
 <label class="field">Title<input name="title" value="${esc(job.title)}" required maxlength="100"></label>
 <label class="field">Description<textarea name="description" rows="4" maxlength="5000">${esc(job.description)}</textarea></label>
 <div class="form-row">${youtubeSelect('visibility','Visibility',youtubeOptions.visibility,job.visibility)}${youtubeSelect('category','Category',youtubeOptions.category,job.category)}</div>
 ${youtubeToggle('made_for_kids','Made for kids',job.made_for_kids)}
 <label class="field">Choose upload time <span>Optional · ${esc(timezone)}. Leave blank to use the automatic schedule.</span><input type="datetime-local" name="scheduled_at" value="${localTime}"></label>
 <button class="primary" type="submit">Save details</button>
 </form>`;
}
function youtubeJobActions(job){
 const button=(action,label,style='button')=>`<button type="button" class="${style}" data-yt-job-action="${action}">${label}</button>`;
 if(job.status==='draft'||job.status==='queued'){
  return `<div class="inline-actions">${job.status==='draft'?button('approve','Approve for schedule'):button('hold','Hold for review')}
   ${youtubeData.connected?button('upload_now','Upload now') : ''}${button('skip','Skip')}</div>`;
 }
 if(job.status==='invalid')return button('skip','Dismiss');
 if(job.status==='skipped'&&!job.error)return button('restore','Restore draft');
 if(job.status==='failed'||job.status==='uncertain'){
  return `${job.status==='failed'?button('retry','Retry upload'):''}
   ${job.saved_session||job.status==='uncertain'?`<details class="youtube-recovery"><summary>Resolve in YouTube Studio</summary>
    <p class="help">Open YouTube Studio and check whether the video arrived before resetting the upload.</p>
    <a class="button small" href="https://studio.youtube.com/" target="_blank" rel="noopener noreferrer">Open Studio ${icon('external')}</a>
    <label class="field">Video ID if found<input id="youtube-video-id" maxlength="11" pattern="[A-Za-z0-9_-]{11}"></label>
    <div class="inline-actions">${button('confirm_uploaded','Mark as uploaded')}${button('confirm_absent','I checked. Create a new draft.')}</div></details>`:''}`;
 }
 return '';
}
async function openYoutubeJob(id){
 const job=await api('/api/youtube/jobs/'+Number(id));
 if(currentPage!=='youtube')return;
 const dialog=$('#editor');
 $('#editor-title').textContent=youtubeKind(job.kind)+' details';
 const editable=['draft','queued','failed'].includes(job.status)&&!job.saved_session;
 const link=job.video_id?`<a class="button" href="https://studio.youtube.com/video/${encodeURIComponent(job.video_id)}/edit" target="_blank" rel="noopener noreferrer">Open in YouTube Studio ${icon('external')}</a>`:'';
 const when=job.uploaded_at||job.scheduled_at;
 $('#editor-body').innerHTML=`${job.status!=='invalid'?`<video class="editor-video" src="/api/youtube/jobs/${job.id}/video" controls playsinline preload="metadata"></video><p id="youtube-preview-error" class="help" hidden>Preview unavailable. <a download href="/api/youtube/jobs/${job.id}/video">Download the video</a>.</p>`:''}
  <p class="editor-name">${esc(job.source_name)}</p>
  <p class="youtube-drawer-state">${badge(youtubeStates[job.status],job.error?'rust':'')} <span>${esc(job.actual_visibility||job.visibility)}${when?` · ${day(when)} ${clock(when)}`:''}</span></p>
  ${job.error?`<p class="editor-error" role="alert">${esc(job.error)}</p>`:''}
  ${editable?youtubeEditForm(job):`<h3>${esc(job.title)}</h3><p class="youtube-description">${esc(job.description)}</p>`}
  <div class="youtube-drawer-actions">${link}${youtubeJobActions(job)}</div>`;
 const video=$('#editor-body video');
 video?.addEventListener('error',()=>{video.hidden=true;$('#youtube-preview-error').hidden=false;});
 const form=$('#youtube-edit');
 form?.addEventListener('submit',async e=>{
  e.preventDefault();
  const el=e.currentTarget.elements, when=el.scheduled_at.value;
  await busy(e.submitter,async()=>{
   await api('/api/youtube/jobs/'+job.id,'PATCH',{title:el.title.value,description:el.description.value,
    visibility:el.visibility.value,category:el.category.value,made_for_kids:el.made_for_kids.checked,
    scheduled_at:when?new Date(when).getTime()/1000:null});
   await render();await openYoutubeJob(job.id);toast('Upload details saved.');
  });
 });
 dialog.querySelectorAll('[data-yt-job-action]').forEach(button=>button.addEventListener('click',()=>busy(button,async()=>{
  const action=button.dataset.ytJobAction;
  if(['approve','upload_now'].includes(action)&&form&&[...form.elements].some(el=>el.name&&(el.type==='checkbox'?el.checked!==el.defaultChecked:el.tagName==='SELECT'?el.value!==[...el.options].find(option=>option.defaultSelected)?.value:el.value!==el.defaultValue))){
   toast('Save your changes before scheduling this upload.',true);return;
  }
  if(action==='upload_now'&&!window.confirm(`Upload “${job.title}” to YouTube as ${job.visibility} now?`))return;
  if(action==='confirm_absent'&&!window.confirm('Confirm you checked YouTube Studio and this video was absent.'))return;
  const video_id=$('#youtube-video-id')?.value.trim()||'';
  if(action==='confirm_uploaded'&&!/^[A-Za-z0-9_-]{11}$/.test(video_id)){toast('Enter the 11-character YouTube video ID.',true);return;}
  await api('/api/youtube/jobs/'+job.id+'/action','POST',{action,video_id});
  dialog.close();await render();toast(action==='upload_now'?'Upload requested. Track progress in the queue.':'Upload queue updated.');
 })));
 if(!dialog.open)dialog.showModal();
}
function bindYoutube(){
 const prefs=$('#youtube-preferences-form');
 prefs.addEventListener('submit',async e=>{
  e.preventDefault();
  const elements=e.currentTarget.elements;
  await busy(e.submitter,async()=>{
   const values={...youtubeData.preferences};
   Object.keys(values).forEach(key=>{
    const el=elements[key];
    if(el)values[key]=typeof values[key]==='boolean'?el.checked:typeof values[key]==='number'?Number(el.value):el.value;
   });
   await api('/api/youtube/preferences','PUT',values);
   await render();$('#youtube-preferences').open=true;toast('YouTube preferences saved.');
  });
 });
 $('#youtube-credentials').addEventListener('submit',async e=>{
  e.preventDefault();
  const el=e.currentTarget.elements;
  await busy(e.submitter,async()=>{
   await api('/api/youtube/credentials','PUT',{client_id:el.client_id.value,client_secret:el.client_secret.value});
   const connection=await api('/api/youtube/connect','POST');
   window.location.assign(connection.url);
  });
 });
 $('#content').querySelectorAll('[data-youtube-job]').forEach(button=>button.addEventListener('click',()=>busy(button,()=>openYoutubeJob(button.dataset.youtubeJob))));
 $('#content').querySelectorAll('[data-youtube-view]').forEach(button=>button.addEventListener('click',()=>{
  youtubeView=button.dataset.youtubeView;youtubeOffset=0;render().catch(e=>toast(e.message,true));
 }));
 $('#content').querySelectorAll('[data-yt-control]').forEach(button=>button.addEventListener('click',()=>busy(button,async()=>{
  switch(button.dataset.ytControl){
  case 'copy': {
   const input=$('#youtube-callback');
   input.select();
   if(navigator.clipboard)await navigator.clipboard.writeText(input.value);
   else if(!document.execCommand('copy'))throw new Error('Copy the selected redirect URI.');
   toast('Redirect URI copied.');break;
  }
  case 'disconnect':
   if(!window.confirm('Disconnect the channel and pause automatic YouTube uploads?'))return;
   await api('/api/youtube/disconnect','POST');await render();toast('YouTube disconnected.');break;
  case 'upload': chooseUpload('youtube');break;
  case 'scan': toast((await api('/api/youtube/scan','POST')).message);break;
  case 'setup':
   $('#youtube-connection').open=true;
   $('#youtube-connection summary').focus();
   break;
  case 'pause': {
   const paused=!youtubeData.preferences.paused;
   if(!paused&&!window.confirm('Resume scheduled YouTube uploads with their saved visibility?'))return;
   await api('/api/youtube/preferences','PUT',{...youtubeData.preferences,paused});
   await render();break;
  }
  case 'next': youtubeOffset+=50;await render();break;
  case 'previous': youtubeOffset=Math.max(0,youtubeOffset-50);await render();break;
  }
 })));
 const error=new URLSearchParams(location.search).get('youtube_error');
 if(error){history.replaceState(null,'',location.pathname+'#youtube');toast(error,true);}
}

function scheduleControls(){const s={...state.settings,...scheduleDraft};return `<form id="schedule-form" class="schedule-controls"><label>Preview days<input name="preview_days" type="number" min="1" max="90" required value="${s.preview_days}"></label><label>Trial Reels / day<input name="trials_per_day" type="number" min="0" max="10" required value="${s.trials_per_day}"></label><label>Grid Reels / day<input name="main_per_day" type="number" min="0" max="10" required value="${s.main_per_day}"></label><button class="primary" type="submit">${icon('clock')}Save & build schedule</button><a href="#settings" class="text-button">Timing & caption options</a><p>Set either daily count to 0 to stop that destination. Building the schedule does not resume publishing.${state.settings.trials_enabled?'':' Trial publishing is also switched off in Settings.'}</p></form>`;}
function schedulePost(j){const off=j.lane==='main'?!state.settings.main_per_day:!state.settings.trials_enabled||!state.settings.trials_per_day;const status=j.status==='published'?'Published':postingNow(j)?'Post now requested':off?'Destination switched off':!j.approved?'Needs approval':({queued:'Ready',retry:'Retry pending',processing:'Preparing to publish',publishing:'Publishing',uncertain:'Check Instagram',failed:'Needs attention'}[j.status]||j.status);const movable=['queued','retry','failed'].includes(j.status)&&!postingNow(j);return `<button type="button" class="schedule-post${movable?' draggable':''}" data-job="${j.id}" data-at="${j.published_at||j.scheduled_at}" ${movable?`data-drag-kind="job" data-drag-id="${j.id}"`:''} aria-label=""Review ${esc(j.name)}, ${j.lane==='main'?'grid':'Trial'} Reel at ${clock(j.published_at||j.scheduled_at)}"><time>${clock(j.published_at||j.scheduled_at)}</time><img src="/api/preview/${j.clip_id}" alt="" loading="lazy"><span class="schedule-post-copy"><strong>${esc(j.name)}</strong><span class="schedule-caption">${esc(j.caption)}</span><span class="schedule-status ${j.error||!j.approved?'attention':''}">${esc(status)}${j.forced?' · Placed by you':j.pinned?' · Time pinned':''}</span>${KIND_SHORT[j.kind]||(j.post_on&&!j.pinned)?`<span class="clip-flags">${KIND_SHORT[j.kind]?`<span class="flag kind">${esc(KIND_SHORT[j.kind])}</span>`:''}${j.post_on&&!j.pinned?'<span class="flag dated">Dated</span>':''}</span>`:''}</span>${icon('edit')}</button>`;}
const LANE_INFO={trial:['Non-followers first','Trial'],main:['Profile grid + feed','grid'],youtube:['YouTube Shorts','YouTube']};
function youtubePost(y){
 const movable=['queued','failed'].includes(y.status);
 const status=y.status==='uploaded'?'On YouTube':y.status==='uploading'?'Uploading':y.status==='failed'?'Needs attention':'Ready';
 return `<a class="schedule-post youtube-post${movable?' draggable':''}" href="#youtube" data-at="${y.at}" ${movable?`data-drag-kind="youtube" data-drag-id="${y.id}"`:''}><time>${clock(y.at)}</time>${y.kind==='short'?`<img src="/api/preview/${esc(y.source_key)}" alt="" loading="lazy">`:`<span class="set-thumb">${icon('play')}</span>`}<span class="schedule-post-copy"><strong>${esc(y.title)}</strong><span class="schedule-status ${y.error?'attention':''}">${esc(status)}${y.mirror_of?' · Follows its grid Reel':y.forced?' · Placed by you':''}</span></span>${icon('external')}</a>`;
}
function scheduleView(data){
 const lanes=['trial','main',...(data.youtube?.connected?['youtube']:[])];
 const empty=lane=>lane==='youtube'?'No YouTube uploads scheduled.':data.limits[lane]?`No ${lane==='trial'?'Trial':'grid'} Reels scheduled.`:'This destination is switched off.';
 return `<p class="schedule-summary">${data.totals.trial} Trial${data.totals.trial===1?'':'s'} and ${data.totals.main} grid Reel${data.totals.main===1?'':'s'}${data.youtube?.connected?` and ${data.totals.youtube} YouTube upload${data.totals.youtube===1?'':'s'}`:''} planned across ${data.days.length} day${data.days.length===1?'':'s'}. All times in ${esc(data.timezone)}.</p>
 <p class="help drag-hint">Drag posts between days and destinations, drag them onto <strong>On hold</strong> to take them out, or drag footage in. Posts you place go out on that day even past the daily limit.${data.youtube?.connected&&data.youtube.mirror?' Grid Reels also go out as YouTube Shorts at the same time.':''}</p>
 ${data.preparing||data.unscheduled||data.overdue?`<div class="notice"><p>${data.preparing?`${data.preparing} clip${data.preparing===1?' is':'s are'} still preparing or waiting for a caption. `:''}${data.unscheduled?`${data.unscheduled} post${data.unscheduled===1?' has':'s have'} no slot yet, including held posts and switched-off destinations. `:''}${data.overdue?`${data.overdue} older post${data.overdue===1?' needs':'s need'} review in the post list. `:''}</p></div>`:''}
 ${data.days.map(d=>`<section class="schedule-day" aria-label="Day ${d.number}, ${day(d.timestamp)}"><div class="schedule-day-heading"><h3>Day ${d.number}<span>${day(d.timestamp)}</span></h3><span>${d.trial.length} Trial${d.trial.length===1?'':'s'} · ${d.main.length} grid${data.youtube?.connected?` · ${d.youtube.length} YouTube`:''}</span></div><div class="schedule-lanes lanes-${lanes.length}">${lanes.map(lane=>`<div class="schedule-lane"><div class="schedule-lane-heading">${lane==='youtube'?'<span class="lane youtube">YouTube</span>':laneTag(lane)}<span>${LANE_INFO[lane][0]}</span></div><div class="drop-zone-lane" data-drop-lane="${lane}" data-drop-day="${d.date}">${d[lane].length?d[lane].map(lane==='youtube'?youtubePost:schedulePost).join(''):`<p class="schedule-empty">${empty(lane)}</p>`}</div></div>`).join('')}</div></section>`).join('')}
 <p class="help">Daily counts are limits, not filler. Available footage, captions, same-clip spacing and conservation can leave gaps. Published posts today remain visible for context.</p>`;
}
let trayData=null,trayTab='footage',trayQuery='',dragState=null;
function trayItems(){
 if(!trayData)return '<p class="help">Loading…</p>';
 const posted=c=>[c.main==='published'?'grid':'',c.trial==='published'?'Trial':'',c.youtube==='uploaded'?'YouTube':''].filter(Boolean);
 if(trayTab==='held')return trayData.held.length||trayData.drafts.length?`${trayData.held.map(j=>`<div class="tray-item draggable" data-drag-kind="job" data-drag-id="${j.id}"><img src="/api/preview/${esc(j.clip_id)}" alt=""><span><strong>${esc(j.name)}</strong><small>${j.lane==='main'?'Grid':'Trial'} · on hold</small></span><button type="button" class="text-button" data-release-kind="job" data-release-id="${j.id}">Auto</button></div>`).join('')}${trayData.drafts.map(y=>`<div class="tray-item draggable" data-drag-kind="youtube" data-drag-id="${y.id}">${y.kind==='short'?`<img src="/api/preview/${esc(y.source_key)}" alt="">`:`<span class="set-thumb">${icon('play')}</span>`}<span><strong>${esc(y.title)}</strong><small>YouTube ${y.kind==='short'?'Short':'set'} · draft</small></span><button type="button" class="text-button" data-release-kind="youtube" data-release-id="${y.id}">Auto</button></div>`).join('')}`:'<p class="help">Nothing on hold. Drag a post here to take it out of the schedule.</p>';
 return trayData.footage.length?trayData.footage.map(c=>`<div class="tray-item draggable" data-drag-kind="clip" data-drag-id="${esc(c.id)}" data-short="${c.short_ok?1:0}"><img src="/api/preview/${esc(c.id)}" alt=""><span><strong>${esc(c.name)}</strong><small>${posted(c).length?'Posted: '+posted(c).join(', '):'Not posted yet'} · ${Math.round(c.duration)}s</small></span></div>`).join(''):'<p class="help">No prepared footage matches.</p>';
}
function scheduleTray(){
 return `<section class="side-section schedule-tray" data-drop-hold="1" aria-label="Drag into the schedule"><h3>Drag in</h3><div class="tabs" role="group"><button type="button" class="tab ${trayTab==='footage'?'active':''}" data-tray-tab="footage">Footage</button><button type="button" class="tab ${trayTab==='held'?'active':''}" data-tray-tab="held">On hold${trayData?` (${trayData.held.length+trayData.drafts.length})`:''}</button></div>${trayTab==='footage'?`<input type="search" id="tray-search" placeholder="Search footage" value="${esc(trayQuery)}" aria-label="Search footage">`:''}<div class="tray-list" id="tray-list">${trayItems()}</div><p class="help tray-drop">Drop a post here to put it on hold.</p></section>`;
}
async function loadTray(){try{trayData=await api('/api/schedule/tray?q='+encodeURIComponent(trayQuery));const list=$('#tray-list');if(list){list.innerHTML=trayItems();icons();}const held=document.querySelector('[data-tray-tab="held"]');if(held)held.textContent=`On hold (${trayData.held.length+trayData.drafts.length})`;}catch(e){}}
function laneTimes(zone,skip){return [...zone.querySelectorAll('[data-at]')].filter(el=>el!==skip).map(el=>({el,at:Number(el.dataset.at)}));}
function dropPosition(zone,y,skip){
 const items=laneTimes(zone,skip);let index=items.length;
 for(let i=0;i<items.length;i++){const r=items[i].el.getBoundingClientRect();if(y<r.top+r.height/2){index=i;break;}}
 return {items,index,after:index>0?items[index-1].at:null,before:index<items.length?items[index].at:null};
}
function endDrag(){if(!dragState)return;cancelAnimationFrame(dragState.frame);dragState.ghost?.remove();dragState.marker?.remove();document.querySelectorAll('.drop-over').forEach(el=>el.classList.remove('drop-over'));dragState.source.classList.remove('dragging-source');document.body.classList.remove('is-dragging');dragState=null;}
function targetAt(x,y){const el=document.elementFromPoint(x,y);return el?.closest('[data-drop-lane],[data-drop-hold]');}
function autoScroll(){
 // Keep scrolling while the pointer rests near the top or bottom edge.
 if(!dragState)return;const y=dragState.y,edge=70;
 const speed=y<edge?-Math.ceil((edge-y)/4):y>window.innerHeight-edge?Math.ceil((y-window.innerHeight+edge)/4):0;
 if(speed){window.scrollBy(0,speed);moveDrag(dragState.x,dragState.y);}
 dragState.frame=requestAnimationFrame(autoScroll);
}
function moveDrag(x,y){
 const d=dragState;d.x=x;d.y=y;d.ghost.style.transform=`translate(${x+12}px,${y+12}px)`;
 document.querySelectorAll('.drop-over').forEach(el=>el.classList.remove('drop-over'));
 const target=targetAt(x,y);d.target=target;
 if(!target){d.marker.remove();return;}
 target.classList.add('drop-over');
 if(target.dataset.dropLane){const pos=dropPosition(target,y,d.source);const ref=pos.items[pos.index]?.el;if(ref)target.insertBefore(d.marker,ref);else target.appendChild(d.marker);}else d.marker.remove();
}
async function finishDrag(x,y){
 const d=dragState;if(!d)return;const target=targetAt(x,y);const kind=d.kind,id=d.id;
 let request=null;
 if(target?.dataset.dropLane){
  const lane=target.dataset.dropLane,pos=dropPosition(target,y,d.source);
  if(lane==='youtube'&&kind==='clip'&&d.source.dataset.short==='0'){endDrag();toast('YouTube Shorts need a square or vertical clip up to 3 minutes.',true);return;}
  request=['/api/schedule/place',{kind,id,lane,day:target.dataset.dropDay,after:pos.after,before:pos.before}];
 }else if(target?.dataset.dropHold&&kind!=='clip')request=['/api/schedule/hold',{kind,id}];
 endDrag();
 if(!request)return;
 try{const r=await api(request[0],'POST',request[1]);toast(r.at?`Placed for ${day(r.at)} at ${clock(r.at)}.`:'Put on hold.');await refresh();loadTray();}
 catch(e){toast(e.message,true);}
}
document.addEventListener('pointerdown',e=>{
 const source=e.target.closest('.draggable[data-drag-kind]');if(!source||e.button>0||dragState)return;
 const startX=e.clientX,startY=e.clientY,touch=e.pointerType!=='mouse';let started=false,timer=null;
 const begin=()=>{
  started=true;const r=source.getBoundingClientRect();
  const ghost=document.createElement('div');ghost.className='drag-ghost';const img=source.querySelector('img');
  ghost.innerHTML=(img?`<img src="${img.src}" alt="">`:'')+`<span>${esc(source.querySelector('strong')?.textContent||'Post')}</span>`;document.body.appendChild(ghost);
  const marker=document.createElement('div');marker.className='drop-marker';
  dragState={source,ghost,marker,kind:source.dataset.dragKind,id:source.dataset.dragId};source.classList.add('dragging-source');document.body.classList.add('is-dragging');
  moveDrag(startX,startY);dragState.frame=requestAnimationFrame(autoScroll);if(navigator.vibrate&&touch)navigator.vibrate(15);
 };
 if(touch)timer=setTimeout(begin,350);
 const move=ev=>{
  if(!started){if(Math.hypot(ev.clientX-startX,ev.clientY-startY)>6){if(touch){clearTimeout(timer);cleanup();return;}begin();}else return;}
  ev.preventDefault();moveDrag(ev.clientX,ev.clientY);
 };
 const up=ev=>{clearTimeout(timer);cleanup();if(started){ev.preventDefault();finishDrag(ev.clientX,ev.clientY);suppressClick=true;setTimeout(()=>suppressClick=false,50);}};
 const cleanup=()=>{document.removeEventListener('pointermove',move);document.removeEventListener('pointerup',up);document.removeEventListener('pointercancel',cancel);};
 const cancel=()=>{clearTimeout(timer);cleanup();endDrag();};
 document.addEventListener('pointermove',move,{passive:false});document.addEventListener('pointerup',up);document.addEventListener('pointercancel',cancel);
});
// Images inside draggable cards would otherwise start the browser's own drag and cancel ours.
document.addEventListener('dragstart',e=>{if(e.target.closest?.('.draggable'))e.preventDefault();});
let suppressClick=false;
document.addEventListener('click',e=>{if(suppressClick){e.preventDefault();e.stopImmediatePropagation();}},true);
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&dragState)endDrag();});
function captionTools(){
 return `<details id="caption-guidance" class="caption-guidance" ${guidanceOpen?'open':''}>
  <summary>Caption guidance</summary>
  <form id="caption-guidance-form">
   <label class="field">Instructions for the caption model
    <textarea name="caption_instructions" maxlength="4000" placeholder="Example: Use lowercase, keep it to one sentence, and skip the hashtags.">${esc(guidanceDraft??state.settings.caption_instructions)}</textarea>
    <span>Save your tone, preferred wording and phrases to avoid. New captions and rewrites use these instructions. Saving guidance leaves existing captions as they are.</span>
   </label>
   <button class="button" type="submit">Save guidance</button>
  </form>
 </details>
 <section id="caption-selection" class="caption-selection" aria-label="Regenerate captions">
  <div class="caption-actions">
   <strong id="caption-selected-count" aria-live="polite"></strong>
   <button type="button" class="button small" data-action="select-clips">Select page</button>
   <button type="button" class="button small" data-action="select-all-clips">Select all clips</button>
   <button type="button" class="button small" data-action="clear-clips">Clear selection</button>
   <button type="button" class="primary small" data-action="regenerate-clips">${icon('spark')}Regenerate selected</button>
  </div>
  <p class="help" id="caption-selection-help"></p>
  <details class="batch-guidance" ${batchGuidance?'open':''}>
   <summary>One-off guidance</summary>
   <label class="field">Instructions for this selection only
    <textarea id="batch-guidance" maxlength="4000" placeholder="Example: Make these a little more playful.">${esc(batchGuidance)}</textarea>
    <span>These instructions take priority for this batch. Your saved guidance stays the same.</span>
   </label>
  </details>
  <p id="caption-progress" class="help" role="status"></p>
  <div id="caption-batch-actions" class="inline-actions"></div>
 </section>`;
}
const laneName=lane=>lane==='main'?'Grid':'Trial';
const KINDS={performance:'DJ / performance',talking:'Talking to camera',bts:'Behind the scenes',promo:'Promo / announcement'};
const KIND_SHORT={talking:'Talking',bts:'Behind the scenes',promo:'Promo'};
const kindOptions=(selected,inherit)=>(inherit?`<option value="" ${!selected?'selected':''}>Same as its set (${esc(KINDS[inherit]||inherit)})</option>`:'')+Object.entries(KINDS).map(([k,l])=>`<option value="${k}" ${selected===k?'selected':''}>${l}</option>`).join('');
const shortDate=iso=>iso?new Intl.DateTimeFormat('en-AU',{day:'numeric',month:'short'}).format(new Date(iso+'T12:00:00')):'';
function timingSummary(clip){
 if(clip.post_on)return `Dated · ${shortDate(clip.post_on)}${clip.post_at?' '+clip.post_at:''}`;
 if(clip.not_before&&clip.not_after)return `Window · ${shortDate(clip.not_before)}–${shortDate(clip.not_after)}`;
 if(clip.not_before)return `From ${shortDate(clip.not_before)}`;
 if(clip.not_after)return `Until ${shortDate(clip.not_after)}`;
 return '';
}
function postedSummary(clip){
 if(!clip.posts?.length)return '';
 const lanes=[...new Set(clip.posts.map(p=>laneName(p.lane)))].join(' + ');
 return `Posted · ${lanes} · ${day(clip.posts[clip.posts.length-1].published_at)}`;
}
function songSummary(clip){
 if(!clip.song_clips)return '';
 return `Same song · ${clip.song_clips+1} clips${clip.similar?.length?` · ${clip.top_match}% match`:''}`;
}
function setsPanel(sets){
 if(!sets.length)return '';
 return `<details class="sets-panel" ${setsOpen?'open':''}><summary>Sets & mixing <span class="help">${sets.length} set${sets.length===1?'':'s'} · ${sets.filter(x=>x.mode==='mix').length} mixed in</span></summary>
 <p class="help">Each upload or subfolder is a set. Mixed sets spread through the queue so you don’t get the same night back to back. Clips with the same song are kept apart too.</p>
 <div class="sets-list">${sets.map(set=>`<div class="set-row"><div><strong>${esc(set.name)}</strong><span>${set.clips} clip${set.clips===1?'':'s'} · ${set.posted||0} posted · added ${day(set.created_at)}</span></div><div class="set-controls"><select data-set-kind="${esc(set.id)}" aria-label="Clip type for ${esc(set.name)}">${kindOptions(set.kind||'performance')}</select><select data-set-mode="${esc(set.id)}" aria-label="Posting order for ${esc(set.name)}"><option value="mix" ${set.mode==='mix'?'selected':''}>Mix into the queue</option><option value="next" ${set.mode==='next'?'selected':''}>Keep together, post next</option></select></div></div>`).join('')}</div></details>`;
}
function canRewrite(clip){return !clip.rebuilding&&!['processing','error'].includes(clip.status);}
function clipCard(clip){
 const id=esc(clip.id), name=esc(clip.name);
 const status=clip.rebuilding?'Rebuilding video':clip.disabled?'Excluded':({ready:'Ready to rotate',captioning:'Writing caption',needs_caption:'Needs a caption',processing:'Preparing',error:'Needs attention'}[clip.status]||clip.status);
 return `<article class="clip-card">
  <button class="clip-poster" data-clip="${id}" aria-label="Review ${name}">${canRewrite(clip)?`<img src="/api/preview/${id}" alt="" loading="lazy">`:icon('film')}${badge(status,clip.status==='ready'&&!clip.disabled&&!clip.rebuilding?'green':'rust')}<span class="clip-duration">${Math.round(clip.duration)}s</span></button>
  <h3>${name}</h3>${clip.posts?.length||clip.song_clips||KIND_SHORT[clip.kind]||timingSummary(clip)?`<div class="clip-flags">${KIND_SHORT[clip.kind]?`<span class="flag kind">${esc(KIND_SHORT[clip.kind])}</span>`:''}${timingSummary(clip)?`<span class="flag dated">${esc(timingSummary(clip))}</span>`:''}${clip.posts?.length?`<span class="flag posted">${icon('check')}${esc(postedSummary(clip))}</span>`:''}${clip.song_clips?`<span class="flag song">${esc(songSummary(clip))}</span>`:''}</div>`:''}<p class="clip-caption">${esc(clip.caption||clip.category||clip.caption_source||'Waiting for preparation')}</p>
  ${canRewrite(clip)?`<label class="clip-select"><input type="checkbox" data-caption-select="${id}" aria-label="Select ${name} for caption regeneration">Select</label>`:''}
 </article>`;
}
function captionControls(){
 if(!state||!$('#caption-selection'))return;
 const count=selectedClips.size;
 const configured=state.ai_configured&&state.settings.ai_model;
 $('#caption-selected-count').textContent=`${count} selected`;
 $('[data-action="select-clips"]').disabled=captionBusy||!records.some(clip=>canRewrite(clip)&&!selectedClips.has(clip.id));
 $('[data-action="select-all-clips"]').disabled=captionBusy||!!state.rebuilding||!['ready','captioning','needs_caption'].some(k=>state.counts[k]);
 $('[data-action="rebuild-clips"]').disabled=captionBusy||!!state.rebuilding||!Object.values(state.counts).some(Boolean);
 $('[data-action="clear-clips"]').disabled=captionBusy||!count;
 $('[data-action="regenerate-clips"]').disabled=captionBusy||!count||guidanceDraft!==null||!configured;
 $('#caption-selection-help').textContent=guidanceDraft!==null?'Save your caption guidance before regenerating.':!configured?'Connect a caption provider and choose a model in Settings.':state.rebuilding?'Wait for rebuilding to finish to select the whole library.':'Select prepared clips, then regenerate. Select all clips includes every page. Edited post captions and published posts are kept.';
 $('#caption-progress').textContent=captionProgress;
 batchButtons(captionBatch);
 $('#caption-progress').classList.toggle('caption-failed',captionFailed);
 const oneOff=$('#batch-guidance');
 oneOff.disabled=captionBusy;
 if(oneOff.value!==batchGuidance)oneOff.value=batchGuidance;
 $('#caption-guidance-form textarea').disabled=captionBusy;
 $('#caption-guidance-form button').disabled=captionBusy||guidanceDraft===null;
 document.querySelectorAll('[data-caption-select]').forEach(box=>{
  box.checked=selectedClips.has(box.dataset.captionSelect);
  box.disabled=captionBusy;
 });
}
let batchWatch=null, captionBatch=null;
async function regenerateSelected(){
 if(captionBusy||guidanceDraft!==null||!selectedClips.size)return;
 captionBusy=true;captionFailed=false;captionProgress='Starting…';captionControls();
 try{
  captionBatch=await api('/api/rewrite-batches','POST',{clips:[...selectedClips.keys()],instructions:batchGuidance.trim()});
  batchGuidance='';
 }catch(e){captionBusy=false;captionFailed=true;captionProgress=e.message;captionControls();return;}
 watchBatch();
}
function describeBatch(b){
 const finished=b.done+b.failed+b.skipped;
 if(b.status==='running')return `Rewriting on the server: ${b.done} of ${b.total} done${b.failed?`, ${b.failed} failed`:''}.${b.message?' '+b.message:' You can leave this page; it keeps going.'}`;
 if(b.status==='paused')return `${b.done} of ${b.total} done. ${b.message}`;
 if(b.status==='cancelled')return `${b.done} of ${b.total} rewritten before you cancelled.`;
 return b.message||`${finished} of ${b.total} finished.`;
}
function batchButtons(b){
 const box=$('#caption-batch-actions');if(!box)return;
 box.innerHTML=b&&['running','paused'].includes(b.status)?`${b.status==='paused'?'<button type="button" class="button small" data-batch-action="resume">Resume</button>':''}<button type="button" class="text-button" data-batch-action="cancel">Cancel batch</button>`:b&&b.status==='done'&&b.failed?'<button type="button" class="button small" data-batch-action="resume">Retry the failed clips</button>':'';
}
async function watchBatch(){
 if(batchWatch)return;
 batchWatch=true;let misses=0;
 try{
  while(state&&!$('#shell').hidden){
   let b;
   try{b=(await api('/api/rewrite-batches/latest')).batch;misses=0;}
   catch(e){misses++;captionProgress=`Connection to Reelflow lost (${e.message}). The batch keeps running on the server; reconnecting…`;captionControls();await new Promise(r=>setTimeout(r,Math.min(30000,3000*misses)));continue;}
   if(!b)break;
   captionBatch=b;
   for(const item of b.items)if(item.status==='done'||item.status==='skipped')selectedClips.delete(item.clip_id);else if(!selectedClips.has(item.clip_id))selectedClips.set(item.clip_id,item.name||item.clip_id);
   captionBusy=b.status==='running';captionFailed=b.status==='paused'||(b.status==='done'&&b.failed>0);
   captionProgress=describeBatch(b);captionControls();batchButtons(b);
   if(b.status!=='running'){if(currentPage==='library')await refresh();else toast(captionProgress,captionFailed);break;}
   await new Promise(r=>setTimeout(r,3000));
  }
 }finally{batchWatch=null;}
}
function bindCaptionTools(){
 const form=$('#caption-guidance-form');
 if(!form)return;
 $('#caption-guidance').addEventListener('toggle',e=>{guidanceOpen=e.currentTarget.open;});
 const input=form.elements.caption_instructions, oneOff=$('#batch-guidance');
 oneOff.addEventListener('input',()=>{batchGuidance=oneOff.value;});
 input.addEventListener('input',()=>{
  guidanceDraft=input.value===state.settings.caption_instructions?null:input.value;
  captionControls();
 });
 form.addEventListener('submit',async e=>{
  e.preventDefault();
  if(captionBusy||guidanceDraft===null)return;
  captionBusy=true;
  captionControls();
  try{
   await api('/api/settings','PATCH',{values:{caption_instructions:input.value}});
   await loadState();
   guidanceDraft=null;
   toast('Caption guidance saved.');
  }catch(err){toast(err.message,true);}
  finally{captionBusy=false;captionControls();}
 });
 document.querySelectorAll('[data-caption-select]').forEach(box=>box.addEventListener('change',()=>{
  const clip=records.find(r=>r.id===box.dataset.captionSelect);
  if(!clip)return;
  if(box.checked)selectedClips.set(clip.id,clip.name);else selectedClips.delete(clip.id);
  captionControls();
 }));
 captionControls();
}
function safeLink(url){try{const u=new URL(url);return ['https:','http:'].includes(u.protocol)?esc(u.href):'#';}catch{return '#';}}
function postingNow(j){return j.post_now_at&&['queued','retry','processing','publishing'].includes(j.status);}
function jobRow(j){return `<article class="queue-row"><img class="thumb" src="/api/preview/${j.clip_id}" alt="" loading="lazy"><div class="row-info"><h3>${esc(j.name)}</h3><p>${esc(j.caption)}</p><div class="row-meta">${laneTag(j.lane)}${KIND_SHORT[j.kind]?`<span class="flag kind">${esc(KIND_SHORT[j.kind])}</span>`:''}${j.post_on&&!j.pinned?'<span class="flag dated">Dated</span>':''}<span>${j.status==='published'?'Published':j.status==='rejected'?'Rejected':postingNow(j)?'Post now requested':j.disabled?'Clip excluded':!j.approved?'Awaiting approval':j.status==='queued'?'Ready to post':esc(j.status)}</span>${j.pinned?'<span>Time pinned</span>':''}${j.error?badge('Needs attention','rust'):''}</div></div><div class="post-time"><strong>${clock(j.published_at||j.scheduled_at)}</strong><span>${j.scheduled_at||j.published_at?day(j.published_at||j.scheduled_at):'Awaiting a slot'}</span></div><button class="icon-button" data-job="${j.id}" aria-label="Review post ${j.id}">${icon(j.status==='published'?'external':'edit')}</button></article>`;}
function setupStages(){return `<div class="setup-stage"><span class="stage-circle ${state.account?'done':''}">${state.account?icon('check'):'1'}</span><div><h3>Connect your Instagram</h3><p>Bring your account, previous captions and audience insights.</p></div><a href="#settings" class="button small">${state.account?'Connected':'Connect account'} ${icon('arrow')}</a></div><div class="setup-stage"><span class="stage-circle ${state.ai_configured?'done':''}">${state.ai_configured?icon('check'):'2'}</span><div><h3>Give the desk a voice</h3><p>Sign in with OpenRouter or connect an AI provider for short, footage-aware captions.</p></div><a href="#settings" class="button small">${state.ai_configured?'Configure model':'Set up captions'} ${icon('arrow')}</a></div><div class="setup-stage"><span class="stage-circle">3</span><div><h3>Add your source material</h3><p>Clips become regular and Trial Reels, mixed into a flexible running order.</p></div><button class="button small" data-action="upload">Add footage ${icon('arrow')}</button></div>`;}
function field(name,title,type='text',help='',attrs=''){const val=state.settings[name];return `<label>${title}<input name="${name}" type="${type}" value="${esc(val)}" ${attrs}>${help?`<span>${help}</span>`:''}</label>`;}
function toggle(name,title,help=''){return `<label class="toggle"><div>${title}${help?`<span>${help}</span>`:''}</div><input type="checkbox" name="${name}" ${state.settings[name]?'checked':''}></label>`;}
function settingSelect(name,title,options,help=''){
 return `<label>${title}<select name="${name}">${options.map(([value,label])=>`<option value="${value}" ${state.settings[name]===value?'selected':''}>${label}</option>`).join('')}</select>${help?`<span>${help}</span>`:''}</label>`;
}
function captionRules(s){
 return `<h3>Caption rules</h3>
 <label>Blocked words and phrases<textarea name="caption_blocklist" maxlength="4000" rows="3">${esc(s.caption_blocklist)}</textarea><span>One per line. The app checks whole words and phrases, ignoring case.</span></label>
 <label>Sign-offs<textarea name="caption_signoff" maxlength="132" rows="2">${esc(s.caption_signoff)}</textarea><span>One allowed ending per line. Leave blank to use quoted sign-offs from your guidance.</span></label>
 ${settingSelect('caption_avoid_camera','Camera and editing language',[['guidance','Follow caption guidance'],['avoid','Block it'],['allow','Allow it']])}
 ${toggle('caption_unique','Avoid repeating recent captions','Checks the wording and opening against recent drafts and posts.')}`;
}
let tagGroups=null,tagStats={},tagsDirty=false,tagSuggestions=[];
const TAG_MODES=[['always','Always include'],['pool','AI picks from these'],['never','Never use']];
const fmtReach=n=>n==null?'':n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':String(n);
function tagChip(tag,g){
 const st=tagStats[tag.toLowerCase()];
 const info=st?`${st.uses}×${st.avg_reach!=null?' · '+fmtReach(st.avg_reach):''}`:'';
 const title=st?`Posted ${st.uses} time${st.uses===1?'':'s'}${st.avg_reach!=null?`, average reach ${st.avg_reach.toLocaleString()}`:''}${st.last?`, last ${day(st.last)}`:''}`:'Not posted yet';
 return `<span class="lib-tag" data-tag="${esc(tag.toLowerCase())}" title="${esc(title)}">${esc(tag)}${info?`<small>${esc(info)}</small>`:''}<button type="button" aria-label="Remove ${esc(tag)}" data-remove-tag="${g}" data-tag-value="${esc(tag)}">×</button></span>`;
}
function tagGroupCard(group,g){
 const pool=group.mode==='pool';
 const kinds=group.kinds||[];
 return `<div class="tag-group mode-${group.mode}" data-group="${g}">
 <div class="tag-group-head"><input aria-label="Group name" data-group-field="name" maxlength="40" value="${esc(group.name)}"><select aria-label="How ${esc(group.name)} is used" data-group-field="mode">${TAG_MODES.map(([v,l])=>`<option value="${v}" ${group.mode===v?'selected':''}>${l}</option>`).join('')}</select><button type="button" class="icon-button" data-remove-group="${g}" aria-label="Delete ${esc(group.name)}">${icon('x')}</button></div>
 <p class="help">${group.mode==='always'?'Added to every post, before the AI’s picks. Counts toward the five.':group.mode==='never'?'Blocked, even if the AI suggests them.':'The AI picks the tags that fit each clip, within the limits below. Tags it skips rotate in later.'}</p>
 ${pool?`<div class="form-row tag-limits"><label>At least<input type="number" min="0" max="5" data-group-field="min" value="${group.min}"><span>per post</span></label><label>At most<input type="number" min="0" max="5" data-group-field="max" value="${group.max}"><span>per post</span></label></div>`:''}
 <div class="tag-kinds" role="group" aria-label="Clip types for ${esc(group.name)}"><span class="help">Use for</span><label><input type="checkbox" data-group-kind="all" ${!kinds.length?'checked':''}>All clip types</label>${Object.entries(KINDS).map(([k,l])=>`<label><input type="checkbox" data-group-kind="${k}" ${kinds.includes(k)?'checked':''}>${l}</label>`).join('')}</div>
 <div class="tag-tools">${group.tags.length>12?`<input type="search" class="tag-filter" placeholder="Filter ${group.tags.length} tags" aria-label="Filter tags in ${esc(group.name)}">`:''}<span class="help">${group.tags.length} tag${group.tags.length===1?'':'s'}</span>${group.tags.length?`<button type="button" class="text-button" data-sort-group="${g}">Sort by reach</button><button type="button" class="text-button" data-copy-group="${g}">Copy all</button>`:''}</div>
 <div class="lib-tags">${group.tags.map(t=>tagChip(t,g)).join('')||'<p class="help">No tags yet. Paste a list below.</p>'}</div>
 <details class="tag-paste" ${group.tags.length?'':'open'}><summary>Add tags</summary><textarea rows="4" data-paste-text="${g}" placeholder="Paste any list: #a #b, one per line, numbered, or comma separated. An AI’s answer with a sentence on top works too."></textarea><div class="inline-actions"><button type="button" class="button small" data-paste-group="${g}">Add to ${esc(group.name)}</button></div><p class="help" data-paste-result="${g}" role="status"></p></details>
 </div>`;
}
function hashtagLibrary(){
 if(!tagGroups||!tagsDirty)tagGroups=JSON.parse(JSON.stringify(state.settings.hashtag_groups||[]));
 return `<section class="settings-section" id="hashtag-section"><h2>Hashtag library</h2>
 <p>Group your hashtags. Put your own tag in <strong>Always include</strong>, paste your big lists into groups the AI picks from, and block anything you never want.</p>
 <div class="form-row tag-totals">${field('hashtags_per_post','Hashtags per post','number','Instagram allows five. 0 turns hashtags off.','min="0" max="5"')}${field('hashtag_new_max','New tags the AI may add','number','Tags it finds itself, outside your library. 0 keeps to your lists.','min="0" max="5"')}</div>
 <p id="tags-dirty" class="notice-inline" ${tagsDirty?'':'hidden'}>Unsaved hashtag changes. Save settings to apply them.</p>
 <div id="tag-library">${tagGroups.map(tagGroupCard).join('')}</div>
 <div class="inline-actions"><button type="button" class="button small" data-add-group="always">${icon('plus')}Always-include group</button><button type="button" class="button small" data-add-group="pool">${icon('plus')}Group the AI picks from</button><button type="button" class="button small" data-add-group="never">${icon('plus')}Never-use group</button></div>
 <div class="tag-suggest"><h3>Find more with AI</h3><div class="inline-actions"><select id="suggest-count" aria-label="How many suggestions">${[12,30,60].map(n=>`<option value="${n}">${n} suggestions</option>`).join('')}</select><button type="button" class="button small" data-action="suggest-tags">${icon('spark')}Suggest tags</button></div><div id="tag-suggestions" class="tag-suggestions" aria-live="polite"></div></div>
 <p class="help">The model sees each group, your best-reaching tags and what you used recently. Existing captions keep their tags until you use Regenerate selected in the Footage library.</p>
 </section>`;
}
function markTagsDirty(){tagsDirty=true;const note=$('#tags-dirty');if(note)note.hidden=false;}
function renderTagLibrary(){const box=$('#tag-library');if(box)box.innerHTML=tagGroups.map(tagGroupCard).join('');renderSuggestions();}
function renderSuggestions(){
 const box=$('#tag-suggestions');if(!box)return;
 const pools=tagGroups.map((g,i)=>[g,i]).filter(([g])=>g.mode!=='never');
 if(!tagSuggestions.length){box.innerHTML='';return;}
 const inLib=new Set(tagGroups.flatMap(g=>g.tags.map(t=>t.toLowerCase())));
 box.innerHTML=`<p class="help">Click a tag to add it to the selected group, then save settings.</p><div class="inline-actions"><select id="suggest-target" aria-label="Group to add suggestions to">${pools.map(([g,i])=>`<option value="${i}">Add to ${esc(g.name)}</option>`).join('')||'<option value="new">Add to a new group</option>'}</select><button type="button" class="text-button" data-add-all-suggestions>Add all</button></div>${tagSuggestions.map(t=>`<button type="button" class="tag-chip" data-add-tag="${esc(t)}" ${inLib.has(t.toLowerCase())?'disabled':''}>${esc(t)}</button>`).join('')}`;
}
function suggestionTarget(){
 const value=$('#suggest-target')?.value;
 if(value&&value!=='new')return Number(value);
 tagGroups.push({name:'Suggested',mode:'pool',tags:[],min:0,max:5,kinds:[]});return tagGroups.length-1;
}
function addTags(g,tags){
 const have=new Set(tagGroups.flatMap(x=>x.tags.map(t=>t.toLowerCase())));
 const fresh=tags.filter(t=>!have.has(t.toLowerCase())&&(have.add(t.toLowerCase()),true));
 tagGroups[g].tags.push(...fresh);markTagsDirty();return fresh.length;
}
function bindTagLibrary(){
 const section=$('#hashtag-section');if(!section)return;
 tagStats={};api('/api/hashtags/stats').then(data=>{tagStats=data.tags||{};if($('#tag-library'))renderTagLibrary();}).catch(()=>{});
 section.addEventListener('input',e=>{
  const card=e.target.closest('[data-group]');if(!card)return;const g=Number(card.dataset.group);
  if(e.target.classList.contains('tag-filter')){const q=e.target.value.trim().toLowerCase().replace(/^#/,'');card.querySelectorAll('.lib-tag').forEach(chip=>chip.hidden=!!q&&!chip.dataset.tag.includes(q));return;}
  const field=e.target.dataset.groupField;if(!field)return;
  tagGroups[g][field]=field==='min'||field==='max'?Number(e.target.value):e.target.value;markTagsDirty();
  if(field==='mode'){if(e.target.value==='pool'){tagGroups[g].min=tagGroups[g].min||0;tagGroups[g].max=tagGroups[g].max||5;}renderTagLibrary();}
 });
 section.addEventListener('change',e=>{
  const kind=e.target.dataset.groupKind;if(!kind)return;const g=Number(e.target.closest('[data-group]').dataset.group);
  if(kind==='all')tagGroups[g].kinds=[];else{const set=new Set(tagGroups[g].kinds);e.target.checked?set.add(kind):set.delete(kind);tagGroups[g].kinds=[...set];}
  markTagsDirty();renderTagLibrary();
 });
 section.addEventListener('click',async e=>{
  const b=e.target.closest('button');if(!b)return;
  if(b.dataset.removeTag!==undefined){const g=Number(b.dataset.removeTag);tagGroups[g].tags=tagGroups[g].tags.filter(t=>t!==b.dataset.tagValue);markTagsDirty();b.closest('.lib-tag').remove();return;}
  if(b.dataset.removeGroup!==undefined){const g=Number(b.dataset.removeGroup);if(tagGroups[g].tags.length&&!confirm(`Delete “${tagGroups[g].name}” and its ${tagGroups[g].tags.length} tags?`))return;tagGroups.splice(g,1);markTagsDirty();renderTagLibrary();return;}
  if(b.dataset.addGroup){const mode=b.dataset.addGroup;tagGroups.push({name:{always:'Always',pool:'New group',never:'Never use'}[mode],mode,tags:[],min:0,max:mode==='pool'?5:5,kinds:[]});markTagsDirty();renderTagLibrary();$('#tag-library').lastElementChild?.querySelector('[data-group-field=name]')?.focus();return;}
  if(b.dataset.sortGroup!==undefined){const g=Number(b.dataset.sortGroup);const score=t=>tagStats[t.toLowerCase()]?.avg_reach??-1;tagGroups[g].tags.sort((a,c)=>score(c)-score(a));markTagsDirty();renderTagLibrary();return;}
  if(b.dataset.copyGroup!==undefined){const g=Number(b.dataset.copyGroup);try{await navigator.clipboard.writeText(tagGroups[g].tags.join(' '));toast('Copied '+tagGroups[g].tags.length+' tags.');}catch{toast('Copy is blocked in this browser.',true);}return;}
  if(b.dataset.pasteGroup!==undefined){
   const g=Number(b.dataset.pasteGroup);const area=section.querySelector(`[data-paste-text="${g}"]`);if(!area.value.trim())return;
   await busy(b,async()=>{const res=await api('/api/hashtags/parse','POST',{text:area.value});const added=addTags(g,res.tags);const dup=res.tags.length-added;
    renderTagLibrary();const out=section.querySelector(`[data-paste-result="${g}"]`);
    if(out)out.textContent=`Added ${added} tag${added===1?'':'s'}${dup?`, skipped ${dup} already in the library`:''}${res.rejected_count?`, ignored ${res.rejected_count} that aren’t valid hashtags (${res.rejected.slice(0,5).join(', ')}${res.rejected_count>5?'…':''})`:''}. Save settings to apply.`;});
   return;
  }
  if(b.dataset.addTag){const g=suggestionTarget();addTags(g,[b.dataset.addTag]);b.disabled=true;renderTagLibrary();return;}
  if(b.dataset.addAllSuggestions!==undefined){const g=suggestionTarget();const n=addTags(g,tagSuggestions);toast(`Added ${n} tag${n===1?'':'s'} to ${tagGroups[g].name}. Save settings to apply.`);renderTagLibrary();return;}
 });
}
let collabDraft=null,collabSigned=[];
function collabRows(){
 const signed=new Set(collabSigned.map(a=>a.username));
 return collabDraft.length?collabDraft.map((a,i)=>`<li class="collab-row" data-collab="${i}">
  <span class="collab-move"><button type="button" class="icon-button" data-collab-up="${i}" aria-label="Move @${esc(a.username)} up" ${i?'':'disabled'}>↑</button><button type="button" class="icon-button" data-collab-down="${i}" aria-label="Move @${esc(a.username)} down" ${i<collabDraft.length-1?'':'disabled'}>↓</button></span>
  <span class="collab-name">@${esc(a.username)}</span>
  <label class="collab-auto"><input type="checkbox" data-collab-auto="${i}" ${a.auto_accept?'checked':''}>Accept automatically</label>
  ${signed.has(a.username)?`<span class="badge green">Signed in</span><button type="button" class="text-button" data-collab-signout="${esc(a.username)}">Sign out</button>`:`<a class="text-button" href="/auth/instagram/start?collab=1" title="Log in to Instagram as @${esc(a.username)} in this browser first">Sign in as @${esc(a.username)}</a>`}
  <button type="button" class="icon-button" data-collab-remove="${i}" aria-label="Remove @${esc(a.username)}">${icon('x')}</button></li>`).join(''):'<li class="help">No collaborators yet.</li>';
}
function collaboratorSettings(){
 if(!collabDraft)collabDraft=JSON.parse(JSON.stringify(state.settings.collab_accounts||[]));
 const s=state.settings;
 return `<section class="settings-section" id="collab-section"><h2>Collaborators</h2><p>Invite your other Instagram accounts as collaborators on every grid Reel. Accounts you sign in here accept on their own, on your schedule.</p>
 ${toggle('collab_enabled','Invite collaborators on grid Reels','Trial Reels never get collaborators, since they are meant for non-followers.')}
 <ol class="collab-list" id="collab-list">${collabRows()}</ol>
 <div class="collab-add"><input id="collab-new" placeholder="username" maxlength="31" aria-label="Collaborator username"><button type="button" class="button small" data-collab-add>${icon('plus')}Add</button></div>
 <div class="form-row">${settingSelect('collab_per_post','Invite per post',[[1,'1 account'],[2,'2 accounts'],[3,'3 accounts (Instagram’s limit)']].map(([v,l])=>[v,l]),'With more accounts listed, each Reel invites the next ones in turn.')}${settingSelect('collab_order','Accept order',[['listed','In the order above'],['random','Random']])}</div>
 <div class="form-row">${field('collab_first_hours','First accepts after (hours)','number','From when the Reel publishes. 0 = straight away.','min="0" max="1440"')}${field('collab_gap_hours','Then one more every (hours)','number','Between each account accepting.','min="0" max="1440"')}</div>
 <p class="help">To sign in a collaborator, log in to Instagram as that account in this browser, then press its Sign in link. While your Meta app is in development mode, add each account as an Instagram tester in the app first. Accounts that aren’t signed in still get the invite and accept in the Instagram app.</p></section>`;
}
function bindCollaborators(){
 const section=$('#collab-section');if(!section)return;
 api('/api/collaborators').then(r=>{collabSigned=r.connected;const list=$('#collab-list');if(list)list.innerHTML=collabRows();icons();}).catch(()=>{});
 const redraw=()=>{$('#collab-list').innerHTML=collabRows();icons();};
 const add=()=>{const input=$('#collab-new');const name=input.value.trim().replace(/^@/,'').toLowerCase();if(!/^[a-z0-9._]{1,30}$/.test(name)){toast('Enter an Instagram username: letters, numbers, dots and underscores.',true);return;}if(!collabDraft.some(a=>a.username===name))collabDraft.push({username:name,auto_accept:true});input.value='';redraw();};
 section.addEventListener('keydown',e=>{if(e.target.id==='collab-new'&&e.key==='Enter'){e.preventDefault();add();}});
 section.addEventListener('change',e=>{const i=e.target.dataset.collabAuto;if(i!==undefined)collabDraft[Number(i)].auto_accept=e.target.checked;});
 section.addEventListener('click',async e=>{
  const b=e.target.closest('button');if(!b)return;
  if(b.dataset.collabAdd!==undefined)return add();
  const swap=(i,j)=>{[collabDraft[i],collabDraft[j]]=[collabDraft[j],collabDraft[i]];redraw();};
  if(b.dataset.collabUp!==undefined)return swap(Number(b.dataset.collabUp),Number(b.dataset.collabUp)-1);
  if(b.dataset.collabDown!==undefined)return swap(Number(b.dataset.collabDown),Number(b.dataset.collabDown)+1);
  if(b.dataset.collabRemove!==undefined){collabDraft.splice(Number(b.dataset.collabRemove),1);return redraw();}
  if(b.dataset.collabSignout){await busy(b,async()=>{await api(`/api/collaborators/${encodeURIComponent(b.dataset.collabSignout)}/sign-out`,'POST');collabSigned=collabSigned.filter(a=>a.username!==b.dataset.collabSignout);redraw();toast('Signed out.');});}
 });
}
function multicamSettings(){
 return `<section class="settings-section"><h2>Multicam editor</h2>${toggle('multicam_enabled','Show the Multicam tab','Upload a DJ mix and camera angles; Reelflow syncs them, cuts on the music, and delivers a full edit and social clips.')}</section>`;
}
function contentMix(){
 const mix=state.settings.content_mix||{};
 return `<section class="settings-section"><h2>Content mix</h2><p>DJ footage is the backbone. Other clip types are sprinkled in between, never back to back.</p>
 ${['talking','bts','promo'].map(kind=>{const rule=mix[kind]||{};return `<div class="mix-row"><strong>${KINDS[kind]}</strong><div class="form-row"><label>Post to<select data-mix="${kind}" data-mix-field="lanes">${[['both','Grid and Trials'],['main','Grid only'],['trial','Trials only']].map(([v,l])=>`<option value="${v}" ${rule.lanes===v?'selected':''}>${l}</option>`).join('')}</select></label><label>At most one every<input type="number" min="0" max="30" data-mix="${kind}" data-mix-field="every" value="${rule.every??3}"><span>posts in each lane</span></label></div></div>`;}).join('')}
 <p class="help">Mark clip types when you upload, per set under Footage library → Sets & mixing, or per clip. Footage subfolders named “talking”, “BTS” or “promo” are detected automatically.</p></section>`;
}
function reelPresentation(){
 return `<section class="settings-section"><h2>Reel covers & export</h2>
 ${settingSelect('cover_mode','Cover selection',[['auto','Automatic · clear frames'],['color','Automatic · favour colour'],['instagram','Let Instagram choose']],'Reelflow samples seven frames and prefers clear, well-exposed images. Override the frame for any clip in the Footage library.')}
 ${settingSelect('story_share','Share to Instagram Stories',[['grid','Grid Reels'],['all','Grid and Trial Reels'],['off','Off']],'After a Reel publishes, its video is also posted as a Story (first 59 seconds if longer). Instagram’s API can’t add the Reel sticker. Sharing Trials to Stories shows them to followers.')}
 ${settingSelect('gpu_mode','Video encoding',[['auto','Use the NVIDIA GPU when available'],['off','CPU only']],state.gpu?.nvenc?`GPU found: ${esc(state.gpu.name)}. Clip exports, funny edits and multicam renders encode on it, and fall back to the CPU if it is busy.`:'No NVIDIA GPU is visible to Reelflow, so encoding uses the CPU. To use one (for example a T1000), pass it to the container; see the README.')}
 ${settingSelect('export_quality','Export quality',[['standard','Standard'],['detail','Higher detail']],'Higher detail uses more storage and bandwidth. Use Rebuild all videos to apply it to existing exports. Both profiles preserve the source framing.')}
 </section>`;
}
function settingsView(){const s=state.settings;return header('Workspace settings','Set it up once. Come back when you want to change the mix.')+`<form id="settings-form"><div class="settings-grid"><div><section class="settings-section"><h2>Instagram</h2><div class="connection">${icon('instagram')}<div><strong>${state.account?'@'+esc(state.account.username):'No account connected'}</strong><p>${state.account?'Connection expires '+day(state.account.expires_at):'Business or Creator account required'}</p>${badge(state.account?'Connected':'Setup needed',state.account?'green':'')}</div><div class="inline-actions"><a href="/auth/instagram/start" class="button">${state.account?'Reconnect Instagram':'Connect Instagram'} ${icon('arrow')}</a>${state.account?'<button type="button" data-action="disconnect" class="text-button">Disconnect</button>':''}</div><p class="help">${state.meta_configured?'Meta app is configured. Login imports your recent posts on the next worker pass.':'Add META_APP_ID and META_APP_SECRET to compose.yaml, set your public HTTPS URL, and restart Docker. The deployment guide covers the exact redirect URL and permissions.'}</p><p class="help">Redirect URL: <code>${esc(state.public_url)}/auth/instagram/callback</code></p></section><section class="settings-section"><h2>Captions & voice</h2><p>The account’s previous posts lead. These notes steer the result for any artist, business or creator.</p>${field('brand_name','Account / brand name') }<label>What is this account about?<textarea name="business_context">${esc(s.business_context)}</textarea></label><label>Voice & factual guardrails<textarea name="voice_notes" maxlength="4000">${esc(s.voice_notes)}</textarea></label><label>Custom caption instructions<textarea name="caption_instructions" maxlength="4000" placeholder="e.g. Write like us: dry humour, lowercase, no emojis. One short sentence. Never say ‘big things coming’.">${esc(s.caption_instructions)}</textarea><span>Sent to the caption model with every new caption and rewrite. Set tone, wording, hashtags or phrases to avoid. Existing captions stay as they are until you choose Rewrite caption. Factual accuracy and the caption format still apply.</span></label>${captionRules(s)}<div class="connection">${icon('spark')}<div><strong>${state.ai_configured?'AI provider connected':'Connect a caption provider'}</strong><p>${esc(state.ai_base)}</p></div></div><div class="inline-actions"><a href="/auth/openrouter/start" class="button">Sign in with OpenRouter ${icon('arrow')}</a><button type="button" class="button" data-action="models">Load model catalog</button></div><label>Model ID<input name="ai_model" list="model-options" value="${esc(s.ai_model)}" placeholder="Choose a model from the live catalog"><datalist id="model-options"></datalist><span>Use a model that accepts images with visual captions enabled. Model names come from the provider; no assumed model alias.</span></label><label>Or paste an API key<input name="ai_key" type="password" autocomplete="new-password" placeholder="${state.ai_configured?'A key is stored securely. Leave blank to keep it.':'OpenRouter or configured provider API key'}"><span>Encrypted on the server. Never returned to your browser.</span></label>${toggle('ai_vision','Use sampled footage frames','Three silent frames go to your selected AI provider. Disable for text and operator notes only.')}<p class="help">OpenRouter sign-in uses your OpenRouter credit. To use another OpenAI-compatible provider, change AI_BASE_URL in compose.yaml. A missing provider or failed caption holds the clip for review.</p></section>${reelPresentation()}${contentMix()}${hashtagLibrary()}${collaboratorSettings()}${multicamSettings()}</div><div><section class="settings-section"><h2>The posting rhythm</h2>${field('timezone','Audience timezone','text','Editable until you know where your audience is. Use an IANA name, e.g. Europe/London.')}${toggle('paused','Pause all publishing')}${toggle('dry_run','Dry run','Prepare and review the queue without posting to Instagram.')}${toggle('require_approval','Require approval for new posts','Off means fully automatic. Existing approvals stay as they are.')}${toggle('trials_enabled','Publish Trial Reels','Every clip once. Trials stay Trials; automatic graduation is off.')}<div class="form-row">${field('trials_per_day','Trial Reels per day','number','0 switches Trials off.','min="0" max="10"')}${field('main_per_day','Grid Reels per day','number','0 switches grid posts off.','min="0" max="10"')}</div><div class="form-row">${field('preview_days','Schedule preview (days)','number','','min="1" max="90"')}${field('min_gap_days','Same clip, lane gap (days)','number','','min="0" max="90"')}</div><p class="help">Grid Reels are shared to your profile and feed. Trials reach non-followers first and stay off your grid. Trial eligibility is checked by Instagram when publishing. There is no fallback that silently publishes a Trial to your main feed.</p><h3>Posting times</h3><div class="form-row">${field('posting_start_hour','First posting hour','number','24-hour time, in the audience timezone.','min="0" max="22"')}${field('posting_end_hour','Last posting hour','number','Allow at least 90 minutes between posts to the same destination.','min="1" max="23"')}</div>${toggle('learn_timing','Learn posting times from engagement','Off uses your preferred weekday hour and spreads extra posts across your window.')}<h3>Preferred hour by weekday</h3><div class="week-hours">${['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map((d,i)=>`<label>${d}<input type="number" name="hour_${i}" min="0" max="23" value="${s.schedule_hours[i]}" aria-label="${d} starting hour"></label>`).join('')}</div><p class="help">24-hour format. One grid slot favours this hour, within your posting window; extra posts and Trials spread across that window. When timing learning is on, five mature same-weekday observations unlock timing choices; every fifth date explores another hour.</p></section><section class="settings-section"><h2>Keep the footage going</h2>${toggle('conserve','Automatically conserve footage','Use the lighter rhythm below when stock is low or a footage break would exhaust the library.')}<div class="form-row">${field('conserve_every_days','Conservation: post every N days','number','','min="2" max="14"')}${field('conserve_main_per_day','Grid Reels per active day','number','Never exceeds your normal daily count.','min="1" max="10"')}</div>${field('conserve_trials_per_day','Trial Reels per active day','number','Never exceeds your normal daily count.','min="1" max="10"')}${field('low_watermark','Low-footage threshold (clips)','number','','min="0" max="365"')}${field('drought_until','No new footage until','date','Clear this date when you are filming again. If your conservation pace still cannot cover the gap, you get an alert.')}<div class="form-row">${field('ntfy_url','ntfy server','url')}${field('ntfy_topic','ntfy topic','text','Use a private topic or an authenticated server.')}</div><label>ntfy access token<input name="ntfy_token" type="password" autocomplete="new-password" placeholder="${state.ntfy_token_set?'Token stored. Leave blank to keep it.':'Optional bearer token'}"></label><button type="button" class="button small" data-action="notify">Send a test notification</button><p class="help">Save changes before testing. Low stock alerts are limited to once per day, failures have durable delivery retries, and post problems include a recovery prompt.</p></section></div></div><div class="save-bar"><button class="primary" type="submit">Save settings ${icon('check')}</button><span>Changes apply to the future queue. Pinned times stay in place.</span></div></form>`;}
function bindTray(){
 const tray=$('.schedule-tray');if(!tray)return;
 loadTray();
 let timer;
 tray.addEventListener('input',e=>{if(e.target.id!=='tray-search')return;trayQuery=e.target.value;clearTimeout(timer);timer=setTimeout(loadTray,250);});
 tray.addEventListener('click',async e=>{
  const b=e.target.closest('button');if(!b)return;
  if(b.dataset.trayTab){trayTab=b.dataset.trayTab;tray.outerHTML=scheduleTray();icons();bindTray();return;}
  if(b.dataset.releaseKind){await busy(b,async()=>{await api('/api/schedule/release','POST',{kind:b.dataset.releaseKind,id:b.dataset.releaseId});toast('Handed back to the automatic schedule.');await refresh();});}
 });
}
function bindForms(){
 bindCaptionTools();
 bindCollaborators();
 bindTray();
 bindMulticam();
 bindTagLibrary();
 $('.sets-panel')?.addEventListener('toggle',e=>{setsOpen=e.currentTarget.open;});
 document.querySelectorAll('[data-set-kind]').forEach(select=>select.addEventListener('change',async()=>{select.disabled=true;try{await api('/api/batches/'+encodeURIComponent(select.dataset.setKind),'PATCH',{kind:select.value});toast(`Set marked as ${KINDS[select.value]}. The queue has been rebuilt.`);await refresh();}catch(e){toast(e.message,true);select.disabled=false;}}));
 document.querySelectorAll('[data-set-mode]').forEach(select=>select.addEventListener('change',async()=>{select.disabled=true;try{await api('/api/batches/'+encodeURIComponent(select.dataset.setMode),'PATCH',{mode:select.value});toast(select.value==='next'?'This set posts next, in order. The queue has been rebuilt.':'This set is mixed into the queue.');await refresh();}catch(e){toast(e.message,true);select.disabled=false;}}));
 const scheduleForm=$('#schedule-form');if(scheduleForm){scheduleForm.addEventListener('input',()=>{scheduleDraft=Object.fromEntries(['preview_days','trials_per_day','main_per_day'].map(k=>[k,scheduleForm.elements.namedItem(k).value]));});scheduleForm.addEventListener('submit',async e=>{e.preventDefault();const form=e.currentTarget;await busy(e.submitter,async()=>{const values=Object.fromEntries(['preview_days','trials_per_day','main_per_day'].map(k=>[k,Number(form.elements.namedItem(k).value)]));await api('/api/settings','PATCH',{values});scheduleDraft=null;toast('Schedule rebuilt. Publishing controls are unchanged.');await refresh();});});}
 const form=$('#settings-form');if(form)form.addEventListener('submit',async e=>{e.preventDefault();await busy(e.submitter,async()=>{const fd=new FormData(form);const values={};Object.keys(state.settings).forEach(k=>{if(['schedule_hours','content_mix','hashtag_groups','collab_accounts'].includes(k))return;const el=form.elements.namedItem(k);if(!el)return;const type=typeof state.settings[k];values[k]=type==='boolean'?el.checked:type==='number'?Number(el.value):el.value;});values.schedule_hours=Array.from({length:7},(_,i)=>Number(fd.get('hour_'+i)));if(tagGroups)values.hashtag_groups=tagGroups;if(collabDraft)values.collab_accounts=collabDraft;values.content_mix=Object.fromEntries(['talking','bts','promo'].map(kind=>[kind,{lanes:form.querySelector(`[data-mix="${kind}"][data-mix-field="lanes"]`).value,every:Number(form.querySelector(`[data-mix="${kind}"][data-mix-field="every"]`).value)}]));await api('/api/settings','PATCH',{values});tagsDirty=false;collabDraft=null;const ai_key=fd.get('ai_key'),ntfy_token=fd.get('ntfy_token');if(ai_key||ntfy_token)await api('/api/keys','POST',{ai_key,ntfy_token});scheduleDraft=null;toast('Settings saved. The future queue has been updated.');await refresh();});});
 const zone=$('#drop-zone');if(zone){zone.addEventListener('dragover',e=>{e.preventDefault();zone.classList.add('dragging');});zone.addEventListener('dragleave',()=>zone.classList.remove('dragging'));zone.addEventListener('drop',e=>{e.preventDefault();zone.classList.remove('dragging');uploadDialog(e.dataTransfer.files);});}
}
async function busy(button,fn){if(button)button.disabled=true;try{await fn();}catch(e){toast(e.message,true);}finally{if(button)button.disabled=false;}}
function uploadDialog(files){
 const list=[...files];if(!list.length)return;
 $('#editor-title').textContent='Add footage';
 $('#editor-body').innerHTML=`<form id="upload-form"><p class="editor-name">${list.length} video${list.length===1?'':'s'} · ${(bytes=>bytes>=1e9?(bytes/1e9).toFixed(2)+' GB':Math.max(1,Math.round(bytes/1e6))+' MB')(list.reduce((n,f)=>n+f.size,0))}</p>
 <label class="field">Set name<input name="batch_name" maxlength="80" placeholder="e.g. Mojo's · 14 Nov"><span>Optional. Helps you find this set in the library.</span></label>
 <label class="field">What’s in it?<select name="kind">${kindOptions('performance')}</select><span>Talking, behind-the-scenes and promo clips are sprinkled between DJ clips. Set the spacing in Settings → Content mix.</span></label>
 <fieldset class="field choice"><legend>How should this set post?</legend>
 <label><input type="radio" name="mode" value="mix" checked><span><strong>Mix it in (recommended)</strong>Spread these clips through the queue alongside earlier sets, so followers get a little of each night.</span></label>
 <label><input type="radio" name="mode" value="next"><span><strong>Keep together, post next</strong>Post this set before everything else, in filename order. Good for promoting a recent gig.</span></label></fieldset>
 <div class="drawer-actions"><button class="primary" type="submit">${icon('upload')}Upload ${list.length===1?'video':list.length+' videos'}</button></div></form>`;
 $('#upload-form').addEventListener('submit',e=>{e.preventDefault();const form=e.currentTarget;$('#editor').close();uploadFiles(list,'instagram',{name:form.elements.batch_name.value.trim(),mode:form.elements.mode.value,kind:form.elements.kind.value});});
 $('#editor').showModal();
}
async function uploadFiles(files,destination='instagram',set=null){const batch=destination==='instagram'?Array.from(crypto.getRandomValues(new Uint8Array(10)),b=>b.toString(16).padStart(2,'0')).join(''):'';const query=destination==='youtube'?'?destination=youtube':`?batch=${batch}&mode=${set?.mode||'mix'}&kind=${set?.kind||'performance'}&batch_name=${encodeURIComponent(set?.name||'')}`;for(let i=0;i<files.length;i++){const f=files[i];toast(`Uploading ${i+1}/${files.length}: ${f.name}`);const fd=new FormData();fd.append('file',f);try{await api('/api/upload'+query,'POST',fd);}catch(e){toast(e.message,true);return;}}toast(destination==='youtube'?'Set received. It will appear after intake.':'Footage uploaded. The worker will prepare settled files on its next pass.');location.hash=destination==='youtube'?'youtube':'library';await refresh();}
function chooseUpload(destination='instagram'){let input=document.createElement('input');input.type='file';input.accept='.mp4,.mov,.m4v,.mkv,.webm';input.multiple=true;input.addEventListener('change',()=>destination==='instagram'?uploadDialog(input.files):uploadFiles(input.files,destination));input.click();}
function postNowControl(job){
 if(postingNow(job))return '<p class="notice post-now-tools">Post now requested. Check Activity for the result.</p>';
 if(!['queued','retry'].includes(job.status))return '';
 const hint=state.settings.dry_run?'Turn off Dry run to post live.':'Send this Reel with its saved caption. The automatic queue fills the freed slot.';
 return `<div class="post-now-tools"><button class="primary" type="button" data-post-now="${job.id}" ${state.settings.dry_run?'disabled':''}>Post now</button><p class="help">${hint}</p></div>`;
}
function publishedTools(job){
 if(job.status!=='published')return '';
 const url=job.permalink|| (state.account?.username?`https://www.instagram.com/${encodeURIComponent(state.account.username)}/reels/`:'https://www.instagram.com/');
 return `<div class="post-now-tools">
  <a class="button" href="${safeLink(url)}" target="_blank" rel="noopener noreferrer">Open on Instagram ${icon('external')}</a>
  <details class="batch-guidance">
   <summary>Remove & repost</summary>
   <p class="help">Delete the Reel on Instagram, or archive it if Instagram offers that option. Then confirm below to put it back in the queue for review.</p>
   <button class="button" type="button" data-job-action="confirm_removed" data-id="${job.id}">I removed it — requeue</button>
  </details>
 </div>`;
}
async function openJob(id){const job=records.find(r=>r.id===Number(id));if(!job)return;
 $('#editor-title').textContent=job.status==='published'?'Published Reel':'Review Reel';
 const editable=['queued','retry','failed'].includes(job.status);const local=job.scheduled_at?new Date(job.scheduled_at*1000):null;const localValue=local?new Date(local.getTime()-local.getTimezoneOffset()*60000).toISOString().slice(0,16):'';
 $('#editor-body').innerHTML=`<video class="editor-video" src="/api/preview/${job.clip_id}?video=true" poster="/api/preview/${job.clip_id}" controls playsinline preload="metadata"></video><p class="editor-name">${esc(job.name)} · ${laneTag(job.lane)} · Post #${job.id}</p><form id="job-form"><label class="field">Caption<textarea name="caption" maxlength="2200" ${editable?'':'readonly'}>${esc(job.caption)}</textarea></label>${editable?`<label class="field">Posting time (${esc(Intl.DateTimeFormat().resolvedOptions().timeZone)})<input type="datetime-local" name="scheduled_at" value="${localValue}"><span>Changing this pins the time. The queue displays ${esc(state.settings.timezone)}.</span></label><label class="toggle field"><span>Approved for automatic posting</span><input type="checkbox" name="approved" ${job.approved?'checked':''}></label><label class="toggle field"><span>Keep this posting time pinned</span><input type="checkbox" name="pinned" ${job.pinned?'checked':''}></label><div class="drawer-actions"><button class="primary" type="submit">Save changes</button><button class="button danger" type="button" data-job-action="reject" data-id="${job.id}">Reject post</button></div>`:''}${job.error?`<p class="editor-error">${esc(job.error)}</p>`:''}${job.reason?`<p class="editor-help">${esc(job.reason)}</p>`:''}</form>${postNowControl(job)}${job.status==='failed'?`<div class="drawer-actions"><button class="button" data-job-action="retry" data-id="${job.id}">Retry after fixing</button></div>`:''}${job.status==='rejected'?`<button class="button" data-job-action="restore" data-id="${job.id}">Restore to queue</button>`:''}${job.status==='uncertain'?`<div class="notice"><p>Check the Instagram account before choosing. These actions resolve the duplicate-prevention hold.</p></div><label class="field">Instagram media ID (optional)<input id="resolved-media-id" inputmode="numeric"></label><div class="inline-actions"><button class="button" data-job-action="confirm_posted" data-id="${job.id}">I confirmed it posted</button><button class="button danger" data-job-action="confirm_not_posted" data-id="${job.id}">I confirmed it did not post</button></div>`:''}${publishedTools(job)}${job.status==='published'||job.collaborators?`<section class="clip-history" id="job-extras">${job.collaborators?`<h3>Collaborators</h3><p class="help">Invited: ${esc(job.collaborators.split(',').map(u=>'@'+u).join(', '))}</p>`:''}</section>`:''}`;
 if(job.status==='published')api(`/api/jobs/${job.id}/collaborators`).then(({collaborators,story})=>{const box=$('#job-extras');if(!box)return;
  const people=collaborators.length?`<h3>Collaborators</h3><ul>${collaborators.map(c=>`<li><strong>@${esc(c.username)}</strong> <span>${c.status==='accepted'?`Accepted ${day(c.accepted_at)} ${clock(c.accepted_at)}`:c.status==='pending'?`Accepts ${day(c.due_at)} ${clock(c.due_at)}`:c.status==='manual'?esc(c.error||'Accept in the Instagram app'):esc('Not accepted: '+c.error)}</span></li>`).join('')}</ul>`:box.innerHTML;
  const storyLine=story?`<h3>Story</h3><p class="help">${story.status==='published'?'Shared to Stories.':story.status==='failed'?esc('Not shared: '+story.error):'Sharing to Stories…'}</p>`:'';
  box.innerHTML=people+storyLine;}).catch(()=>{});
 $('#job-form')?.addEventListener('submit',async e=>{e.preventDefault();const form=e.currentTarget;await busy(e.submitter,async()=>{const fd=new FormData(form);let data={caption:fd.get('caption'),approved:form.elements.approved.checked,pinned:form.elements.pinned.checked};if(fd.get('scheduled_at')!==localValue&&fd.get('scheduled_at'))data.scheduled_at=new Date(fd.get('scheduled_at')).getTime()/1000;await api('/api/jobs/'+job.id,'PATCH',data);$('#editor').close();toast('Post updated.');await refresh();});});
 $('#editor').showModal();
}
function clipExport(clip){
 if(!canRewrite(clip))return `<div class="empty-state">${icon('film')}${clip.rebuilding?'<p>Rebuilding from the original footage. Check back when the export is ready.</p>':''}</div>`;
 const url='/api/preview/'+encodeURIComponent(clip.id);
 return `<video class="editor-video clip-export" src="${url}?video=true" poster="${url}" controls playsinline preload="metadata"></video>
 <div class="clip-export-tools"><a class="button small" href="${url}?video=true" download="${esc(clip.name.replace(/\.[^.]+$/, '')+'.mp4')}">Download export</a><p class="help" id="clip-export-size">Video file sent to Instagram</p></div>`;
}
function coverControls(clip){
 const mode=state.settings.cover_mode;
 const automatic=mode==='color'?clip.cover_color_ms:mode==='auto'?clip.cover_auto_ms:null;
 const offset=clip.cover_manual_ms??automatic;
 const label=clip.cover_manual_ms!=null?'Selected frame':offset!=null?'Automatic cover':mode==='instagram'?'Instagram chooses the cover':'Automatic cover pending';
 return `<section class="clip-cover" aria-labelledby="clip-cover-heading">
 <h3 id="clip-cover-heading">Reel cover</h3>
 ${offset!=null?`<img id="clip-cover-preview" src="/api/clips/${clip.id}/cover" alt="Saved Reel cover frame">`:''}
 <p id="clip-cover-label" class="help" role="status">${label}${offset!=null?` · ${(offset/1000).toFixed(2)}s`:''}</p>
 <div class="inline-actions"><button type="button" class="button small" id="clip-cover-pick" disabled>Use current frame</button>
 <button type="button" class="button small" id="clip-cover-reset" ${clip.cover_manual_ms==null?'disabled':''}>Use cover setting</button></div>
 <p class="help">Scrub the video to pick a frame, then save footage.</p></section>`;
}
const fmtTime=t=>`${Math.floor(t/60)}:${String(Math.floor(t%60)).padStart(2,'0')}.${Math.floor((t%1)*10)}`;
let mcData=null,mcMix=null,mcCams=[],mcMain=0,mcBusy=false,mcPoll=null;
const AUDIO_EXT=/\.(wav|aiff?|flac|mp3|m4a)$/i,VIDEO_EXT=/\.(mp4|mov)$/i;
const fmtSize=b=>b>=1e9?(b/1e9).toFixed(1)+' GB':Math.max(1,Math.round(b/1e6))+' MB';
const fileName=f=>f.file?f.file.name:f.source;
const mcActive=j=>['queued','running','cancelling'].includes(j.status);
function mcInputs(){
 const cam=(c,i)=>`<li class="mc-cam"><label class="mc-main" title="Main camera"><input type="radio" name="mc-main" value="${i}" ${mcMain===i?'checked':''}>Main</label><input class="mc-label" data-mc-label="${i}" maxlength="40" value="${esc(c.label)}" aria-label="Name for camera ${i+1}"><span class="mc-file">${esc(fileName(c))}${c.size?` · ${fmtSize(c.size)}`:''}</span><button type="button" class="icon-button" data-mc-remove="cam:${i}" aria-label="Remove camera ${i+1}">${icon('x')}</button></li>`;
 return `<div class="mc-slot"><strong>Mix</strong>${mcMix?`<span class="mc-file">${esc(fileName(mcMix))}${mcMix.size?` · ${fmtSize(mcMix.size)}`:''}</span><button type="button" class="icon-button" data-mc-remove="mix" aria-label="Remove the mix">${icon('x')}</button>`:'<span class="help">Add the audio of the set: the desk recording or a bounce.</span>'}</div>
 <div class="mc-slot mc-cams"><strong>Cameras</strong>${mcCams.length?`<ol>${mcCams.map(cam).join('')}</ol>`:'<span class="help">Add 1 to 8 camera videos (MP4 or MOV). They sync themselves to the mix, so start times don’t matter.</span>'}</div>`;
}
function mcJob(j){
 const r=j.result||{};const active=mcActive(j);
 const tone=j.status==='done'?'green':['failed','interrupted'].includes(j.status)?'rust':'';
 const label={draft:'Uploading',queued:'Queued',running:'Working',cancelling:'Cancelling',done:'Finished',failed:'Failed',interrupted:'Interrupted',cancelled:'Cancelled'}[j.status]||j.status;
 const facts=[r.bpm?`${Math.round(r.bpm)} BPM`:'',r.drops?`${r.drops} drop${r.drops===1?'':'s'}`:'',r.clips?.length?`${r.clips.length} clip${r.clips.length===1?'':'s'}`:''].filter(Boolean).join(' · ');
 return `<article class="mc-job" data-mc-job="${j.id}"><div class="mc-job-head"><h3>${esc(j.name)}</h3>${badge(label,tone)}</div>
 ${active?`<p class="help">${esc(j.stage)}</p><progress max="1" value="${j.progress}"></progress><p class="help">${Math.round(j.progress*100)}%</p>`:''}
 ${j.message?`<p class="${['failed','interrupted'].includes(j.status)?'editor-error':'help'}">${esc(j.message)}</p>`:''}
 ${facts?`<p class="help">${esc(facts)}</p>`:''}${r.guide?`<p class="help">AI: ${esc(r.guide)}</p>`:''}
 ${r.clips?.length?`<ul class="mc-clips">${r.clips.map(c=>`<li>${esc(c.title||'Clip')} <span>${Math.round(c.duration||0)}s${c.look&&c.look!=='none'?' · '+esc(c.look):''}</span></li>`).join('')}</ul>`:''}
 <div class="inline-actions">${active?`<button type="button" class="text-button" data-mc-action="cancel" data-id="${j.id}">Cancel</button>`:''}${['failed','interrupted','cancelled'].includes(j.status)?`<button type="button" class="button small" data-mc-action="retry" data-id="${j.id}">Retry</button>`:''}${r.full&&!r.youtube&&j.status==='done'?`<a class="button small" href="/api/multicam/jobs/${j.id}/full" download>${icon('upload')}Download full edit</a>`:''}${r.queued?`<a class="text-button" href="#library">See clips in the library</a>`:''}${r.youtube?`<a class="text-button" href="#youtube">Open YouTube</a>`:''}${!active?`<button type="button" class="text-button" data-mc-action="delete" data-id="${j.id}">Delete</button>`:''}</div>
 ${j.log?.length?`<details class="mc-log"><summary>Engine log</summary><pre>${esc(j.log.join('\n'))}</pre></details>`:''}</article>`;
}
function mcJobs(data){return data.jobs.length?data.jobs.map(mcJob).join(''):'<p class="help">Finished edits and their progress appear here.</p>';}
function multicamView(data){
 const sources=data.sources||[];
 const choice=(name,options,checked)=>options.map(([v,l,h])=>`<label><input type="radio" name="${name}" value="${v}" ${v===checked?'checked':''}><span><strong>${l}</strong>${h?esc(h):''}</span></label>`).join('');
 return header('Multicam editor','Upload the mix and your camera angles. Reelflow syncs them, cuts on the music, and hands you a full edit plus social clips.')+`<div class="mc-layout">
 <form id="mc-form" class="mc-form">
 <section class="mc-step"><h2><span>1</span>The set</h2><label class="field">Name<input name="name" maxlength="80" placeholder="e.g. Mojo’s · 14 Nov" value="${esc(mcData?.draftName||'')}"></label></section>
 <section class="mc-step"><h2><span>2</span>Mix and cameras</h2>
 <div class="mc-drop" id="mc-drop">${icon('upload')}<p><strong>Drop the mix and camera videos here</strong><br>Audio files become the mix; MP4 and MOV files become cameras.</p><button type="button" class="button small" data-mc-pick>Choose files</button><input type="file" id="mc-files" multiple accept=".wav,.aif,.aiff,.flac,.mp3,.m4a,.mp4,.mov" hidden></div>
 ${sources.length?`<details class="mc-sources"><summary>Use files already on the server (${sources.length})</summary><ul>${sources.map(f=>`<li><span>${esc(f.path)} · ${fmtSize(f.size)}</span><button type="button" class="text-button" data-mc-source="${esc(f.path)}" data-kind="${f.kind}">${f.kind==='audio'?'Use as the mix':'Add as a camera'}</button></li>`).join('')}</ul></details>`:`<p class="help">Huge files? Copy them into <code>${esc(data.source_folder)}</code> on the server and pick them here instead of uploading.</p>`}
 <div id="mc-inputs">${mcInputs()}</div></section>
 <section class="mc-step"><h2><span>3</span>What to make</h2>
 <label class="toggle field"><span>Full edit (16:9)</span><input type="checkbox" name="full" checked></label>
 <label class="toggle field"><span>Send it to YouTube as a draft when it’s 20–30 minutes</span><input type="checkbox" name="youtube" checked></label>
 <label class="field">Social clips (9:16)<select name="clips">${[[12,'12 clips'],[6,'6 clips'],[20,'20 clips'],[30,'30 clips'],[0,'As many as the music offers'],[-1,'None']].map(([v,l])=>`<option value="${v}">${l}</option>`).join('')}</select><span>Whole musical phrases: build-up and drop, drop, groove and breakdown moments that start on a bar line and land on the downbeat.</span></label>
 <label class="toggle field"><span>Add the clips to the Instagram queue</span><input type="checkbox" name="queue" checked></label>
 <label class="field">In the queue<select name="mode"><option value="mix">Mix them in with everything else</option><option value="next">Keep together, post next</option></select></label></section>
 <section class="mc-step"><h2><span>4</span>Style</h2>
 <fieldset class="field choice compact"><legend>Pace</legend>${choice('pace',[['chilled','Chilled','Longer holds, fewer cuts.'],['balanced','Balanced','Follows the music: quick in drops, long in breakdowns.'],['energetic','Energetic','More cuts, still on the beat.']],'balanced')}</fieldset>
 <fieldset class="field choice compact"><legend>Looks on the clips</legend>${choice('looks',[['auto','Mix it up','About a third in black and white, the rest rotating warm, vivid, vintage and film looks.'],['off','Natural colour','']],'auto')}</fieldset>
 <label class="field">Quality<select name="quality"><option value="1080">1080p · fastest</option><option value="2160">4K · slower, for big screens</option></select></label>
 <label class="field">Notes for the AI (optional)<textarea name="notes" maxlength="2000" rows="3" placeholder="e.g. The big drop is at 42:10. Skip the first five minutes. Stay on the booth camera from 1:05:00 to 1:06:30."></textarea><span>Your caption AI turns these into settings: drops, parts to keep out of clips and camera preferences. The engine still makes every cut on the music. Facts here also go to the clip captions.</span></label></section>
 <div class="drawer-actions"><button class="primary" type="submit" id="mc-start">${icon('spark')}Make the edit</button></div>
 <progress id="mc-upload" max="1" value="0" hidden></progress><p id="mc-status" class="help" role="status"></p>
 </form>
 <aside class="mc-side"><h2>Edits</h2><div id="mc-jobs">${mcJobs(data)}</div>
 <section class="side-section"><h3>How it cuts</h3><p>Every camera syncs to the mix by its sound. Cuts land on bar lines and phrase starts, always on a drop’s first downbeat: quick through drops, building through build-ups, long in breakdowns, and never a rigid back-and-forth.</p><p class="help">Each clip opens on a different camera, so two clips from one song never look the same.</p></section></aside></div>`;
}
function mcAddFiles(files){
 let skipped=0;
 for(const file of files){
  if(AUDIO_EXT.test(file.name))mcMix={file,size:file.size};
  else if(VIDEO_EXT.test(file.name)&&mcCams.length<8)mcCams.push({file,size:file.size,label:'Camera '+String.fromCharCode(65+mcCams.length)});
  else skipped++;
 }
 if(skipped)toast(`Skipped ${skipped} file${skipped===1?'':'s'}: use WAV, AIFF, FLAC, MP3 or M4A for the mix and MP4 or MOV for cameras (up to 8).`,true);
 $('#mc-inputs').innerHTML=mcInputs();icons();
}
async function mcUpload(jobId,file,onProgress){
 const chunk=mcData.chunk||64*1024*1024;let offset=0,name=file.name;
 while(true){
  const end=Math.min(file.size,offset+chunk),final=end>=file.size;let tries=0,res;
  for(;;){
   try{
    const r=await fetch(`/api/multicam/jobs/${jobId}/upload?name=${encodeURIComponent(file.name)}&offset=${offset}&final=${final}`,{method:'POST',headers:{'X-CSRF-Token':state.csrf,'Content-Type':'application/octet-stream'},body:file.slice(offset,end)});
    res=await r.json().catch(()=>({detail:`Upload failed (HTTP ${r.status}).`}));
    if(!r.ok)throw new Error(typeof res.detail==='string'?res.detail:'Upload failed.');
    break;
   }catch(e){if(++tries>=5)throw e;await new Promise(r=>setTimeout(r,2000*tries));}
  }
  if(res.resend_from!==undefined){offset=res.resend_from;continue;}
  offset=end;onProgress(offset);if(res.name)name=res.name;
  if(final)return name;
 }
}
async function mcSubmit(form){
 if(mcBusy)return;
 if(!mcMix){toast('Add the mix (an audio file).',true);return;}
 if(!mcCams.length){toast('Add at least one camera video.',true);return;}
 const fd=new FormData(form);const clips=Number(fd.get('clips'));
 if(!form.elements.full.checked&&clips<0){toast('Make a full edit, social clips or both.',true);return;}
 mcBusy=true;$('#mc-start').disabled=true;const bar=$('#mc-upload'),status=$('#mc-status');
 try{
  const {id}=await api('/api/multicam/jobs','POST',{name:fd.get('name')||''});
  const uploads=[mcMix,...mcCams].filter(x=>x.file);const total=uploads.reduce((n,x)=>n+x.file.size,0)||1;let done=0;
  bar.hidden=!uploads.length;
  for(const item of uploads){
   status.textContent=`Uploading ${item.file.name}…`;
   const base=done;item.upload=await mcUpload(id,item.file,sent=>{bar.value=(base+sent)/total;status.textContent=`Uploading ${item.file.name} · ${Math.round(bar.value*100)}%`;});
   done+=item.file.size;
  }
  const ref=x=>x.file?{upload:x.upload}:{source:x.source};
  status.textContent='Starting the edit…';
  await api(`/api/multicam/jobs/${id}/start`,'POST',{audio:ref(mcMix),cameras:mcCams.map(cam=>({...ref(cam),label:cam.label})),main:Math.min(mcMain,mcCams.length-1),
   clips,full:form.elements.full.checked,youtube:form.elements.youtube.checked,queue:form.elements.queue.checked,mode:fd.get('mode'),
   pace:fd.get('pace'),looks:fd.get('looks'),quality:fd.get('quality'),notes:fd.get('notes')||''});
  mcMix=null;mcCams=[];mcMain=0;status.textContent='';bar.hidden=true;
  toast('Edit started. You can leave this page; it keeps going on the server.');
  await render();
 }catch(e){status.textContent=e.message;toast(e.message,true);}
 finally{mcBusy=false;const b=$('#mc-start');if(b)b.disabled=false;}
}
function bindMulticam(){
 const form=$('#mc-form');if(!form)return;
 const input=$('#mc-files'),zone=$('#mc-drop');
 input.addEventListener('change',()=>{mcAddFiles(input.files);input.value='';});
 zone.addEventListener('dragover',e=>{e.preventDefault();zone.classList.add('dragging');});
 zone.addEventListener('dragleave',()=>zone.classList.remove('dragging'));
 zone.addEventListener('drop',e=>{e.preventDefault();zone.classList.remove('dragging');mcAddFiles(e.dataTransfer.files);});
 form.addEventListener('input',e=>{if(e.target.dataset.mcLabel!==undefined)mcCams[Number(e.target.dataset.mcLabel)].label=e.target.value;if(e.target.name==='name')mcData.draftName=e.target.value;});
 form.addEventListener('change',e=>{if(e.target.name==='mc-main')mcMain=Number(e.target.value);});
 form.addEventListener('submit',e=>{e.preventDefault();mcSubmit(form);});
 form.addEventListener('click',e=>{
  const b=e.target.closest('button');if(!b)return;
  if(b.dataset.mcPick!==undefined){input.click();return;}
  if(b.dataset.mcRemove){const [kind,i]=b.dataset.mcRemove.split(':');if(kind==='mix')mcMix=null;else{mcCams.splice(Number(i),1);if(mcMain>=mcCams.length)mcMain=0;}$('#mc-inputs').innerHTML=mcInputs();icons();return;}
  if(b.dataset.mcSource){const path=b.dataset.mcSource;if(b.dataset.kind==='audio')mcMix={source:path};else if(mcCams.length<8&&!mcCams.some(c=>c.source===path))mcCams.push({source:path,label:'Camera '+String.fromCharCode(65+mcCams.length)});$('#mc-inputs').innerHTML=mcInputs();icons();}
 });
 clearInterval(mcPoll);
 mcPoll=setInterval(async()=>{
  if(currentPage!=='multicam'||!$('#mc-jobs')){clearInterval(mcPoll);return;}
  if(!mcData?.jobs.some(mcActive))return;
  try{const data=await api('/api/multicam');mcData={...data,draftName:mcData.draftName};const open=[...document.querySelectorAll('.mc-log[open]')].map(d=>d.closest('[data-mc-job]').dataset.mcJob);$('#mc-jobs').innerHTML=mcJobs(data);open.forEach(id=>{const d=document.querySelector(`[data-mc-job="${id}"] .mc-log`);if(d)d.open=true;});icons();}catch{}
 },3000);
}
async function openEditTool(id){
 let clip=records.find(r=>r.id===id);if(!clip)[clip]=await api('/api/clips?only='+encodeURIComponent(id)+'&limit=1');if(!clip)return;
 const url='/api/preview/'+encodeURIComponent(id);
 $('#editor-title').textContent='Funny edit';
 $('#editor-body').innerHTML=`<p class="editor-name">${esc(clip.name)}</p>
 <div class="edit-stage"><video id="edit-video" src="${url}?video=true" poster="${url}" playsinline preload="auto"></video><canvas id="edit-canvas" aria-label="Video frame. Drag a box around the person to follow."></canvas></div>
 <div class="edit-transport"><button type="button" class="icon-button" id="edit-play" aria-label="Play">${icon('play')}</button><input type="range" id="edit-seek" min="0" max="${clip.duration}" step="0.033" value="0" aria-label="Position in the clip"><span id="edit-time" class="help">0:00.0</span></div>
 <p class="help" id="edit-hint" role="status">Pause on the moment, then drag a box around the person’s head. Tap a dashed box to use a detected face.</p>
 <form id="edit-form">
 <fieldset class="field edit-effects"><legend>Effects</legend>
 ${[['arrow','Arrow',true],['circle','Circle',true],['spotlight','Spotlight',false],['zoom','Zoom punch-in',false]].map(([v,l,on])=>`<label><input type="checkbox" name="effects" value="${v}" ${on?'checked':''}>${l}</label>`).join('')}
 </fieldset>
 <label class="field">Label<input name="label" maxlength="40" placeholder="e.g. this guy"><span>Optional. Shown by the arrow in bold capitals.</span></label>
 <label class="field">How long<select name="effect_length"><option value="3">3 seconds</option><option value="5" selected>5 seconds</option><option value="8">8 seconds</option><option value="12">12 seconds</option><option value="0">Until the end</option></select></label>
 <label class="toggle field"><span>Replace the original in the queue</span><input type="checkbox" name="replace" checked></label><p class="help">The original is excluded from future posting. Its file and history stay.</p>
 <div class="drawer-actions"><button class="primary" type="submit" id="edit-render" disabled>${icon('spark')}Render edit</button><button class="button" type="button" data-open-clip="${clip.id}">Back</button></div>
 <progress id="edit-progress" max="1" value="0" hidden></progress>
 </form>`;
 const video=$('#edit-video'),canvas=$('#edit-canvas'),seek=$('#edit-seek'),hint=$('#edit-hint'),render=$('#edit-render'),play=$('#edit-play');
 let box=null,boxAt=0,faces=[],drag=null,faceTimer;
 const rect=()=>{const w=video.clientWidth,h=video.clientHeight,vw=video.videoWidth||9,vh=video.videoHeight||16;const s=Math.min(w/vw,h/vh);return {x:(w-vw*s)/2,y:(h-vh*s)/2,w:vw*s,h:vh*s};};
 const toFrame=(px,py)=>{const r=rect();return {x:Math.min(1,Math.max(0,(px-r.x)/r.w)),y:Math.min(1,Math.max(0,(py-r.y)/r.h))};};
 function paint(){
  const dpr=window.devicePixelRatio||1;canvas.width=video.clientWidth*dpr;canvas.height=video.clientHeight*dpr;
  const g=canvas.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);g.clearRect(0,0,canvas.width,canvas.height);const r=rect();
  const outline=(b,style,dash)=>{g.setLineDash(dash);g.lineWidth=2.5;g.strokeStyle=style;g.strokeRect(r.x+b.x*r.w,r.y+b.y*r.h,b.w*r.w,b.h*r.h);};
  if(!box)faces.forEach(f=>outline(f,'rgba(255,255,255,.9)',[6,5]));
  if(box){g.fillStyle='rgba(164,52,33,.18)';g.fillRect(r.x+box.x*r.w,r.y+box.y*r.h,box.w*r.w,box.h*r.h);outline(box,'#e8382b',[]);}
 }
 function setBox(b){box=b;boxAt=video.currentTime;render.disabled=!box;hint.textContent=box?`Following from ${fmtTime(boxAt)}. Drag again to redo, or render.`:'Pause on the moment, then drag a box around the person’s head. Tap a dashed box to use a detected face.';paint();}
 async function findFaces(){
  faces=[];paint();if(!video.paused)return;
  try{const t=video.currentTime;const data=await api(`/api/clips/${encodeURIComponent(id)}/faces?t=${t.toFixed(2)}`);if(Math.abs(video.currentTime-t)<.05&&video.paused){faces=data.faces;if(!box&&faces.length)hint.textContent=`${faces.length} face${faces.length===1?'':'s'} found. Tap one, or drag your own box.`;paint();}}catch{}
 }
 const moved=()=>{seek.value=video.currentTime;$('#edit-time').textContent=fmtTime(video.currentTime);if(box&&Math.abs(video.currentTime-boxAt)>.04)setBox(null);};
 video.addEventListener('timeupdate',moved);video.addEventListener('seeked',()=>{moved();clearTimeout(faceTimer);faceTimer=setTimeout(findFaces,250);});
 video.addEventListener('loadeddata',()=>{paint();findFaces();});video.addEventListener('pause',()=>{play.innerHTML=icon('play');play.setAttribute('aria-label','Play');findFaces();});
 video.addEventListener('play',()=>{play.innerHTML=icon('pause');play.setAttribute('aria-label','Pause');faces=[];setBox(null);});
 play.addEventListener('click',()=>video.paused?video.play():video.pause());
 seek.addEventListener('input',()=>{video.pause();video.currentTime=Number(seek.value);});
 window.addEventListener('resize',paint);
 canvas.addEventListener('pointerdown',e=>{video.pause();const b=canvas.getBoundingClientRect();drag={start:toFrame(e.clientX-b.left,e.clientY-b.top)};drag.end=drag.start;canvas.setPointerCapture(e.pointerId);});
 canvas.addEventListener('pointermove',e=>{if(!drag)return;const b=canvas.getBoundingClientRect();drag.end=toFrame(e.clientX-b.left,e.clientY-b.top);const a=drag.start,z=drag.end;box={x:Math.min(a.x,z.x),y:Math.min(a.y,z.y),w:Math.abs(z.x-a.x),h:Math.abs(z.y-a.y)};paint();});
 canvas.addEventListener('pointerup',()=>{if(!drag)return;const a=drag.start,z=drag.end;drag=null;const r=rect();
  if(Math.abs(z.x-a.x)*r.w<12&&Math.abs(z.y-a.y)*r.h<12){
   const hit=faces.find(f=>a.x>=f.x&&a.x<=f.x+f.w&&a.y>=f.y&&a.y<=f.y+f.h);
   if(hit)return setBox({...hit});
   const side=Math.min(r.w,r.h)*.18,w=side/r.w,h=side/r.h;return setBox({x:Math.min(Math.max(0,a.x-w/2),1-w),y:Math.min(Math.max(0,a.y-h/2),1-h),w,h});
  }
  setBox({x:Math.min(a.x,z.x),y:Math.min(a.y,z.y),w:Math.max(.02,Math.abs(z.x-a.x)),h:Math.max(.02,Math.abs(z.y-a.y))});
 });
 canvas.addEventListener('keydown',e=>{if(e.key===' '){e.preventDefault();play.click();}});
 $('#edit-form').addEventListener('submit',async e=>{
  e.preventDefault();const form=e.currentTarget;const effects=[...form.querySelectorAll('[name=effects]:checked')].map(x=>x.value);
  if(!box){toast('Mark the person first.',true);return;}if(!effects.length){toast('Choose at least one effect.',true);return;}
  const progress=$('#edit-progress');
  await busy(e.submitter,async()=>{
   const {id:editId}=await api(`/api/clips/${encodeURIComponent(id)}/edits`,'POST',{start:boxAt,box:{x:box.x,y:box.y,w:Math.min(box.w,1-box.x),h:Math.min(box.h,1-box.y)},effects,label:form.elements.label.value.trim(),length:Number(form.elements.effect_length.value),replace:form.elements.replace.checked});
   progress.hidden=false;hint.textContent='Tracking and rendering… keep this open or come back later; the edit saves to the library either way.';
   for(;;){
    await new Promise(resolve=>setTimeout(resolve,1500));
    const status=await api('/api/edits/'+editId);progress.value=status.progress;
    if(status.status==='done'){$('#editor').close();toast('Edit saved to the library. It joins the queue once prepared, usually within a couple of minutes.');await refresh();return;}
    if(status.status==='error')throw new Error(status.message);
    if(!$('#editor').open)return;
   }
  });
 });
 if(!$('#editor').open)$('#editor').showModal();
}
function clipTiming(clip){
 const mode=clip.post_on?'date':(clip.not_before||clip.not_after)?'window':'any';
 return `<section class="clip-timing"><h3>Type & timing</h3>
 <label class="field">Clip type<select name="kind">${kindOptions(clip.own_kind||'',clip.set_kind||'performance')}</select><span>Talking, behind-the-scenes and promo clips follow Settings → Content mix.</span></label>
 <fieldset class="field choice compact" id="timing-choice"><legend>When should it post?</legend>
 <label><input type="radio" name="timing" value="any" ${mode==='any'?'checked':''}><span><strong>Whenever it fits</strong>Mixed into the queue like everything else.</span></label>
 <label><input type="radio" name="timing" value="date" ${mode==='date'?'checked':''}><span><strong>On a specific date</strong>For announcements and anything tied to a day.</span></label>
 <label><input type="radio" name="timing" value="window" ${mode==='window'?'checked':''}><span><strong>Between two dates</strong>For example, promo for a gig that stops once the gig is over.</span></label></fieldset>
 <div class="timing-fields" data-timing="date" ${mode==='date'?'':'hidden'}><div class="form-row"><label class="field">Date<input type="date" name="post_on" value="${esc(clip.post_on||'')}" min="${new Date().toISOString().slice(0,10)}"></label><label class="field">Time (optional)<input type="time" name="post_at" value="${esc(clip.post_at||'')}"></label></div><p class="help">Times are ${esc(state.settings.timezone)}. Leave the time blank to use that day’s best slot. Daily limits still apply.</p></div>
 <div class="timing-fields" data-timing="window" ${mode==='window'?'':'hidden'}><div class="form-row"><label class="field">Not before<input type="date" name="not_before" value="${esc(clip.not_before||'')}"></label><label class="field">Not after<input type="date" name="not_after" value="${esc(clip.not_after||'')}"></label></div><p class="help">Leave either side blank for an open-ended window.</p></div>
 </section>`;
}
function clipHistory(clip){
 const posts=clip.posts||[];
 return `<section class="clip-history"><h3>Posting history</h3>${posts.length?`<ul>${posts.map(p=>`<li>${laneTag(p.lane)} <span>${day(p.published_at)} · ${clock(p.published_at)}</span>${p.permalink?` <a href="${esc(p.permalink)}" target="_blank" rel="noopener">Open on Instagram</a>`:''}</li>`).join('')}</ul>`:'<p class="help">Not posted yet.</p>'}</section>`;
}
function clipSong(clip){
 const state_=!state.song_matching?'Audio matching needs FFmpeg with Chromaprint. Name songs here to keep them apart.':clip.audio_state===2?'No usable audio in this clip.':clip.audio_state===0?'Listening for repeats once the clip is prepared.':clip.similar?.length?'':'No other clip plays this song so far.';
 return `<section class="clip-song"><h3>Song</h3>
 <label class="field">Song name<input name="song" maxlength="120" value="${esc(clip.song||'')}" placeholder="Optional · e.g. the track title"><span>Clips with the same name are treated as one song. Give a clip its own name to split it from a wrong match.</span></label>
 ${clip.similar?.length?`<p class="help">Same song in ${clip.similar.length} other clip${clip.similar.length===1?'':'s'}. Reelflow keeps them apart in the queue.</p><ul class="similar-list">${clip.similar.map(m=>`<li><button type="button" class="similar-clip" data-open-clip="${esc(m.id)}"><img src="/api/preview/${esc(m.id)}" alt="" loading="lazy"><span>${esc(m.name)}</span><strong>${m.score}%</strong></button></li>`).join('')}</ul><p class="help">Percentages are audio-fingerprint matches. Around 100% is the same moment of the track; lower scores are a repeated loop or section.</p>`:clip.song_clips?`<p class="help">Named the same as ${clip.song_clips} other clip${clip.song_clips===1?'':'s'}.</p>`:`<p class="help">${state_}</p>`}
 </section>`;
}
async function openClip(id){let clip=records.find(r=>r.id===id);if(!clip){[clip]=await api('/api/clips?only='+encodeURIComponent(id)+'&limit=1');}if(!clip)return;let coverOverride;$('#editor-title').textContent='Source material';$('#editor-body').innerHTML=`${clipExport(clip)}${canRewrite(clip)?coverControls(clip):''}<p class="editor-name">${esc(clip.name)}</p><form id="clip-form"><label class="field">Facts & context<textarea name="notes" maxlength="3000" placeholder="Only facts the caption writer should know: venue, track, date, what is happening…">${esc(clip.notes)}</textarea></label><label class="field">Caption<textarea name="caption" maxlength="2200" placeholder="Write a caption, or connect AI in Settings.">${esc(clip.caption)}</textarea><span>Manual captions make prepared footage ready. Upcoming posts with edited captions keep their own version.</span></label>${clipTiming(clip)}${clipSong(clip)}${clipHistory(clip)}<label class="toggle field"><span>Exclude this clip from future posting</span><input type="checkbox" name="disabled" ${clip.disabled?'checked':''}></label><div class="drawer-actions"><button class="primary" type="submit">Save footage</button><button class="button" type="button" data-regenerate="${clip.id}" ${canRewrite(clip)?'':'disabled'}>${icon('spark')}Rewrite caption</button>${canRewrite(clip)&&clip.status!=='processing'?`<button class="button" type="button" data-funny-edit="${clip.id}">${icon('edit')}Funny edit</button>`:''}</div>${clip.status==='ready'&&!clip.disabled&&!clip.rebuilding&&clip.duration<=180?`<div class="youtube-drawer-actions"><button type="button" class="button" data-create-short="${clip.id}">${icon('play')}Create YouTube Short draft</button></div>`:''}${clip.error?`<p class="editor-error">${esc(clip.error)}</p>`:''}${clip.status==='error'?`<button type="button" class="button" data-retry-clip="${clip.id}">Retry preparation</button>`:''}</form>`;$('#clip-form').addEventListener('submit',async e=>{e.preventDefault();const form=e.currentTarget;await busy(e.submitter,async()=>{let body={notes:form.elements.notes.value,disabled:form.elements.disabled.checked,song:form.elements.song.value,kind:form.elements.kind.value};const timing=form.elements.timing.value;if(timing==='date'&&!form.elements.post_on.value){toast('Choose a date, or pick “Whenever it fits”.',true);return;}Object.assign(body,{post_on:timing==='date'?form.elements.post_on.value:'',post_at:timing==='date'?form.elements.post_at.value:'',not_before:timing==='window'?form.elements.not_before.value:'',not_after:timing==='window'?form.elements.not_after.value:''});if(form.elements.caption.value.trim())body.caption=form.elements.caption.value.trim();if(coverOverride!==undefined)body.cover_manual_ms=coverOverride;await api('/api/clips/'+clip.id,'PATCH',body);$('#editor').close();toast('Footage updated.');await refresh();});});const video=$('#editor .clip-export');
 if(video){
  const label=$('#clip-export-size');
  const pick=$('#clip-cover-pick'), reset=$('#clip-cover-reset'), coverLabel=$('#clip-cover-label'), preview=$('#clip-cover-preview');
  const size=()=>{
   if(video.videoWidth)label.textContent=`Export: ${video.videoWidth} × ${video.videoHeight}`;
   pick.disabled=!Number.isFinite(video.duration);
  };
  preview?.addEventListener('error',()=>{preview.hidden=true;});
  pick.addEventListener('click',()=>{
   video.pause();
   coverOverride=Math.max(0,Math.min(Math.floor(video.currentTime*1000),Math.floor(Math.min(video.duration,clip.duration)*1000)-50));
   if(preview)preview.hidden=true;
   coverLabel.textContent=`Selected frame · ${(coverOverride/1000).toFixed(2)}s · Save footage to apply`;
   reset.disabled=false;
  });
  reset.addEventListener('click',()=>{
   coverOverride=null;
   if(preview)preview.hidden=true;
   coverLabel.textContent='Use cover setting · Save footage to apply';
   reset.disabled=true;
  });
  video.addEventListener('loadedmetadata',size);
  size();
 }
 document.querySelectorAll('#timing-choice input').forEach(radio=>radio.addEventListener('change',()=>document.querySelectorAll('.timing-fields').forEach(el=>el.hidden=el.dataset.timing!==radio.value)));
 if(!$('#editor').open)$('#editor').showModal();}
$('#close-editor').addEventListener('click',()=>{$('#editor video')?.pause();$('#editor').close();});
$('#editor').addEventListener('close',()=>$('#editor video')?.pause());
$('#pause-button').addEventListener('click',e=>busy(e.currentTarget,async()=>{await api('/api/settings','PATCH',{values:{paused:!state.settings.paused}});await refresh();toast(state.settings.paused?'Publishing paused.':'Queue resumed'+(state.settings.dry_run?' in dry-run mode.':'.'));}));
$('#logout').addEventListener('click',()=>busy($('#logout'),async()=>{if(state.demo){toast('This is a local preview. No account is signed in.');return;}await api('/api/logout','POST');selectedClips.clear();guidanceDraft=null;guidanceOpen=false;batchGuidance='';captionProgress='';state=null;showLogin();}));
$('#login-form').addEventListener('submit',async e=>{e.preventDefault();const form=e.currentTarget;$('#login-error').textContent='';try{await api('/api/login','POST',{password:form.elements.password.value});form.reset();await start();}catch(err){$('#login-error').textContent=err.message;}});
window.addEventListener('hashchange',()=>{offset=0;filter='all';if(state)render().catch(e=>toast(e.message,true));});
document.addEventListener('click',async e=>{
 const target=e.target.closest('button');if(!target)return;
 if(target.dataset.queueView){queueView=target.dataset.queueView;offset=0;await render();return;}
 if(target.dataset.filter){filter=target.dataset.filter;offset=0;await render();return;}if(target.dataset.batchAction&&captionBatch){await busy(target,async()=>{captionBatch=await api(`/api/rewrite-batches/${captionBatch.id}/${target.dataset.batchAction}`,'POST');captionProgress=describeBatch(captionBatch);captionControls();if(captionBatch.status==='running')watchBatch();else if(currentPage==='library')await refresh();});return;}if(target.dataset.mcAction){const action=target.dataset.mcAction;if(action==='delete'&&!confirm('Delete this edit and its files? Clips already in the library stay.'))return;await busy(target,async()=>{await api(`/api/multicam/jobs/${target.dataset.id}/${action}`,'POST');toast({cancel:'Cancelling the edit.',retry:'Edit queued again.',delete:'Edit deleted.'}[action]);await render();});return;}if(target.dataset.libraryView){libraryView=target.dataset.libraryView;offset=0;await render();return;}if(target.dataset.openClip){await openClip(target.dataset.openClip);return;}if(target.dataset.funnyEdit){await openEditTool(target.dataset.funnyEdit);return;}
 if(target.dataset.createShort){await busy(target,async()=>{
  const result=await api('/api/youtube/shorts','POST',{clip_id:target.dataset.createShort});
  $('#editor').close();
  youtubeFocus=result.id;youtubeView='queue';youtubeOffset=0;location.hash='youtube';
 });return;}
 if(target.dataset.job){await openJob(target.dataset.job);return;}
 if(target.dataset.clip){await openClip(target.dataset.clip);return;}
 if(target.dataset.retryClip){await busy(target,async()=>{await api('/api/clips/'+target.dataset.retryClip+'/retry','POST');$('#editor').close();toast('Preparation will retry on the next worker pass.');await refresh();});return;}
 if(target.dataset.regenerate){if(captionBusy){toast('Wait for the caption operation to finish.');return;}await busy(target,async()=>{await rewriteCaption(target.dataset.regenerate);$('#editor').close();toast('A fresh caption is ready.');await refresh();});return;}
 if(target.dataset.postNow){
  const caption=$('#job-form textarea[name="caption"]');
  if(caption&&caption.value.trim()!==caption.defaultValue.trim()){
   toast('Save your caption changes before posting.',true);return;
  }
  await busy(target,async()=>{
   await api('/api/jobs/'+target.dataset.postNow+'/post-now','POST');
   $('#editor').close();toast('Post now requested. Check Activity for the result.');await refresh();
  });
  return;
 }
 if(target.dataset.jobAction){await busy(target,async()=>{await api('/api/jobs/'+target.dataset.id+'/action','POST',{action:target.dataset.jobAction,media_id:$('#resolved-media-id')?.value||''});$('#editor').close();if(target.dataset.jobAction==='confirm_removed'){queueView='list';location.hash='queue';toast('Reel requeued for review. Use Post now when you’re ready.');}else toast('Post state updated.');await refresh();});return;}
 const a=target.dataset.action;if(!a)return;
 if(a==='unblock-trials'){await busy(target,async()=>{const r=await api('/api/trials/unblock','POST');toast(`Trial Reels resumed. ${r.requeued} refused Trial${r.requeued===1?'':'s'} queued again.`);await refresh();});return;}
 if(a==='suggest-tags'){await busy(target,async()=>{const {hashtags}=await api('/api/hashtags/suggest','POST',{count:Number($('#suggest-count')?.value||12)});tagSuggestions=hashtags;renderSuggestions();if(!hashtags.length)$('#tag-suggestions').innerHTML='<p class="help">No new suggestions. Add more context about the account, then try again.</p>';});return;}
 if(a==='select-clips'||a==='clear-clips'){
  if(captionBusy)return;
  if(a==='clear-clips'){selectedClips.clear();batchGuidance='';}
  else records.filter(canRewrite).forEach(clip=>selectedClips.set(clip.id,clip.name));
  captionControls();return;
 }
 if(a==='select-all-clips'||a==='rebuild-clips'){
  if(captionBusy)return;
  captionBusy=true;captionControls();
  try{
   if(a==='select-all-clips'){
    const clips=await api('/api/clips/caption-selection');
    selectedClips.clear();
    clips.forEach(clip=>selectedClips.set(clip.id,clip.name));
    if(!clips.length)toast('No prepared clips are ready to rewrite yet.');
   }else{
    const {count}=await api('/api/clips/rebuild','POST');
    selectedClips.clear();captionProgress='';
    toast(count?`Queued ${count} video${count===1?'':'s'} to rebuild from the originals.`:'No videos to rebuild.');
    await refresh();
   }
  }catch(err){toast(err.message,true);}
  finally{captionBusy=false;captionControls();}
  return;
 }
 if(a==='regenerate-clips'){await regenerateSelected();return;}
 if(a==='upload'){chooseUpload();return;}
 if(a==='next'||a==='previous'){offset=Math.max(0,offset+(a==='next'?50:-50));await render();return;}
 await busy(target,async()=>{
  if(a==='scan'){const r=await api('/api/scan','POST');toast(r.message);}
  if(a==='notify'){const r=await api('/api/notifications/test','POST');toast(r.message);}
  if(a==='disconnect'){await api('/api/account/disconnect','POST');await refresh();toast('Account disconnected; posting is paused.');}
  if(a==='models'){const models=await api('/api/models');$('#model-options').innerHTML=models.filter(m=>!state.settings.ai_vision||m.vision).map(m=>`<option value="${esc(m.id)}">${esc(m.name)}${m.vision?' · images':''}</option>`).join('');toast(`${models.length} models loaded. Type a name in the Model ID field.`);}
 });
});
async function start(){try{await loadState();$('#login').hidden=true;$('#shell').hidden=false;await render();resumeBatchWatch();}catch(e){if(!state)showLogin();else toast(e.message,true);}}
async function resumeBatchWatch(){try{const {batch}=await api('/api/rewrite-batches/latest');if(batch&&['running','paused'].includes(batch.status)){captionBatch=batch;watchBatch();}}catch{}}
icons();start();setInterval(()=>{if(!state||$('#editor').open||dragState)return;
 if(currentPage==='queue'&&scheduleDraft)return;
 if(currentPage==='library'&&(captionBusy||guidanceDraft!==null||$('#caption-guidance')?.contains(document.activeElement)||
  $('#caption-selection')?.contains(document.activeElement)||document.activeElement?.matches('[data-caption-select]')))return;
 if(currentPage==='youtube'&&($('#content').contains(document.activeElement)||$('#youtube-preferences')?.open||
  (youtubeData?.connected&&$('#youtube-connection')?.open)))return;
 if(['queue','library','activity','youtube'].includes(currentPage))refresh().catch(e=>toast(e.message,true));},30000);

window.addEventListener('unhandledrejection',event=>{event.preventDefault();toast(event.reason?.message||'The request could not finish. Check the connection and try again.',true);});

document.addEventListener('click',e=>{
 const link=e.target.closest('a[href^="/auth/"]');if(!link||!state)return;
 if(state.demo){e.preventDefault();toast('Connect accounts in the Docker deployment. This local preview does not store credentials.',true);}
 else if(link.getAttribute('href').includes('instagram')&&!state.meta_configured){e.preventDefault();toast('Set the Meta app ID, app secret and public HTTPS URL in compose.yaml, then restart Docker. See the deployment guide.',true);}
});
