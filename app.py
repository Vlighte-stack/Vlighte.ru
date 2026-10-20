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
   if fn is not None and 'physfrom' in fn.attrib and 'physto' in fn.attrib:
    lo=float(fn.get('physfrom'));hi=float(fn.get('physto'))
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

def generate(payload):
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

STORE=ROOT/'saved-files'
def save_pair(payload,generated):
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
  (folder/('source_'+filename)).write_text(source,encoding='utf-8')
  (folder/('HOG4_'+name+'.xml')).write_text(generated,encoding='utf-8')
  info={'id':token,'name':name,'folder':stamp+'_'+name,'source':filename,'date':datetime.datetime.now(datetime.timezone.utc).isoformat(),'url':'/api/files/'+token+'.zip'}
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
    if re.fullmatch(r'[a-f0-9]{32}',record.get('id','')) and len(list(path.parent.glob('*.xml')))==2:records.append(record)
   except (OSError,ValueError,TypeError):continue
 records.sort(key=lambda r:r.get('date',''),reverse=True)
 folder=ROOT/'library'
 if folder.is_dir():
  temporary=folder/('index.json.'+uuid.uuid4().hex+'.new');temporary.write_text(json.dumps(records,ensure_ascii=False),encoding='utf-8');temporary.replace(folder/'index.json')
 return records

def read_saved_xml(path):
 match=re.fullmatch(r'/api/files/([a-f0-9]{32})/(source|hog)',path)
 if not match:return None
 folder=STORE/match.group(1)
 if not (folder/'info.json').is_file():return None
 prefix='source_' if match.group(2)=='source' else 'HOG4_'
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
   if not 0<size<=(4_200_000 if path in ('/api/generate','/api/archive') else 2_100_000):raise ValueError('Размер запроса превышает лимит.')
   if not environ.get('CONTENT_TYPE','').startswith('application/json'):raise ValueError('Ожидается JSON.')
   payload=json.loads(environ['wsgi.input'].read(size))
   if path=='/api/import':root,system=convert(parse(payload['xml']));data={'xml':serialize(root),'system':system}
   elif path=='/api/generate':data={'xml':generate(payload)}
   else:
    if not payload.get('sourceXml'):raise ValueError('Отсутствует исходный XML.')
    generated=payload['xml'];validate_hog(parse(generated))
    data={'saved':save_pair(payload,generated)}
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

def library_manage(environ,start_response,path):
 method=environ.get('REQUEST_METHOD','GET')
 role=owner_role(environ,owner_credentials()) or 'guest'
 if path=='/api/files/session':
  if method!='GET':return library_json(start_response,'405 Method Not Allowed',{'error':'Ожидается GET.'})
  return library_json(start_response,'200 OK',{'role':role,'can_edit':role=='owner'})
 restricted=path=='/api/files/trash' or re.fullmatch(r'/api/files/([a-f0-9]{32})/(delete|restore)',path)
 if restricted and role!='owner':return library_json(start_response,'403 Forbidden',{'error':'Гостю доступен только просмотр.','code':'read_only'})
 if path=='/api/files/trash':
  if method!='GET':return library_json(start_response,'405 Method Not Allowed',{'error':'Ожидается GET.'})
  records=[]
  for folder in (STORE/'.trash').glob('*'):
   if not re.fullmatch(r'[a-f0-9]{32}',folder.name) or folder.is_symlink():continue
   try:
    info=json.loads((folder/'info.json').read_text(encoding='utf-8'))
    if info.get('id')==folder.name:records.append(info)
   except (OSError,ValueError,TypeError):continue
  records.sort(key=lambda item:item.get('date',''),reverse=True)
  return library_json(start_response,'200 OK',records)
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
 page='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Вход в файловый склад — Vlighte</title><style>body{font:16px system-ui;background:#f5f7fa;color:#233647;margin:0;display:grid;min-height:100vh;place-items:center}main{box-sizing:border-box;background:white;border-radius:14px;box-shadow:0 4px 24px #0001;padding:32px;width:min(420px,92vw)}h1{font-size:26px;margin:0 0 12px}p{line-height:1.5;color:#596a78}label{display:block;margin:20px 0 7px}input,button{box-sizing:border-box;width:100%;padding:13px;border:1px solid #b9c6d2;border-radius:7px;font:inherit}button{margin-top:24px;background:#287bb5;color:white;border:0;cursor:pointer}.message{color:#a43c27}a{color:#287bb5}</style><main><h1>Файловый склад</h1><p>Проверка доступа — вход владельца для редактирования</p><p><a href="/library/?view=guest">Продолжить без входа</a></p><p class="message">MESSAGE</p><form method="post" action="/owner/login"><label for="username">Логин</label><input id="username" name="username" autocomplete="username" required maxlength="100"><label for="password">Пароль</label><input id="password" name="password" type="password" autocomplete="current-password" required maxlength="500"><button type="submit">Войти</button></form><p><a href="/">Вернуться к редактору</a></p></main></html>'''.replace('MESSAGE',message)
 body=page.encode('utf-8')
 start_response('200 OK',[('Content-Type','text/html; charset=utf-8'),('Content-Length',str(len(body))),('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),('Referrer-Policy','same-origin'),('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")]);return [body]

def application(environ,start_response):
 import hmac, mimetypes
 from urllib.parse import unquote
 path = environ.get('PATH_INFO','/')
 if path == '/owner/login':return owner_login(environ,start_response)
 protected = path.rstrip('/') == '/library' or path.startswith('/library/') or path.rstrip('/') == '/api/files' or path.startswith('/api/files/')
 if not protected:
  return _public_application(environ,start_response)
 managed=library_manage(environ,start_response,path)
 if managed is not None:return managed
 if path.startswith('/api/files/'):
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
