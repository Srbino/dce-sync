'use strict';
const $ = id => document.getElementById(id);
const markupCache=new Map();
let key = location.hash.slice(1) || sessionStorage.getItem('dce-key');
if (key) sessionStorage.setItem('dce-key', key);
history.replaceState(null, '', location.pathname);
let state = null, selected = null, pending = false, lastLogs = '';
const labels = {idle:'Ready',queued:'Queued',downloading:'Downloading',merging:'Merging',done:'Completed',error:'Error',cancelled:'Stopped'};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = n => new Intl.NumberFormat('en-US').format(n);
function size(n) { if (!n) return '0 B'; const i = Math.min(3, Math.floor(Math.log(n)/Math.log(1024))); return `${new Intl.NumberFormat('en-US',{maximumFractionDigits:i ? 1 : 0}).format(n / 1024**i)} ${['B','KB','MB','GB'][i]}`; }
function date(s) { return s ? new Date(s+'T12:00:00').toLocaleDateString('en-US') : 'Not downloaded yet'; }
async function api(path, body, timeout=10000) {
 const response = await fetch('/api/'+path, {method:body ? 'POST':'GET', headers:{'X-DCE-Key':key || '',...(body ? {'Content-Type':'application/json'} : {})}, ...(body ? {body:JSON.stringify(body)} : {}),signal:AbortSignal.timeout(timeout)});
 let result; try {result=await response.json();} catch {throw new Error('The local app returned an invalid response. Reopen Discord Archive.');} if (!response.ok) throw new Error(response.status===404 ? 'This app instance is out of date. Close and reopen Discord Archive to load the update.' : result.error || 'Request failed.'); return result;
}
function visible() {
 const q = $('search').value.toLocaleLowerCase('en'), filter = $('filter').value;
 return state.channels.filter(r => `${r.name} ${r.display_name || ""} ${r.server}`.toLocaleLowerCase('en').includes(q) && (filter==='all' || (filter==='active' ? ['downloading','merging','queued'].includes(r.status) : r.status===filter)));
}
function render() {
 if($('run-panel'))$('run-panel').hidden=!state.running && !state.operation;
 const rows = state.channels, shown = visible(), active = rows.filter(r=>['downloading','merging'].includes(r.status));
 const done = rows.filter(r=>r.status==='done').length, errors = rows.filter(r=>r.status==='error').length;
 const attempted = rows.filter(r=>r.status!=='idle'), terminal = attempted.filter(r=>['done','error','cancelled'].includes(r.status)).length;
 $('channel-count').textContent = number(rows.length); $('server-count').textContent = `${new Set(rows.map(r=>r.server)).size} servers`;
 $('total-size').textContent = size(rows.reduce((s,r)=>s+r.bytes,0)); $('file-count').textContent = `${number(rows.reduce((s,r)=>s+r.files,0))} files across tracked channels`;
 $('done-count').textContent = `${done} / ${attempted.length || rows.length}`;
 $('result-count').textContent = state.operation==='snapshot' ? 'Full archive backup including media' : errors ? `${errors} channels need attention` : state.running ? 'Sync in progress' : done ? 'Messages safely archived' : 'Ready when you are';
 $('download-size').textContent = size(rows.reduce((s,r)=>s+r.downloaded,0));
 $('count-badge').textContent = rows.length;
 $('run-label').textContent = state.maintenance || (state.running && state.operation==='snapshot' ? state.version_status || 'Preparing snapshot…' : state.running ? `${state.operation==='organize'?'Organizing':state.operation==='export'?'Exporting':'Syncing'} · ${active.map(r=>'#'+r.name).join(', ') || 'Preparing…'} · finished ${terminal}/${attempted.length}` : state.operation==='snapshot' ? state.version_status || 'Snapshot finished' : attempted.length ? `Run finished · ${done} completed · ${errors} errors · ${rows.filter(r=>r.status==='cancelled').length} stopped` : 'Choose channels to start a sync.');
 $('connection').dataset.status = state.running || state.discovery_loading ? 'busy' : 'ready';
 $('connection').textContent = state.running ? `${state.operation==='export'?'Exporting':state.operation==='snapshot'?'Saving version':state.operation==='organize_all'?'Organizing':state.operation==='relocate'?'Moving archive':'Syncing'} · ${active.length} active` : state.discovery_loading ? 'Loading Discord…' : 'Connected locally';
 $('activity-dock').classList.toggle('working',state.running || state.discovery_loading);
 $('run-panel')?.classList.toggle('has-errors',errors>0 || !!state.error);
 $('activity-summary').textContent=state.logs.length ? state.logs[state.logs.length-1].text : state.running?'Preparing downloads…':'Ready when you are';
 $('overall-bar').style.width = `${attempted.length ? terminal/attempted.length*100 : 0}%`;
 $('organize').disabled=state.running || pending;
 $('export-open').disabled = $('start').disabled = state.running || pending || !selected.size;
 $('snapshot').disabled=state.running || pending;$('version-status').textContent=state.version_status || '';
 $('cancel').hidden = !state.running; $('cancel').disabled = pending;
 $('start').textContent = `↓ Sync selected (${selected.size})`;
 $('select-all').disabled = state.running; $('select-all').checked = shown.length>0 && shown.every(r=>selected.has(r.name)); $('select-all').indeterminate = shown.some(r=>selected.has(r.name)) && !shown.every(r=>selected.has(r.name));
 let html = '', server = null;
 for (const r of shown) {
  if (r.server!==server) { server=r.server; html+=`<div class="server-heading">${avatar(server,r.server_icon)}${esc(server)}<small>${shown.filter(x=>x.server===server).length} ${shown.filter(x=>x.server===server).length===1?'channel':'channels'}</small><button class="text-button" data-sync-server="${esc(server)}" ${state.running || pending?"disabled":""}>↓ Sync server</button></div>`; }
  const busy = ['downloading','merging'].includes(r.status);
  const progress = busy || r.status==='done' ? `<div class="progress ${busy && r.percent===null?'indeterminate':''}" role="progressbar" aria-label="${esc(r.name)}" ${r.percent!==null?`aria-valuenow="${r.percent}" aria-valuemin="0" aria-valuemax="100"`:''}><i style="width:${r.percent===null?30:r.percent}%"></i></div>` : '';
  html+=`<div class="channel"><label class="channel-name"><input type="checkbox" data-channel="${esc(r.name)}" aria-label="Select ${esc(r.name)}" ${selected.has(r.name)?'checked':''} ${state.running?'disabled':''}><span class="hash">#</span><span><strong>${esc(r.display_name || r.name)}</strong><small>${esc(r.id)}</small></span></label><div><span class="status ${r.status}"><span class="status-dot"></span>${labels[r.status]}${busy && r.percent!==null?` · ${r.percent}% estimated`:''}</span>${progress}<div class="detail">${r.status==='downloading'?`${size(r.downloaded)} · ${r.elapsed} s · ${esc(r.detail)}`:esc(r.detail)}</div></div><div class="archive-cell">${size(r.bytes)}<small>${r.messages===null?`${r.files} ${r.files===1?'file':'files'}`:`${number(r.messages)} messages`}</small></div><div class="date-cell">${date(r.last)}<small>${r.duplicates?`${number(r.duplicates)} duplicates merged`:r.last?'Incremental sync':'Full available history'}</small></div><div class="channel-actions"><button class="text-button" data-open-channel="${esc(r.name)}" ${r.files?'':'disabled'}>Open folder</button><button class="text-button channel-sync" data-sync-channel="${esc(r.name)}" ${state.running || pending?'disabled':''}>↓ Sync</button></div></div>`;
 }
 replaceHtml('channels', html || '<div class="empty">No matching channels. Use Add channels to build your archive.</div>');
 $('output-path').textContent = state.output;
 if (state.error) showError(state.error);
 if ($('server-browser').open) renderCatalog();
 const logData = JSON.stringify(state.logs);
 if (logData!==lastLogs) {
  const atBottom = $('logs').scrollHeight - $('logs').scrollTop - $('logs').clientHeight < 45;
  if (state.logs.length) $('logs').innerHTML = state.logs.map(l=>`<div class="log-line"><time>${esc(l.time)}</time><b>#${esc(l.channel)}</b><span>${esc(l.text)}</span></div>`).join('');
  if(atBottom) $('logs').scrollTop = $('logs').scrollHeight;
  lastLogs = logData;
 }
}
function showError(message) { $('error').textContent = message; $('error').hidden = false; }
async function action(path, body) {
 pending=true; if(state)render(); $('error').hidden=true;
 try { await api(path,body); await refresh(); } catch(e) {showError(e.message);} finally {pending=false;if(state)render();}
}
async function refresh() {
 try {state=await api('state');if(selected===null)selected=new Set(state.channels.map(r=>r.name));render();}
 catch(e){$('connection').textContent='Connection lost';$('connection').dataset.status='offline';showError(e.message);$('start').disabled=$('organize').disabled=$('cancel').disabled=true;}
}
$('channels').addEventListener('change',e=>{if(e.target.dataset.channel){e.target.checked?selected.add(e.target.dataset.channel):selected.delete(e.target.dataset.channel);render();}});
$('select-all').addEventListener('change',e=>{visible().forEach(r=>e.target.checked?selected.add(r.name):selected.delete(r.name));render();});
$('search').addEventListener('input',()=>state&&render());$('filter').addEventListener('change',()=>state&&render());
$('start').onclick=()=>action('start',{operation:'sync',channels:[...selected]});
$('organize').onclick=openLibrary;
$('cancel').onclick=()=>action('cancel',{});$('open').onclick=()=>action('open',{});
$('toggle-log').onclick=()=>{const hidden=!$('logs').hidden;$('logs').hidden=hidden;$('toggle-log').textContent=hidden?'Expand':'Collapse';$('toggle-log').setAttribute('aria-expanded',String(!hidden));$('activity-dock').classList.toggle('expanded',!hidden);};
async function poll(){await refresh();if($('server-browser').open || testingConnection)await refreshCatalog();if(testingConnection && catalog && !catalog.loading)finishConnectionTest();setTimeout(poll,state?.running || state?.discovery_loading ? 500 : 1500);}poll();

