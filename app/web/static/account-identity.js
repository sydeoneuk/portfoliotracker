(() => {
  'use strict';
  if (!document.getElementById('account-identity')) return;
  const el=id=>document.getElementById(id);
  const node=(tag,text,css)=>{const element=document.createElement(tag); if(text!==undefined)element.textContent=text; if(css)element.className=css; return element;};
  const queue=new Map(); let offset=0, serial=0;
  const status=text=>{el('scope-status').textContent=text;};
  const key=item=>JSON.stringify([item.platform_id,item.namespace,item.ticker]);
  async function api(path,body) {
    const response=await fetch('/admin/account-identity'+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await response.json(); if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'Request failed'); return data;
  }
  function button(text,action) {
    const control=node('button',text,'btn btn-sm btn-outline-secondary');control.type='button';
    control.addEventListener('click',async()=>{control.disabled=true;try{await action();}catch(error){status(error.message);}finally{control.disabled=false;}});return control;
  }
  function renderQueue() {
    el('scope-queue-count').textContent=queue.size+' queued';
    el('scope-queue').replaceChildren(...[...queue.entries()].map(([id,item])=>{
      const row=node('div',undefined,'border-bottom py-2');
      row.append(node('span',`${item.platform_id} · ${item.namespace} · ${item.ticker} · ${item.claim.isin} · ${item.claim.currency} · ${item.claim.statement_reference} `));
      row.append(button('Remove',async()=>{queue.delete(id);renderQueue();}));return row;
    }));
    el('scope-batch-apply').disabled=!queue.size||!el('scope-batch-reviewed').checked;
  }
  function render(item) {
    const card=node('details',undefined,'border rounded bg-white p-3 mb-2');
    card.append(node('summary',`${item.account_name} (#${item.platform_id}, owner ${item.owner_id}) · ${item.namespace} · ${item.ticker} · ${item.status}`));
    if(item.legacy_alias_conflict)card.append(node('p','This legacy ticker has different identity claims across accounts/sources. Resolve each scope independently.','text-danger'));
    card.append(node('p','Retained identity claims: '+JSON.stringify(item.claims)));
    card.append(node('p','Catalogue hints (not source proof): '+JSON.stringify(item.catalogue_hint),'small text-muted'));
    if(item.other_alias_hints.length)card.append(node('p','Other catalogue aliases to review: '+item.other_alias_hints.join(', '),'small'));
    for(const problem of item.problems)card.append(node('p',problem,'text-danger'));
    const refs=node('details');refs.append(node('summary',`${item.reference_count} referenced records; up to 30 evidence references`),node('pre',JSON.stringify(item.references,null,2),'small text-wrap'));card.append(refs);
    if(item.resolution)card.append(node('p',`Existing scoped listing ID ${item.resolution.listing_id}; statement ${item.resolution.claim.statement_reference}`));
    if(item.active===false){card.append(node('p','This account is archived. Its evidence remains available, but new resolutions are blocked.'));return card;}
    if(item.problems.length)return card;
    function field(title,value,max) {const label=node('label',title,'d-block small mt-2');const input=node('input',undefined,'form-control form-control-sm');input.value=value||'';input.maxLength=max;label.append(input);card.append(label);return input;}
    const isin=field('Statement ISIN',item.claims.isin[0]||'',12);
    const units=field('Statement listing quote units (not cost/settlement currency)',item.claims.currency[0]||'',3);
    const mic=field('Statement MIC if supplied',item.claims.mic[0]||'',4);
    const symbol=field('Provider symbol to verify',item.catalogue_hint.yf_ticker||'',40);
    const reference=field('Dated statement / source reference covering this scope','',240);
    const review=node('div');
    card.append(button('Verify account/source identity',async()=>{
      review.replaceChildren();status('Verifying this scope against retained claims and provider identity…');
      const data=await api('/verify',{platform_id:item.platform_id,namespace:item.namespace,ticker:item.ticker,
        isin:isin.value.trim(),currency:units.value.trim(),mic:mic.value.trim()||null,symbol:symbol.value.trim(),statement_reference:reference.value.trim()});
      review.append(node('p',`Proposed ${data.claim.isin} · ${data.claim.mic||'unknown MIC'} · ${data.claim.currency} · Provider ${data.candidate.symbol}`));
      for(const evidence of data.candidate.evidence||[])review.append(node('p',evidence));
      const details=node('details');details.append(node('summary','Provider evidence'),node('pre',JSON.stringify(data.candidate,null,2),'small text-wrap'));review.append(details);
      if(data.can_apply) {
        review.append(button('Queue verified scope',async()=>{
          if(queue.size>=20&&!queue.has(key(item)))throw new Error('Apply or remove queued scopes before adding more (maximum 20).');
          queue.set(key(item),{...item,claim:data.claim,token:data.review_token});el('scope-batch-reviewed').checked=false;renderQueue();status('Scope queued. Review the batch below; no resolution has been applied.');
        }));status('Scope verified. Review its identity and statement reference before queuing.');
      } else status('Verification failed. Resolve the evidence failures before applying a scoped mapping.');
    }),review);return card;
  }
  async function load() {
    const request=++serial;
    const data=await api('/audit?'+new URLSearchParams({search:el('scope-query').value,offset,held_only:el('scope-held').checked}));
    if(request!==serial)return;
    el('scope-results').replaceChildren(...data.items.map(render));
    el('scope-page').textContent=`${data.total?offset+1:0}–${offset+data.items.length} of ${data.total} · Unassigned event records requiring separate review: ${data.unassigned_event_records}`;
    el('scope-prev').disabled=offset===0;el('scope-next').disabled=offset+data.items.length>=data.total;
  }
  async function history() {
    const data=await api('/history');
    el('scope-history').replaceChildren(...data.items.map(item=>{
      const row=node('div',undefined,'border-bottom p-2');
      row.append(node('div',`#${item.id} · Account ${item.platform_id} · ${item.namespace} · ${item.ticker} · ${item.action} · actor ${item.actor_id??'deleted admin'} · ${item.created_at} UTC`));
      row.append(node('div',`Listing ${item.before?.listing_id??'unresolved'} → ${item.after?.listing_id??'unresolved'}`));
      if(item.can_undo)row.append(button('Undo scoped resolution',async()=>{const result=await api('/undo',{audit_id:item.id});await load();await history();status(result.message);}));return row;
    }));
  }
  el('scope-batch-reviewed').addEventListener('change',renderQueue);
  el('scope-batch-apply').addEventListener('click',async()=>{
    const button=el('scope-batch-apply');button.disabled=true;
    const entries=[...queue.entries()];
    try {
      const result=await api('/apply-batch',{review_tokens:entries.map(([,item])=>item.token),reviewed:el('scope-batch-reviewed').checked});
      el('scope-batch-results').replaceChildren(...result.items.map(item=>{
        const [id,entry]=entries[item.item];
        if(['applied','already_processed'].includes(item.status))queue.delete(id);
        return node('p',`${entry.platform_id} · ${entry.namespace} · ${entry.ticker}: ${item.status}${item.error?' — '+item.error:''}`);
      }));
      await load();await history();status('Batch processed. Check each item result; blocked items remain queued for removal and fresh verification. Financial records were not changed.');
    }catch(error){status(error.message+' Completed server items can be retried safely with these review keys.');}
    finally{el('scope-batch-reviewed').checked=false;renderQueue();}
  });
  const refresh=()=>load().catch(error=>status(error.message));
  el('scope-search').addEventListener('submit',event=>{event.preventDefault();offset=0;refresh();});
  el('scope-held').addEventListener('change',()=>{offset=0;refresh();});
  el('scope-prev').addEventListener('click',()=>{offset=Math.max(0,offset-25);refresh();});
  el('scope-next').addEventListener('click',()=>{offset+=25;refresh();});
  Promise.all([load(),history()]).catch(error=>status(error.message));
})();
