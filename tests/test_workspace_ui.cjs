const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(require('node:path').join(__dirname,'../scripts/ui.html'),'utf8'),script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements={};const get=id=>elements[id]??=( {value:'',textContent:'',hidden:false,disabled:false,readOnly:false,reset(){for(const [key,value] of Object.entries(elements))if(key.startsWith('session-'))value.value='';},replaceChildren(){}} );
const context=vm.createContext({document:{getElementById:get},location:{origin:'http://localhost'},fetch:async()=>({ok:false,status:409,json:async()=>({error:'Conflict: draft kept'})})});
vm.runInContext(script,context);
get('session-project').value='work';get('session-summary').value='My unsaved draft';get('session-next').value='Next step';
(async()=>{await get('session-form').onsubmit({preventDefault(){}});assert.equal(get('session-summary').value,'My unsaved draft');assert.equal(get('session-next').value,'Next step');assert.equal(get('session-status').textContent,'Conflict: draft kept');assert.equal(get('save-session').disabled,false);get('lock').onclick();assert.equal(get('session-summary').value,'');console.log('UI conflict preserves draft; lock clears it');})();