let catalog = null, activeGuild = null, catalogBusy = false;
const picked = new Map();
function avatar(name, url) {
 const initials = name.split(/\s+/u).slice(0,2).map(s=>[...s][0] || '').join('').toUpperCase();
 const safe = typeof url==='string' && /^https:\/\/cdn\.discordapp\.com\/icons\/\d+\/(?:a_)?[a-f\d]{32}\.png\?size=64$/i.test(url);
 return `<span class="guild-avatar"><span>${esc(initials)}</span>${safe?`<img src="${esc(url)}" alt="" loading="lazy" referrerpolicy="no-referrer">`:''}</span>`;
}
document.addEventListener('load',event=>{if(event.target.matches?.('.guild-avatar img'))event.target.parentElement.classList.add('image-loaded');},true);
document.addEventListener('error', event=>{if(event.target.matches?.('.guild-avatar img')){event.target.hidden=true;event.target.parentElement.classList.remove('image-loaded');}},true);
$('channels').addEventListener('click', event=>{
 const button=event.target.closest('button'); if(!button)return;
 if(button.dataset.openChannel){action('open',{channel:button.dataset.openChannel});return;}
 if(state.running)return;
 if(button.dataset.syncChannel)action('start',{operation:'sync',channels:[button.dataset.syncChannel]});
 if(button.dataset.syncServer)action('start',{operation:'sync',channels:state.channels.filter(r=>r.server===button.dataset.syncServer).map(r=>r.name)});
});
async function openBrowser() {
 $('server-browser').showModal();
 if(!settingsData){try{settingsData=await api('settings');}catch(error){showError(error.message);}}
 await refreshCatalog();
 if(catalog && catalog.guilds===null && !catalog.loading)await loadCatalog(null);
}
async function refreshCatalog() {
 try {catalog=await api('catalog');renderCatalog();}
 catch(error){$('catalog-error').textContent=error.message;$('catalog-error').hidden=false;}
}
async function loadCatalog(guild, refresh=false) {
 catalogBusy=true;renderCatalog();$('catalog-error').hidden=true;
 try {await api('discover',{guild,refresh,threads:$('thread-mode').value});await refreshCatalog();}
 catch(error){$('catalog-error').textContent=error.message;$('catalog-error').hidden=false;}
 finally {catalogBusy=false;renderCatalog();}
}
function catalogVisible() {
 const query=$('catalog-search').value.toLocaleLowerCase();
 return (catalog?.channels[activeGuild] || []).filter(c=>`${c.name} ${c.category}`.toLocaleLowerCase().includes(query)).sort((a,b)=>a.category.localeCompare(b.category,'en') || a.name.localeCompare(b.name,'en'));
}
function renderCatalog() {
 if(!catalog)return;
 const loading=catalog.loading || catalogBusy;
 const guilds=(catalog.guilds || []).filter(g=>g.name.toLocaleLowerCase().includes($('guild-search').value.toLocaleLowerCase()));
 $('refresh-guilds').disabled=loading;
 $('thread-mode').disabled=loading || !activeGuild || activeGuild==='@me';
 $('refresh-channels').disabled=loading || !activeGuild;
 if(catalog.error){$('catalog-error').textContent=catalog.error;$('catalog-error').hidden=false;}
 else if(catalog.loading)$('catalog-error').hidden=true;
 replaceHtml('guild-list',catalog.guilds===null?`<div class="empty">${loading?'Loading your Discord servers…':'Could not load servers. Try Refresh.'}</div>`:guilds.map(g=>`<button class="guild-choice ${g.id===activeGuild?'chosen':''}" data-guild="${esc(g.id)}" ${loading?'disabled':''}>${avatar(g.name,g.icon_url)}<span><strong>${esc(g.name)}</strong><small>${g.tracked?`${g.tracked} tracked`:'Not tracked yet'}</small></span><span class="guild-chevron">›</span></button>`).join('') || '<div class="empty">No matching servers.</div>');
 const guild=(catalog.guilds || []).find(g=>g.id===activeGuild);
 $('guild-title').textContent=guild?.name || 'Select a server';
 const rows=catalogVisible();
 let content='', category=null;
 if(activeGuild && catalog.channels[activeGuild]) {
  for(const channel of rows) {
   if(channel.category!==category){category=channel.category;content+=`<div class="catalog-category">${esc(category)}</div>`;}
   const label=channel.tracked?(channel.status==='downloading'?'Downloading':channel.status==='queued'?'Queued':'Tracking'):channel.archived?'On disk':'New';
   content+=`<label class="catalog-channel"><input type="checkbox" data-pick="${esc(channel.id)}" ${picked.has(channel.id)?'checked':''} ${catalogBusy?'disabled':''}><span class="hash">${channel.kind==='thread'?'↳':'#'}</span><span class="catalog-channel-name"><strong>${esc(channel.name)}</strong><small>${channel.last?`Last downloaded ${date(channel.last)}`:channel.archived?'Existing archive found':channel.id}</small></span><span class="tracking-badge ${channel.tracked?'tracked':''}">${label}</span></label>`;
  }
 }
 replaceHtml('catalog-channels',content || `<div class="empty">${!activeGuild?'Pick a server to see its channels.':loading?'Loading channels…':catalog.error?'Could not load channels. Try Refresh.':'No matching channels.'}</div>`);
 $('catalog-all').disabled=!rows.length || catalogBusy;
 $('catalog-all').checked=rows.length>0 && rows.every(c=>picked.has(c.id));
 $('catalog-all').indeterminate=rows.some(c=>picked.has(c.id)) && !rows.every(c=>picked.has(c.id));
 $('picked-count').textContent=picked.size?`${picked.size} channel${picked.size===1?'':'s'} selected`:'No channels selected';
 $('picked-hint').textContent=state?.running?'A sync is running. You can browse while it finishes.':'Tracked channels stay in your archive. Select them to sync again.';
 $('add-only').disabled=$('add-sync').disabled=!picked.size || catalogBusy || !!state?.running || pending;
}
async function saveChannels(sync) {
 catalogBusy=true;renderCatalog();$('catalog-error').hidden=true;
 try {
  const result=await api('channels/add',{channels:[...picked.values()],sync});
  selected=new Set(result.channels);picked.clear();$('server-browser').close();await refresh();
 } catch(error){$('catalog-error').textContent=error.message;$('catalog-error').hidden=false;}
 finally{catalogBusy=false;renderCatalog();}
}
$('browse').onclick=openBrowser;if($('browse-nav'))$('browse-nav').onclick=openBrowser;
$('close-browser').onclick=()=>$('server-browser').close();
$('refresh-guilds').onclick=()=>loadCatalog(null,true);
$('refresh-channels').onclick=()=>activeGuild&&loadCatalog(activeGuild,true);
$('guild-search').oninput=$('catalog-search').oninput=renderCatalog;
$('guild-list').onclick=event=>{
 const button=event.target.closest('[data-guild]');if(!button || button.disabled)return;
 $('thread-mode').value=catalog.threadModes?.[button.dataset.guild] || settingsData?.options.threads || 'None';
 activeGuild=button.dataset.guild;$('catalog-search').value='';renderCatalog();loadCatalog(activeGuild);
};
$('catalog-channels').onchange=event=>{
 const id=event.target.dataset.pick;if(!id)return;
 if(event.target.checked)picked.set(id,{guild:activeGuild,id});else picked.delete(id);
 renderCatalog();
};
$('catalog-all').onchange=event=>{catalogVisible().forEach(c=>{if(event.target.checked)picked.set(c.id,{guild:activeGuild,id:c.id});else picked.delete(c.id);});renderCatalog();};
$('add-only').onclick=()=>saveChannels(false);$('add-sync').onclick=()=>saveChannels(true);

