const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = '') {this.tag = tag; this.children = []; this.events = {}; this.style = {}; this.value = ''; this.checked = false;}
  append(...items) {this.children.push(...items);}
  prepend(...items) {this.children.unshift(...items);}
  after(item) {this.sibling = item;}
  replaceChildren(...items) {this.children = items;}
  addEventListener(event, handler) {this.events[event] = handler;}
  set innerHTML(_) {throw new Error('Source data must be rendered as text');}
}
const all = element => [element, ...element.children.flatMap(all)];
function setup(fetch, elements = {}) {
  const window = {};
  vm.runInNewContext(fs.readFileSync('app/web/static/import-review.js', 'utf8'), {
    window, document: {getElementById: id => elements[id] || null, createElement: tag => new Element(tag)}, fetch
  });
  return window.ImportReview;
}
const preview = {batch_id: 7, status: 'review', source: 'ii_holdings', filename: '<script>bad</script>',
  effective_date: '2026-10-30', parser_version: 'csv-1', checksum: 'sha', summary: 'Preview only', can_apply: true,
  before: [{ticker: 'TEST', quantity: 7}], proposed: [{ticker: 'TEST', quantity: 10}]};

test('preview displays differences and requires review before apply', async () => {
  let submitted;
  const ui = setup(async (url, options) => {
    assert.equal(url, '/imports/apply'); submitted = JSON.parse(options.body);
    return {ok: true, json: async () => ({...preview, status: 'applied', summary: 'Applied', can_apply: false})};
  });
  const container = new Element(); ui.render(container, preview);
  const button = all(container).find(el => el.textContent === 'Apply reviewed import');
  const checkbox = all(container).find(el => el.type === 'checkbox');
  assert.equal(button.disabled, true);
  assert.ok(all(container).some(el => el.textContent === 'TEST'));
  checkbox.checked = true; checkbox.events.change();
  assert.equal(button.disabled, false);
  await button.events.click();
  assert.deepEqual(submitted, {batch_id: 7, reviewed: true});
  assert.ok(all(container).some(el => el.textContent === 'Applied'));
  assert.equal(all(container).some(el => el.textContent === 'Apply reviewed import'), false);
});

test('customer preview explains missing costs and keeps technical evidence collapsed', () => {
  const ui = setup(); const container = new Element();
  ui.render(container, {...preview, import_effect: {
    explanation: 'Uses statement quantities.', holdings_count: 1, costs_unavailable: 1
  }});
  assert.ok(all(container).some(el => el.textContent === 'Uses statement quantities.'));
  assert.ok(all(container).some(el => String(el.textContent).includes('Returns will be incomplete')));
  const technical = all(container).find(el => el.tag === 'details' &&
    el.children.some(child => child.textContent === 'Technical import details'));
  assert.ok(technical); assert.ok(!technical.open);
  assert.ok(all(technical).some(el => el.textContent === 'Checksum: sha'));
  assert.ok(all(container).some(el => el.textContent === 'Unavailable'));
});

test('failed application shows conflict without claiming success', async () => {
  const ui = setup(async () => ({ok: false, json: async () => ({detail: 'Account changed since preview'})}));
  const container = new Element(); ui.render(container, preview);
  const checkbox = all(container).find(el => el.type === 'checkbox'); checkbox.checked = true;
  const button = all(container).find(el => el.textContent === 'Apply reviewed import');
  await button.events.click();
  assert.ok(all(container).some(el => el.textContent === 'Account changed since preview'));
  assert.equal(button.disabled, false);
});

test('rejected uploads have no apply action', () => {
  const ui = setup(); const container = new Element();
  ui.render(container, {...preview, status: 'rejected', can_apply: false, errors: ['Older statement']});
  assert.ok(all(container).some(el => el.textContent === 'Older statement'));
  assert.equal(all(container).some(el => el.type === 'checkbox'), false);
});

