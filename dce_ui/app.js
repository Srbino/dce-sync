'use strict';
const $ = id => document.getElementById(id);
let key = location.hash.slice(1) || sessionStorage.getItem('dce-key');
if (key) sessionStorage.setItem('dce-key', key);
history.replaceState(null, '', location.pathname);
let state = null, selected = null, pending = false, lastLogs = '';
const labels = {idle:'Ready',queued:'Queued',downloading:'Downloading',merging:'Merging',done:'Completed',error:'Error',cancelled:'Stopped'};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = n => new Intl.NumberFormat('en-US').format(n);
function size(n) { if (!n) return '0 B'; const i = Math.min(3, Math.floor(Math.log(n)/Math.log(1024))); return `${new Intl.NumberFormat('en-US',{maximumFractionDigits:i ? 1 : 0}).format(n / 1024**i)} ${['B','KB','MB','GB'][i]}`; }
function date(s) { return s ? new Date(s+'T12:00:00').toLocaleDateString('en-US') : 'Not downloaded yet'; }
async function api(path, body) {
 const response = await fetch('/api/'+path, {method:body ? 'POST':'GET', headers:{'X-DCE-Key':key || '',...(body ? {'Content-Type':'application/json'} : {})}, ...(body ? {body:JSON.stringify(body)} : {}),signal:AbortSignal.timeout(10000)});
 const result = await response.json(); if (!response.ok) throw new Error(result.error || 'Request failed.'); return result;
}
function visible() {
 const q = $('search').value.toLocaleLowerCase('en'), filter = $('filter').value;
 return state.channels.filter(r => `${r.name} ${r.display_name || ""} ${r.server}`.toLocaleLowerCase('en').includes(q) && (filter==='all' || (filter==='active' ? ['downloading','merging','queued'].includes(r.status) : r.status===filter)));
}
function render() {
 const rows = state.channels, shown = visible(), active = rows.filter(r=>['downloading','merging'].includes(r.status));
 const done = rows.filter(r=>r.status==='done').length, errors = rows.filter(r=>r.status==='error').length;
 const attempted = rows.filter(r=>r.status!=='idle'), terminal = attempted.filter(r=>['done','error','cancelled'].includes(r.status)).length;
 $('channel-count').textContent = number(rows.length); $('server-count').textContent = `${new Set(rows.map(r=>r.server)).size} servers in this workspace`;
 $('total-size').textContent = size(rows.reduce((s,r)=>s+r.bytes,0)); $('file-count').textContent = `${number(rows.reduce((s,r)=>s+r.files,0))} files across tracked channels`;
 $('done-count').textContent = `${done} / ${attempted.length || rows.length}`;
 $('result-count').textContent = errors ? `${errors} channels need attention` : state.running ? 'Sync in progress' : done ? 'Messages safely archived' : 'Ready when you are';
 $('download-size').textContent = size(rows.reduce((s,r)=>s+r.downloaded,0));
 $('count-badge').textContent = rows.length;
 $('run-label').textContent = state.running ? `${state.operation==='organize'?'Organizing':'Syncing'} · ${active.map(r=>'#'+r.name).join(', ') || 'Preparing…'} · finished ${terminal}/${attempted.length}` : attempted.length ? `Run finished · ${done} completed · ${errors} errors · ${rows.filter(r=>r.status==='cancelled').length} stopped` : 'Choose channels to start a sync.';
 $('overall-bar').style.width = `${attempted.length ? terminal/attempted.length*100 : 0}%`;
 $('start').disabled = $('organize').disabled = state.running || pending || !selected.size;
 $('cancel').hidden = !state.running; $('cancel').disabled = pending;
 $('start').textContent = `↓ Sync selected (${selected.size})`;
 $('select-all').disabled = state.running; $('select-all').checked = shown.length>0 && shown.every(r=>selected.has(r.name)); $('select-all').indeterminate = shown.some(r=>selected.has(r.name)) && !shown.every(r=>selected.has(r.name));
 let html = '', server = null;
 for (const r of shown) {
  if (r.server!==server) { server=r.server; html+=`<div class="server-heading">${avatar(server,r.server_icon)}${esc(server)}<small>${shown.filter(x=>x.server===server).length} channels</small><button class="text-button" data-sync-server="${esc(server)}" ${state.running || pending?"disabled":""}>↓ Sync server</button></div>`; }
  const busy = ['downloading','merging'].includes(r.status);
  const progress = busy || r.status==='done' ? `<div class="progress ${busy && r.percent===null?'indeterminate':''}" role="progressbar" aria-label="${esc(r.name)}" ${r.percent!==null?`aria-valuenow="${r.percent}" aria-valuemin="0" aria-valuemax="100"`:''}><i style="width:${r.percent===null?30:r.percent}%"></i></div>` : '';
  html+=`<div class="channel"><label class="channel-name"><input type="checkbox" data-channel="${esc(r.name)}" aria-label="Select ${esc(r.name)}" ${selected.has(r.name)?'checked':''} ${state.running?'disabled':''}><span class="hash">#</span><span><strong>${esc(r.display_name || r.name)}</strong><small>${esc(r.id)}</small></span></label><div><span class="status ${r.status}"><span class="status-dot"></span>${labels[r.status]}${busy && r.percent!==null?` · ${r.percent}% estimated`:''}</span>${progress}<div class="detail">${r.status==='downloading'?`${size(r.downloaded)} · ${r.elapsed} s · ${esc(r.detail)}`:esc(r.detail)}</div></div><div class="archive-cell">${size(r.bytes)}<small>${r.messages===null?`${r.files} files`:`${number(r.messages)} messages`}</small></div><div class="date-cell">${date(r.last)}<small>${r.duplicates?`${number(r.duplicates)} duplicates merged`:r.last?'Incremental sync':'Full available history'}</small><button class="text-button channel-sync" data-sync-channel="${esc(r.name)}" ${state.running || pending?'disabled':''}>↓ Sync</button></div></div>`;
 }
 $('channels').innerHTML = html || '<div class="empty">No matching channels. Use Add channels to build your archive.</div>';
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
 try {state=await api('state');if(selected===null)selected=new Set(state.channels.map(r=>r.name));$('connection').textContent='● Connected locally';render();}
 catch(e){$('connection').textContent='○ Connection lost';showError(e.message);$('start').disabled=$('organize').disabled=$('cancel').disabled=true;}
}
$('channels').addEventListener('change',e=>{if(e.target.dataset.channel){e.target.checked?selected.add(e.target.dataset.channel):selected.delete(e.target.dataset.channel);render();}});
$('select-all').addEventListener('change',e=>{visible().forEach(r=>e.target.checked?selected.add(r.name):selected.delete(r.name));render();});
$('search').addEventListener('input',()=>state&&render());$('filter').addEventListener('change',()=>state&&render());
$('start').onclick=()=>action('start',{operation:'sync',channels:[...selected]});
$('organize').onclick=()=>action('start',{operation:'organize',channels:[...selected]});
$('cancel').onclick=()=>action('cancel',{});$('open').onclick=()=>action('open',{});
$('toggle-log').onclick=()=>{const hidden=!$('logs').hidden;$('logs').hidden=hidden;$('toggle-log').textContent=hidden?'Show output':'Hide output';$('toggle-log').setAttribute('aria-expanded',String(!hidden));};
async function poll(){await refresh();if($('server-browser').open)await refreshCatalog();setTimeout(poll,1000);}poll();

