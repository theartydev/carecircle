const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(require('node:path').join(__dirname,'../app_web.py'),'utf8');
const js=source.slice(source.indexOf('let familyMembers ='),source.indexOf('async function analyzeRx()'));
const elements={};
function element(){return {innerHTML:'',textContent:'',hidden:false,value:'',style:{},children:[],replaceChildren(){this.children=[];},appendChild(c){this.children.push(c);},setAttribute(k,v){this[k]=v;},focus(){}};}
const members=[{patient_id:'dad',name:'Dad Demo',relationship:'Dad',care_state:{medicines:[{name:'Dad only medicine'}],follow_up:{status:'CONFIRMED',confirmed_date:'2026-10-05'}}},{patient_id:'mom',name:'Mom Demo',relationship:'Mom',care_state:{}}];
const context={AbortController,setTimeout,clearTimeout,console,
 document:{getElementById:id=>elements[id]||(elements[id]=element()),querySelector:()=>elements.label||(elements.label=element()),querySelectorAll:()=>[],createElement:element},
 val:s=>String(s).replaceAll('<','&lt;'),fmtDate:s=>s,
 renderFollowupCard:(fu,id)=>{elements['followup-card'].textContent=id;},
 fetch:async()=>({ok:true,json:async()=>({members})})};
vm.createContext(context);vm.runInContext(js,context);
(async()=>{
 await context.loadFamily();
 assert.ok(elements['care-summary'].innerHTML.includes('Dad only medicine'));
 context.selectMember('mom');
 assert.equal(elements['workspace-title'].textContent,'Mom Demo’s Care Space');
 assert.ok(!elements['care-summary'].innerHTML.includes('Dad only medicine'));
 assert.equal(elements['followup-card'].style.display,'none');
 assert.equal(elements['rx-file'].value,'');assert.equal(elements['rx-input'].value,'');
 context.setFamilyBusy(true);context.selectMember('dad');
 assert.equal(elements['workspace-title'].textContent,'Mom Demo’s Care Space');
 context.setFamilyBusy(false);context.selectMember('dad');
 assert.ok(elements['care-summary'].innerHTML.includes('Dad only medicine'));
 context.openMemberForm(true);assert.equal(elements['member-name'].value,'Dad Demo');
 context.openMemberForm(false);assert.equal(elements['member-name'].value,'');
 console.log('Family UI: member isolation, switch guard, form reset and editing passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