test('automatic cash identity receipts show evidence without CSV matching actions', () => {
  const ui = setup(); const container = new Element();
  ui.render(container, {...preview, source: 't212_identity_reconciliation', status: 'applied',
    can_apply: false, before: undefined, proposed: undefined,
    identity_adoption: {table: 'transactions', record_pk: 5, before: {platform_id: null}, after: {platform_id: 7}}});
  assert.ok(all(container).some(el => el.tag === 'pre' && el.textContent.includes('platform_id')));
  assert.equal(all(container).some(el => el.textContent === 'Match investments'), false);
});

test('holdings uploads require a source date and pass it to the request', () => {
  const input = new Element('input'); input.id = 'csv-import-file';
  const ui = setup(undefined, {'csv-import-file': input, 'csv-import-platform-type': {value: 'interactive_investor'}});
  const fields = [];
  assert.throws(() => ui.options(input, {append: (...args) => fields.push(args)}, 'snapshot'), /effective date/);
  all(input.sibling).find(el => el.type === 'date').value = '2026-10-30';
  ui.options(input, {append: (...args) => fields.push(args)}, 'snapshot');
  assert.deepEqual(fields, [['effective_date', '2026-10-30']]);
});

test('matching keeps ambiguous holdings in a visible attention list and supports retry', async () => {
  const requests = [];
  const ui = setup(async (url, options) => {
    requests.push([url, options?.body]);
    if (!options) return {ok: true, json: async () => ({tickers: ['EXACT', '<AMBIGUOUS>']})};
    const {ticker} = JSON.parse(options.body);
    return {ok: true, json: async () => ({ticker,
      status: ticker === 'EXACT' ? 'reused' : 'needs_attention', reason: 'Choose a listing.'})};
  });
  const container = new Element();
  ui.render(container, {...preview, status: 'applied', can_apply: false});
  const button = all(container).find(el => el.textContent === 'Match investments');
  await button.events.click();
  assert.equal(requests.length, 3);
  assert.ok(all(container).some(el => el.textContent === '1 investments matched; 1 need attention. Your import is complete.'));
  assert.ok(all(container).some(el => el.textContent === '<AMBIGUOUS>: Needs attention — Choose a listing.'));
  assert.equal(button.disabled, false);
  assert.equal(all(container).some(el => el.textContent === 'Apply reviewed import'), false);
});

test('matching failure does not hide the completed import and allows retry', async () => {
  const ui = setup(async () => ({ok: false, json: async () => ({detail: 'Account is updating; retry.'})}));
  const container = new Element();
  ui.render(container, {...preview, status: 'applied', summary: 'Import complete', can_apply: false});
  const button = all(container).find(el => el.textContent === 'Match investments');
  await button.events.click();
  assert.ok(all(container).some(el => el.textContent === 'Import complete'));
  assert.ok(all(container).some(el => el.textContent === 'Account is updating; retry.'));
  assert.equal(button.disabled, false);
});

test('verified account price refresh reports failures and re-enables retry', async () => {
  const button = new Element('button'), status = new Element('div');
  setup(async (url, options) => {
    if (url === '/imports/pricing/work') return {ok: true, json: async () => ({items: [
      {platform_id: 7, ticker: 'ONE'}, {platform_id: 8, ticker: 'TWO'}]})};
    assert.equal(url, '/imports/attention/auto'); assert.equal(options.method, 'POST');
    const item = JSON.parse(options.body);
    return {ok: true, json: async () => ({status: 'matched', pricing: {
      status: item.ticker === 'ONE' ? 'fetched' : 'unavailable', reason: 'FX unavailable'}})};
  }, {'account-prices-refresh': button, 'account-prices-status': status});
  await button.events.click();
  assert.ok(all(status).some(el => String(el.textContent).includes('1 account quotes refreshed')));
  assert.ok(all(status).some(el => String(el.textContent).includes('FX unavailable')));
  assert.equal(button.disabled, false);
});
