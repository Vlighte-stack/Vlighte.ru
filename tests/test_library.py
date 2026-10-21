import os,runpy,types,sys,json,io,tempfile,time
from pathlib import Path
os.environ['VLIGHTE_GEO_BOOTSTRAP']='0'
sys.modules['fcntl']=types.SimpleNamespace(flock=lambda *a:None,LOCK_EX=2)
m=runpy.run_path('app.py');g=m['application'].__globals__
record={'username':'owner','hash':'ab'*32};g['owner_credentials']=lambda:record
value='owner.'+str(int(time.time())+600)+'.test'
cookie='vlighte_owner='+value+'.'+m['owner_signature'](record,value)
def req(path,method='GET',payload=None,owner=False,origin='https://vlighte.ru'):
 raw=json.dumps(payload or {}).encode();result={}
 env={'PATH_INFO':path,'REQUEST_METHOD':method,'HTTP_COOKIE':cookie if owner else '', 'HTTP_HOST':'vlighte.ru','HTTP_ORIGIN':origin,'CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(raw)),'wsgi.input':io.BytesIO(raw)}
 body=b''.join(m['application'](env,lambda status,headers:result.update(status=status)))
 return result['status'],json.loads(body) if body else None
with tempfile.TemporaryDirectory() as tmp:
 g['ROOT']=Path(tmp);g['STORE']=Path(tmp)/'saved-files';trash=g['STORE']/'.trash';trash.mkdir(parents=True);(Path(tmp)/'library').mkdir()
 # All purge targets are verified disposable descendants of this test directory.
 assert Path(tmp).resolve() in trash.resolve().parents
 active='a'*32;one='b'*32;newer='c'*32
 def create(parent,token):
  folder=parent/token;folder.mkdir();assert Path(tmp).resolve() in folder.resolve().parents
  (folder/'info.json').write_text(json.dumps({'id':token,'name':'Test','date':'2026-10-21','origin':{'ip':'8.8.8.8'},'countryCode':'US'}))
  (folder/'source_test.xml').write_text('<source/>');(folder/'HOG4_test.xml').write_text('<hog/>')
  return folder
 live=create(g['STORE'],active);old=create(trash,one);later=create(trash,newer)
 assert req('/api/files/owner-list')[0]=='403 Forbidden'
 assert req('/api/files/trash/clear','POST',{'ids':[one]})[0]=='403 Forbidden'
 assert old.exists()
 items=req('/api/files/owner-list',owner=True)[1];assert items[0]['requestIp']=='8.8.8.8' and 'origin' not in items[0]
 assert req('/api/files/trash',owner=True)[1][0]['requestIp']=='8.8.8.8'
 public=m['refresh_library']();assert 'requestIp' not in public[0] and '8.8.8.8' not in json.dumps(public)
 assert req('/api/files/trash/clear','POST',{'ids':[one]},True,'https://evil.example')[0]=='403 Forbidden'
 assert req('/api/files/trash/clear',owner=True)[0]=='405 Method Not Allowed'
 for ids in [['../'+active],[one,one],[active,1]]:
  assert req('/api/files/trash/clear','POST',{'ids':ids},True)[0]=='400 Bad Request'
 assert old.exists() and live.exists()
 # Clearing a confirmed snapshot cannot remove active or newly trashed records.
 status,result=req('/api/files/trash/clear','POST',{'ids':[one,active]},True)
 assert status=='200 OK' and result['removed']==1 and result['failed']==0
 assert not old.exists() and live.exists() and later.exists()
 assert req('/api/files/trash/clear','POST',{'ids':[]},True)[1]['removed']==0
 assert req('/api/files/trash/clear','POST',{'ids':[newer]},True)[1]['removed']==1
 assert live.exists()
 print('PASS owner-only IP, public IP excluded, owner-only purge/POST/CSRF, malformed IDs rejected, snapshot-only deletion, active/new trash protected, empty trash idempotent')
library=Path('library/index.html').read_text(encoding='utf-8')
assert "const zip=document.createElement" not in library
assert "confirm('Удалить безвозвратно" in library and 'request-ip' in library
