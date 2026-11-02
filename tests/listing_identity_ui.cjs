const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() {this.children=[]; this.events={}; this.value='';}
  append(...items) {this.children.push(...items);}
  replaceChildren(...items) {this.children=items;}
  addEventListener(name, handler) {this.events[name]=handler;}
  setAttribute() {}
  set innerHTML(_) {throw new Error('Unsafe rendering of catalogue data');}
}
const all = element => [element, ...element.children.flatMap(all)];
const settle = () => new Promise(resolve => setImmediate(resolve));

for (const verified of [true, false]) {
  test(`identity evidence ${verified ? 'allows' : 'blocks'} explicit apply`, async () => {
    const elements={}; const get=id => elements[id] ||= new Element();
    let applied=false;
    vm.runInNewContext(fs.readFileSync('app/web/static/listing-identity.js','utf8'), {
      document:{getElementById:get,createElement:()=>new Element()}, URLSearchParams,
      fetch:async(path,options)=>({ok:true,json:async()=>{
        if (path.endsWith('/history')) return {items:[]};
        if (path.endsWith('/verify')) return {candidate:{symbol:'SHEL.L',isin:'GB00BP6MXD84',mic:'XLON',currency:'GBX',evidence:['Evidence']},can_apply:verified,review_token:verified?'signed-review':null};
        if (path.endsWith('/apply')) {applied=true; assert.equal(JSON.parse(options.body).review_token,'signed-review'); return {security_id:1,listing_id:2};}
        return {items:[{ticker:'SHEL',name:'Shell',status:applied?'verified':'unresolved',yf_ticker:'SHEL.L'}],total:1};
      }})
    });
    await settle();
    const find = label => all(get('identity-results')).find(el=>el.textContent===label);
    await find('Verify listing identity').events.click();
    assert.equal(applied,false);
    assert.equal(Boolean(find('Apply verified alias link')),verified);
    if (verified) {
      await find('Apply verified alias link').events.click();
      assert.equal(applied,true);
      assert.match(get('identity-status').textContent,/Financial records are unchanged/);
    } else assert.match(get('identity-status').textContent,/Identity unresolved/);
  });
}