let catalog = null, activeGuild = null, catalogBusy = false;
const picked = new Map();
function avatar(name, url) {
 const initials = name.split(/\s+/u).slice(0,2).map(s=>[...s][0] || '').join('').toUpperCase();
 const safe = typeof url==='string' && /^https:\/\/cdn\.discordapp\.com\/icons\/\d+\/(?:a_)?[a-f\d]{32}\.png\?size=64$/i.test(url);
 return `<span class="guild-avatar"><span>${esc(initials)}</span>${safe?`<img src="${esc(url)}" alt="" loading="lazy" referrerpolicy="no-referrer">`:''}</span>`;
}
document.addEventListener('error', event=>{if(event.target.matches?.('.guild-avatar img'))event.target.hidden=true;},true);
$('channels').addEventListener('click', event=>{
 const button=event.target.closest('button'); if(!button || state.running)return;
 if(button.dataset.syncChannel)action('start',{operation:'sync',channels:[button.dataset.syncChannel]});
 if(button.dataset.syncServer)action('start',{operation:'sync',channels:state.channels.filter(r=>r.server===button.dataset.syncServer).map(r=>r.name)});
});
async function openBrowser() {
 $('server-browser').showModal();
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
 return (catalog?.channels[activeGuild] || []).filter(c=>`${c.name} ${c.category}`.toLocaleLowerCase().includes(query));
}
function renderCatalog() {
 if(!catalog)return;
 const loading=catalog.loading || catalogBusy;
 const guilds=(catalog.guilds || []).filter(g=>g.name.toLocaleLowerCase().includes($('guild-search').value.toLocaleLowerCase()));
 $('refresh-guilds').disabled=loading;
 $('thread-mode').disabled=loading || !activeGuild;
 $('refresh-channels').disabled=loading || !activeGuild;
 if(catalog.error){$('catalog-error').textContent=catalog.error;$('catalog-error').hidden=false;}
 else if(catalog.loading)$('catalog-error').hidden=true;
 $('guild-list').innerHTML=catalog.guilds===null?`<div class="empty">${loading?'Loading your Discord servers…':'Could not load servers. Try Refresh.'}</div>`:guilds.map(g=>`<button class="guild-choice ${g.id===activeGuild?'chosen':''}" data-guild="${esc(g.id)}" ${loading?'disabled':''}>${avatar(g.name,g.icon_url)}<span><strong>${esc(g.name)}</strong><small>${g.tracked?`${g.tracked} tracked`:'Not tracked yet'}</small></span><span class="guild-chevron">›</span></button>`).join('') || '<div class="empty">No matching servers.</div>';
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
 $('catalog-channels').innerHTML=content || `<div class="empty">${!activeGuild?'Pick a server to see its channels.':loading?'Loading channels…':catalog.error?'Could not load channels. Try Refresh.':'No matching channels.'}</div>`;
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
$('browse').onclick=$('browse-nav').onclick=openBrowser;
$('close-browser').onclick=()=>$('server-browser').close();
$('refresh-guilds').onclick=()=>loadCatalog(null,true);
$('refresh-channels').onclick=()=>activeGuild&&loadCatalog(activeGuild,true);
$('guild-search').oninput=$('catalog-search').oninput=renderCatalog;
$('guild-list').onclick=event=>{
 const button=event.target.closest('[data-guild]');if(!button || button.disabled)return;
 $('thread-mode').value=catalog.threadModes?.[button.dataset.guild] || 'None';
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
