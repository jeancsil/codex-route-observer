const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = (n) => Number(n || 0).toLocaleString();
const duration = (n) => n == null ? '—' : n < 1000 ? `${Math.round(n)} ms` : `${(n / 1000).toFixed(1)} s`;
const percent = (n) => n == null ? '—' : `${Math.round(n * 100)}%`;
let config, offset = 0, items = [], total = 0, live = true, busy = false, detailId = null, debounce;
const form = $('#filters');
const params = () => {
  const p = new URLSearchParams();
  for (const [key, value] of new FormData(form)) {
    if (!value) continue;
    p.set(key, key === 'after' || key === 'before' ? new Date(value).toISOString() : value);
  }
  p.set('offset', offset); p.set('limit', '50');
  return p;
};
const name = (model) => config?.policy.models[model]?.label || model || 'Not provided';
async function get(url) {
  const response = await fetch(url, {cache: 'no-store'});
  if (!response.ok) throw new Error(`Observer returned HTTP ${response.status}`);
  return response.json();
}
function comparison(row) {
  if (row.classification_status !== 'complete') return `<span class="badge pending">${esc(row.classification_status)}</span>`;
  if (row.uncertain) return '<span class="badge uncertain">Low confidence</span>';
  if (row.mismatch == null) return '<span class="badge pending">Unknown sent model</span>';
  return row.mismatch ? '<span class="badge change">Different model ↗</span>' : '<span class="badge same">Same model</span>';
}
function render(data) {
  items = data.items; total = data.summary.total;
  const s = data.summary;
  $('#total').textContent = fmt(s.total);
  $('#scope-label').textContent = s.latest_request_at ? `Latest: ${new Date(s.latest_request_at).toLocaleString()}` : 'Across the current view';
  $('#mismatches').textContent = s.classified ? fmt(s.mismatches) : '—';
  $('#mismatch-note').textContent = s.classified ? `${percent(s.mismatches / s.classified)} of classified requests` : 'Waiting for classifications';
  $('#classified').textContent = fmt(s.classified);
  $('#pending-note').textContent = `${fmt(s.pending)} pending · ${fmt(s.uncertain)} low confidence${s.classification_errors ? ` · ${s.classification_errors} unavailable` : ''}`;
  $('#latency').textContent = duration(s.avg_latency_ms);
  $('#request-count').textContent = fmt(s.total);
  const max = Math.max(1, ...data.matrix.map(x => x.count));
  $('#route-map').innerHTML = data.matrix.length ? data.matrix.slice(0, 6).map(x => `
    <div class="route ${x.requested_model === x.recommended_model ? 'same' : ''}">
      <div class="route-label"><span class="model" title="${esc(x.requested_model)}">${esc(name(x.requested_model))}</span><span class="arrow">→</span><span class="model" title="${esc(x.recommended_model)}">${esc(name(x.recommended_model))}</span></div>
      <div class="route-count"><span>${fmt(x.count)} request${x.count === 1 ? '' : 's'}</span><span>${percent(x.count / Math.max(1,s.classified))}</span></div>
      <div class="route-bar"><span style="width:${x.count / max * 100}%"></span></div>
    </div>`).join('') : '<p class="empty-inline">Model comparisons will appear as requests are classified.</p>';
  const html = items.map(row => {
    const date = new Date(row.created_at);
    return `<tr tabindex="0" data-id="${esc(row.id)}" aria-label="View request ${esc(row.id.slice(0,8))}">
      <td><span class="row-time" title="${esc(date.toLocaleString())}">${esc(date.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}))}</span><span class="row-category">${esc(row.category || row.classification_status)}</span></td>
      <td><div class="prompt">${esc(row.prompt_preview || 'No user request in available context')}</div><div class="session">${esc(row.session_id)}</div></td>
      <td><span class="model-name">${esc(name(row.requested_model))}</span></td>
      <td><span class="model-name">${row.recommended_model ? esc(name(row.recommended_model)) : `<span class="muted">${esc({pending:'Awaiting verdict',disabled:'Jev disabled',error:'Unavailable',skipped:'Not classified',interrupted:'Interrupted'}[row.classification_status] || row.classification_status)}</span>`}</span></td>
      <td><div class="confidence"><div class="mini-bar"><span style="width:${Math.max(0,Math.min(100,(row.confidence || 0)*100))}%"></span></div>${percent(row.confidence)}</div></td>
      <td>${comparison(row)}</td><td class="duration">${duration(row.latency_ms)}<span class="row-category">${esc(row.delivery_status)}</span></td></tr>`;
  }).join('');
  if ($('#rows').innerHTML !== html) $('#rows').innerHTML = html;
  $('#empty').hidden = items.length > 0;
  if (!items.length) {
    const filtered = [...new FormData(form).values()].some(Boolean);
    $('#empty h3').textContent = filtered ? 'No requests match these filters.' : 'Your next request starts the story.';
    $('#empty p').textContent = filtered ? 'Try another model, task, or time range.' : 'Start the observer, then launch Codex through it. Requests will appear here as you work.';
    $('#empty code').hidden = filtered;
  }
  $('#page-info').textContent = total ? `${fmt(offset+1)}–${fmt(Math.min(offset+50,total))} of ${fmt(total)} requests` : 'No requests';
  $('#prev').disabled = offset === 0; $('#next').disabled = offset + 50 >= total;
  $('#export').disabled = items.length === 0;
}
async function refresh() {
  if (busy) return;
  busy = true;
  try {
    const data = await get('/api/requests?' + params());
    render(data); $('#load-error').hidden = true;
    if (detailId && $('#detail').open) showDetail(await get('/api/requests/' + encodeURIComponent(detailId)));
  } catch (error) {
    $('#load-error').textContent = `${error.message}. Retrying while live updates are enabled.`;
    $('#load-error').hidden = false;
  } finally { busy = false; }
}
async function facets() {
  const data = await get('/api/facets');
  for (const key of ['requested_model','recommended_model','category','session_id']) {
    const select = form.elements[key], value = select.value;
    const options = [...new Set([...(data[key] || []), ...(value ? [value] : [])])];
    const first = select.options[0].outerHTML;
    select.innerHTML = first + options.map(v => `<option value="${esc(v)}">${esc(key.includes('model') ? name(v) : v)}</option>`).join('');
    select.value = value;
  }
}
function filterChanged() {
  offset = 0;
  const p = params(); p.delete('offset'); p.delete('limit');
  history.replaceState(null, '', location.pathname + (p.size ? '?' + p : ''));
  clearTimeout(debounce); debounce = setTimeout(refresh, 200);
}
form.addEventListener('submit', e => { e.preventDefault(); filterChanged(); });
form.addEventListener('input', filterChanged);
$('#clear').onclick = () => { form.reset(); filterChanged(); };
$('#prev').onclick = () => { offset = Math.max(0, offset-50); refresh(); };
$('#next').onclick = () => { offset += 50; refresh(); };
$('#live-toggle').onclick = () => {
  live = !live; $('#live-toggle').setAttribute('aria-pressed', String(live));
  $('#live-label').textContent = live ? 'Live updates' : 'Updates paused';
  if (live) refresh();
};
function meta(label, value) { return `<div><dt>${esc(label)}</dt><dd>${esc(value ?? '—')}</dd></div>`; }
function showDetail(row) {
  $('#detail-content').innerHTML = `<h2>${esc(row.category ? row.category[0].toUpperCase() + row.category.slice(1) : 'Request')} · ${esc(row.id.slice(0,8))}</h2>
    <div class="detail-pair"><small>SENT TO</small><strong>${esc(name(row.requested_model))}</strong><span class="arrow">↓</span><small>JEV RECOMMENDS</small><strong>${esc(row.recommended_model ? name(row.recommended_model) : row.classification_status)}</strong></div>
    <p>${comparison(row)} &nbsp; ${percent(row.confidence)} confidence</p>
    <dl class="detail-meta">${meta('Time',new Date(row.created_at).toLocaleString())}${meta('Transport',row.transport)}${meta('Session',row.session_id)}${meta('Reported by upstream',row.reported_model || 'Not provided')}${meta('Reasoning effort',row.reasoning_effort)}${meta('Delivery',row.delivery_status)}${meta('Upstream duration',duration(row.latency_ms))}${meta('First byte',duration(row.first_byte_ms))}${meta('Jev duration',duration(row.classification_ms))}${meta('Input / output tokens',`${row.input_tokens ?? '—'} / ${row.output_tokens ?? '—'}`)}${meta('Cached input tokens',row.cached_tokens)}${meta('Policy',row.policy_version)}${meta('Classifier model',row.classifier_model)}${meta('Context',row.context_missing ? 'User request unavailable' : row.context_truncated ? 'Bounded context excerpt' : 'Available context')}</dl>
    ${row.rationale ? `<h3>Policy rationale</h3><p>${esc(row.rationale)}</p>` : ''}
    ${row.classification_error ? `<h3>Classification unavailable</h3><p>${esc(row.classification_error)}</p>` : ''}
    ${row.upstream_error ? `<h3>Upstream issue</h3><p>${esc(row.upstream_error)}</p>` : ''}
    ${row.probabilities ? `<h3>Model probabilities</h3>${Object.entries(row.probabilities).sort((a,b)=>b[1]-a[1]).map(([m,p])=>`<div class="probability"><div><span>${esc(name(m))}</span><span>${percent(p)}</span></div><div class="route-bar"><span style="width:${Math.max(0,Math.min(100,p*100))}%"></span></div></div>`).join('')}` : ''}
    <h3>Latest user request · stored excerpt</h3><div class="detail-preview">${esc(row.prompt_preview || 'No user request in available context')}</div>`;
}
async function openDetail(id) {
  try { detailId = id; showDetail(await get('/api/requests/'+encodeURIComponent(id))); $('#detail').showModal(); }
  catch (error) { $('#load-error').textContent = error.message; $('#load-error').hidden = false; }
}
$('#rows').addEventListener('click', e => { const row=e.target.closest('[data-id]'); if(row) openDetail(row.dataset.id); });
$('#rows').addEventListener('keydown', e => { if(e.key==='Enter'||e.key===' '){ const row=e.target.closest('[data-id]'); if(row){e.preventDefault();openDetail(row.dataset.id);} } });
$('#close-detail').onclick = () => { $('#detail').close(); detailId = null; };
$('#detail').addEventListener('close', () => { detailId = null; });
$('#policy-button').onclick = () => {
  if (!config) return;
  $('#policy-content').innerHTML = `<h2>${esc(config.policy.version)}</h2><p>${esc(config.policy.objective)}</p><p>Confidence below ${percent(config.policy.minimum_confidence)} is marked for review.</p>${Object.entries(config.policy.models).map(([id,m])=>`<div class="policy-card"><strong>${esc(m.label)}</strong><code>${esc(id)}</code><p>${esc(m.criteria)}</p></div>`).join('')}<p>Update <code>policy.toml</code> and restart the observer to change the candidates or decision criteria.</p>`;
  $('#policy').showModal();
};
$('#close-policy').onclick = () => $('#policy').close();
for (const id of ['detail','policy']) $('#'+id).addEventListener('click', e => { if(e.target === $('#'+id) && e.clientX < e.target.getBoundingClientRect().left) e.target.close(); });
$('#export').onclick = () => {
  const columns=['created_at','session_id','requested_model','reported_model','recommended_model','category','complexity','confidence','mismatch','classification_status','delivery_status','latency_ms','prompt_preview'];
  const cell = value => { let s=String(value ?? ''); if (/^[=+\-@\t\r]/.test(s)) s="'"+s; return '"'+s.replace(/"/g,'""')+'"'; };
  const csv=[columns.map(cell).join(','),...items.map(r=>columns.map(k=>cell(r[k])).join(','))].join('\r\n');
  const link=document.createElement('a'); link.href=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'})); link.download='codex-requests.csv'; link.click(); setTimeout(()=>URL.revokeObjectURL(link.href),1000);
};
async function init() {
  try {
    config=await get('/api/config'); $('#policy-version').textContent=config.policy.version;
    if(!config.classification_enabled){ $('#notice').hidden=false; $('#notice').textContent='Jev classification is off. Export TYPESAFE_API_KEY in the server’s shell and restart to enable model recommendations. Requests are still forwarded and recorded.'; }
    const query=new URLSearchParams(location.search);
    for(const [key,value] of query){
      const field=form.elements[key]; if(!field) continue;
      if(key==='after'||key==='before'){
        const date=new Date(value); if(!Number.isNaN(date.valueOf())){date.setMinutes(date.getMinutes()-date.getTimezoneOffset());field.value=date.toISOString().slice(0,16);}
      }else{if(field.tagName==='SELECT'&&![...field.options].some(o=>o.value===value))field.add(new Option(value,value));field.value=value;}
    }
    await facets(); await refresh();
  }catch(error){$('#load-error').textContent=error.message;$('#load-error').hidden=false;}
}
init();
setInterval(()=>{if(live&&!document.hidden)refresh();},3000);
setInterval(()=>{if(live&&!document.hidden)facets().catch(()=>{});},12000);
