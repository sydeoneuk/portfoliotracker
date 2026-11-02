const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag) {this.tag = tag; this.children = []; this.events = {}; this.value = ''; this.checked = false;}
  append(...items) {this.children.push(...items);}
  prepend(...items) {this.children.unshift(...items);}
  replaceChildren(...items) {this.children = items;}
  addEventListener(event, handler) {this.events[event] = handler;}
  setAttribute(key, value) {this[key] = value;}
  set innerHTML(_) {throw new Error('Render source text safely');}
}
const all = el => [el, ...el.children.flatMap(all)];
const find = (el, text) => all(el).find(item => item.textContent === text);
function setup(fetch) {
  const window = {};
  vm.runInNewContext(fs.readFileSync('app/web/static/holding-attention.js', 'utf8'), {
    window, fetch, document: {getElementById: () => null, createElement: tag => new Element(tag)}});
  const container = new Element('div');
  window.HoldingAttention.renderItem(container, {ticker: '<TEST>', account_name: 'ISA', platform_id: 7,
    reason: 'Check statement', can_review: true, isin: '', currency: '', mic: '',
    hints: {isin: 'GB00BP6MXD84', currency: 'GBP', symbol: 'SHEL.L'}});
  return container;
}
const reply = result => ({ok: true, json: async () => result});
const verified = {can_apply: true, review_token: 'signed-review', candidate: {
  symbol: 'SHEL.L', isin: 'GB00BP6MXD84', exchange: 'LSE', currency: 'GBX', price: '2000', observed_at: 'today'}};

test('statement confirmation is required and catalogue hints are copied only explicitly', async () => {
  let calls = 0; const container = setup(async () => {calls++;});
  const fields = all(container).filter(el => el.tag === 'input' && el.type === 'text');
  assert.equal(fields[0].value, '');
  await find(container, 'Verify investment').events.click();
  assert.equal(calls, 0);
  find(container, 'Copy catalogue hints').events.click();
  assert.equal(fields[0].value, 'GB00BP6MXD84');
  assert.equal(all(container).find(el => el.type === 'checkbox').checked, false);
});

test('verified review exposes an explicit apply, then reports saved match and failed price separately', async () => {
  const requests = [];
  const container = setup(async (url, options) => {
    requests.push([url, JSON.parse(options.body)]);
    return reply(url.endsWith('verify') ? verified : {pricing: {status: 'unavailable', reason: 'Provider offline; retry.'}});
  });
  all(container).find(el => el.type === 'checkbox').checked = true;
  await find(container, 'Verify investment').events.click();
  assert.equal(requests.length, 1);
  await find(container, 'Confirm match and refresh price').events.click();
  assert.deepEqual(requests[1], ['/imports/attention/apply', {review_token: 'signed-review', reviewed: true}]);
  assert.ok(find(container, 'Refresh this price'));
  assert.ok(all(container).some(el => String(el.textContent).includes('Match saved. Provider offline')));
});

test('editing statement details invalidates the verified apply action', async () => {
  const container = setup(async () => reply(verified));
  all(container).find(el => el.type === 'checkbox').checked = true;
  await find(container, 'Verify investment').events.click();
  assert.ok(find(container, 'Confirm match and refresh price'));
  const field = all(container).find(el => el.type === 'text');
  field.value = 'CHANGED'; field.events.input();
  assert.equal(find(container, 'Confirm match and refresh price'), undefined);
});

test('ambiguous listings require selection and re-verification; failed verification cannot apply', async () => {
  const container = setup(async () => reply({can_apply: false,
    candidates: [{symbol: 'ONE.L', exchange: 'LSE', currency: 'GBX'}], message: 'Choose listing'}));
  all(container).find(el => el.type === 'checkbox').checked = true;
  await find(container, 'Verify investment').events.click();
  assert.equal(find(container, 'Confirm match and refresh price'), undefined);
  find(container, 'ONE.L · LSE · GBX').events.click();
  assert.equal(all(container).filter(el => el.type === 'text')[4].value, 'ONE.L');
  assert.equal(find(container, 'Confirm match and refresh price'), undefined);
});

test('stale apply removes its action and directs the user back to verification', async () => {
  const container = setup(async url => url.endsWith('verify') ? reply(verified) : {
    ok: false, status: 409, json: async () => ({detail: 'Source changed.'})});
  all(container).find(el => el.type === 'checkbox').checked = true;
  await find(container, 'Verify investment').events.click();
  await find(container, 'Confirm match and refresh price').events.click();
  assert.equal(find(container, 'Confirm match and refresh price'), undefined);
  assert.ok(all(container).some(el => String(el.textContent).includes('Verify the investment again')));
});

test('loading attention attempts automatic repair before presenting a manual form', async () => {
  const elements = {};
  for (const id of ['holding-attention-load', 'holding-attention-results', 'holding-attention-status',
      'holding-attention-search', 'holding-attention-prev', 'holding-attention-next']) elements[id] = new Element('div');
  const calls = []; let lists = 0;
  const window = {};
  vm.runInNewContext(fs.readFileSync('app/web/static/holding-attention.js', 'utf8'), {
    window, document: {getElementById: id => elements[id], createElement: tag => new Element(tag)},
    fetch: async (url, options) => {
      calls.push(url);
      if (url.includes('/list?')) return reply(++lists === 1 ? {total: 1, items: [{
        platform_id: 7, ticker: 'AAIF', can_review: true, reason: 'Legacy holding'}]} : {total: 0, items: []});
      assert.equal(url, '/imports/attention/auto');
      assert.deepEqual(JSON.parse(options.body), {platform_id: 7, ticker: 'AAIF'});
      return reply({status: 'matched', pricing: {status: 'fetched'}});
    }
  });
  elements['holding-attention-load'].events.click();
  // The click launches an asynchronous page operation, as in the browser.
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.length, 3);
  assert.equal(all(elements['holding-attention-results']).some(el => el.type === 'text'), false);
  assert.ok(elements['holding-attention-status'].textContent.includes('1 prices restored automatically'));
});
