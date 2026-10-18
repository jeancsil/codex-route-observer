(() => {
  const $ = selector => document.querySelector(selector);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = value => Number(value || 0).toLocaleString();
  const time = value => value == null ? '—' : value < 1000 ? `${Math.round(value)} ms` : `${(value/1000).toFixed(2)} s`;
  // Money is an exact decimal string from integer accounting on the server.
  const money = value => value == null ? 'Unavailable' : `$${value}`;
  const bytes = value => value == null ? '—' : value < 1024 ? `${value} B` : `${(value/1024).toFixed(1)} KB`;
  let offset = 0, busy = false, pricing, lastRows = '';
  const open = new Set(), bodies = new Map(), loading = new Set();
  const form = $('#jev-filters');

  async function get(path) {
    const response = await fetch(path, {cache:'no-store'});
    if (!response.ok) throw new Error(`Observer returned HTTP ${response.status}`);
    return response.json();
  }

  function params() {
    const p = new URLSearchParams({limit:'20',offset:String(offset)});
    if(form.elements.status.value) p.set('status',form.elements.status.value);
    if(form.elements.period.value) p.set('after',new Date(Date.now()-Number(form.elements.period.value)*3600000).toISOString());
    return p;
  }

  function render(data) {
    const s = data.summary;
    $('#jev-total').textContent = number(s.total);
    $('#jev-status-note').textContent = `${number(s.succeeded)} succeeded · ${number(s.errors)} failed · ${number(s.pending)} in flight${s.interrupted ? ` · ${s.interrupted} interrupted` : ''}`;
    $('#jev-time').textContent = time(s.avg_duration_ms);
    $('#jev-time-note').textContent = `P95 ${time(s.p95_duration_ms)} · queue ${time(s.avg_queue_ms)} avg`;
    $('#jev-tokens').textContent = s.usage_reported ? number(s.input_tokens) : '—';
    $('#jev-token-note').textContent = `${number(s.output_tokens)} output tokens · ${number(s.questions)} questions`;
    $('#jev-cost').textContent = s.cost_usd == null ? '—' : money(s.cost_usd);
    $('#jev-cost-note').textContent = `${number(s.priced)} priced · ${number(s.unpriced)} unavailable / in flight`;
    $('#jev-history').hidden = !s.legacy_classifications;
    $('#jev-history').textContent = `${number(s.legacy_classifications)} earlier classifications have no saved usage or response. Their costs are unavailable and excluded from these totals.`;
    const html = data.items.map(row => {
      const statusClass = row.status === 'success' ? 'same' : row.status === 'error' ? 'error' : 'pending';
      const response = row.response_bytes ? `<details class="jev-response" data-call-id="${esc(row.id)}" ${open.has(row.id) ? 'open' : ''}>
        <summary>Show response · ${bytes(row.response_bytes)}</summary>
        <div class="jev-response-meta">${number(row.question_count)} questions · ${bytes(row.request_bytes)} sent${row.response_truncated ? ' · response truncated at 64K characters' : ''}</div>
        <pre>${esc(bodies.get(row.id) || 'Loading response…')}</pre>
        <div class="jev-response-meta">${row.cost_usd == null ? 'Cost unavailable: missing usage, unverified model rate, or non-direct endpoint.' : `${number(row.input_tokens)} input tokens × $0.042 / 1M; output free. ${money(row.cost_usd)}`}</div>
      </details>` : `<span class="muted">${row.status === 'pending' ? 'Awaiting response' : 'No response body'}</span>`;
      return `<tr>
        <td><span class="row-time" title="${esc(new Date(row.created_at).toLocaleString())}">${esc(new Date(row.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}))}</span><span class="row-category">${esc(row.reported_model || row.requested_model)}</span><span class="row-category jev-session" title="${esc(row.session_id)}">${esc(row.session_id)}</span></td>
        <td><span class="badge ${statusClass}">${esc(row.status)}</span><span class="row-category">${row.http_status ? `HTTP ${row.http_status}` : 'No HTTP status'}${row.error_type ? ` · ${esc(row.error_type)}` : ''}</span></td>
        <td class="duration">${time(row.duration_ms)}<span class="row-category">Queue ${time(row.queue_ms)}</span></td>
        <td class="jev-token-cell">${row.input_tokens == null ? '—' : number(row.input_tokens)} / ${row.output_tokens == null ? '—' : number(row.output_tokens)}</td>
        <td class="jev-money">${money(row.cost_usd)}</td><td class="jev-response-cell">${response}</td>
      </tr>`;
    }).join('');
    if(lastRows !== html) { $('#jev-rows').innerHTML = html; lastRows = html; }
    $('#jev-empty').hidden = data.items.length > 0;
    $('#jev-page-info').textContent = s.total ? `${number(offset+1)}–${number(Math.min(offset+20,s.total))} of ${number(s.total)} API calls` : 'No captured calls';
    $('#jev-prev').disabled = offset === 0;
    $('#jev-next').disabled = offset+20 >= s.total;
  }

  async function refresh() {
    if(busy) return;
    busy = true;
    try {
      render(await get('/api/jev-calls?'+params()));
      $('#jev-error').hidden = true;
    } catch(error) {
      $('#jev-error').textContent = `Jev metrics unavailable: ${error.message}`;
      $('#jev-error').hidden = false;
    } finally { busy = false; }
  }

  $('#jev-rows').addEventListener('toggle', async event => {
    const detail = event.target.closest('.jev-response');
    if(!detail) return;
    const id = detail.dataset.callId;
    if(!detail.open) { open.delete(id); return; }
    open.add(id);
    if(open.size > 100) open.delete(open.values().next().value);
    if(loading.has(id)) return;
    if(bodies.has(id)) { detail.querySelector('pre').textContent = bodies.get(id); return; }
    loading.add(id);
    try {
      const call = await get('/api/jev-calls/'+encodeURIComponent(id));
      let body = call.response_text || 'No response body recorded.';
      try { body = JSON.stringify(JSON.parse(body),null,2); } catch { /* Preserve non-JSON errors. */ }
      bodies.set(id,body);
      const visible = [...$('#jev-rows').querySelectorAll('.jev-response')].find(node => node.dataset.callId === id);
      if(visible) visible.querySelector('pre').textContent = body;
      // Keep disclosure caches bounded while browsing long histories.
      if(bodies.size > 100) bodies.delete(bodies.keys().next().value);
    } catch(error) { detail.querySelector('pre').textContent = error.message; }
    finally { loading.delete(id); }
  }, true);
  form.addEventListener('submit',event=>event.preventDefault());
  form.addEventListener('change',()=>{offset=0;refresh();});
  $('#jev-prev').onclick=()=>{offset=Math.max(0,offset-20);refresh();};
  $('#jev-next').onclick=()=>{offset+=20;refresh();};

  async function init() {
    try {
      pricing=(await get('/api/config')).jev_pricing;
      if(pricing) $('#jev-pricing').innerHTML=`<a href="https://docs.typesafe.ai/models" target="_blank" rel="noopener noreferrer">Verified rate</a>: $${esc(pricing.input_usd_per_million)} / 1M input tokens; output free. Checked ${esc(pricing.verified_at)}. Exact list-price calculation, excluding credits, discounts, and tax. Each call stores its model and rate.`;
    } catch { /* Call metrics can still load if configuration is temporarily unavailable. */ }
    await refresh();
  }
  init();
  setInterval(()=>{if(!document.hidden && $('#live-toggle').getAttribute('aria-pressed')==='true') refresh();},3000);
  $('#live-toggle').addEventListener('click',()=>{if($('#live-toggle').getAttribute('aria-pressed')==='true') refresh();});
})();
