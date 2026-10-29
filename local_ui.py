"""Single-page local UI (Analyze, Discover, Featured, Settings). Plain HTML + JS, no external assets."""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>MintScope AI (local)</title>
<style>
:root{--b:#d0d7de;--mut:#57606a;--acc:#0969da;--bg:#f6f8fa}
*{box-sizing:border-box} body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#1f2328}
header{display:flex;align-items:center;gap:24px;padding:14px 24px;border-bottom:1px solid var(--b);background:#fff}
header h1{font-size:1.15rem;margin:0} header .tag{font-size:.75rem;color:var(--mut);border:1px solid var(--b);border-radius:20px;padding:2px 8px}
nav button{background:none;border:0;padding:8px 12px;font-size:.95rem;cursor:pointer;border-radius:6px;color:#1f2328}
nav button.on{background:var(--bg);font-weight:600}
main{max-width:980px;margin:0 auto;padding:24px}
section{display:none} section.on{display:block}
.card{border:1px solid var(--b);border-radius:10px;padding:16px;margin:12px 0;background:#fff}
input,select,textarea{font:inherit;padding:8px 10px;border:1px solid var(--b);border-radius:6px}
input.wide{width:100%} .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
button.p{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:8px 14px;font:inherit;cursor:pointer}
button.s{background:#fff;border:1px solid var(--b);border-radius:6px;padding:8px 14px;font:inherit;cursor:pointer}
.mut{color:var(--mut);font-size:.88rem} .score{font-size:2.2rem;font-weight:700}
.flag{display:inline-block;margin:2px 4px 2px 0;padding:2px 8px;border-radius:12px;font-size:.8rem;background:#eee}
.flag.high{background:#ffebe9;color:#a40e26} .flag.warning{background:#fff8c5;color:#7d4e00} .flag.info{background:#ddf4ff;color:#0a3069}
table{border-collapse:collapse;width:100%;font-size:.9rem} td,th{border-bottom:1px solid var(--b);padding:6px 8px;text-align:left}
.sp{background:#fff1e5;color:#953800;border-radius:4px;padding:1px 6px;font-size:.75rem;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.feat img{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:6px;background:var(--bg)}
.ok{color:#1a7f37} .bad{color:#a40e26} pre{white-space:pre-wrap;font-size:.8rem;background:var(--bg);padding:8px;border-radius:6px;max-height:300px;overflow:auto}
footer{max-width:980px;margin:0 auto;padding:12px 24px 32px;color:var(--mut);font-size:.8rem}
</style></head><body>
<header><h1>MintScope AI</h1><span class="tag">local app</span>
<nav><button data-t="analyze" class="on">Analyze</button><button data-t="discover">Discover</button>
<button data-t="featured">Featured</button><button data-t="settings">Settings</button></nav>
<span id="keybadge" class="mut"></span></header>
<main>
<section id="analyze" class="on">
 <div class="card"><div class="row"><input id="a_in" class="wide" style="flex:1" placeholder="Contract address (0x...) or OpenSea link">
 <select id="a_chain"><option>ethereum</option><option>base</option></select><button class="p" id="a_go">Analyze</button></div>
 <p class="mut">Works without any key from public onchain sources (Ethereum, Base). Add your own OpenSea key in Settings for market data.</p></div>
 <div id="a_out"></div>
 <div class="card"><b>Pro</b> <span class="mut">compare up to 5 collections, bulk JSON export</span>
 <textarea id="p_in" rows="3" class="wide" style="width:100%;margin-top:8px" placeholder="One contract or link per line"></textarea>
 <div class="row" style="margin-top:8px"><button class="s" id="p_cmp">Compare</button><button class="s" id="p_exp">Export JSON</button><span id="p_msg" class="mut"></span></div>
 <div id="p_out"></div></div>
</section>
<section id="discover">
 <div class="card row"><b>New collections launched</b><select id="d_period"><option value="today">today</option><option value="week">this week</option><option value="month">this month</option></select>
 <select id="d_chain"><option value="all">Ethereum + Base</option><option>ethereum</option><option>base</option></select>
 <button class="p" id="d_go">Discover</button><span id="d_msg" class="mut"></span></div>
 <div id="d_out"></div>
</section>
<section id="featured">
 <div id="f_out"></div>
 <div class="card"><b>Submit a collection</b> <span class="mut">it is analyzed automatically and reviewed before it can be featured</span>
 <div class="row" style="margin-top:8px"><input id="s_in" style="flex:1" placeholder="Contract address or link"><select id="s_chain"><option>ethereum</option><option>base</option></select>
 <input id="s_mint" style="flex:1" placeholder="Mint link (optional, https)"><button class="s" id="s_go">Submit</button></div><div id="s_out" class="mut"></div></div>
</section>
<section id="settings">
 <div class="card"><b>OpenSea API key</b> <span class="mut">optional</span>
 <p class="mut">Stored only on this device (<span id="k_store"></span>). Sent only to api.opensea.io. Never shown again after saving.</p>
 <div class="row"><span>Current:</span><b id="k_mask">not set</b></div>
 <div class="row" style="margin-top:8px"><input id="k_in" type="password" autocomplete="off" style="flex:1" placeholder="Paste your OpenSea API key">
 <button class="p" id="k_save">Save</button><button class="s" id="k_test">Test key</button><button class="s" id="k_del">Remove</button></div>
 <div id="k_msg" class="mut" style="margin-top:6px"></div></div>
 <div class="card"><b>MintScope Pro</b> <span class="mut" id="l_price"></span>
 <div class="row" style="margin-top:8px"><input id="l_in" style="flex:1" placeholder="Paste license (MSL1....)"><button class="s" id="l_go">Activate</button></div>
 <div id="l_msg" class="mut" style="margin-top:6px"></div></div>
 <div class="card mut">You pay your own API usage, if any. No data or keys are sent to MintScope servers.</div>
</section>
</main>
<footer>MintScope is an analysis and risk-research tool. Not financial advice; no guarantee of profit, price, or that a project is or is not a scam.</footer>
<script>
const H={'Content-Type':'application/json','X-MintScope-Local':'1'};
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(p,m='GET',b){const r=await fetch(p,{method:m,headers:H,body:b?JSON.stringify(b):undefined});
 let j={};try{j=await r.json()}catch(e){} if(!r.ok){throw new Error(typeof j.detail==='string'?j.detail:(j.detail&&j.detail.error)||JSON.stringify(j.detail||j))} return j}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{document.querySelectorAll('nav button,section').forEach(x=>x.classList.remove('on'));
 b.classList.add('on');$(b.dataset.t).classList.add('on'); if(b.dataset.t==='featured')loadFeatured()});
const flags=fs=>(fs||[]).map(f=>`<span class="flag ${esc(f.severity)}" title="${esc(f.message)}">${esc(f.code)}</span>`).join('')||'<span class="mut">no red flags</span>';
async function state(){const s=await api('/api/local/state');const k=s.keys.opensea_api_key;
 $('k_mask').textContent=k.set?k.masked:(s.key_from_env?'set by environment':'not set');$('k_store').textContent=k.storage==='os-keyring'?'OS keyring':'private file, permission 600';
 $('keybadge').textContent=k.set||s.key_from_env?'OpenSea key: on':'keyless mode';
 $('l_price').textContent=`one-time: $${s.pro_pricing.pro_intro_usd} intro, then $${s.pro_pricing.pro_usd}`;
 $('l_msg').textContent=s.license?`Active: ${s.license.tier} (${s.license.license_id})`:(s.licensing_configured?'No license':'Licensing not configured in this build')}
function report(r){const o=r.onchain,m=(o||{}).metrics||{};const os=r.opensea;
 let h=`<div class="card"><div class="row" style="justify-content:space-between"><div><b>${esc(r.name||r.input)}</b><div class="mut">${esc(r.parsed.chain)} ${esc(r.parsed.address||r.parsed.slug)}</div></div>
 <div style="text-align:right"><div class="score">${esc(r.risk_score)}<span class="mut">/100</span></div><div class="mut">safety score (${esc(r.score_source)})</div></div></div>`;
 if(o){h+=`<p>${flags(o.red_flags)}</p><table><tr><th>Standard</th><td>${esc(m.standard)}</td><th>Verified</th><td>${esc(m.contract_verified??'unknown')}</td></tr>
 <tr><th>Holders</th><td>${esc(m.holders??'unknown')}</td><th>Supply</th><td>${esc(m.total_supply??'unknown')}</td></tr>
 <tr><th>Transfers 7d</th><td>${esc(m.transfers_7d)}</td><th>Wallets 7d</th><td>${esc(m.unique_wallets_7d)}</td></tr></table>
 <p class="mut">${o.score.components.map(c=>`${esc(c.name)}: ${esc(c.score??'n/a')}`).join(' · ')}</p>`}
 if(os){h+=`<p><b>OpenSea</b> score ${esc(os.score.value)} · ${flags(os.risk_flags)}</p>`}
 (r.notes||[]).forEach(n=>h+=`<p class="mut">${esc(n)}</p>`);
 h+=`<details><summary class="mut">raw JSON</summary><pre>${esc(JSON.stringify(r,null,1))}</pre></details></div>`;return h}
$('a_go').onclick=async()=>{$('a_out').innerHTML='<p class="mut">Analyzing (public RPC, 5-60 s)...</p>';
 try{$('a_out').innerHTML=report(await api('/api/local/analyze','POST',{input:$('a_in').value,chain:$('a_chain').value}))}catch(e){$('a_out').innerHTML=`<p class="bad">${esc(e.message)}</p>`}};
const lines=()=>$('p_in').value.split('\n').map(s=>s.trim()).filter(Boolean);
$('p_cmp').onclick=async()=>{$('p_msg').textContent='working...';try{const r=await api('/api/local/compare','POST',{inputs:lines(),chain:$('a_chain').value});
 $('p_out').innerHTML='<table><tr><th>Name</th><th>Score</th><th>High flags</th><th>Holders</th><th>Verified</th></tr>'+r.rows.map(x=>x.error?`<tr><td>${esc(x.input)}</td><td colspan=4 class="bad">${esc(x.error)}</td></tr>`:`<tr><td>${esc(x.name)}</td><td>${esc(x.risk_score)}</td><td>${esc(x.high_flags)}</td><td>${esc(x.holders??'?')}</td><td>${esc(x.verified??'?')}</td></tr>`).join('')+'</table>';$('p_msg').textContent=''}catch(e){$('p_msg').textContent=e.message}};
$('p_exp').onclick=async()=>{$('p_msg').textContent='working...';try{const r=await api('/api/local/export','POST',{inputs:lines(),chain:$('a_chain').value});
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(r,null,2)],{type:'application/json'}));a.download='mintscope-export.json';a.click();$('p_msg').textContent=`exported ${r.count}`}catch(e){$('p_msg').textContent=e.message}};
$('d_go').onclick=async()=>{$('d_msg').textContent='Scanning public chain data (today ~1 min, month ~12 min)...';
 try{const r=await api(`/discover?period=${$('d_period').value}&chain=${$('d_chain').value}&limit=20`);$('d_msg').textContent=`${r.new_collections_found} new collections found`;
 $('d_out').innerHTML='<div class="card"><table><tr><th>#</th><th>Name</th><th>Chain</th><th>Score</th><th>Minters</th><th>Red flags</th></tr>'+r.results.map(x=>`<tr><td>${x.rank}</td><td>${esc(x.name)}<div class="mut">${esc(x.contract_address)}</div></td><td>${esc(x.chain)}</td><td>${esc(x.score.value)}</td><td>${esc(x.metrics.unique_minters)}</td><td>${flags(x.red_flags)}</td></tr>`).join('')+'</table></div>'}catch(e){$('d_msg').textContent=e.message}};
async function loadFeatured(){try{const r=await api('/api/local/featured');
 $('f_out').innerHTML=(r.items.length?'<div class="grid">'+r.items.map(i=>`<div class="card feat">${i.image_url?`<img src="${esc(i.image_url)}" alt="">`:''}
 <div class="row" style="justify-content:space-between"><b>${esc(i.name)}</b>${i.sponsored?`<span class="sp">Sponsored</span>`:''}</div>
 <div class="mut">${esc(i.chain)} · ${esc(i.contract_address.slice(0,10))}…</div><div><span class="score" style="font-size:1.4rem">${esc(i.risk.score)}</span><span class="mut">/100 safety</span></div>
 <p>${flags(i.risk.red_flags)}</p><p class="mut">${esc(i.note)}</p>${i.sponsored?`<p class="mut">Sponsored ${esc(i.sponsored.start.slice(0,10))} to ${esc(i.sponsored.end.slice(0,10))}. Does not change the score.</p>`:''}
 ${i.mint_redirect?`<a href="${esc(i.mint_redirect)}" target="_blank" rel="noopener">Mint page</a>`:''}</div>`).join('')+'</div>':'<div class="card mut">No featured collections yet.</div>')+`<p class="mut">${esc(r.disclosure||'')}</p>`}catch(e){$('f_out').innerHTML=`<p class="bad">${esc(e.message)}</p>`}}
$('s_go').onclick=async()=>{$('s_out').textContent='Analyzing...';try{const r=await api('/api/local/submit','POST',{input:$('s_in').value,chain:$('s_chain').value,mint_url:$('s_mint').value||null});
 $('s_out').innerHTML=`${esc(r.message)} Score: ${esc(r.risk_score??'n/a')}. ${r.suspicious?'<b class="bad">Flagged for careful review.</b>':''} ${flags(r.checks)}`}catch(e){$('s_out').textContent=e.message}};
$('k_save').onclick=async()=>{try{const r=await api('/api/local/key','POST',{opensea_api_key:$('k_in').value});$('k_in').value='';$('k_msg').textContent=`Saved (${r.masked}).`;state()}catch(e){$('k_msg').textContent=e.message}};
$('k_test').onclick=async()=>{$('k_msg').textContent='Testing...';try{const r=await api('/api/local/key/test','POST',{opensea_api_key:$('k_in').value||null});$('k_msg').innerHTML=`<span class="${r.ok?'ok':'bad'}">${esc(r.message)}</span>`}catch(e){$('k_msg').textContent=e.message}};
$('k_del').onclick=async()=>{await api('/api/local/key','DELETE');$('k_msg').textContent='Removed.';state()};
$('l_go').onclick=async()=>{try{const r=await api('/api/local/license','POST',{license:$('l_in').value});$('l_in').value='';$('l_msg').textContent='Activated: '+r.license.tier;state()}catch(e){$('l_msg').textContent=e.message}};
state();
// Deep links: #analyze=<contract or link>, #tab=discover|featured|settings
(()=>{const h=decodeURIComponent(location.hash.slice(1));if(h.startsWith('analyze=')){$('a_in').value=h.slice(8);$('a_go').click()}
 else if(h.startsWith('tab=')){const b=document.querySelector(`nav button[data-t="${h.slice(4)}"]`);if(b)b.click()}})();
</script></body></html>"""
