const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
class Element {
  constructor(){this.children=[];this.events={};this.value='';this.checked=false;}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;}
  addEventListener(name,handler){this.events[name]=handler;}
  set innerHTML(_){throw new Error('Identity evidence must be rendered as text');}
}
const all=element=>[element,...element.children.flatMap(all)];
const settle=()=>new Promise(resolve=>setImmediate(resolve));

async function setup({verified=true,two=false,partial=false}={}){
  const elements={};const get=id=>elements[id]||=new Element();let submissions=0;
  get('scope-held').checked=true;
  const base={platform_id:1,owner_id:1,account_name:'ISA',ticker:'SHEL',status:'unresolved',claims:{isin:[],mic:[],currency:[]},
    catalogue_hint:{yf_ticker:'SHEL.L'},other_alias_hints:[],problems:[],references:[],reference_count:1};
  const rows=[{...base,namespace:'holdings:legacy'},...(two?[{...base,namespace:'events:t212'}]:[])];
  vm.runInNewContext(fs.readFileSync('app/web/static/account-identity.js','utf8'),{
    document:{getElementById:get,createElement:()=>new Element()},URLSearchParams,
    fetch:async(path,options)=>({ok:true,json:async()=>{
      if(path.endsWith('/history'))return{items:[]};
      if(path.endsWith('/verify')){const claim=JSON.parse(options.body);return{claim,candidate:{symbol:claim.symbol,evidence:['Exact ISIN match']},can_apply:verified,review_token:'token-'+claim.namespace};}
      if(path.endsWith('/apply-batch')){
        submissions++;const body=JSON.parse(options.body);assert.equal(body.reviewed,true);
        return{items:body.review_tokens.map((token,item)=>({item,status:partial&&item===1?'blocked':'applied',error:partial&&item===1?'Source changed':undefined}))};
      }
      return{items:rows,total:rows.length,unassigned_event_records:0};
    }})
  });
  await settle();
  async function verifyAndQueue(index){
    const card=get('scope-results').children[index];
    const find=text=>all(card).find(el=>el.textContent===text);
    for(const [label,value] of [['Statement ISIN','GB00BP6MXD84'],['Statement listing quote units (not cost/settlement currency)','GBP'],['Dated statement / source reference covering this scope','Statement 2026-11-01']])find(label).children[0].value=value;
    await find('Verify account/source identity').events.click();
    if(verified)await find('Queue verified scope').events.click();
    return card;
  }
  return{get,verifyAndQueue,submissions:()=>submissions};
}

test('verified scopes queue without application until batch is acknowledged',async()=>{
  const app=await setup();await app.verifyAndQueue(0);
  assert.equal(app.submissions(),0);assert.equal(app.get('scope-batch-apply').disabled,true);
  app.get('scope-batch-reviewed').checked=true;app.get('scope-batch-reviewed').events.change();
  assert.equal(app.get('scope-batch-apply').disabled,false);
  await app.get('scope-batch-apply').events.click();
  assert.equal(app.submissions(),1);assert.equal(app.get('scope-queue-count').textContent,'0 queued');
  assert.match(app.get('scope-status').textContent,/Financial records were not changed/);
});

test('failed verification offers no queue action',async()=>{
  const app=await setup({verified:false});const card=await app.verifyAndQueue(0);
  assert.equal(all(card).some(el=>el.textContent==='Queue verified scope'),false);
  assert.match(app.get('scope-status').textContent,/Verification failed/);
  assert.equal(app.submissions(),0);
});

test('partial batch shows each outcome and retains only blocked scopes',async()=>{
  const app=await setup({two:true,partial:true});await app.verifyAndQueue(0);await app.verifyAndQueue(1);
  app.get('scope-batch-reviewed').checked=true;app.get('scope-batch-reviewed').events.change();
  await app.get('scope-batch-apply').events.click();
  assert.equal(app.get('scope-queue-count').textContent,'1 queued');
  assert.ok(all(app.get('scope-batch-results')).some(el=>el.textContent?.includes('blocked — Source changed')));
  assert.ok(all(app.get('scope-batch-results')).some(el=>el.textContent?.includes('holdings:legacy · SHEL: applied')));
  assert.equal(app.get('scope-batch-reviewed').checked,false);
});
