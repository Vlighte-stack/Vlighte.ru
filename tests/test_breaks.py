"""Portable native Break/ModeMaster/virtual tests. Synthetic input only."""
import os,runpy,tempfile,base64,zipfile,io,json
from pathlib import Path
os.environ['VLIGHTE_GEO_BOOTSTRAP']='0';m=runpy.run_path('app.py');E=m['E'];q=m['q'];mq=m['mq']
def template():
 h=E.Element(q('Hog'));lib=E.SubElement(h,q('Library'));maker=E.SubElement(lib,q('Manufacturer'),name='User Created');product=E.SubElement(maker,q('Product'),name='Synthetic');types=E.SubElement(product,q('Types'));t=E.SubElement(types,q('Type'),name='IP 1000 STR line',revisionid='0',status='User',uuid='00000000-0000-0000-0000-000000000001');E.SubElement(t,q('History'),revision='0',author='',date='2026-10-26',comment='');E.SubElement(t,q('Origin')).text='User';E.SubElement(t,q('Icon'),type='circle',size='medium');protocol=E.SubElement(E.SubElement(t,q('Protocols')),q('Protocol'),name='DMX');E.SubElement(protocol,q('Dmxfootprint'),size='1');ch=E.SubElement(E.SubElement(protocol,q('Channels')),q('Channel'),number='1');E.SubElement(E.SubElement(ch,q('Ranges')),q('Range'),dmxstart='0',dmxend='255',function='Intensity',feature='Intensity',start='0',end='100');compound=E.SubElement(E.SubElement(lib,q('CompoundTypes')),q('CompoundType'),name='Synthetic',mode='1ch',revisionId='0',uuid='00000000-0000-0000-0000-000000000002');E.SubElement(compound,q('History'),revision='0',author='',date='2026-10-26',comment='');E.SubElement(compound,q('Origin')).text='User';E.SubElement(compound,q('Icon'),type='circle',size='medium');E.SubElement(E.SubElement(compound,q('Parts')),q('Part'),type='IP 1000 STR line',number='0',plotX='0',plotY='0');return m['serialize'](h)
def fixture():
 r=E.fromstring('<GDTF DataVersion="1.2"><FixtureType Name="Synthetic"><Geometries><Geometry Name="Base"/></Geometries><DMXModes><DMXMode Name="Mode" Geometry="Base"><DMXChannels/></DMXMode></DMXModes></FixtureType></GDTF>');return r,r.find('FixtureType'),r.find('.//DMXMode')
def channel(mode,brk,offset,attr):
 c=E.SubElement(mode.find('DMXChannels'),'DMXChannel',DMXBreak=str(brk),Offset=str(offset),Geometry='Base');lc=E.SubElement(c,'LogicalChannel',Attribute=attr);E.SubElement(lc,'ChannelFunction',Name='Function',Attribute=attr,DMXFrom='0/1',Default='0/1',PhysicalFrom='0',PhysicalTo='1');return c
def encode(root):
 b=io.BytesIO()
 with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:z.writestr('description.xml',m['serialize'](root))
 return base64.b64encode(b.getvalue()).decode()
def clean(text):
 root=m['parse'](text)
 for n in root.iter():
  for child in list(n):
   if child.tag is E.Comment:n.remove(child)
 return m['serialize'](root)
def payload(imported,source):
 c=m['first'](m['parse'](imported['xml']),'CompoundType');parts=m['children'](m['first'](c,'Parts'),'Part');return {'xml':imported['xml'],'sourceXml':source,'compoundIndex':0,'name':c.get('name'),'exportGdtf':True,'parts':[{'type':p.get('type'),'number':p.get('number'),'plotX':p.get('plotX','0'),'plotY':p.get('plotY','0'),'sourceIndex':i} for i,p in enumerate(parts)]}
