const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function chart(data) {
  const template = fs.readFileSync('app/web/templates/analysis.html', 'utf8');
  const source = template.slice(template.indexOf('  const historyMoney'), template.indexOf('  if (divData.labels.length'))
    .replace(/{{ as_of_date \| tojson }}/g, 'null').replace(/{{ as_of_date_label \| tojson }}/g, '""');
  const elements = new Map();
  const get = id => {if (!elements.has(id)) elements.set(id, {style:{}}); return elements.get(id);};
  const window = {location: {search:'?account=platform_7', href:''}};
  let config;
  vm.runInNewContext(source, {portfolioHistoryData:data, Intl, URLSearchParams, window,
    document:{getElementById:get}, Chart: function(el, value) {config=value;}});
  return {config, window, get};
}
test('current label and chart use display currency and preserve recorded-date navigation', () => {
  const ui = chart({labels:['01 Nov 2026','Current'], values:[100,123.45], latest:123.45, currency:'USD', iso_dates:['2026-11-01',null], point_notes:['Recorded daily value','Current market value'], note:'Last recorded values carried forward'});
  assert.match(ui.get('historyLatestLabel').textContent, /Current US\$123.45/);
  assert.match(ui.config.options.plugins.tooltip.callbacks.label({raw:123.45}), /US\$123.45/);
  ui.config.options.onClick({}, [{index:1}]);
  assert.equal(ui.window.location.href, '');
  ui.config.options.onClick({}, [{index:0}]);
  assert.match(ui.window.location.href, /account=platform_7&as_of=2026-11-01/);
  assert.match(ui.get('portfolioHistoryNote').textContent, /carried forward/);
});
test('missing history or quotes shows a useful message instead of an empty canvas', () => {
  const ui = chart({labels:[],values:[],latest:null,note:'Current total unavailable'});
  assert.equal(ui.get('portfolioHistoryWrap').hidden, true);
  assert.equal(ui.get('portfolioHistoryNote').textContent, 'Current total unavailable');
});
