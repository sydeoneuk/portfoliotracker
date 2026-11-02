(() => {
  'use strict';
  if (!document.getElementById('listing-identity')) return;
  const el = id => document.getElementById(id);
  const node = (tag, text, css) => {const result = document.createElement(tag); if (text !== undefined) result.textContent = text; if (css) result.className = css; return result;};
  let offset = 0, serial = 0;
  const status = text => {el('identity-status').textContent = text;};
  async function api(path, body) {
    const response = await fetch('/admin/listing-identity' + path, body === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Request failed');
    return data;
  }
  function button(label, action) {
    const control = node('button', label, 'btn btn-sm btn-outline-secondary'); control.type = 'button';
    control.addEventListener('click', async () => {
      control.disabled = true;
      try {await action();} catch (error) {status(error.message);}
      finally {control.disabled = false;}
    }); return control;
  }
  async function history() {
    const data = await api('/history');
    el('identity-history').replaceChildren(...data.items.map(item => {
      const row = node('div', undefined, 'border-bottom p-2');
      row.append(node('div', `${item.ticker} · ${item.action} · ${item.created_at} UTC · actor ${item.actor_id ?? 'deleted administrator'}`));
      row.append(node('div', `Listing ${item.before?.listing_id ?? 'unlinked'} → ${item.after?.listing_id ?? 'unlinked'}`));
      if (item.can_undo) row.append(button('Undo link change', async () => {
        const result = await api('/undo', {audit_id: item.id}); await load(); await history(); status(result.message);
      })); return row;
    }));
  }
  function render(item) {
    const card = node('details', undefined, 'border rounded bg-white p-3 mb-2');
    card.append(node('summary', `${item.ticker} — ${item.name || item.ticker} · ${item.status}`));
    card.append(node('p', `ISIN ${item.isin || 'missing'} · MIC ${item.mic_code || 'missing'} · Recorded units ${item.currency_code || 'missing'}`));
    card.append(node('p', `Security ID ${item.security_id ?? 'unresolved'} · Listing ID ${item.listing_id ?? 'unresolved'} · Quote ${item.quote_currency || 'unknown'} × ${item.unit_scale || '?'}`));
    const input = node('input', undefined, 'form-control form-control-sm mb-2'); input.value = item.yf_ticker || ''; input.maxLength = 40;
    input.setAttribute('aria-label', 'Yahoo symbol for ' + item.ticker);
    const review = node('div');
    card.append(input, button('Verify listing identity', async () => {
      review.replaceChildren(); status('Checking provider evidence…');
      const data = await api('/verify', {ticker: item.ticker, symbol: input.value.trim()});
      const candidate = data.candidate;
      review.append(node('p', `Provider: ${candidate.symbol} · ISIN ${candidate.isin || 'unverified'} · MIC ${candidate.mic || 'unverified'} · Quote units ${candidate.currency || 'unknown'}`));
      for (const reason of candidate.evidence || []) review.append(node('p', reason));
      const evidence = node('details'); evidence.append(node('summary', 'Verification evidence'), node('pre', JSON.stringify(candidate, null, 2), 'small text-wrap')); review.append(evidence);
      if (data.can_apply) {
        review.append(node('p', 'Applying links this alias to a stable listing. It does not merge holdings, orders, costs or dividend history. Review expires in 15 minutes.'));
        review.append(button('Apply verified alias link', async () => {
          const result = await api('/apply', {review_token: data.review_token}); await load(); await history();
          status(`Alias linked to security ${result.security_id}, listing ${result.listing_id}. Financial records are unchanged.`);
        }));
        status('Identity verified. Review the evidence before applying.');
      } else status('Identity unresolved. Resolve the evidence failures before linking; missing metadata can be repaired through the admin metadata review.');
    }), review); return card;
  }
  async function load() {
    const request = ++serial;
    const data = await api('/audit?' + new URLSearchParams({search: el('identity-query').value, offset}));
    if (request !== serial) return;
    el('identity-results').replaceChildren(...data.items.map(render));
    el('identity-page').textContent = `${data.total ? offset + 1 : 0}–${offset + data.items.length} of ${data.total}`;
    el('identity-prev').disabled = offset === 0; el('identity-next').disabled = offset + data.items.length >= data.total;
  }
  const refresh = () => load().catch(error => status(error.message));
  el('identity-search').addEventListener('submit', event => {event.preventDefault(); offset = 0; refresh();});
  el('identity-prev').addEventListener('click', () => {offset = Math.max(0, offset - 25); refresh();});
  el('identity-next').addEventListener('click', () => {offset += 25; refresh();});
  Promise.all([load(), history()]).catch(error => status(error.message));
})();
