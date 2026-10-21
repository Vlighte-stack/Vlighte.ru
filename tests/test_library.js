const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('library/index.html','utf8');
const source=html.slice(html.indexOf('<script>')+8,html.indexOf('</script>')).replace(/init\(\);\s*$/,'');
class Element{
 constructor(tag='div'){this.tag=tag;this.children=[];this.events={};this.style={};this.attributes={};this.hidden=false;this.disabled=false;this.textContent='';}
 append(...nodes){this.children.push(...nodes)}
 replaceChildren(...nodes){this.children=[...nodes];this.textContent='';}
 setAttribute(name,value){this.attributes[name]=value;}
 addEventListener(name,fn){this.events[name]=fn;}
}
const descendants=e=>[e,...e.children.flatMap(descendants)];
async function test(owner){
 const elements={};const element=id=>elements[id]||(elements[id]=new Element());
 let items=[{id:'a'.repeat(32),name:'Fixture A',date:'2026-10-21',formats:['hog','ma2'],requestIp:'8.8.8.8'},{id:'b'.repeat(32),name:'Fixture B',date:'2026-10-21'}];
 const purges=[];let approve=false,confirmations=[];
 const ctx={document:{getElementById:element,createElement:tag=>new Element(tag),createTextNode:text=>Object.assign(new Element('#text'),{textContent:text})},location:{search:owner?'?view=edit':'?view=guest'},URLSearchParams,Intl,Date,confirm:text=>{confirmations.push(text);return approve;},fetch:async(path,options={})=>{
  let data;if(path.startsWith('/api/files/session'))data={can_edit:owner};
  else if(path==='/api/files/trash/clear'){const payload=JSON.parse(options.body);purges.push(payload.ids);items=items.filter(x=>!payload.ids.includes(x.id));data={removed:payload.ids.length,failed:0};}
  else data=items;
  return {ok:true,status:200,redirected:false,json:async()=>data};
 }};
 vm.createContext(ctx);vm.runInContext(source,ctx);await ctx.init();
 let nodes=descendants(element('files'));assert.equal(element('files').children.length,2);
 assert(nodes.some(x=>x.className==='record-top'));assert(nodes.some(x=>x.textContent==='Открыть:'));
 assert(!nodes.some(x=>String(x.textContent).includes('ZIP')));
 assert.equal(nodes.filter(x=>x.className==='request-ip').length,owner?2:0);
 if(!owner){await ctx.removeFromTrash(items[0].id,'Fixture A',new Element('button'));assert.equal(purges.length,0);return;}
 vm.runInContext("view='trash'",ctx);await ctx.load();nodes=descendants(element('files'));
 assert.equal(nodes.filter(x=>x.textContent==='Удалить навсегда').length,2);
 assert.equal(nodes.filter(x=>x.textContent==='Восстановить').length,2);
 assert(!element('clear-trash').hidden && !element('clear-trash').disabled);
 await ctx.removeFromTrash(items[0].id,'Fixture A',new Element('button'));assert.equal(purges.length,0);assert(confirmations[0].includes('Fixture A'));
 approve=true;await ctx.removeFromTrash(items[0].id,'Fixture A',new Element('button'));
 assert.deepEqual(purges,[['a'.repeat(32)]]);assert.equal(items.length,1);assert.equal(element('files').children.length,1);assert(!element('clear-trash').disabled);
 await ctx.emptyTrash();assert.deepEqual(purges,[['a'.repeat(32)],['b'.repeat(32)]]);assert(element('clear-trash').disabled);
}
(async()=>{await test(false);await test(true);console.log('PASS guest/owner compact rendering, grouped links no ZIP, owner-only IP, per-record purge and restore, cancellation, exactly one ID removed, bulk clear retained')})();
