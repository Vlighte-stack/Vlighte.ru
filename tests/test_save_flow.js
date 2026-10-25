const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('public/index.html','utf8');
const source=html.slice(html.indexOf('let saveFlowBusy=false;'),html.indexOf('\nrenderSavedFiles();',html.indexOf('let saveFlowBusy=false;')));
async function test(format,failure){
 const events=[];let written,archived;const element=()=>({style:{},append(){},prepend(){},replaceChildren(){}});
 const ctx={window:{showSaveFilePicker:()=>{events.push('picker');if(failure==='cancel')return Promise.reject(Object.assign(Error('cancel'),{name:'AbortError'}));return Promise.resolve({createWritable:async()=>({write:async blob=>{events.push('write');written=await blob.text();},close:async()=>{events.push('close');if(failure==='disk')throw Error('disk full')},abort:async()=>events.push('abort')})});}},document:{querySelector:()=>({}),createElement:element},DOMParser:class{parseFromString(){return {documentElement:{namespaceURI: String()}}}},Blob,atob,loadRevision:1,xmlDocument:{},selectedCompound:null,parts:[{type:'Test',number:'0.1'}],sourceXml:'<source/>',sourceName:'source.xml',el:id=>id==='compoundName'?{value:'Test'}:id==='exportFormat'?{value:format}:element(),XMLSerializer:class{serializeToString(){return '<input/>'}},showStatus(){},api:async(path,payload)=>{events.push(path);if(path==='/api/archive')archived=payload;return {xml:'<hog/>',ma2Xml:'<ma/>',gdtfBase64:'R0RURi1iaW5hcnk=',warnings:[],originReceipt:'signed-generation-origin'}}};
 vm.createContext(ctx);vm.runInContext(source,ctx);const promise=ctx.generateAndSave();assert.deepEqual(events,['picker']);await promise;await new Promise(resolve=>setImmediate(resolve));
 if(failure==='cancel'){assert.deepEqual(events,['picker']);return}
 assert.equal(written,format==='gdtf'?'GDTF-binary':format==='ma2'?'<ma/>':'<hog/>');
 if(failure==='disk'){assert(!archived);assert(!events.includes('/api/archive'));return}
 assert.deepEqual(events,['picker','/api/generate','write','close','/api/archive']);assert.equal(archived.xml,'<hog/>');assert.equal(archived.ma2Xml,'<ma/>');assert.equal(archived.sourceXml,'<source/>');assert.equal(archived.originReceipt,'signed-generation-origin');
}
(async()=>{for(const format of ['hog','ma2','gdtf'])for(const failure of [null,'cancel','disk'])await test(format,failure);console.log('PASS: both selections open picker synchronously; selected XML written; both archived only after close; cancellation and disk failure never archive')})();


