"""Standalone fixture converter. Run behind a reverse proxy in production."""
from pathlib import Path
import xml.etree.ElementTree as E
import json, re, uuid, hashlib, copy, datetime, base64, io, zipfile, os, shutil
from urllib.parse import urlsplit
ROOT=Path(__file__).parent
H='http://schema.hogfour.com/Fixture'; M='http://schemas.malighting.de/grandma2/xml/MA'
E.register_namespace('',H); E.register_namespace('xsi','http://www.w3.org/2001/XMLSchema-instance')
q=lambda name:f'{{{H}}}{name}'
local=lambda n:n.tag.split('}')[-1] if isinstance(n.tag,str) else ''
def descendants(n,name):return [e for e in n.iter() if local(e)==name]
def children(n,name):return [e for e in n if local(e)==name]
def first(n,name):return next(iter(descendants(n,name)),None)
def parse(text):
 if not isinstance(text,str) or len(text.encode('utf8'))>2_000_000:raise ValueError('Размер XML превышает 2 МБ.')
 # Reject DTD/entity declarations, including non-UTF8 representations.
 if re.search(r'<!\s*(DOCTYPE|ENTITY)',text,re.I) or '\x00' in text:raise ValueError('DTD и сущности XML запрещены.')
 try:return E.fromstring(text,parser=E.XMLParser(target=E.TreeBuilder(insert_comments=True)))
 except E.ParseError:raise ValueError('Некорректный XML.')
def serialize(root):
 E.indent(root,space='\t');return '<?xml version="1.0" encoding="UTF-8"?>\n'+E.tostring(root,encoding='unicode')
def fingerprint(n):
 return hashlib.sha256(json.dumps([(local(e),sorted(e.attrib.items())) for e in n.iter() if isinstance(e.tag,str)],ensure_ascii=False).encode()).hexdigest()
def validate_hog(root):
 if root.tag not in (q('Hog'),'Hog') or first(root,'Library') is None:raise ValueError('Ожидается профиль HOG.')
 types={}
 for t in descendants(root,'Type'):
  name=t.get('name');foot=first(t,'Dmxfootprint')
  if not name or name in types or foot is None:raise ValueError('Некорректные типы HOG.')
  size=int(foot.get('size','0'))
  if not 1<=size<=512:raise ValueError('Некорректный DMX footprint.')
  types[name]=size
 if not types:raise ValueError('В профиле отсутствуют типы.')
 return types