$('thread-mode').onchange=()=>activeGuild&&loadCatalog(activeGuild,true);


let settingsData=null, testingConnection=false;
function replaceHtml(id, html) {
 const element=$(id);if(markupCache.get(id)===html)return;markupCache.set(id,html);
 const focused=document.activeElement;
 const focusData=element.contains(focused) ? ['channel','pick','guild'].find(key=>focused.dataset[key]) : null;
 const focusValue=focusData?focused.dataset[focusData]:null;
 const scroll=element.scrollTop;element.innerHTML=html;element.scrollTop=scroll;
 if(focusData)element.querySelector(`[data-${focusData}="${CSS.escape(focusValue)}"]`)?.focus({preventScroll:true});
}
async function openSettings(){
 $('settings-dialog').showModal();$('settings-error').hidden=true;
 try{
  settingsData=await api('settings');
  for(const [name,value] of Object.entries(settingsData.options)){
   const input=$('setting-'+name);if(!input)continue;
   if(input.type==='checkbox')input.checked=value;else input.value=String(value);
  }
  renderSettings();renderStorageSchedule();
 }catch(error){settingsError(error.message);}
}
function renderSettings(){
 if(!settingsData)return;
 $('token-status').textContent=settingsData.token_present?'Token configured':'No token configured';
 $('token-dot').classList.toggle('configured',settingsData.token_present);
 $('token-source').textContent=`Source: ${settingsData.token_source}${settingsData.token_age_days===null?'':` · saved ${settingsData.token_age_days} days ago`}`;
 $('engine-version').textContent=settingsData.engine.version;
 $('settings-output').textContent=settingsData.output;
 $('save-settings').disabled=$('save-token').disabled=!!state?.running;
 $('settings-save-note').textContent=state?.running?'Finish or stop the current sync before changing settings.':'Settings apply to your next sync.';
 $('test-connection').disabled=testingConnection;
}
function settingsError(message){$('settings-error').textContent=message;$('settings-error').hidden=false;}
async function finishConnectionTest(){
 testingConnection=false;$('test-connection').disabled=false;
 if(catalog.error){settingsError(catalog.error);$('token-status').textContent='Connection test failed';$('token-dot').classList.remove('configured');}
 else{$('token-status').textContent=`Connected · ${catalog.guilds?.length || 0} servers available`;$('token-dot').classList.add('configured');$('settings-error').hidden=true;}
}
$('settings-open').onclick=openSettings;if($('settings-nav'))$('settings-nav').onclick=openSettings;
$('close-settings').onclick=()=>{$('discord-token').value='';$('settings-dialog').close();};
$('settings-dialog').addEventListener('close',()=>{$('discord-token').value='';});
$('settings-form').onsubmit=async event=>{
 event.preventDefault();const options={};
 for(const name of ['jobs','retries'])options[name]=Number($('setting-'+name).value);
 for(const name of ['media','reuse_media','utc','markdown','full_history'])options[name]=$('setting-'+name).checked;
 options.threads=$('setting-threads').value;options.layout=$('setting-layout').value;
 $('save-settings').disabled=true;
 try{settingsData=await api('settings',{options});$('settings-dialog').close();await refresh();}
 catch(error){settingsError(error.message);}finally{renderSettings();}
};
$('save-token').onclick=async()=>{
 const input=$('discord-token');$('save-token').disabled=true;$('settings-error').hidden=true;
 try{settingsData=await api('token',{token:input.value});input.value='';catalog=null;renderSettings();$('token-status').textContent='Token saved · test your connection';}
 catch(error){settingsError(error.message);}finally{$('save-token').disabled=!!state?.running;}
};
$('test-connection').onclick=async()=>{
 testingConnection=true;$('test-connection').disabled=true;$('token-status').textContent='Checking Discord…';$('settings-error').hidden=true;
 try{await api('discover',{refresh:true});await refreshCatalog();if(!catalog.loading)finishConnectionTest();}
 catch(error){testingConnection=false;settingsError(error.message);$('test-connection').disabled=false;}
};