with tempfile.TemporaryDirectory() as tmp:
 rootdir=Path(tmp);(rootdir/'private').mkdir();(rootdir/'private/known.json').write_text('{}');(rootdir/'private/strobe.xml').write_text(template(),encoding='utf-8');m['import_profile'].__globals__['ROOT']=rootdir
 r,f,mode=fixture();channel(mode,1,1,'Dimmer');channel(mode,2,1,'ColorAdd_R');channel(mode,2,2,'ColorAdd_G');choices=m['import_profile']({'gdtf':encode(r)})['modes'];assert len(choices)==2;assert [m['import_profile']({'gdtf':encode(r),'mode':c['name']})['summary']['footprint'] for c in choices]==[1,2]
 try:m['import_profile']({'gdtf':encode(r),'mode':'Mode'});raise AssertionError('Implicit break drop')
 except ValueError:pass
 r,f,mode=fixture();master=channel(mode,1,1,'Gobo1');master.find('./LogicalChannel/ChannelFunction').set('PhysicalTo','255');follower=channel(mode,1,2,'Pan');lc=follower.find('LogicalChannel');lc.clear();lc.set('Attribute','Pan')
 for index,(lo,hi,pf,pt) in enumerate([(0,127,-90,90),(128,255,-180,180)]):E.SubElement(lc,'ChannelFunction',Name='Conditional'+str(index),Attribute='Pan',DMXFrom='0/1',Default='128/1',PhysicalFrom=str(pf),PhysicalTo=str(pt),ModeMaster='Base_Gobo1',ModeFrom=str(lo)+'/1',ModeTo=str(hi)+'/1')
 imp=m['import_profile']({'gdtf':encode(r),'mode':'Mode'});first=m['generate_formats'](payload(imp,imp['sourceXml']));hog=clean(first['xml']);second=m['generate_formats'](payload(m['import_profile']({'xml':hog}),hog));assert second.get('gdtfBase64'),second.get('gdtfError');groot,_,modes=m['read_gdtf'](second['gdtfBase64']);encoded=encode(m['parse'](clean(m['serialize'](groot))));third=m['import_profile']({'gdtf':encoded,'mode':modes[0].get('Name')});last_hog=clean(m['generate_formats'](payload(third,third['sourceXml']))['xml']);final=m['generate_formats'](payload(m['import_profile']({'xml':last_hog}),last_hog));ma=m['parse'](clean(final['ma2Xml']));pan=next(c for c in m['descendants'](ma,'ChannelType') if c.get('attribute')=='PAN');assert pan.get('mode')=='GOBO1',pan.attrib;assert {(n.get('mode_dmx_start'),n.get('mode_dmx_end')) for n in m['children'](pan,'ChannelFunction')}=={('0','127'),('128','255')};assert m['descendants'](m['parse'](last_hog),'Qualifier')
 ma=E.Element(mq('MA'));f=E.SubElement(ma,mq('FixtureType'),name='Virtual',mode='1ch');module=E.SubElement(E.SubElement(f,mq('Modules')),mq('Module'),index='0',**{'class':'LED'})
 for index,(attr,coarse) in enumerate([('COLORRGB1','1'),('DIM',None)]):
  c=E.SubElement(module,mq('ChannelType'),index=str(index),attribute=attr)
  if coarse:c.set('coarse',coarse)
  E.SubElement(c,mq('ChannelFunction'),index='0',attribute=attr,physfrom='0',physto='1',min_dmx_24='0',max_dmx_24='16777215')
 E.SubElement(E.SubElement(f,mq('Instances')),mq('Instance'),module_index='0',patch='1');source=m['ma2_serialize'](ma);imp=m['import_profile']({'xml':source});hog=clean(m['generate_formats'](payload(imp,source))['xml']);assert m['descendants'](m['parse'](hog),'Virtual');back=m['parse'](m['generate_formats'](payload(m['import_profile']({'xml':hog}),hog))['ma2Xml']);assert any(c.get('attribute')=='DIM' and 'coarse' not in c.attrib for c in m['descendants'](back,'ChannelType'))
print('PASS separate active Break profiles, no implicit dropping, native ModeMaster/Qualifiers roundtrip without comments, native virtual DIM')
