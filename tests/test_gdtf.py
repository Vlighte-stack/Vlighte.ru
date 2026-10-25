import os,runpy,tempfile,types,sys,base64,zipfile,io
from pathlib import Path
os.environ['VLIGHTE_GEO_BOOTSTRAP']='0'
if os.name=='nt':sys.modules['fcntl']=types.SimpleNamespace(flock=lambda *a:None,LOCK_EX=2)
m=runpy.run_path('app.py');g=m['save_pair'].__globals__;E=m['E'];mq=m['mq']
ma=E.Element(mq('MA'));f=E.SubElement(ma,mq('FixtureType'),name='Synthetic',mode='3ch');mods=E.SubElement(f,mq('Modules'));mod=E.SubElement(mods,mq('Module'),index='0',**{'class':'LED'})
for index,attr,coarse,fine,pf,pt in [(0,'DIM','1',None,0,1),(1,'TILT','2','3',-90,90),(2,'DIM2',None,None,0,1)]:
 c=E.SubElement(mod,mq('ChannelType'),index=str(index),attribute=attr,default='0')
 if coarse:c.set('coarse',coarse)
 if fine:c.set('fine',fine)
 E.SubElement(c,mq('ChannelFunction'),index='0',attribute=attr,min_dmx_24='0',max_dmx_24='16777215',physfrom=str(pf),physto=str(pt))
E.SubElement(E.SubElement(f,mq('Instances')),mq('Instance'),index='0',module_index='0',patch='1')
source=m['ma2_serialize'](ma);encoded,_=m['gdtf_from_ma'](source,'Synthetic');raw=m['validate_gdtf_export'](encoded);root,_,_=m['read_gdtf'](encoded)
assert m['ma_channel_layout'](m['gdtf_ma_recovery'](root))==m['ma_channel_layout'](ma)
assert zipfile.ZipFile(io.BytesIO(raw)).namelist()==['description.xml']
hog='<Hog xmlns="http://schema.hogfour.com/Fixture"><Library><Type name="Synthetic"><Dmxfootprint size="3"/></Type></Library></Hog>'
with tempfile.TemporaryDirectory() as tmp:
 g['ROOT']=Path(tmp);g['STORE']=Path(tmp)/'saved-files';(Path(tmp)/'library').mkdir()
 payload={'sourceXml':source,'sourceName':'source.xml','name':'Synthetic','ma2Xml':source}
 first=m['save_pair'](payload,hog);payload['gdtfBase64']=encoded;again=m['save_pair'](payload,hog)
 assert first['id']==again['id'] and again['duplicate'] and again['formats']==['hog','ma2','gdtf']
 assert len(list(g['STORE'].glob('*/info.json')))==1
 with zipfile.ZipFile(g['STORE']/first['id']/'bundle.zip') as z:assert len(z.namelist())==3
 status=[];body=b''.join(m['download_gdtf']({},lambda s,h:status.append(s),'/api/files/'+first['id']+'/download/gdtf'));assert status==['200 OK'] and body==raw
 g['owner_credentials']=lambda:{'hash':'ab'*32};payload['xml']=hog;payload['originReceipt']=m['origin_receipt']({'ip':'192.0.2.1','countryCode':None,'generatedAt':'test'},hog,source,encoded)
 assert m['archive_origin'](payload,{})['ip']=='192.0.2.1'
 payload['gdtfBase64']+='A'
 try:m['archive_origin'](payload,{});raise AssertionError('Changed GDTF accepted')
 except ValueError:pass
print('PASS GDTF binary ZIP/recovery, 16-bit and virtual channel definitions, three formats in one record, download, signed hash tampering')