$('export-open').onclick=()=>{$('export-error').hidden=true;$('export-selection').textContent=`${selected.size} channels selected`;$('export-dialog').showModal();};
$('export-close').onclick=()=>$('export-dialog').close();
$('export-form').onsubmit=async event=>{
 event.preventDefault();const options={};
 for(const name of ['format','after','before','filter','partition','locale'])options[name]=$('export-'+name).value;
 options.reverse=$('export-reverse').checked;
 try{await api('start',{operation:'export',channels:[...selected],options});$('export-dialog').close();await refresh();}
 catch(error){$('export-error').textContent=error.message;$('export-error').hidden=false;}
};
$('snapshot').onclick=()=>action('start',{operation:'snapshot',channels:[]});
$('versions-open').onclick=()=>action('open',{location:'versions'});
$('reports-open').onclick=()=>action('open',{location:'reports'});
const mobileLayout=matchMedia('(max-width:1000px)');
function placeActivity(){const dock=$('activity-dock');dock.classList.toggle('mobile-activity',mobileLayout.matches);if(mobileLayout.matches)document.querySelector('main').insertBefore(dock,document.querySelector('main footer'));else document.querySelector('.sidebar').append(dock);}
mobileLayout.addEventListener('change',placeActivity);placeActivity();

function renderStorageSchedule(){
 $('storage-path').value=settingsData.output;
 const schedule=settingsData.schedule;
 $('schedule-enabled').checked=schedule.enabled;
 $('schedule-time').value=schedule.time;
 $('schedule-timezone').textContent=`Local time (${schedule.timezone}). Your Mac must be powered on and you must be logged in. A sleeping Mac runs the job after waking.`;
 $('schedule-channels').innerHTML=state.channels.map(r=>`<label><input type="checkbox" data-scheduled="${esc(r.name)}" ${schedule.channels.includes(r.name)?'checked':''}><span>${esc(r.server)} <strong>#${esc(r.display_name || r.name)}</strong></span></label>`).join('');
 $('schedule-save').disabled=!schedule.supported || state.running;
 $('schedule-status').textContent=!schedule.supported?'Built-in scheduling is available on macOS.':schedule.last?`Last scheduled run: ${schedule.last.status} · ${schedule.last.finished || schedule.last.started}`:schedule.enabled?'Daily sync enabled.':'No automatic downloads scheduled.';
}
$('storage-browse').onclick=async()=>{try{const result=await api('folder/choose',{},130000);if(result.path)$('storage-path').value=result.path;}catch(error){settingsError(error.message);}};
$('storage-save').onclick=async()=>{
 try{await api('storage',{path:$('storage-path').value,copy_existing:$('storage-copy').checked});$('settings-dialog').close();await refresh();}
 catch(error){settingsError(error.message);}
};
$('schedule-all').onclick=()=>document.querySelectorAll('[data-scheduled]').forEach(input=>input.checked=true);
$('schedule-save').onclick=async()=>{
 try{settingsData.schedule=await api('schedule',{enabled:$('schedule-enabled').checked,time:$('schedule-time').value,channels:[...document.querySelectorAll('[data-scheduled]:checked')].map(i=>i.dataset.scheduled)});renderStorageSchedule();$('schedule-status').textContent=settingsData.schedule.enabled?'Daily sync saved. Runs even when this window is closed.':'Daily sync disabled.';}
 catch(error){settingsError(error.message);}
};
async function openLibrary(){
 $('library-dialog').showModal();$('library-error').hidden=true;$('library-plan').textContent='Scanning local exports…';$('library-organize').disabled=true;
 try{
  const library=await api('library');
  $('library-summary').textContent=`${library.files} files · ${library.channels.length} conversations · ${size(library.bytes)}`;
  $('library-plan').innerHTML=library.channels.map(r=>`<div><span>${esc(r.server)} / ${esc(r.category)}</span><strong>#${esc(r.display_name)}</strong><small>${r.files} files → 1 archive${r.tracked?' · Tracking':' · Historical'}</small></div>`).join('') || '<p>No local message exports found.</p>';
  $('library-organize').disabled=state.running || !library.files;
  if(library.skipped.length){$('library-error').hidden=false;$('library-error').textContent=`${library.skipped.length} unrecognized files will be kept in place.`;}
 }catch(error){$('library-error').hidden=false;$('library-error').textContent=error.message;}
}
$('library-close').onclick=()=>$('library-dialog').close();
$('library-organize').onclick=async()=>{try{await api('start',{operation:'organize_all',channels:[]});$('library-dialog').close();await refresh();}catch(error){$('library-error').hidden=false;$('library-error').textContent=error.message;}};
