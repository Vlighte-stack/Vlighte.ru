"""Portable regression for the console-tested IP1000 virtual DIM mapping."""
import os,runpy,tempfile,copy,json
from pathlib import Path
os.environ['VLIGHTE_GEO_BOOTSTRAP']='0'
m=runpy.run_path('app.py');g=m['application'].__globals__;E=m['E']
q=m['q'];mq=m['mq'];ds=m['descendants'];ch=m['children'];first=m['first']
helper=Path('tests/test_breaks.py').read_text(encoding='utf-8');scope={'m':m,'E':E,'q':q,'mq':mq}
exec(helper[helper.index('def template():'):helper.index('def fixture():')],scope)
def request(imp,source):
 c=first(m['parse'](imp['xml']),'CompoundType');parts=ch(first(c,'Parts'),'Part')
 return {'xml':imp['xml'],'sourceXml':source,'compoundIndex':0,'name':c.get('name'),'exportGdtf':True,'parts':[dict(p.attrib,sourceIndex=i) for i,p in enumerate(parts)]}
def definitions(t,tag):return [n.attrib for n in ds(t,tag)]
with tempfile.TemporaryDirectory() as td:
 g['ROOT']=Path(td);private=Path(td)/'private';private.mkdir();(private/'known.json').write_text('{}');(private/'strobe.xml').write_text(scope['template'](),encoding='utf-8')
 source=Path('tests/fixtures/IP1000_MA42_original.xml').read_text(encoding='utf-8-sig')
 imp=m['import_profile']({'xml':source});req=request(imp,source);result=m['generate_formats'](req);hog=m['strip_recovery'](m['parse'](result['xml']));main=ds(hog,'Type')[0];channels={n.get('number'):n for n in ds(main,'Channel')}
 assert {(a.get('function'),a.get('feature')) for a in ds(main,'Affected')}=={('Cyan','Variable'),('Magenta','Variable'),('Yellow','Variable'),('Sub Intensity 1','Intensity')}
 assert channels['3'].get('constant') is None and first(channels['3'],'Range').get('function')=='Sub Intensity 1'
 assert first(channels['14'],'Range').get('function')=='Sub Intensity 2'
 assert next(d.get('value') for d in ds(main,'Default') if d.get('function')=='Sub Intensity 1')=='100.0'
 assert len(ds(hog,'Part'))==13 and first(main,'Dmxfootprint').get('size')=='14'
 assert all(not ds(t,'Virtual') for t in ds(hog,'Type')[1:])
 expected=m['parse'](Path('docs/conversion-checks/IP1000/2.00.49/IP1000_HOG42_2.00.49.xml').read_text(encoding='utf-8'))
 for a,b in zip(ds(expected,'Type'),ds(hog,'Type')):
  for tag in ('Channel','Range','Default','Virtual','Affected'):assert definitions(a,tag)==definitions(b,tag),(a.get('name'),tag)
 back,_=m['ma2_from_hog'](req,m['serialize'](hog));module=ds(m['parse'](back),'Module')[0];physical={n.get('coarse'):n for n in ch(module,'ChannelType') if n.get('coarse')}
 assert physical['3'].get('attribute')=='COLORRGB7' and physical['14'].get('attribute')=='DIM3'
 assert any(n.get('attribute')=='DIM' and 'coarse' not in n.attrib for n in ch(module,'ChannelType'))
 # Another full MA->HOG pass without recovery comments must keep channel 3's role.
 again,_=m['convert'](m['parse'](back));assert first(next(n for n in ds(ds(again,'Type')[0],'Channel') if n.get('number')=='3'),'Range').get('function')=='Sub Intensity 1'
 # Explicit source native HOG scope is preserved as provided.
 same,_=m['convert'](hog);assert definitions(ds(same,'Type')[0],'Affected')==definitions(main,'Affected')
 # A normal physical Sub Intensity 1 outside Virtual does not become an emitter.
 plain=copy.deepcopy(hog);t=ds(plain,'Type')[0]
 for a in list(first(t,'Virtual')):
  if a.get('function')=='Sub Intensity 1':first(t,'Virtual').remove(a)
 plain_ma,_=m['ma2_from_hog'](req,m['serialize'](plain));pm=ds(m['parse'](plain_ma),'Module')[0]
 assert next(c for c in ch(pm,'ChannelType') if c.get('coarse')=='3').get('attribute')=='DIM2'
 no=m['parse'](source);mod=ds(no,'Module')[0]
 for c in list(mod):
  if m['local'](c)=='ChannelType' and 'coarse' not in c.attrib:mod.remove(c)
 assert not ds(m['convert'](no)[0],'Virtual')
 custom=m['parse'](source);virtual=next(c for c in ds(custom,'ChannelType') if c.get('attribute')=='DIM' and 'coarse' not in c.attrib);fn=first(virtual,'ChannelFunction');fn.set('physfrom','.1');fn.set('physto','.8');virtual.set('default','51');custom_hog=m['convert'](custom)[0];v=first(custom_hog,'Virtual');assert (float(v.get('start')),float(v.get('end')))==(10,80)
 assert float(next(d.get('value') for d in ds(custom_hog,'Default') if d.get('function')=='Intensity'))==24
 virtual.append(copy.deepcopy(fn))
 try:m['convert'](custom)
 except ValueError:pass
 else:raise AssertionError('Composite virtual curve was silently flattened')
print('PASS IP1000 console-tested definitions, virtual DIM scope/default/scale, channel3, independent DIM3/cells, comment-free reverse mapping and normal physical DIM2')
