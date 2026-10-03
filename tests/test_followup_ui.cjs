// Execute the actual embedded UI function with a minimal DOM and controlled network.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(require('node:path').join(__dirname, '../app_web.py'), 'utf8');
const js = source.slice(source.indexOf('async function _followupAction('), source.indexOf("document.getElementById('rx-file').addEventListener", source.indexOf('async function _followupAction(')));
async function check(kind) {
  const card = {innerHTML:'controls', _fuState:{status:'AWAITING_CONFIRMATION'}, appendChild(e){this.error=e;}};
  const input = {value:'2026-10-06'};
  let tick, cleared=false, calls=0, rendered=false, resolveLate;
  const context = {
    AbortController, Promise, Error,
    setFamilyBusy:()=>{}, loadFamily:async()=>{},
    document:{getElementById:id=>id==='followup-card'?card:input, createElement:()=>({setAttribute(){}})},
    setTimeout:fn=>{tick=fn;return 1;}, clearTimeout:()=>{cleared=true;},
    renderFollowupCard:()=>{rendered=true;card.innerHTML='confirmed';},
    fetch:async()=>{
      calls++;
      if(kind==='pending') return new Promise(resolve=>{resolveLate=resolve;});
      if(kind==='network') throw Error('offline');
      return {ok:kind!=='http', json:async()=>{
        if(kind==='body') return new Promise(()=>{});
        if(kind==='json') throw Error('invalid json');
        return kind==='http'?{error:'failed'}:{follow_up:{status:'CONFIRMED'}};
      }};
    }
  };
  vm.createContext(context);vm.runInContext(js,context);
  const pending=context._followupAction('raj_sharma',{action:'confirm'});
  await context._followupAction('raj_sharma',{action:'confirm'});
  assert.equal(calls,1,'duplicate request suppressed');
  if(kind==='pending'||kind==='body') tick();
  await pending;
  assert.equal(card._saving,false);assert.ok(cleared);
  if(kind==='success') assert.ok(rendered);
  else {
    assert.equal(card.innerHTML,'controls');assert.ok(card.error.textContent);
    assert.equal(input.value,'2026-10-06');assert.equal(rendered,false);
  }
  if(resolveLate) {
    resolveLate({ok:true,json:async()=>({follow_up:{status:'CONFIRMED'}})});
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(rendered,false,'late result must not replace recovered controls');
  }
}
(async()=>{for(const kind of ['success','http','network','json','pending','body']) await check(kind);console.log('6 UI scenarios passed');})().catch(e=>{console.error(e);process.exitCode=1;});