def convert_channels(root):
 if root.tag in (q('Hog'),'Hog'):validate_hog(root);return repair_generated_background(root),'HOG'
 if root.tag!='{'+M+'}MA':raise ValueError('Не удалось определить профиль MA2 или HOG.')
 fixtures=descendants(root,'FixtureType')
 if len(fixtures)!=1:raise ValueError('В файле должна быть одна FixtureType.')
 fixture=fixtures[0]
 known=json.loads((ROOT/'private/known.json').read_text())
 matching=known.get(fingerprint(fixture))
 if matching:return parse((ROOT/'private'/matching).read_text()),'MA2'
 modules={n.get('index'):n for n in descendants(fixture,'Module')};instances=descendants(fixture,'Instance')
 if not modules or not instances:raise ValueError('В профиле нет модулей или экземпляров.')
 hog=parse((ROOT/'private/strobe.xml').read_text());lib=first(hog,'Library');base=copy.deepcopy(next(t for t in descendants(hog,'Type') if t.get('name')=='IP 1000 STR line'));compound=copy.deepcopy(first(hog,'CompoundType'));lib.clear()
 manufacturer=E.SubElement(lib,q('Manufacturer'),name='User Created');product=E.SubElement(manufacturer,q('Product'),name=fixture.get('name','MA2 Fixture'));types=E.SubElement(product,q('Types'));E.SubElement(product,q('CompoundTypes')).append(compound)
 compound.set('name',f"{fixture.get('name','MA2 Fixture')} {fixture.get('mode','')} HOG4".strip());partlist=first(compound,'Parts');partlist.clear()
 slots={};defs={};nextpatch=1
 for instance in instances:
  module=modules.get(instance.get('module_index'))
  if module is None:raise ValueError('Неизвестный модуль MA2.')
  patch=int(instance.get('patch',nextpatch));width=0
  if patch<1:raise ValueError('Некорректный patch.')
  for c in children(module,'ChannelType'):
   if 'coarse' not in c.attrib:continue
   coarse=int(c.get('coarse'));fine=int(c.get('fine')) if 'fine' in c.attrib else None
   if coarse<1 or (fine is not None and fine!=coarse+1):raise ValueError('Требуется сопоставление раздельных fine-каналов.')
   attr=c.get('attribute','Channel');fn=first(c,'ChannelFunction');label=fn.get('attribute_user_name',attr) if fn is not None else attr;key=(module.get('index'),coarse)
   if key not in defs:
    size=2 if fine else 1;name=f"{fixture.get('name','MA2')} M{key[0]} {coarse} {label}";t=copy.deepcopy(base);t.set('name',name);t.set('uuid',str(uuid.uuid4()));first(t,'Dmxfootprint').set('size',str(size));channels=first(t,'Channels');channels.clear();channel=E.SubElement(channels,q('Channel'),number='1')
    if attr=='DUMMY':channel.set('unused','true')
    else:
     if fine:E.SubElement(E.SubElement(channel,q('AttachedChannels')),q('AttachedChannel')).text='2'
     ranges=E.SubElement(channel,q('Ranges'));rgb={'COLORRGB1':('I Red','Cyan'),'COLORRGB2':('I Green','Magenta'),'COLORRGB3':('I Blue','Yellow')};intensity=bool(re.fullmatch(r'DIM(?:MER)?\d*|COLORRGB\d+|(?:RED|GREEN|BLUE|WHITE)\d+',attr));maximum=65535 if fine else 255
     E.SubElement(ranges,q('Range'),dmxstart='0',dmxend=str(maximum),function=rgb[attr][0] if attr in rgb else 'Intensity' if intensity else 'Beam Fx',feature='Intensity' if intensity else 'Continuous',start='0',end='100')
     if attr in rgb:E.SubElement(ranges,q('Range'),dmxstart=str(maximum),dmxend='0',function=rgb[attr][1],feature='Variable',start='0',end='100')
    if attr=='TILT':
     if fn is None or 'physfrom' not in fn.attrib or 'physto' not in fn.attrib:raise ValueError('В профиле отсутствует физический диапазон Tilt.')
     lo=float(fn.get('physfrom'));hi=float(fn.get('physto'))
     import math
     if not math.isfinite(lo) or not math.isfinite(hi) or hi<=lo:raise ValueError('Некорректный диапазон Tilt.')
     ranges=first(channel,'Ranges');ranges.clear();E.SubElement(ranges,q('Range'),dmxstart='0',dmxend=str(65535 if fine else 255),function='Tilt',feature='Tilt',start=str(lo),end=str(hi))
    types.append(t);defs[key]=(name,size)
   name,size=defs[key];absolute=patch+coarse-1
   for j in range(size):
    if absolute+j in slots:raise ValueError(f'Пересечение DMX {absolute+j}.')
    slots[absolute+j]=(name,size,module.get('index'),absolute)
   width=max(width,coarse+size-1)
  nextpatch=patch+width
 end=max(slots,default=0)
 if not 1<=end<=512:raise ValueError('Некорректный размер DMX.')
 address=1;index=0;last=None;x=y=0;gap=False
 while address<=end:
  if address in slots:name,size,moduleid,_=slots[address]
  else:
   name,size,moduleid='MA2 Unused DMX',1,'unused'
   if not gap:
    t=copy.deepcopy(base);t.set('name',name);t.set('uuid',str(uuid.uuid4()));c=first(t,'Channel');c.clear();c.set('number','1');c.set('unused','true');types.append(t);gap=True
  if last is not None:
   if last==moduleid:x+=1
   else:x=0;y-=1
  E.SubElement(partlist,q('Part'),type=name,number=f'0.{index*10+1}',plotX=str(x),plotY=str(y));index+=1;last=moduleid;address+=size
 # Number repeated channel types in independent bands; preserve physical DMX order.
 parts=children(partlist,'Part');counts={}
 for part in parts:counts[part.get('type')]=counts.get(part.get('type'),0)+1
 repeated=[name for name,count in counts.items() if count>1]
 if repeated:
  prefix_count=next((i for i,part in enumerate(parts) if part.get('type') in repeated),0)
  starts={};cursor=max(10,((prefix_count+9)//10)*10)
  for name in repeated:
   starts[name]=cursor;cursor+=max(10,((counts[name]+9)//10)*10)
  seen={};prefix=0;tail=0;in_repeated=False
  for part in parts:
   name=part.get('type')
   if name in starts:
    in_repeated=True;seen[name]=seen.get(name,0)+1;number=starts[name]+seen[name]
   elif not in_repeated:prefix+=1;number=prefix
   else:tail+=1;number=cursor+10+tail
   part.set('number',f'0.{number}')
 return hog,'MA2'

def repair_generated_background(root):
 """Apply the console-confirmed BG/Intensity pairing to our legacy output."""
 backgrounds={'BG Red','BG Green','BG Blue','BG White'}
 for t in descendants(root,'Type'):
  for channel in descendants(t,'Channel'):
   ranges=descendants(channel,'Range')
   if len(ranges)!=1:continue
   r=ranges[0]
   if list(r) or r.get('dmxstart')!='0' or r.get('dmxend') not in ('255','65535') or r.get('start')!='0' or r.get('end')!='100':continue
   name=r.get('function','')
   if name in backgrounds and r.get('feature')=='Variable':r.set('feature','Intensity');continue
   if not name.startswith('Sub Intensity ') or r.get('feature')!='Intensity':continue
   comments=[n for n in first(channel,'Ranges') if n.tag is E.Comment]
   for comment in comments:
    text=comment.text or ''
    source=re.match(r'MA2 (RED|GREEN|BLUE|WHITE)\d* \(',text)
    old=re.match(r'(BG (?:Red|Green|Blue|White)) compatibility repair: independent Sub Intensity \d+\.',text)
    if source and 'secondary colour mapped to independent Sub Intensity; unverified BG feature excluded.' in text:
     r.set('function','BG '+source.group(1).title());comment.text='MA2 '+source.group(1)+': console-confirmed BG/Intensity mapping.';break
    if old:
     r.set('function',old.group(1));comment.text=old.group(1)+': console-confirmed Intensity feature.';break
 return root

def map_module_channels(target,source,module):
 """Translate MA attributes semantically; allocate independent HOG controls."""
 counts={};macro=0;fallback=0
 def reserve(name,limit=9):
  counts[name]=counts.get(name,0)+1;n=counts[name]
  if n>limit:raise ValueError(f'Нужно сопоставить дополнительные каналы {name}.')
  return name if n==1 else f'{name} {n}'
 def macro_name(rate=False):
  nonlocal macro
  macro+=1
  if macro>8:raise ValueError('Нужно сопоставить дополнительные Macro-каналы.')
  return ('Macro' if macro==1 else f'Macro {macro}')+(' Rate' if rate else '')
 rgb={'COLORRGB1':('I Red','Cyan'),'COLORRGB2':('I Green','Magenta'),'COLORRGB3':('I Blue','Yellow')}
 color_names={'RED':'I Red','GREEN':'I Green','BLUE':'I Blue','WHITE':'I White','AMBER':'I Amber','COLORRGB4':'I Amber','COLORRGB5':'I White','COLORRGB14':'Yellow','CYAN':'Cyan','MAGENTA':'Magenta','YELLOW':'Yellow'}
 motion={'PAN':'Pan','TILT':'Tilt'}
 controls={'RESET','CONTROL','DIMMERCURVE','DIMMER_CURVE','FIXTURECONTROL','LAMPON','LAMPOFF','LAMPRESET'}
 rate_functions={}
 for ch,c in zip(children(target,'Channel'),source):
  functions=children(c,'ChannelFunction');fn=functions[0] if functions else None
  attr=c.get('attribute','').upper();effective=fn.get('attribute',attr).upper() if fn is not None else attr
  label=fn.get('attribute_user_name',attr) if fn is not None else attr
  sub=fn.get('subattribute','').upper() if fn is not None else ''
  coarse=int(c.get('coarse'));maximum=65535 if 'fine' in c.attrib else 255
  if effective in ('DUMMY','RESERVED') or attr in ('DUMMY','RESERVED'):
   attached=first(ch,'AttachedChannels');ch.clear();ch.set('number',str(sum((2 if 'fine' in prior.attrib else 1) for prior in source[:list(source).index(c)])+1));ch.set('unused','true');continue
  attached=next(iter(children(ch,'AttachedChannels')),None)
  for child in list(ch):
   if child is not attached:ch.remove(child)
  ch.attrib.pop('unused',None);ranges=E.SubElement(ch,q('Ranges'))
  comment=None;inverse=None;lo=0;hi=100;feature='Variable'
  compact_attr=re.sub(r'[ _-]','',attr)
  plain=re.sub(r'\d+$','',compact_attr)
  if attr in motion:
   function=reserve(motion[attr],6);feature=motion[attr]
   if fn is None or not all(k in fn.attrib for k in ('physfrom','physto')):raise ValueError(f'В профиле отсутствует физический диапазон {function}.')
   lo=float(fn.get('physfrom'));hi=float(fn.get('physto'))
  elif re.fullmatch(r'DIM(?:MER)?\d*',attr):
   function='Intensity' if not counts.get('Intensity') else f"Sub Intensity {counts['Intensity']}";counts['Intensity']=counts.get('Intensity',0)+1;feature='Intensity'
  elif attr in rgb or compact_attr in color_names or plain in color_names or (attr.startswith('COLORRGB') and 'white' in label.lower()):
   function,inverse=rgb[attr] if attr in rgb else (color_names.get(compact_attr,color_names.get(plain,'I White')),None)
   if function.startswith('I '):
    if counts.get(function):
     comment='MA2 '+attr+' ('+label+'): background colour, console-confirmed BG/Intensity mapping.'
     function='BG '+function[2:];feature='Intensity';inverse=None
    else:feature='Intensity'
   counts[function]=counts.get(function,0)+1
   if counts[function]>1:raise ValueError(f'Нужно сопоставить повторяющийся цвет {function}.')
  elif compact_attr in ('CTO','CTB'):
   function=compact_attr;feature='Variable'
  elif 'STROBE' in compact_attr and 'DURATION' in compact_attr:
   function='Strobe Duration';feature='Duration';hi=1
  elif re.fullmatch(r'(?:SHUTTER|STROBE|STROBEFREQUENCY|STROBERATE)\d*',compact_attr):
   function=reserve('Strobe',4);feature='Rate';hi=10
   comment='MA2 '+attr+': no explicit shutter modes; linear Rate scale requires console/fixture validation.'
   if functions:
    import math
    for mode in functions:
     low=float(mode.get('physfrom','0'));high=float(mode.get('physto','10'))
     dlow=int(mode.get('min_dmx_24','0'));dhigh=int(mode.get('max_dmx_24','16777215'))
     if not all(math.isfinite(v) for v in (low,high)) or high<low or not 0<=dlow<=dhigh<=16777215:raise ValueError(f'Некорректный диапазон {attr}.')
     E.SubElement(ranges,q('Range'),dmxstart=str(round(dlow*maximum/16777215)),dmxend=str(round(dhigh*maximum/16777215)),function=function,feature=feature,start=str(low),end=str(high))
    continue
  elif compact_attr in ('PANTILTSPEED','PANSPEED','TILTSPEED','POSITIONSPEED','PT_SPEED','YZHOUSPEED','MOVEMENTSPEED'):
   function='Position Time';feature='Track'
  elif plain in ('ZOOM','FOCUS','IRIS','FROST'):
   function=plain.title();feature='Variable' if plain in ('ZOOM','FOCUS','FROST') else 'Iris'
  elif compact_attr in ('EFFECTSPEED','EFFECTRATE','MACROSPEED','MACRORATE'):
   function=rate_functions.get('effect')
   if function is None:function=macro_name(rate=True)
   feature='Rate';hi=100
  elif compact_attr in ('EFFECTWHEEL','EFFECT','MACRO','EFFECTSELECT'):
   function=macro_name();feature='Continuous';hi=255;rate_functions['effect']=function+' Rate'
  elif compact_attr in ('COLORMACRO','COLOURMACRO','COLORPRESET','COLOURPRESET','BACKCOLOR'):
   function=macro_name();feature='Continuous';hi=255
   comment='MA2 '+attr+': colour/control selector preserved as independent HOG Macro.'
  elif re.fullmatch(r'COLOR\d+|COLOUR\d+',compact_attr):
   function=reserve('Colour');feature='Continuous';hi=360
  elif compact_attr.startswith('GOBO'):
   if module.get('class','').upper() in ('LED','NONE'):
    function=macro_name();feature='Continuous';hi=255
    comment='MA2 '+attr+': pixel pattern selector/modifier preserved as independent HOG Macro.'
   else:
    function=reserve('Gobo',3);feature='Continuous';hi=360
    if fn is not None and 'physfrom' in fn.attrib and 'physto' in fn.attrib:
     lo=float(fn.get('physfrom'));hi=float(fn.get('physto'))
  elif compact_attr in controls or c.get('preset','').upper()=='CONTROL':
   sets=[n for f in functions for n in children(f,'ChannelSet')]
   reset=next((n for n in sets if re.search(r'reset|сброс',n.get('name',''),re.I)),None)
   if compact_attr=='RESET' or reset is not None:
    function='Fixture Control'
    E.SubElement(ranges,q('Range'),dmxstart='0',function=function,feature='Idle')
    E.SubElement(ranges,q('Range'),dmxstart='255',function=function,feature='Global Reset')
    continue
   function=macro_name();feature='Continuous';hi=255
   comment='MA2 '+attr+': discrete control values not supplied; raw control preserved as Macro, reset/curve thresholds not invented.'
  else:
   fallback+=1
   if fallback>9:raise ValueError('Нужно сопоставить дополнительные неизвестные каналы.')
   function='Beam Fx' if fallback==1 else f'Beam Fx {fallback}';feature='Continuous'
   comment='MA2 '+attr+' ('+label+'): no reliable HOG correspondence; Beam Fx fallback.'
  import math
  if not all(math.isfinite(float(v)) for v in (lo,hi)) or hi<=lo:raise ValueError(f'Некорректный диапазон {attr}.')
  E.SubElement(ranges,q('Range'),dmxstart='0',dmxend=str(maximum),function=function,feature=feature,start=str(lo),end=str(hi))
  if inverse and any(prior.get('attribute','').upper() in ({'Cyan':{'CYAN','COLORCMY1'},'Magenta':{'MAGENTA','COLORCMY2'},'Yellow':{'YELLOW','COLORCMY3','COLORRGB14'}}[inverse]) for prior in source):inverse=None
  if inverse:E.SubElement(ranges,q('Range'),dmxstart=str(maximum),dmxend='0',function=inverse,feature='Variable',start='0',end='100')
  if comment:ranges.insert(0,E.Comment(comment.replace('--','—')))

META_PREFIX='VLIGHTE_MA2_MODULES:'
def module_metadata(root):
 for node in root:
  if node.tag is E.Comment and (node.text or '').startswith(META_PREFIX):
   return json.loads(base64.b64decode(node.text[len(META_PREFIX):],validate=True))
 return None

def convert(root):
 if root.tag in (q('Hog'),'Hog'):return convert_channels(root)
 if root.tag==mq('MA'):
  recovery=read_hog_roundtrip(root)
  if recovery is not None:return parse(recovery['hogXml']),'MA2'
 # Reuse the tested DMX conversion, then expose source Module definitions.
 flat,system=convert_channels(root)
 fixture=descendants(root,'FixtureType')[0]
 modules={m.get('index'):m for m in descendants(fixture,'Module')}
 instances=descendants(fixture,'Instance')
 flattypes={t.get('name'):t for t in descendants(flat,'Type')}
 physical={};cursor=1
 for part in children(first(flat,'Parts'),'Part'):
  t=flattypes[part.get('type')]
  for ch in descendants(t,'Channel'):
   physical[cursor+int(ch.get('number'))-1]=copy.deepcopy(ch)
  cursor+=int(first(t,'Dmxfootprint').get('size'))
 placements=[];nextpatch=1;module_defs={};used=set()
 for inst in instances:
  mid=inst.get('module_index');module=modules[mid]
  source=sorted([c for c in children(module,'ChannelType') if 'coarse' in c.attrib],key=lambda c:int(c.get('coarse')))
  width=max((int(c.get('fine',c.get('coarse'))) for c in source),default=0)
  patch=int(inst.get('patch',nextpatch));nextpatch=patch+width
  if not width:raise ValueError('Модуль не содержит DMX-каналов.')
  placements.append({'module':mid,'patch':patch})
  for c in source:
   for offset in [int(c.get('coarse'))]+([int(c.get('fine'))] if 'fine' in c.attrib else []):
    address=patch+offset-1
    if address in used:raise ValueError(f'Пересечение DMX {address}.')
    used.add(address)
  if mid in module_defs:continue
  prototype=copy.deepcopy(flattypes[next(iter(flattypes))]);name=f"{fixture.get('name','MA2')} M{mid}"
  prototype.set('name',name);prototype.set('uuid',str(uuid.uuid4()));prototype.attrib.pop('mode',None);prototype.set('part',f'M{mid}')
  target=first(prototype,'Channels');target.clear();offsets=[];compact=1
  for c in source:
   coarse=int(c.get('coarse'));fine=int(c.get('fine')) if 'fine' in c.attrib else None
   ch=copy.deepcopy(physical[patch+coarse-1]);ch.set('number',str(compact))
   attached=descendants(ch,'AttachedChannel')
   if fine:
    if len(attached)!=1:raise ValueError('Не удалось перенести Fine-канал.')
    attached[0].text=str(compact+1)
   target.append(ch);offsets.append({'number':compact,'offset':coarse,'size':2 if fine else 1})
   compact+=2 if fine else 1
  map_module_channels(target,source,module)
  first(prototype,'Dmxfootprint').set('size',str(compact-1))
  module_defs[mid]={'name':name,'channels':offsets,'width':width,'type':prototype}
 lib=first(flat,'Library');lib.clear()
 manufacturer=E.SubElement(lib,q('Manufacturer'),name='User Created')
 product=E.SubElement(manufacturer,q('Product'),name=fixture.get('name','MA2'))
 types=E.SubElement(product,q('Types'))
 for mid in modules:
  if mid in module_defs:types.append(module_defs[mid]['type'])
 compound=copy.deepcopy(first(parse((ROOT/'private/strobe.xml').read_text()),'CompoundType'))
 compound.set('name',f"{fixture.get('name','MA2')} {fixture.get('mode','')} HOG4".strip())
 compound.set('mode',fixture.get('mode','MA2'));compound.set('uuid',str(uuid.uuid4()))
 partlist=first(compound,'Parts');partlist.clear();seen={};last=None;x=y=0
 bands={};band=0
 for mid in module_defs:
  bands[mid]=band;count=sum(p['module']==mid for p in placements);band+=max(10,((count+9)//10)*10)
 for i,placement in enumerate(placements):
  mid=placement['module'];seen[mid]=seen.get(mid,0)+1
  if last is not None:
   if mid==last:x+=1
   else:x=0;y-=1
  E.SubElement(partlist,q('Part'),type=module_defs[mid]['name'],number='0' if i==0 else f"0.{bands[mid]+seen[mid]}",plotX=str(x),plotY=str(y));last=mid
 E.SubElement(product,q('CompoundTypes')).append(compound)
 metadata={'version':1,'modules':{v['name']:{k:value for k,value in v.items() if k!='type'} for v in module_defs.values()},'placements':[{'type':module_defs[p['module']]['name'],'patch':p['patch']} for p in placements],'extent':max(used)}
 flat.insert(0,E.Comment(META_PREFIX+base64.b64encode(json.dumps(metadata,ensure_ascii=True,separators=(',',':')).encode()).decode()))
 return flat,system

def materialize_modules(root,payload,metadata):
 # Compound parts have no arbitrary patch offset in the HOG XSD. Split only
 # where another instance interrupts a module's original DMX addresses.
 types={t.get('name'):t for t in descendants(root,'Type')}
 compound=next(c for c in descendants(root,'CompoundType') if c.get('name')==payload['name'].strip())
 for parent in root.iter():
  for child in list(parent):
   if local(child)=='CompoundType' and child is not compound:parent.remove(child)
 edited=children(first(compound,'Parts'),'Part');slots={};cursor=1
 for i,(part,request) in enumerate(zip(edited,payload['parts'])):
  name=part.get('type');definition=metadata['modules'].get(name)
  if definition is None:raise ValueError('Неизвестный модуль MA2.')
  source=request.get('sourceIndex')
  original=metadata['placements'][source] if isinstance(source,int) and 0<=source<len(metadata['placements']) and payload.get('compoundIndex')==0 else None
  patch=original['patch'] if original and original['type']==name else cursor
  channels={int(c.get('number')):c for c in descendants(types[name],'Channel')}
  for spec in definition['channels']:
   address=patch+spec['offset']-1;ch=copy.deepcopy(channels[spec['number']])
   for offset in range(spec['size']):
    if address+offset in slots:raise ValueError(f'Пересечение DMX {address+offset}.')
    slots[address+offset]=(i,address,ch,spec['size'])
  cursor=max(cursor,patch+definition['width'])
 extent=max(slots,default=0)
 if not 1<=extent<=512:raise ValueError('DMX footprint превышает 512.')
 groups=[];address=1
 while address<=extent:
  if address not in slots:
   groups.append((None,address,[E.Element(q('Channel'),number='1',unused='true')],1));address+=1;continue
  owner,start,ch,size=slots[address];begin=address;out=[]
  while address<=extent and address in slots and slots[address][0]==owner:
   _,start,ch,size=slots[address]
   if address!=start:raise ValueError('Некорректный Fine-канал.')
   node=copy.deepcopy(ch);node.set('number',str(address-begin+1))
   for attached in descendants(node,'AttachedChannel'):attached.text=str(address-begin+2)
   out.append(node);address+=size
  groups.append((owner,begin,out,address-begin))
 # Reuse one output type for equal module segments across all instances.
 group=next(n for n in root.iter() if local(n)=='Types');group.clear()
 parts=first(compound,'Parts');parts.clear();cache={};numbers=set();extra=1
 for owner,address,channels,width in groups:
  template=types[edited[owner].get('type')] if owner is not None else next(iter(types.values()))
  key=(template.get('name') if owner is not None else 'Unused',tuple(E.tostring(c) for c in channels),width)
  if key not in cache:
   t=copy.deepcopy(template);target=first(t,'Channels');target.clear();target.extend(channels);first(t,'Dmxfootprint').set('size',str(width))
   count=sum(1 for k in cache if k[0]==key[0]);name=key[0] if count==0 else f'{key[0]} section {count+1}'
   t.set('name',name);t.set('uuid',str(uuid.uuid4()));group.append(t);cache[key]=name
  attrs=dict(edited[owner].attrib) if owner is not None else {'number':'0','plotX':'0','plotY':'0'}
  if attrs['number'] in numbers or owner is None:
   while f'0.{extra}' in numbers or any(p.get('number')==f'0.{extra}' for p in edited):extra+=1
   attrs['number']=f'0.{extra}';extra+=1
  numbers.add(attrs['number']);attrs['type']=cache[key];E.SubElement(parts,q('Part'),attrs)
 for node in list(root):
  if node.tag is E.Comment and (node.text or '').startswith(META_PREFIX):root.remove(node)
 return root

def normalize_compound_location(root):
 """Place compound definitions at Library level, as in the verified HOG file."""
 library=first(root,'Library')
 groups=descendants(library,'CompoundTypes')
 if not groups:return root
 parents={child:parent for parent in library.iter() for child in parent}
 group=groups[0]
 for other in groups[1:]:
  group.extend(list(other));parents[other].remove(other)
 parents[group].remove(group);library.append(group)
 return root

def generate_hog(payload):
 root=repair_generated_background(parse(payload['xml']));types=validate_hog(root);compounds=descendants(root,'CompoundType');index=payload.get('compoundIndex');name=payload.get('name','').strip();parts=payload.get('parts',[])
 if not name or len(name)>200 or not parts or len(parts)>512:raise ValueError('Укажите имя и части фикстуры.')
 if index is None:
  library=first(root,'Library');group=next(iter(children(library,'CompoundTypes')),None)
  if group is None:group=E.SubElement(library,q('CompoundTypes'))
  compound=E.SubElement(group,q('CompoundType'),revisionId='0');E.SubElement(compound,q('Origin')).text='User';E.SubElement(compound,q('Icon'),type='circle',size='medium')
 else:
  if not isinstance(index,int) or not 0<=index<len(compounds):raise ValueError('Некорректная фикстура.')
  compound=compounds[index]
 if any(c is not compound and c.get('name')==name for c in compounds):raise ValueError('Имя фикстуры уже используется.')
 compound.set('name',name)
 if module_metadata(root):
  compound.set('mode',compound.get('mode','MA2'));compound.set('uuid',compound.get('uuid',str(uuid.uuid4())))
 numbers=set();footprint=0;out=[]
 for part in parts:
  t=part.get('type');number=part.get('number','')
  if t not in types or not re.fullmatch(r'0(?:\.\d+)?',number):raise ValueError('Некорректный тип или номер части.')
  if re.fullmatch(r'0\.0+',number):number='0'
  if number in numbers:raise ValueError('Повторяется номер части.')
  numbers.add(number);footprint+=types[t]
  attrs={'type':t,'number':number}
  for key in ('plotX','plotY'):
   value=str(part.get(key,''))
   if len(value)>40 or not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)',value):raise ValueError('Некорректные координаты.')
   attrs[key]=value
  original=children(compound,'Parts');old=children(original[0],'Part') if original else [];source=part.get('sourceIndex');node=copy.deepcopy(old[source]) if isinstance(source,int) and 0<=source<len(old) else E.Element(q('Part'));node.attrib.update(attrs);out.append(node)
 if footprint>512:raise ValueError('DMX footprint превышает 512.')
 plist=next(iter(children(compound,'Parts')),None)
 if plist is None:plist=E.SubElement(compound,q('Parts'))
 for p in children(plist,'Part'):plist.remove(p)
 plist.extend(out);history=next(iter(children(compound,'History')),None)
 if history is None:history=E.Element(q('History'));compound.insert(0,history)
 history.attrib.update(revision=history.get('revision','0'),author='Vlighte',date=datetime.date.today().isoformat(),comment='EDIT')
 metadata=module_metadata(root)
 if metadata:root=materialize_modules(root,payload,metadata)
 return serialize(normalize_compound_location(root))

def mq(name):return '{'+M+'}'+name

def validate_ma2(root):
 if root.tag!=mq('MA') or len(descendants(root,'FixtureType'))!=1:raise ValueError('Ожидается один профиль MA2.')
 fixture=first(root,'FixtureType');module_list=first(fixture,'Modules');instance_list=first(fixture,'Instances')
 if module_list is None or instance_list is None:raise ValueError('В MA2 отсутствуют Modules или Instances.')
 modules={n.get('index'):n for n in children(module_list,'Module')}
 if not modules:raise ValueError('В MA2 нет модулей.')
 slots=set();cursor=1
 for inst in children(instance_list,'Instance'):
  module=modules.get(inst.get('module_index'))
  if module is None:raise ValueError('Неизвестный модуль MA2.')
  patch=int(inst.get('patch',cursor));width=0
  for channel in children(module,'ChannelType'):
   if 'coarse' not in channel.attrib:continue
   offsets=[int(channel.get('coarse'))]+([int(channel.get('fine'))] if 'fine' in channel.attrib else [])
   for offset in offsets:
    address=patch+offset-1
    if offset<1 or not 1<=address<=512 or address in slots:raise ValueError('Некорректный или пересекающийся DMX в MA2.')
    slots.add(address);width=max(width,offset)
  cursor=max(cursor,patch+width)
 if not slots:raise ValueError('В MA2 нет DMX-каналов.')
 return max(slots)

def ma2_serialize(root):
 # Never mutate ElementTree's global namespace registry in concurrent WSGI requests.
 root=copy.deepcopy(root)
 for node in root.iter():
  if isinstance(node.tag,str) and node.tag.startswith('{'+M+'}'):node.tag=local(node)
 root.set('xmlns',M)
 return serialize(root)

def ma2_from_original(payload,source):
 """Retain the original MA definitions; edit only fixture name and instances."""
 root=copy.deepcopy(source);fixture=first(root,'FixtureType')
 metadata=module_metadata(parse(payload['xml']))
 if not metadata:return None
 modules={n.get('index'):n for n in children(first(fixture,'Modules'),'Module')}
 mapping={f"{fixture.get('name','MA2')} M{mid}":mid for mid in modules}
 original=children(first(fixture,'Instances'),'Instance');container=first(fixture,'Instances')
 input_compounds=descendants(parse(payload['xml']),'CompoundType');selected=input_compounds[payload['compoundIndex']] if payload.get('compoundIndex') is not None else None
 input_parts=children(first(selected,'Parts'),'Part') if selected is not None else []
 container_attrs=dict(container.attrib);container.clear();container.attrib.update(container_attrs);cursor=1;used=set();extent=0;new_indices={}
 for i,part in enumerate(payload['parts']):
  mid=mapping.get(part['type'])
  if mid is None:raise ValueError('Модуль не найден в исходном MA2.')
  definition=metadata['modules'][part['type']];source_index=part.get('sourceIndex')
  old=original[source_index] if payload.get('compoundIndex')==0 and isinstance(source_index,int) and 0<=source_index<len(original) and original[source_index].get('module_index')==mid else None
  patch=int(metadata['placements'][source_index]['patch']) if old is not None else cursor
  node=copy.deepcopy(old) if old is not None else E.Element(mq('Instance'))
  node.set('index',str(i));node.set('module_index',mid)
  if old is None or patch!=int(old.get('patch',str(patch))):node.set('patch',str(patch))
  previous=input_parts[source_index] if old is not None and source_index<len(input_parts) else None
  for ma_key,hog_key in [('X','plotX'),('Y','plotY')]:
   if previous is None or str(part[hog_key])!=previous.get(hog_key,'0'):node.set(ma_key,str(part[hog_key]))
  if old is not None:new_indices.setdefault(source_index,i)
  for spec in definition['channels']:
   for offset in range(spec['size']):
    address=patch+spec['offset']+offset-1
    if address in used or not 1<=address<=512:raise ValueError('Пересечение или выход за 512 DMX в MA2.')
    used.add(address);extent=max(extent,address)
  cursor=max(cursor,patch+definition['width']);container.append(node)
 for node in container:
  parent=node.get('parent_instance')
  if parent is not None:
   if int(parent) in new_indices:node.set('parent_instance',str(new_indices[int(parent)]))
   else:node.attrib.pop('parent_instance',None)
 old_extent=validate_ma2(source)
 if selected is None or payload['name'].strip()!=selected.get('name'):fixture.set('name',payload['name'].strip())
 if extent!=old_extent:fixture.set('mode',str(extent)+'ch')
 validate_ma2(root)
 return ma2_serialize(root),[]

def ma2_attribute(function,feature):
 mapping={'Intensity':('DIM','DIMMER','DIMMER',.01),'Pan':('PAN','POSITION','POSITION',1),'Tilt':('TILT','POSITION','POSITION',1),
 'I Red':('COLORRGB1','COLORRGB','COLOR',.01),'I Green':('COLORRGB2','COLORRGB','COLOR',.01),'I Blue':('COLORRGB3','COLORRGB','COLOR',.01),
 'I White':('COLORRGB7','COLORRGB','COLOR',.01),'BG Red':('RED','COLORRGB','COLOR',.01),'BG Green':('GREEN','COLORRGB','COLOR',.01),'BG Blue':('BLUE','COLORRGB','COLOR',.01),
 'Cyan':('COLORCMY1','COLORRGB','COLOR',.01),'Magenta':('COLORCMY2','COLORRGB','COLOR',.01),'Yellow':('COLORCMY3','COLORRGB','COLOR',.01),
 'Strobe':('SHUTTER1','SHUTTER','BEAM',1),'Zoom':('ZOOM','FOCUS','FOCUS',1),'Focus':('FOCUS','FOCUS','FOCUS',1),'Iris':('IRIS','BEAM','BEAM',.01)}
 if function in mapping:return mapping[function]
 match=re.fullmatch(r'Sub Intensity (\d+)',function)
 if match:return ('DIM'+str(int(match[1])+1),'DIMMER','DIMMER',.01)
 # Beam Fx has no unique MA2 meaning. Expose an explicitly labelled effect
 # control using known MA2 attributes, preserving its DMX and physical range.
 match=re.fullmatch(r'Beam Fx(?: (\d+))?( <>)?',function)
 if match:
  num=int(match[1] or 1)
  if num not in (1,2):raise ValueError('Нужно сопоставить MA2 для '+function)
  attr=('GOBOWHEELS' if num==1 else 'GOBO5')+('_POS' if match[2] else '')
  if attr=='GOBOWHEELS_POS':attr='GOBO1_POS'
  return (attr,'GOBOALL' if num==1 else 'GOBO5','GOBO',1)
 raise ValueError('Нужно сопоставить функцию HOG4 с MA2: '+function+' / '+feature)

def ma2_from_hog(payload,hog_xml):
 hog=parse(hog_xml);types={n.get('name'):n for n in descendants(hog,'Type')}
 compound=next((c for c in descendants(hog,'CompoundType') if c.get('name')==payload['name'].strip()),None)
 if compound is None:raise ValueError('Не найдена составная фикстура.')
 root=E.Element(mq('MA'),{'{http://www.w3.org/2001/XMLSchema-instance}schemaLocation':M+' http://schemas.malighting.de/grandma2/xml/3.1.2/MA.xsd','major_vers':'3','minor_vers':'1','stream_vers':'2'})
 E.SubElement(root,mq('Info'),datetime=datetime.datetime.now(datetime.timezone.utc).isoformat(),showfile='Vlighte')
 fixture=E.SubElement(root,mq('FixtureType'),index='0',name=payload['name'].strip(),mode='')
 E.SubElement(fixture,mq('short_name')).text=payload['name'].strip()[:32]
 modules=E.SubElement(fixture,mq('Modules'),index='0');instances=E.SubElement(fixture,mq('Instances'),index='1')
 module_ids={};cursor=1;warnings=[]
 for index,part in enumerate(children(first(compound,'Parts'),'Part')):
  name=part.get('type');t=types[name];width=int(first(t,'Dmxfootprint').get('size'))
  if name not in module_ids:
   mid=str(len(module_ids));module_ids[name]=mid
   module=E.SubElement(modules,mq('Module'),index=mid,name=name,**{'class':'LED','beamtype':'Wash'})
   occupied=set();attributes=set()
   for ch_index,channel in enumerate(descendants(t,'Channel')):
    coarse=int(channel.get('number'));attached=descendants(channel,'AttachedChannel')
    fine=int(attached[0].text) if attached else None
    if len(attached)>1 or fine is not None and fine!=coarse+1:raise ValueError('Неподдерживаемая точность канала HOG4 для MA2.')
    for offset in [coarse]+([fine] if fine else []):
     if not 1<=offset<=width or offset in occupied:raise ValueError('Некорректная структура DMX HOG4.')
     occupied.add(offset)
    ranges=children(first(channel,'Ranges'),'Range') if first(channel,'Ranges') is not None else []
    if channel.get('unused')=='true':ranges=[]
    if not ranges:
     attr,feature,preset,scale='DUMMY','CONTROL','CONTROL',1;label='Unused';ranges=[E.Element('Range',dmxstart='0',dmxend='65535' if fine else '255',start='0',end='100')]
    else:
     primary=ranges[0];label=primary.get('function','');attr,feature,preset,scale=ma2_attribute(label,primary.get('feature',''))
     # Complementary HOG colour controls describe the same physical channel.
     inverse={'I Red':'Cyan','I Green':'Magenta','I Blue':'Yellow'}
     ranges=[r for r in ranges if r.get('function')!=inverse.get(label)]
     if any(r.get('function')!=label for r in ranges):raise ValueError('Несколько функций в одном HOG4-канале требуют сопоставления MA2.')
     if attr in attributes and attr!='DUMMY':raise ValueError('Повтор атрибута MA2 в одном модуле: '+attr)
     attributes.add(attr)
     if label.startswith('Beam Fx'):warnings.append(label+': экспортирован как '+attr+'; проверьте назначение эффекта в MA2.')
    c=E.SubElement(module,mq('ChannelType'),index=str(ch_index),attribute=attr,feature=feature,preset=preset,coarse=str(coarse))
    if fine:c.set('fine',str(fine))
    maximum=65535 if fine else 255
    for r_index,r in enumerate(ranges):
     lo=int(r.get('dmxstart','0'));hi=int(r.get('dmxend',str(lo)))
     if not 0<=lo<=hi<=maximum:raise ValueError('Обратный или некорректный диапазон HOG4 требует сопоставления MA2.')
     start=float(r.get('start','0'))*scale;end=float(r.get('end',r.get('start','0')))*scale
     E.SubElement(c,mq('ChannelFunction'),index=str(r_index),**{'from':r.get('start','0'),'to':r.get('end',r.get('start','0')),'min_dmx_24':str(round(lo*16777215/maximum)),'max_dmx_24':str(round(hi*16777215/maximum)),'physfrom':str(start),'physto':str(end),'subattribute':'RESERVED' if attr=='DUMMY' else attr,'attribute':attr,'attribute_user_name':label,'feature':feature,'preset':preset})
   # Explicit unused slots retain the complete footprint, including padding.
   for offset in range(1,width+1):
    if offset not in occupied:
     c=E.SubElement(module,mq('ChannelType'),index=str(len(children(module,'ChannelType'))),attribute='DUMMY',feature='CONTROL',preset='CONTROL',coarse=str(offset))
     E.SubElement(c,mq('ChannelFunction'),index='0',**{'from':'0','to':'100','min_dmx_24':'0','max_dmx_24':'16777215','subattribute':'RESERVED','attribute':'DUMMY','feature':'CONTROL','preset':'CONTROL'})
  E.SubElement(instances,mq('Instance'),index=str(index),module_index=module_ids[name],patch=str(cursor),X=part.get('plotX','0'),Y=part.get('plotY','0'));cursor+=width
 for tag,idx in [('Wheels','2'),('VirtualFunctionBlocks','3'),('FixtureMacroCollect','5')]:E.SubElement(fixture,mq(tag),index=idx)
 E.SubElement(fixture,mq('Body'));E.SubElement(fixture,mq('AutoPresets'),index='4');fixture.set('mode',str(cursor-1)+'ch')
 warnings=sorted(set(warnings))
 for warning in warnings:root.append(E.Comment('VLIGHTE: '+warning.replace('--','—')))
 validate_ma2(root)
 return ma2_serialize(root),warnings

ROUNDTRIP_PREFIX='VLIGHTE_MA2_ROUNDTRIP:'

def hog_semantic_hash(root):
 def value(node):
  return [node.tag,sorted(node.attrib.items()),(node.text or '').strip(),[value(c) for c in node if isinstance(c.tag,str)]]
 return hashlib.sha256(json.dumps(value(root),ensure_ascii=True,separators=(',',':')).encode()).hexdigest()

def read_ma2_roundtrip(root):
 notes=[n for n in root if n.tag is E.Comment and (n.text or '').startswith(ROUNDTRIP_PREFIX)]
 if not notes:return None
 if len(notes)!=1:raise ValueError('Повтор служебных данных MA2 в HOG4.')
 try:
  data=json.loads(base64.b64decode(notes[0].text[len(ROUNDTRIP_PREFIX):],validate=True))
  if not isinstance(data,dict) or data.get('version')!=1 or not isinstance(data.get('ma2Xml'),str) or not isinstance(data.get('compoundName'),str):raise ValueError('Повреждены служебные данные MA2 в HOG4.')
  validate_ma2(parse(data['ma2Xml']))
  if data.get('hogHash')!=hog_semantic_hash(root):raise ValueError('HOG4 изменён вне редактора. Исходные данные MA2 больше не соответствуют каналам; требуется сопоставление.')
  return data
 except (TypeError,KeyError,UnicodeError,json.JSONDecodeError):raise ValueError('Повреждены служебные данные MA2 в HOG4.')

def attach_ma2_roundtrip(hog_xml,ma_xml,name):
 root=parse(hog_xml)
 for node in list(root):
  if node.tag is E.Comment and (node.text or '').startswith(ROUNDTRIP_PREFIX):root.remove(node)
 data={'version':1,'ma2Xml':ma_xml,'compoundName':name,'hogHash':hog_semantic_hash(root)}
 note=ROUNDTRIP_PREFIX+base64.b64encode(json.dumps(data,ensure_ascii=True,separators=(',',':')).encode()).decode()
 root.insert(0,E.Comment(note))
 return serialize(root)

def ma2_from_roundtrip(payload,hog,data):
 """Restore full MA definitions and apply supported assembly edits, never guess Macro mappings."""
 root=parse(data['ma2Xml']);fixture=first(root,'FixtureType')
 compounds=descendants(hog,'CompoundType');index=payload.get('compoundIndex')
 source_compound=next((c for c in compounds if c.get('name')==data['compoundName']),None)
 if source_compound is None or index is not None and compounds[index] is not source_compound:raise ValueError('Данные MA2 относятся к другой фикстуре.')
 original_parts=children(first(source_compound,'Parts'),'Part');requested=payload['parts']
 same=len(requested)==len(original_parts) and all(p.get('sourceIndex')==i and all(str(p.get(k,''))==old.get(k,'0') for k in ('type','plotX','plotY')) for i,(p,old) in enumerate(zip(requested,original_parts)))
 # Part numbers are HOG identifiers, not MA2 instance indexes.
 if not same:
  definitions={t.get('name'):t for t in descendants(hog,'Type')}
  modules={n.get('index'):n for n in children(first(fixture,'Modules'),'Module')}
  original=children(first(fixture,'Instances'),'Instance');spans={};cursor=1
  for i,instance in enumerate(original):
   module=modules[instance.get('module_index')]
   width=max((int(c.get('fine',c.get('coarse'))) for c in children(module,'ChannelType') if 'coarse' in c.attrib),default=0)
   start=int(instance.get('patch',cursor));spans[(start,width)]=i;cursor=max(cursor,start+width)
  bindings={};type_bindings={};cursor=1
  for i,part in enumerate(original_parts):
   width=int(first(definitions[part.get('type')],'Dmxfootprint').get('size'));binding=spans.get((cursor,width))
   if binding is not None:bindings[i]=binding;type_bindings.setdefault(part.get('type'),set()).add(original[binding].get('module_index'))
   cursor+=width
  # Split/interleaved modules can round-trip unchanged. Editing them requires
  # a finer correspondence; refuse rather than discard virtual channels/ranges.
  if len(bindings)!=len(original_parts) or len(set(bindings.values()))!=len(original):raise ValueError('Для правки разделённых модулей MA2 требуется дополнительное сопоставление. Экспорт без правок сохраняет исходник полностью.')
  instances=first(fixture,'Instances');attributes=dict(instances.attrib);instances.clear();instances.attrib.update(attributes)
  cursor=1;old_to_new={};old_extent=validate_ma2(parse(data['ma2Xml']))
  for i,part in enumerate(requested):
   source=part.get('sourceIndex');old_part=original_parts[source] if isinstance(source,int) and 0<=source<len(original_parts) else None
   old_index=bindings.get(source) if old_part is not None and old_part.get('type')==part['type'] else None
   if old_index is None:
    mids=type_bindings.get(part['type'],set())
    if len(mids)!=1:raise ValueError('Нельзя однозначно определить исходный модуль MA2.')
    mid=next(iter(mids));node=E.Element(mq('Instance'),module_index=mid)
   else:
    node=copy.deepcopy(original[old_index]);old_to_new.setdefault(old_index,i)
   node.set('index',str(i))
   if old_index is None or cursor!=next(start for (start,width),oi in spans.items() if oi==old_index):node.set('patch',str(cursor))
   for ma_key,hog_key in [('X','plotX'),('Y','plotY')]:
    if old_part is None or str(part[hog_key])!=old_part.get(hog_key,'0'):node.set(ma_key,str(part[hog_key]))
   instances.append(node);cursor+=int(first(definitions[part['type']],'Dmxfootprint').get('size'))
  for node in instances:
   parent=node.get('parent_instance')
   if parent is not None:
    if int(parent) in old_to_new:node.set('parent_instance',str(old_to_new[int(parent)]))
    else:node.attrib.pop('parent_instance',None)
  if cursor-1!=old_extent:fixture.set('mode',str(cursor-1)+'ch')
 if payload['name'].strip()!=data['compoundName']:fixture.set('name',payload['name'].strip())
 validate_ma2(root)
 return ma2_serialize(root),[]

def generate(payload):
 source=parse(payload['sourceXml']) if payload.get('sourceXml') else None
 if source is not None and source.tag==mq('MA') or ROUNDTRIP_PREFIX in payload.get('xml',''):
  return generate_formats(payload)['xml']
 return generate_hog(payload)


HOG_ROUNDTRIP_PREFIX='VLIGHTE_HOG_ROUNDTRIP:'

def strip_recovery(root):
 root=copy.deepcopy(root)
 for node in list(root):
  if node.tag is E.Comment and (node.text or '').startswith((ROUNDTRIP_PREFIX,HOG_ROUNDTRIP_PREFIX)):root.remove(node)
 return root

def read_hog_roundtrip(root):
 notes=[n for n in root if n.tag is E.Comment and (n.text or '').startswith(HOG_ROUNDTRIP_PREFIX)]
 if not notes:return None
 if len(notes)!=1:raise ValueError('Повтор служебных данных HOG4 в MA2.')
 try:
  data=json.loads(base64.b64decode(notes[0].text[len(HOG_ROUNDTRIP_PREFIX):],validate=True))
  if not isinstance(data,dict) or data.get('version')!=1 or not isinstance(data.get('hogXml'),str):raise ValueError('Повреждены служебные данные HOG4 в MA2.')
  validate_hog(parse(data['hogXml']))
  if data.get('maHash')!=hog_semantic_hash(root):raise ValueError('MA2 изменён вне редактора. Исходные данные HOG4 больше не соответствуют каналам; требуется сопоставление.')
  return data
 except (TypeError,KeyError,UnicodeError,json.JSONDecodeError):raise ValueError('Повреждены служебные данные HOG4 в MA2.')

def attach_hog_roundtrip(ma_xml,hog_xml):
 root=strip_recovery(parse(ma_xml));bare_hog=serialize(strip_recovery(parse(hog_xml)))
 data={'version':1,'hogXml':bare_hog,'maHash':hog_semantic_hash(root)}
 root.insert(0,E.Comment(HOG_ROUNDTRIP_PREFIX+base64.b64encode(json.dumps(data,ensure_ascii=True,separators=(',',':')).encode()).decode()))
 return ma2_serialize(root)

def unchanged_hog_assembly(root,payload):
 index=payload.get('compoundIndex');compounds=descendants(root,'CompoundType')
 if not isinstance(index,int) or not 0<=index<len(compounds) or module_metadata(root):return False
 compound=compounds[index];old=children(first(compound,'Parts'),'Part')
 if payload['name'].strip()!=compound.get('name') or len(payload['parts'])!=len(old):return False
 return all(p.get('sourceIndex')==i and all(str(p.get(k,''))==node.get(k,'0') for k in ('type','number','plotX','plotY')) for i,(p,node) in enumerate(zip(payload['parts'],old)))

def generate_formats(payload):
 input_root=parse(payload['xml']);roundtrip=read_ma2_roundtrip(input_root)
 hog=generate_hog(payload) # Validate every requested edit even on the unchanged path.
 source=parse(payload['sourceXml']) if payload.get('sourceXml') else None
 hog_source=read_hog_roundtrip(source) if source is not None and source.tag==mq('MA') else None
 if roundtrip is not None:original=ma2_from_roundtrip(payload,input_root,roundtrip)
 elif hog_source is not None:
  snapshot=parse(hog_source['hogXml'])
  if hog_semantic_hash(snapshot)!=hog_semantic_hash(input_root):raise ValueError('Данные HOG4 не соответствуют загруженному MA2.')
  index=payload.get('compoundIndex');compounds=descendants(snapshot,'CompoundType')
  if not isinstance(index,int) or not 0<=index<len(compounds):raise ValueError('Для нового HOG-модуля требуется сопоставление с MA2.')
  original=ma2_from_roundtrip(payload,input_root,{'ma2Xml':ma2_serialize(strip_recovery(source)),'compoundName':compounds[index].get('name')})
 else:original=ma2_from_original(payload,source) if source is not None and source.tag==mq('MA') else None
 # Avoid rewriting UUIDs, history or any other original HOG definitions on a
 # format-only save. Actual assembly edits still use the validated generator.
 if unchanged_hog_assembly(input_root,payload):hog=serialize(normalize_compound_location(copy.deepcopy(input_root)))
 ma,warnings=original if original is not None else ma2_from_hog(payload,hog)
 # Both files carry the bare counterpart, not a copy of nested recovery notes.
 # This keeps repeated round trips bounded instead of recursively growing XML.
 bare_hog=serialize(strip_recovery(parse(hog)));bare_ma=ma2_serialize(strip_recovery(parse(ma)))
 hog=attach_ma2_roundtrip(bare_hog,bare_ma,payload['name'].strip())
 ma=attach_hog_roundtrip(bare_ma,bare_hog)
 if len(hog.encode('utf-8'))>2_000_000 or len(ma.encode('utf-8'))>2_000_000:raise ValueError('Сформированный XML превышает 2 МБ с данными восстановления.')
 return {'xml':hog,'ma2Xml':ma,'warnings':warnings}


# Offline lookup: visitor addresses are never sent to a geolocation service.
import ipaddress, functools, threading, time, hmac
_GEO_CACHE=None
_GEO_INSTALL_LOCK=threading.Lock()
_GEO_INSTALL_STATUS='starting'

def ensure_country_assets():
 global _GEO_INSTALL_STATUS
 path=ROOT/'private/country-assets26.zip'
 if path.exists():return
 if not _GEO_INSTALL_LOCK.acquire(blocking=False):return
 try:
  import urllib.request,gzip,csv,tarfile
  def download(url,expected):
   request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (Vlighte offline country database)'})
   with urllib.request.urlopen(request,timeout=15) as response:blob=response.read(20_000_001)
   if len(blob)>20_000_000 or hashlib.sha256(blob).hexdigest()!=expected:raise ValueError('Country asset integrity')
   return blob
  _GEO_INSTALL_STATUS='database-download'
  database=download('https://download.db-ip.com/free/dbip-country-lite-2026-10.csv.gz','097426b8ddae89157d444a59ac1847e873f7943c32d52becc4371c8b0273af80')
  _GEO_INSTALL_STATUS='flags-download'
  flag_archive=download('https://registry.npmjs.org/country-flag-icons/-/country-flag-icons-1.6.20.tgz','0b003c0984b53e12a870f0b9a2ccd9821c588c505cf18d7938dabec3ae669808')
  _GEO_INSTALL_STATUS='building'
  arrays={4:bytearray(),6:bytearray()}
  with gzip.open(io.BytesIO(database),'rt',encoding='utf-8') as source:
   for begin,end,code in csv.reader(source):
    a,b=ipaddress.ip_address(begin),ipaddress.ip_address(end)
    arrays[a.version].extend(a.packed+b.packed+code.encode('ascii'))
  buffer=io.BytesIO()
  with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as out:
   for version,data in arrays.items():out.writestr('ipv'+str(version)+'.bin',data)
   with tarfile.open(fileobj=io.BytesIO(flag_archive),mode='r:gz') as tar:
    for member in tar.getmembers():
     if re.fullmatch(r'package/3x2/[A-Z]{2}\.svg',member.name):out.writestr('flags/'+member.name.rsplit('/',1)[1],tar.extractfile(member).read())
     elif member.name=='package/LICENSE':out.writestr('COUNTRY-FLAG-ICONS-LICENSE',tar.extractfile(member).read())
   out.writestr('GEO-DATA-LICENSE.txt','IP Geolocation by DB-IP https://db-ip.com - Country Lite 2026-10, CC BY 4.0 https://creativecommons.org/licenses/by/4.0/')
  path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
  temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.new')
  fd=os.open(str(temporary),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
  with os.fdopen(fd,'wb') as stream:stream.write(buffer.getvalue())
  temporary.replace(path)
  _GEO_INSTALL_STATUS='ready'
 except Exception as error:
  # Geolocation must never prevent generation or saving a file.
  _GEO_INSTALL_STATUS='unavailable:'+type(error).__name__+(':'+str(error.code) if hasattr(error,'code') else '')
 finally:_GEO_INSTALL_LOCK.release()

def country_assets():
 global _GEO_CACHE
 if _GEO_CACHE is not None:return _GEO_CACHE
 path=ROOT/'private/country-assets26.zip'
 if not path.is_file():return None
 try:
  with zipfile.ZipFile(path) as archive:
   _GEO_CACHE={'4':archive.read('ipv4.bin'),'6':archive.read('ipv6.bin'),'flags':{name[6:8]:archive.read(name) for name in archive.namelist() if re.fullmatch(r'flags/[A-Z]{2}\.svg',name)}}
  return _GEO_CACHE
 except (OSError,ValueError,KeyError,zipfile.BadZipFile):return None

def clean_ip(value):
 try:
  if not isinstance(value,str) or '%' in value:return None
  address=ipaddress.ip_address(value.strip())
  if isinstance(address,ipaddress.IPv6Address) and address.ipv4_mapped:address=address.ipv4_mapped
  return address
 except ValueError:return None

def request_ip(environ):
 remote=clean_ip(environ.get('REMOTE_ADDR',''));server=clean_ip(environ.get('SERVER_ADDR',''))
 if remote is None:return None
 def trusted(address):return address.is_loopback or (server is not None and address==server)
 # A public direct peer is authoritative. Ignore caller-supplied forwarding headers.
 if not trusted(remote):return str(remote)
 chain=environ.get('HTTP_X_FORWARDED_FOR','')
 if len(chain)>4096:return None
 for value in reversed(chain.split(',')):
  address=clean_ip(value)
  if address is None:return None
  if not trusted(address):return str(address)
 return None

def lookup_country(value):
 address=clean_ip(value)
 if address is None or not address.is_global:return None
 assets=country_assets()
 if assets is None:return None
 data=assets[str(address.version)];width=len(address.packed);stride=width*2+2;needle=address.packed
 lo,hi=0,len(data)//stride
 while lo<hi:
  mid=(lo+hi)//2
  if data[mid*stride:mid*stride+width]<=needle:lo=mid+1
  else:hi=mid
 offset=(lo-1)*stride
 if lo==0 or needle>data[offset+width:offset+width*2]:return None
 code=data[offset+width*2:offset+stride].decode('ascii')
 return code if code in assets['flags'] else None

def capture_origin(environ):
 address=request_ip(environ)
 return {'ip':address,'countryCode':lookup_country(address),'generatedAt':datetime.datetime.now(datetime.timezone.utc).isoformat()}

def origin_receipt(origin,hog,ma):
 record=owner_credentials()
 if not record:return None
 data={**origin,'expires':int(time.time())+86400,'hog':hashlib.sha256(hog.encode()).hexdigest(),'ma':hashlib.sha256((ma or '').encode()).hexdigest()}
 value=base64.urlsafe_b64encode(json.dumps(data,separators=(',',':')).encode()).decode()
 signature=hmac.new(bytes.fromhex(record['hash']),('vlighte-origin:'+value).encode(),hashlib.sha256).hexdigest()
 return value+'.'+signature

def archive_origin(payload,environ):
 receipt=payload.get('originReceipt')
 if not receipt:return capture_origin(environ)
 try:
  if not isinstance(receipt,str) or len(receipt)>2048:raise ValueError()
  value,signature=receipt.split('.');record=owner_credentials()
  expected=hmac.new(bytes.fromhex(record['hash']),('vlighte-origin:'+value).encode(),hashlib.sha256).hexdigest()
  if not hmac.compare_digest(signature,expected):raise ValueError()
  data=json.loads(base64.urlsafe_b64decode(value))
  if not 0<data['expires']-int(time.time())<=86400:raise ValueError()
  if data['hog']!=hashlib.sha256(payload['xml'].encode()).hexdigest() or data['ma']!=hashlib.sha256((payload.get('ma2Xml') or '').encode()).hexdigest():raise ValueError()
  return {key:data[key] for key in ('ip','countryCode','generatedAt')}
 except (ValueError,TypeError,KeyError,AttributeError):raise ValueError('Срок подтверждения генерации истёк или данные изменились. Сгенерируйте файл повторно.')

def public_record(info):
 return {key:info[key] for key in ('id','name','folder','source','date','url','formats','countryCode','duplicate') if key in info}

def geo_public(environ,start_response,path):
 if path=='/api/files/geo-status':
  assets=country_assets()
  address=request_ip(environ)
  return library_json(start_response,'200 OK',{'ready':assets is not None,'database':'DB-IP Country Lite 2026-10','ipv4Ranges':len(assets['4'])//10 if assets else 0,'ipv6Ranges':len(assets['6'])//34 if assets else 0,'flags':len(assets['flags']) if assets else 0,'requestCountry':lookup_country(address),'installState':_GEO_INSTALL_STATUS,'addressSource':'unknown' if address is None else 'direct' if address==str(clean_ip(environ.get('REMOTE_ADDR',''))) else 'forwarded'})
 match=re.fullmatch(r'/api/files/flags/([A-Z]{2})\.svg',path)
 if not match:return None
 assets=country_assets();body=assets['flags'].get(match[1]) if assets else None
 start_response('200 OK' if body else '404 Not Found',[('Content-Type','image/svg+xml'),('Content-Length',str(len(body or b''))),('Cache-Control','public, max-age=86400'),('X-Content-Type-Options','nosniff')])
 return [body or b'']

if os.environ.get('VLIGHTE_GEO_BOOTSTRAP','1')=='1':threading.Thread(target=ensure_country_assets,daemon=True).start()


STORE=ROOT/'saved-files'

def export_filename(source,kind,name):
 origin='MA' if parse(source).tag=='{'+M+'}MA' else 'HOG'
 title=re.sub(r'[^a-zA-Z0-9а-яА-ЯёЁ_-]','_',str(name))[:100] or 'Fixture'
 return origin+'-'+('MA' if kind=='ma2' else 'HOG')+'_'+title+'_vlighte_ru.xml'

def object_fingerprint(xml):
 root=parse(xml);ids={}
 def normal(value):
  def identity(match):
   key=match.group(0).lower()
   if key not in ids:ids[key]=str(len(ids))
   return 'UUID:'+ids[key]
  return re.sub(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}',identity,value)
 def node(e):
  if not isinstance(e.tag,str) or local(e) in ('History','Info'):return None
  return [e.tag,[(k,normal(v)) for k,v in sorted(e.attrib.items())],normal((e.text or '').strip()),[n for child in e if (n:=node(child)) is not None]]
 return hashlib.sha256(json.dumps(node(root),ensure_ascii=False).encode()).hexdigest()

def find_existing(payload,generated):
 fingerprint=object_fingerprint(generated)
 for info_path in STORE.glob('*/info.json'):
  try:
   info=json.loads(info_path.read_text(encoding='utf-8'))
   if not re.fullmatch(r'[a-f0-9]{32}',info.get('id','')):continue
   hog=list(info_path.parent.glob('HOG4_*.xml'))
   if len(hog)!=1 or object_fingerprint(hog[0].read_text(encoding='utf-8'))!=fingerprint:continue
   ma=list(info_path.parent.glob('MA2_*.xml'))
   if payload.get('ma2Xml'):
    if len(ma)==1:
     if object_fingerprint(ma[0].read_text(encoding='utf-8'))!=object_fingerprint(payload['ma2Xml']):continue
    else:
     sources=list(info_path.parent.glob('source_*.xml'))
     if len(sources)!=1 or object_fingerprint(sources[0].read_text(encoding='utf-8'))!=object_fingerprint(payload['sourceXml']):continue
   return info
  except (OSError,ValueError,KeyError,TypeError):continue
 return None

def download_xml(environ,start_response,path):
 match=re.fullmatch(r'/api/files/([a-f0-9]{32})/download/(hog|ma2)',path)
 if not match:return None
 saved=read_saved_xml('/api/files/'+match[1]+'/'+match[2])
 if not saved:
  start_response('404 Not Found',[('Content-Length','0'),('Cache-Control','no-store')]);return [b'']
 from urllib.parse import quote
 info=json.loads((STORE/match[1]/'info.json').read_text(encoding='utf-8'))
 source=read_saved_xml('/api/files/'+match[1]+'/source')
 filename=export_filename(source['xml'] if source else saved['xml'],match[2],info.get('name','Fixture'))
 body=saved['xml'].encode('utf-8')
 start_response('200 OK',[('Content-Type','application/xml; charset=utf-8'),('Content-Length',str(len(body))),('Content-Disposition',"attachment; filename=fixture_vlighte_ru.xml; filename*=UTF-8''"+quote(filename,safe='')),('Cache-Control','no-store'),('X-Content-Type-Options','nosniff')]);return [body]

# Serialize duplicate checks and archive writes across Passenger workers.
def save_pair(payload,generated):
 import fcntl
 STORE.mkdir(mode=0o700,parents=True,exist_ok=True)
 with (STORE/'.archive.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  existing=find_existing(payload,generated)
  if existing:return {**existing,'duplicate':True}
  return save_pair_unlocked(payload,generated)

def save_pair_unlocked(payload,generated):
 source=payload.get('sourceXml')
 if source is None:return None
 parse(source)
 filename=Path(str(payload.get('sourceName','source.xml'))).name
 filename=re.sub(r'[^\w. -]','_',filename)[:150] or 'source.xml'
 if not filename.lower().endswith('.xml'):filename+='.xml'
 name=re.sub(r'[^\w -]','_',payload.get('name','HOG4'))[:100] or 'HOG4'
 token=uuid.uuid4().hex;stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
 STORE.mkdir(mode=0o700,parents=True,exist_ok=True)
 folder=STORE/token;folder.mkdir(mode=0o700)
 try:
  (folder/('source_'+filename)).write_bytes(source.encode('utf-8'))
  (folder/('HOG4_'+export_filename(source,'hog',payload.get('name',name)))).write_bytes(generated.encode('utf-8'))
  if payload.get('ma2Xml'):(folder/('MA2_'+export_filename(source,'ma2',payload.get('name',name)))).write_bytes(payload['ma2Xml'].encode('utf-8'))
  info={'id':token,'name':name,'folder':stamp+'_'+name,'source':filename,'date':datetime.datetime.now(datetime.timezone.utc).isoformat(),'url':'/api/files/'+token+'.zip'}
  info['origin']=payload.get('_requestOrigin',{})
  info['countryCode']=info['origin'].get('countryCode')
  if payload.get('ma2Xml'):
   info['formats']=['hog','ma2']
   with zipfile.ZipFile(folder/'bundle.zip','w',zipfile.ZIP_DEFLATED) as archive:
    for file in sorted(folder.glob('*.xml')):
     if file.name.startswith(('HOG4_','MA2_')):archive.write(file,info['folder']+'/'+file.name.split('_',1)[1])
  (folder/'info.json').write_text(json.dumps(info,ensure_ascii=False),encoding='utf-8')
 except Exception:
  shutil.rmtree(folder);raise
 refresh_library()
 return info

def download_pair(path):
 match=re.fullmatch(r'/api/files/([a-f0-9]{32})\.zip',path)
 if not match:return None
 folder=STORE/match.group(1)
 if not (folder/'info.json').is_file():return None
 if (folder/'bundle.zip').is_file():return (folder/'bundle.zip').read_bytes()
 info=json.loads((folder/'info.json').read_text(encoding='utf-8'));buffer=io.BytesIO()
 with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
  for file in sorted(folder.glob('*.xml')):archive.write(file,info['folder']+'/'+file.name)
 return buffer.getvalue()

def refresh_library():
 records=[]
 if STORE.exists():
  for path in STORE.glob('*/info.json'):
   try:
    record=json.loads(path.read_text(encoding='utf-8'))
    if re.fullmatch(r'[a-f0-9]{32}',record.get('id','')) and len(list(path.parent.glob('*.xml'))) in (2,3):records.append(public_record(record))
   except (OSError,ValueError,TypeError):continue
 records.sort(key=lambda r:r.get('date',''),reverse=True)
 folder=ROOT/'library'
 if folder.is_dir():
  temporary=folder/('index.json.'+uuid.uuid4().hex+'.new');temporary.write_text(json.dumps(records,ensure_ascii=False),encoding='utf-8');temporary.replace(folder/'index.json')
 return records

def read_saved_xml(path):
 match=re.fullmatch(r'/api/files/([a-f0-9]{32})/(source|hog|ma2)',path)
 if not match:return None
 folder=STORE/match.group(1)
 if not (folder/'info.json').is_file():return None
 prefix={'source':'source_','hog':'HOG4_','ma2':'MA2_'}[match.group(2)]
 files=sorted(folder.glob(prefix+'*.xml'))
 if len(files)!=1:return None
 return {'xml':files[0].read_text(encoding='utf-8'),'filename':files[0].name[len(prefix):]}

def application(environ,start_response):
 status='200 OK';content_type='application/json; charset=utf-8'
 try:
  path=environ.get('PATH_INFO','/');method=environ.get('REQUEST_METHOD','GET')
  if method=='GET' and path in ('/','/index.html'):
   body=(ROOT/'public/index.html').read_bytes().replace(b'</head>',b'<style>#savedPanel{display:none!important}</style></head>');content_type='text/html; charset=utf-8'
  elif method=='GET' and path.startswith('/api/files/'):
   saved=read_saved_xml(path)
   body=json.dumps(saved,ensure_ascii=False).encode() if saved else download_pair(path)
   if body is None:status='404 Not Found';body=b'{"error":"Not found"}'
   elif saved is None:content_type='application/zip'
  elif method=='POST' and path in ('/api/import','/api/generate','/api/archive'):
   origin=environ.get('HTTP_ORIGIN');host=environ.get('HTTP_HOST','')
   if origin and urlsplit(origin).netloc!=host:raise ValueError('Запрос с другого сайта запрещён.')
   size=int(environ.get('CONTENT_LENGTH','0'))
   if not 0<size<=(12_600_000 if path=='/api/archive' else 8_400_000 if path=='/api/generate' else 2_100_000):raise ValueError('Размер запроса превышает лимит.')
   if not environ.get('CONTENT_TYPE','').startswith('application/json'):raise ValueError('Ожидается JSON.')
   payload=json.loads(environ['wsgi.input'].read(size))
   if path=='/api/import':root,system=convert(parse(payload['xml']));data={'xml':serialize(root),'system':system}
   elif path=='/api/generate':
    data=generate_formats(payload) if payload.get('exportBoth') else {'xml':generate(payload)}
    data['originReceipt']=origin_receipt(capture_origin(environ),data['xml'],data.get('ma2Xml'))
    existing=find_existing({**payload,'ma2Xml':data.get('ma2Xml')},data['xml'])
    if existing:data['existingId']=existing['id']
   else:
    if not payload.get('sourceXml'):raise ValueError('Отсутствует исходный XML.')
    generated=payload['xml'];validate_hog(parse(generated))
    if payload.get('ma2Xml'):validate_ma2(parse(payload['ma2Xml']))
    payload['_requestOrigin']=archive_origin(payload,environ)
    data={'saved':public_record(save_pair(payload,generated))}
   body=json.dumps(data,ensure_ascii=False).encode()
  else:status='404 Not Found';body=b'{"error":"Not found"}'
 except (ValueError,KeyError,TypeError,OverflowError) as e:status='400 Bad Request';body=json.dumps({'error':str(e)},ensure_ascii=False).encode()
 except Exception:status='500 Internal Server Error';body=b'{"error":"Internal server error"}'
 start_response(status,[('Content-Type',content_type),('Content-Length',str(len(body))),('X-Content-Type-Options','nosniff'),('Cache-Control','no-store'),('Referrer-Policy','same-origin')]);return [body]

# Private owner library: credentials are provisioned through the hosting panel.
_public_application = application

def owner_credentials():
 path = ROOT/'private/owner-auth.json'
 try:
  record = json.loads(path.read_text(encoding='utf-8-sig'))
  if 'password' in record:
   password = record.get('password','')
   if len(password) < 8 or password == 'CHANGE_ME_PRIVATE_PASSWORD':
    return None
   salt = os.urandom(32).hex()
   record = {'username':record['username'],'salt':salt,'hash':hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),600000).hex()}
   temporary = path.with_suffix('.new')
   fd = os.open(str(temporary),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
   with os.fdopen(fd,'w') as stream: json.dump(record,stream)
   os.replace(str(temporary),str(path));os.chmod(str(path),0o600)
  return record
 except (OSError,ValueError,KeyError,TypeError):
  return None



def library_json(start_response,status,data):
 body=json.dumps(data,ensure_ascii=False).encode('utf-8')
 start_response(status,[('Content-Type','application/json; charset=utf-8'),('Content-Length',str(len(body))),('Cache-Control','no-store'),('X-Content-Type-Options','nosniff')])
 return [body]


def owner_record(info):
 address=clean_ip(info.get('origin',{}).get('ip'))
 return {**public_record(info),'requestIp':str(address) if address is not None else None}

def owner_records(folder):
 records=[]
 if folder.is_symlink():return records
 for entry in folder.glob('*'):
  if not re.fullmatch(r'[a-f0-9]{32}',entry.name) or entry.is_symlink():continue
  try:
   info=json.loads((entry/'info.json').read_text(encoding='utf-8'))
   if info.get('id')==entry.name:records.append(owner_record(info))
  except (OSError,ValueError,TypeError,AttributeError):continue
 return sorted(records,key=lambda item:item.get('date',''),reverse=True)

def clear_trash(environ,start_response):
 if environ.get('REQUEST_METHOD')!='POST':return library_json(start_response,'405 Method Not Allowed',{'error':'Ожидается POST.'})
 origin=urlsplit(environ.get('HTTP_ORIGIN',''))
 if origin.scheme!='https' or origin.netloc!=environ.get('HTTP_HOST',''):return library_json(start_response,'403 Forbidden',{'error':'Запрос с другого сайта запрещён.'})
 try:
  size=int(environ.get('CONTENT_LENGTH','0'))
  if not 0<size<=65536 or not environ.get('CONTENT_TYPE','').startswith('application/json'):raise ValueError()
  payload=json.loads(environ['wsgi.input'].read(size));ids=payload.get('ids')
  if not isinstance(ids,list) or len(ids)>1000 or any(not isinstance(i,str) or not re.fullmatch(r'[a-f0-9]{32}',i) for i in ids) or len(ids)!=len(set(ids)):raise ValueError()
 except (ValueError,TypeError,AttributeError):return library_json(start_response,'400 Bad Request',{'error':'Некорректный список записей корзины. Обновите страницу.'})
 trash=STORE/'.trash'
 if trash.is_symlink():return library_json(start_response,'409 Conflict',{'error':'Небезопасный путь корзины.'})
 removed=0;failed=0
 for token in ids:
  folder=trash/token
  if not folder.exists() and not folder.is_symlink():continue
  try:
   if folder.is_symlink() or not folder.is_dir() or any(item.is_symlink() for item in folder.rglob('*')):raise ValueError()
   info=json.loads((folder/'info.json').read_text(encoding='utf-8'))
   if info.get('id')!=token:raise ValueError()
   shutil.rmtree(folder);removed+=1
  except (OSError,ValueError,TypeError):failed+=1
 return library_json(start_response,'200 OK',{'ok':failed==0,'removed':removed,'failed':failed})

def library_manage(environ,start_response,path):
 mutating=environ.get('REQUEST_METHOD')=='POST' and (path=='/api/files/trash/clear' or re.fullmatch(r'/api/files/[a-f0-9]{32}/(delete|restore)',path))
 if not mutating:return library_manage_unlocked(environ,start_response,path)
 if owner_role(environ,owner_credentials())!='owner':return library_json(start_response,'403 Forbidden',{'error':'Гостю доступен только просмотр.','code':'read_only'})
 import fcntl
 STORE.mkdir(mode=0o700,parents=True,exist_ok=True)
 with (STORE/'.archive.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  return library_manage_unlocked(environ,start_response,path)

def library_manage_unlocked(environ,start_response,path):
 method=environ.get('REQUEST_METHOD','GET')
 role=owner_role(environ,owner_credentials()) or 'guest'
 if path=='/api/files/session':
  if method!='GET':return library_json(start_response,'405 Method Not Allowed',{'error':'Ожидается GET.'})
  return library_json(start_response,'200 OK',{'role':role,'can_edit':role=='owner'})
 restricted=path in ('/api/files/trash','/api/files/trash/clear','/api/files/owner-list') or re.fullmatch(r'/api/files/([a-f0-9]{32})/(delete|restore)',path)
 if restricted and role!='owner':return library_json(start_response,'403 Forbidden',{'error':'Гостю доступен только просмотр.','code':'read_only'})
 if path=='/api/files/trash/clear':return clear_trash(environ,start_response)
 if path in ('/api/files/trash','/api/files/owner-list'):
  if method!='GET':return library_json(start_response,'405 Method Not Allowed',{'error':'Ожидается GET.'})
  return library_json(start_response,'200 OK',owner_records(STORE/'.trash' if path=='/api/files/trash' else STORE))
 match=re.fullmatch(r'/api/files/([a-f0-9]{32})/(delete|restore)',path)
 if not match:return None
 if method!='POST':return library_json(start_response,'405 Method Not Allowed',{'error':'Ожидается POST.'})
 origin=urlsplit(environ.get('HTTP_ORIGIN',''))
 if origin.scheme!='https' or origin.netloc!=environ.get('HTTP_HOST',''):
  return library_json(start_response,'403 Forbidden',{'error':'Запрос с другого сайта запрещён.'})
 try:
  size=int(environ.get('CONTENT_LENGTH','0'))
  if not 0<=size<=1024:raise ValueError()
 except ValueError:return library_json(start_response,'400 Bad Request',{'error':'Некорректный запрос.'})
 token,action=match.groups();trash=STORE/'.trash'
 source=(STORE/token) if action=='delete' else (trash/token)
 target=(trash/token) if action=='delete' else (STORE/token)
 if source.is_symlink() or not source.is_dir() or not (source/'info.json').is_file():
  return library_json(start_response,'404 Not Found',{'error':'Запись не найдена.'})
 if target.exists():return library_json(start_response,'409 Conflict',{'error':'Запись уже существует.'})
 try:
  trash.mkdir(mode=0o700,exist_ok=True)
  source.rename(target)
  try:records=refresh_library()
  except Exception:
   target.rename(source)
   raise
 except OSError:return library_json(start_response,'503 Service Unavailable',{'error':'Не удалось обновить библиотеку. Попробуйте ещё раз.'})
 return library_json(start_response,'200 OK',{'ok':True,'action':action,'id':token,'count':len(records)})

def owner_signature(record,value):
 import hmac
 return hmac.new(bytes.fromhex(record['hash']),('vlighte-session:'+value).encode(),hashlib.sha256).hexdigest()

def owner_role(environ,record):
 import time,hmac
 from http.cookies import SimpleCookie
 if not record:return None
 try:
  cookie=SimpleCookie();cookie.load(environ.get('HTTP_COOKIE',''))
  pieces=cookie['vlighte_owner'].value.split('.')
  if len(pieces)==3:
   expires,nonce,signature=pieces;role='owner';value=expires+'.'+nonce
  elif len(pieces)==4:
   role,expires,nonce,signature=pieces;value=role+'.'+expires+'.'+nonce
  else:return None
  remaining=int(expires)-int(time.time())
  if role not in ('owner','guest') or not 0<remaining<=28800:return None
  return role if hmac.compare_digest(signature,owner_signature(record,value)) else None
 except (KeyError,ValueError,TypeError):return None

def owner_session(environ,record):
 return owner_role(environ,record) is not None

def owner_setup_status():
 try:
  data=json.loads((ROOT/'private/owner-auth.json').read_text(encoding='utf-8-sig'))
  if not isinstance(data,dict):return 'В настройках должен быть JSON-объект с полями username и password.'
  if not isinstance(data.get('username'),str) or not data['username'].strip():return 'В настройках отсутствует логин username.'
  if 'password' in data:
   password=data['password']
   if password=='CHANGE_ME_PRIVATE_PASSWORD':return 'В файле настроек остался шаблон пароля. Замените его своим паролем и сохраните файл.'
   if not isinstance(password,str):return 'Пароль в настройках должен быть строкой в двойных кавычках.'
   if len(password)<8:return 'Сохранённый пароль короче 8 символов. Задайте пароль минимум из 8 символов и сохраните файл.'
   return 'Не удалось сохранить защищённый пароль. Проверьте права записи файла настроек.'
  return 'В файле настроек отсутствует пароль или повреждены поля защищённого пароля.'
 except OSError:return 'Сервер не может прочитать файл настройки входа. Проверьте его наличие и права доступа.'
 except (ValueError,TypeError):return 'Файл настройки входа содержит ошибку JSON. Проверьте двойные кавычки, запятую и фигурные скобки.'

def owner_login(environ,start_response):
 import time,hmac
 from urllib.parse import parse_qs
 record=owner_credentials();message=''
 if environ.get('REQUEST_METHOD')=='POST':
  origin=environ.get('HTTP_ORIGIN','')
  try:
   size=int(environ.get('CONTENT_LENGTH','0'))
   if not 0<size<=4096 or urlsplit(origin).netloc!=environ.get('HTTP_HOST',''):raise ValueError()
   fields=parse_qs(environ['wsgi.input'].read(size).decode('utf-8'))
   username=fields.get('username',[''])[0];password=fields.get('password',[''])[0]
   digest=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(record['salt']),600000).hex() if record else ''
   valid=record and hmac.compare_digest(username.encode(),record['username'].encode()) and hmac.compare_digest(digest,record['hash'])
   role='owner' if valid else None
   if role:
    value=role+'.'+str(int(time.time())+28800)+'.'+os.urandom(24).hex();token=value+'.'+owner_signature(record,value)
    start_response('303 See Other',[('Location','/library/?view=edit'),('Set-Cookie','vlighte_owner='+token+'; Path=/; Max-Age=28800; Secure; HttpOnly; SameSite=Strict'),('Cache-Control','no-store')]);return [b'']
   message='Неверный логин или пароль.'
  except (ValueError,KeyError,TypeError,UnicodeError):message='Не удалось выполнить вход. Повторите попытку.'
 if not record:message=owner_setup_status()
 page='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Вход в файловый склад — Vlighte</title><style>body{font:16px system-ui;background:#f5f7fa;color:#233647;margin:0;display:grid;min-height:100vh;place-items:center}main{box-sizing:border-box;background:white;border-radius:14px;box-shadow:0 4px 24px #0001;padding:32px;width:min(420px,92vw)}h1{font-size:26px;margin:0 0 12px}p{line-height:1.5;color:#596a78}label{display:block;margin:20px 0 7px}input,button{box-sizing:border-box;width:100%;padding:13px;border:1px solid #b9c6d2;border-radius:7px;font:inherit}button{margin-top:24px;background:#287bb5;color:white;border:0;cursor:pointer}.message{color:#a43c27}a{color:#287bb5}</style><style id="app-typography">
:root{--app-font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;--font-title:24px;--font-heading:18px;--font-body:16px;--font-detail:14px;--font-small:12px}
body{font-family:var(--app-font);font-size:var(--font-body)}
button,input,select,textarea{font-family:var(--app-font)}
button{font-size:var(--font-detail)}
input:not([type]),.channel-list li{font-size:var(--font-detail)}
.module-label{font-size:var(--font-small)}
.config-section p{font-size:var(--font-detail)!important}
h1,h2,.app-brand h1{font-size:var(--font-title)}
h3{font-size:var(--font-heading)}
.manual,.part-info,.note,#notice,#status,input[type=text],input[type=password],input[type=number],select{font-size:var(--font-detail)}
.manual h3,.channel-list-section h3{font-size:var(--font-body)}
.app-footer,.app-footer .warehouse-link,.delete-btn{font-size:var(--font-small)}
button.main-generate{font-size:var(--font-body)}
@media(max-width:600px){h1,.app-brand h1{font-size:20px}}
</style><style>main p{font-size:var(--font-detail)}main button{font-size:var(--font-body)}</style><main><h1>Файловый склад</h1><p>Проверка доступа — вход владельца для редактирования</p><p><a href="/library/?view=guest">Продолжить без входа</a></p><p class="message">MESSAGE</p><form method="post" action="/owner/login"><label for="username">Логин</label><input id="username" name="username" autocomplete="username" required maxlength="100"><label for="password">Пароль</label><input id="password" name="password" type="password" autocomplete="current-password" required maxlength="500"><button type="submit">Войти</button></form><p><a href="/">Вернуться к редактору</a></p></main></html>'''.replace('MESSAGE',message)
 body=page.encode('utf-8')
 start_response('200 OK',[('Content-Type','text/html; charset=utf-8'),('Content-Length',str(len(body))),('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),('Referrer-Policy','same-origin'),('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")]);return [body]

def application(environ,start_response):
 import hmac, mimetypes
 from urllib.parse import unquote
 path = environ.get('PATH_INFO','/')
 if environ.get('REQUEST_METHOD','GET')=='GET':
  geo=geo_public(environ,start_response,path)
  if geo is not None:return geo
 if path == '/owner/login':return owner_login(environ,start_response)
 protected = path.rstrip('/') == '/library' or path.startswith('/library/') or path.rstrip('/') == '/api/files' or path.startswith('/api/files/')
 if not protected:
  return _public_application(environ,start_response)
 managed=library_manage(environ,start_response,path)
 if managed is not None:return managed
 if path.startswith('/api/files/'):
  attachment=download_xml(environ,start_response,path)
  if attachment is not None:return attachment
  saved = read_saved_xml(path)
  body = json.dumps(saved,ensure_ascii=False).encode() if saved else download_pair(path)
  content_type = 'application/json; charset=utf-8' if saved else 'application/zip'
 elif path == '/library':
  start_response('302 Found',[('Location','/library/'),('Cache-Control','no-store')]);return [b'']
 else:
  folder = (ROOT/'library').resolve()
  relative = unquote(path[len('/library/'):]) or 'index.html'
  target = (folder/relative).resolve()
  if folder not in target.parents or not target.is_file(): body = None
  else: body = target.read_bytes()
  content_type = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
 if body is None:
  start_response('404 Not Found',[('Content-Length','0'),('Cache-Control','no-store')]);return [b'']
 start_response('200 OK',[('Content-Type',content_type),('Content-Length',str(len(body))),('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),('Referrer-Policy','same-origin')]);return [body]

if __name__=='__main__':
 from wsgiref.simple_server import make_server
 import argparse
 parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8765);args=parser.parse_args();print(f'http://127.0.0.1:{args.port}',flush=True);make_server('127.0.0.1',args.port,application).serve_forever()
