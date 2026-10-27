"""Portable color regression: native XYZ and explicit MA2 JSON, no fixtures needed."""
import os,runpy,tempfile,zipfile,base64,io,json,copy
from pathlib import Path
os.environ['VLIGHTE_GEO_BOOTSTRAP']='0';m=runpy.run_path('app.py');g=m['application'].__globals__;E=m['E']
helper=Path('tests/test_breaks.py').read_text(encoding='utf-8');scope={'m':m,'E':E,'q':m['q'],'mq':m['mq']};exec(helper[helper.index('def template():'):helper.index('def fixture():')],scope)
def package(xml):
 b=io.BytesIO()
 with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:z.writestr('description.xml',xml.encode())
 return b.getvalue()
def stripped(raw):
 out=io.BytesIO()
 with zipfile.ZipFile(io.BytesIO(raw)) as z,zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as w:
  for name in z.namelist():
   data=z.read(name)
   if name.lower().endswith('.xml'):
    root=m['parse'](data.decode())
    for p in root.iter():
     for c in list(p):
      if not isinstance(c.tag,str):p.remove(c)
    data=m['serialize'](root).encode();assert b'<!--' not in data
   w.writestr(name,data)
 return out.getvalue()
def step(raw,source,target):
 raw=stripped(raw);encoded=base64.b64encode(raw).decode()
 if source=='GDTF':
  modes=m['import_profile']({'gdtf':encoded})['modes'];imp=m['import_profile']({'gdtf':encoded,'mode':modes[0]['name']})
 else:imp=m['import_profile']({'package':encoded})
 root=m['parse'](imp['xml']);compound=m['first'](root,'CompoundType');parts=m['children'](m['first'](compound,'Parts'),'Part');p={'xml':imp['xml'],'sourceXml':imp['sourceXml'],'resourceBase64':imp.get('resourceBase64'),'compoundIndex':0,'name':compound.get('name'),'exportGdtf':True,'packageFormat':'ma2' if target=='MA' else 'hog' if target=='HOG' else None,'parts':[{'type':n.get('type'),'number':n.get('number'),'plotX':n.get('plotX','0'),'plotY':n.get('plotY','0'),'sourceIndex':i} for i,n in enumerate(parts)]};result=m['generate_formats'](p);return base64.b64decode(result['gdtfBase64'] if target=='GDTF' else result['packageBase64'])
source='''<GDTF DataVersion="1.2"><FixtureType Name="Synthetic color"><AttributeDefinitions><FeatureGroups><FeatureGroup Name="Color" Pretty="Color"><Feature Name="Color"/></FeatureGroup></FeatureGroups><Attributes><Attribute Name="Color1" Pretty="Color" Feature="Color.Color" PhysicalUnit="None"/></Attributes></AttributeDefinitions><Wheels><Wheel Name="Colors"><Slot Name="Green" Color="0.172918,0.706097,38.026013"/><Slot Name="Lavender" Color="0.236456,0.120829,38.181043"/></Wheel></Wheels><Geometries><Geometry Name="Base"/></Geometries><DMXModes><DMXMode Name="Mode" Geometry="Base"><DMXChannels><DMXChannel DMXBreak="1" Offset="1" Geometry="Base"><LogicalChannel Attribute="Color1"><ChannelFunction Name="Color" Attribute="Color1" DMXFrom="0/1" Default="0/1" PhysicalFrom="0" PhysicalTo="1" Wheel="Colors"><ChannelSet Name="Green" DMXFrom="0/1" WheelSlotIndex="1"/><ChannelSet Name="Lavender" DMXFrom="128/1" WheelSlotIndex="2"/></ChannelFunction></LogicalChannel></DMXChannel></DMXChannels></DMXMode></DMXModes></FixtureType></GDTF>'''
def colors(raw):
 with zipfile.ZipFile(io.BytesIO(raw)) as z:r=m['parse'](z.read('description.xml').decode())
 return {s.get('Name'):tuple(map(float,s.get('Color').split(','))) for s in r.findall('.//Wheels/Wheel/Slot')}
with tempfile.TemporaryDirectory() as td:
 g['ROOT']=Path(td);private=Path(td)/'private';private.mkdir();(private/'known.json').write_text('{}');(private/'strobe.xml').write_text(scope['template'](),encoding='utf-8');original=package(source)
 for chain in [('GDTF','HOG','MA','HOG','GDTF'),('GDTF','HOG','GDTF','HOG','GDTF')]:
  raw=original
  for src,dst in zip(chain,chain[1:]):
   raw=step(raw,src,dst)
   if dst=='MA':
    with zipfile.ZipFile(io.BytesIO(raw)) as z:manifest=json.loads(z.read('colorimetry.json'))
    assert manifest['schema']=='vlighte.colorimetry/1';assert manifest['profiles'];assert m['color_manifest45'](json.dumps(manifest).encode())
  final=colors(raw)
  for name,values in colors(original).items():assert max(abs(a-b) for a,b in zip(values,final[name]))<1e-10,(chain,name,values,final[name])
 bad=copy.deepcopy(manifest);bad['profiles'][0]['bindings'][0]['xyz'][0]=float('nan')
 try:m['color_manifest45'](json.dumps(bad).encode());raise AssertionError('Nonfinite XYZ accepted')
 except ValueError:pass
print('PASS: native XYZ, MA2 ZIP colorimetry, four-step cycles without comments, nonfinite rejection')
