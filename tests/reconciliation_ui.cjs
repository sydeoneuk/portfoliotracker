// Run with: node --test tests/reconciliation_ui.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

class Element {
  constructor() { this.children = []; this.events = {}; this.value = ''; this.checked = false; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(event, handler) { this.events[event] = handler; }
  setAttribute() {}
  focus() {}
}
function all(element) { return [element, ...element.children.flatMap(all)]; }
const settle = () => new Promise(resolve => setImmediate(resolve));

for (const outcome of ['needs_review', 'verified', 'ambiguous']) {
  test(`manual verification presents actionable ${outcome} outcome`, async () => {
    const elements = {};
    const get = id => elements[id] ||= new Element();
    let proposal = {id: 1, status: 'ambiguous', can_apply: false,
      candidate: {symbol: 'AV.L', verified: true, evidence: []}};
    const item = {ticker: 'AV', name: 'Aviva', isin: 'GB-test', quantity: 1,
      currency_code: 'GBP', proposals: [proposal]};
    let calls = 0;
    vm.runInNewContext(fs.readFileSync('app/web/static/reconciliation.js', 'utf8'), {
      document: {getElementById: get, createElement: () => new Element()},
      URLSearchParams,
      fetch: async (path, options) => ({ok: true, json: async () => {
        if (path.endsWith('/history')) return {items: []};
        if (options?.method === 'POST') { calls++; return {proposals: [proposal]}; }
        return {items: [item], total: 1, offset: 0};
      }})
    });
    await settle();
    assert.equal(get('recon-status').textContent, undefined, 'Initial render: ' + get('recon-status').textContent);
    const find = label => all(get('recon-results')).find(e => e.textContent === label);
    assert.equal(find('Verify statement correction'), undefined);
    await find('Choose this symbol for review').events.click();
    proposal = {id: 2, status: outcome, can_apply: outcome === 'verified',
      candidate: {symbol: 'AV.L', verified: outcome !== 'needs_review',
        evidence: outcome === 'needs_review' ? ['Candidate ISIN does not match.'] : []}};
    await find('Verify entered symbol').events.click();
    assert.equal(calls, 1);
    assert.equal(Boolean(find('Apply verified correction')), outcome === 'verified');
    assert.equal(Boolean(find('Choose this symbol for review')), outcome === 'ambiguous');
    const message = get('recon-status').textContent;
    assert.match(message, outcome === 'needs_review' ? /Verification failed/ :
      outcome === 'verified' ? /Verification passed/ : /More than one listing/);
    if (outcome === 'needs_review') {
      assert.ok(all(get('recon-results')).some(e => e.textContent?.includes('Cannot apply this symbol')));
    }
  });
}

test('admin statement verification previews catalogue changes before apply', async () => {
  const elements = {};
  const get = id => elements[id] ||= new Element();
  get('listing-reconciliation').dataset = {metadataAdmin: 'true'};
  let submitted;
  const correction = {before: {isin: 'OLD', currency_code: null, mic_code: null},
    after: {isin: 'GB00BP6MXD84', currency_code: 'GBP', mic_code: null},
    statement_reference: 'Statement November 2026'};
  vm.runInNewContext(fs.readFileSync('app/web/static/reconciliation.js', 'utf8'), {
    document: {getElementById: get, createElement: () => new Element()}, URLSearchParams,
    fetch: async (path, options) => ({ok: true, json: async () => {
      if (path.endsWith('/history')) return {items: []};
      if (options?.method === 'POST') {
        assert.equal(path, '/admin/reconciliation/metadata/propose');
        submitted = JSON.parse(options.body);
        return {proposals: [{id: 7, status: 'verified', can_apply: true,
          candidate: {symbol: 'SHEL.L', verified: true, evidence: [], metadata_correction: correction}}]};
      }
      return {items: [{ticker: 'SHEL', isin: 'OLD', quantity: 1, proposals: []}], total: 1, offset: 0};
    }})
  });
  await settle();
  const find = label => all(get('recon-results')).find(e => e.textContent === label);
  all(get('recon-results')).find(e => e.placeholder === 'Yahoo symbol, e.g. SHEL.L').value = 'SHEL.L';
  find('Statement ISIN').children[0].value = 'GB00BP6MXD84';
  find('Statement price units').children[0].value = 'GBP';
  find('Statement reference and date').children[0].value = correction.statement_reference;
  await find('Verify statement correction').events.click();
  assert.equal(submitted.currency, 'GBP');
  assert.equal(submitted.isin, 'GB00BP6MXD84');
  assert.ok(find('currency_code: missing → GBP'));
  assert.ok(find('Apply verified metadata and mapping'));
  assert.match(get('recon-status').textContent, /Metadata verified/);
});
