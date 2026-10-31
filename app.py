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
     if not math.isfinite(lo) or not math.isfinite(hi):raise ValueError('Некорректный диапазон Tilt.')
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
 color_names={'RED':'I Red','GREEN':'I Green','BLUE':'I Blue','WHITE':'I White','AMBER':'I Amber','COLORRGB4':'I Amber','COLORRGB5':'I White','COLORRGB7':'I White','COLORRGB14':'Yellow','CYAN':'Cyan','MAGENTA':'Magenta','YELLOW':'Yellow'}
 motion={'PAN':'Pan','TILT':'Tilt'}
 controls={'RESET','CONTROL','DIMMERCURVE','DIMMER_CURVE','FIXTURECONTROL','LAMPON','LAMPOFF','LAMPRESET'}
 rate_functions={}
 for ch,c in zip(children(target,'Channel'),source):
  functions=children(c,'ChannelFunction');fn=functions[0] if functions else None
  attr=c.get('attribute','').upper();effective=fn.get('attribute',attr).upper() if fn is not None else attr
  label=fn.get('attribute_user_name',attr) if fn is not None else attr
  sub=fn.get('subattribute','').upper() if fn is not None else ''
  coarse=int(c.get('coarse'));maximum=65535 if 'fine' in c.attrib else 255
  if attr in ('DUMMY','RESERVED') or (effective in ('DUMMY','RESERVED') and not re.fullmatch(r'COLORRGB[0-9]+',attr)):
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
     if not all(math.isfinite(v) for v in (low,high)) or not 0<=dlow<=dhigh<=16777215:raise ValueError(f'Некорректный диапазон {attr}.')
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
  if not all(math.isfinite(float(v)) for v in (lo,hi)):raise ValueError(f'Некорректный диапазон {attr}.')
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
 # HOG rejects a Type containing only unused channels. Keep padding bytes
 # inside an adjacent active segment, preserving absolute DMX and Fine links.
 def active(segment):return any(c.get('unused')!='true' and descendants(c,'Range') for c in segment[2])
 def join(left,right,owner):
  begin=left[1];nodes=[]
  for segment in (left,right):
   shift=segment[1]-begin
   for channel in segment[2]:
    node=copy.deepcopy(channel);node.set('number',str(int(node.get('number'))+shift))
    for attached in descendants(node,'AttachedChannel'):attached.text=str(int(attached.text)+shift)
    nodes.append(node)
  return (owner,begin,nodes,left[3]+right[3])
 safe=[];pending=None
 for segment in groups:
  if active(segment):
   if pending is not None:segment=join(pending,segment,segment[0]);pending=None
   safe.append(segment)
  elif safe:safe[-1]=join(safe[-1],segment,safe[-1][0])
  else:pending=segment if pending is None else join(pending,segment,None)
 if pending is not None:raise ValueError('HOG4 не поддерживает фикстуру только с неиспользуемыми каналами.')
 groups=safe
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
 result={'xml':hog,'ma2Xml':ma,'warnings':warnings}
 if payload.get('exportGdtf'):
  try:
   result['gdtfBase64'],notes=gdtf_from_ma(ma,payload['name'].strip());warnings.extend(notes)
  except ValueError as error:result['gdtfError']=str(error);warnings.append('GDTF не сформирован: '+str(error))
 return result


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

def origin_receipt(origin,hog,ma,gdtf=None,resources=None):
 record=owner_credentials()
 if not record:return None
 data={**origin,'expires':int(time.time())+86400,'hog':hashlib.sha256(hog.encode()).hexdigest(),'ma':hashlib.sha256((ma or '').encode()).hexdigest()}
 if gdtf:data['gdtf']=hashlib.sha256(gdtf.encode()).hexdigest()
 if resources:data['resources']=hashlib.sha256(resources.encode()).hexdigest()
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
  if (data.get('gdtf') or payload.get('gdtfBase64')) and data.get('gdtf')!=hashlib.sha256((payload.get('gdtfBase64') or '').encode()).hexdigest():raise ValueError()
  if (data.get('resources') or payload.get('resourceBase64')) and data.get('resources')!=hashlib.sha256((payload.get('resourceBase64') or '').encode()).hexdigest():raise ValueError()
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
 origin='GDTF' if GDTF_MARKER in source else 'MA' if parse(source).tag=='{'+M+'}MA' else 'HOG'
 title=re.sub(r'[^a-zA-Z0-9а-яА-ЯёЁ_-]','_',str(name))[:100] or 'Fixture'
 return origin+'-'+('GDTF' if kind=='gdtf' else 'MA' if kind=='ma2' else 'HOG')+'_'+title+'_vlighte_ru'+('.gdtf' if kind=='gdtf' else '.xml')

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
   if not resource_identity_matches44(info,payload):continue
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
  if existing:
   if payload.get('gdtfBase64'):existing=complete_gdtf_record(existing,payload)
   return {**existing,'duplicate':True}
  return save_pair_unlocked(payload,generated)

def complete_gdtf_record(info,payload):
 folder=STORE/info['id']
 if 'gdtf' in info.get('formats',[]) and len(list(folder.glob('GDTF_*.gdtf')))==1:return info
 raw=validate_gdtf_export(payload['gdtfBase64']);filename='GDTF_'+export_filename(payload['sourceXml'],'gdtf',info['name'])
 (folder/filename).write_bytes(raw)
 bundle=folder/('bundle.'+uuid.uuid4().hex+'.new')
 try:
  with zipfile.ZipFile(bundle,'w',zipfile.ZIP_DEFLATED) as archive:
   for file in sorted(folder.iterdir()):
    if file.is_file() and file.name.startswith(('HOG4_','MA2_','GDTF_')):archive.write(file,info['folder']+'/'+file.name.split('_',1)[1])
  bundle.replace(folder/'bundle.zip')
  info={**info,'formats':list(dict.fromkeys(info.get('formats',['hog','ma2'])+['gdtf']))}
  temporary=folder/('info.'+uuid.uuid4().hex+'.new');temporary.write_text(json.dumps(info,ensure_ascii=False),encoding='utf-8');temporary.replace(folder/'info.json')
 finally:
  if bundle.exists():bundle.unlink()
 refresh_library();return info

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
  if payload.get('gdtfBase64'):(folder/('GDTF_'+export_filename(source,'gdtf',payload.get('name',name)))).write_bytes(validate_gdtf_export(payload['gdtfBase64']))
  info={'id':token,'name':name,'folder':stamp+'_'+name,'source':filename,'date':datetime.datetime.now(datetime.timezone.utc).isoformat(),'url':'/api/files/'+token+'.zip'}
  info['origin']=payload.get('_requestOrigin',{})
  info['countryCode']=info['origin'].get('countryCode')
  if payload.get('ma2Xml'):
   info['formats']=['hog','ma2']+(['gdtf'] if payload.get('gdtfBase64') else [])
   with zipfile.ZipFile(folder/'bundle.zip','w',zipfile.ZIP_DEFLATED) as archive:
    for file in sorted(folder.iterdir()):
     if file.is_file() and file.name.startswith(('HOG4_','MA2_','GDTF_')):archive.write(file,info['folder']+'/'+file.name.split('_',1)[1])
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

# GDTF import uses only description.xml; models and thumbnails are not extracted.
GDTF_MARKER='VLIGHTE_GDTF_SOURCE:'

def read_gdtf(encoded):
 try:
  if not isinstance(encoded,str) or len(encoded)>28_000_000:raise ValueError('GDTF превышает 20 МБ.')
  raw=base64.b64decode(encoded,validate=True)
  if len(raw)>20_000_000:raise ValueError('GDTF превышает 20 МБ.')
  with zipfile.ZipFile(io.BytesIO(raw)) as archive:
   matches=[n for n in archive.infolist() if n.filename=='description.xml']
   if len(matches)!=1 or len(archive.infolist())>2048:raise ValueError('В GDTF требуется один description.xml.')
   info=matches[0]
   if info.file_size>2_000_000 or info.flag_bits&1:raise ValueError('description.xml слишком большой или зашифрован.')
   with archive.open(info) as stream:data=stream.read(2_000_001)
   if len(data)>2_000_000:raise ValueError('description.xml превышает 2 МБ.')
  root=parse(data.decode('utf-8-sig'))
  if root.tag!='GDTF' or root.get('DataVersion') not in ('1.0','1.1','1.2'):raise ValueError('Поддерживаются GDTF 1.0–1.2.')
  fixture=root.find('FixtureType');modes=fixture.findall('./DMXModes/DMXMode') if fixture is not None else []
  if not modes or len(modes)>256:raise ValueError('В GDTF нет DMX-режимов или их слишком много.')
  names=[m.get('Name','') for m in modes]
  if any(not n for n in names) or len(set(names))!=len(names):raise ValueError('Названия DMX-режимов должны быть уникальны.')
  return root,fixture,modes
 except (zipfile.BadZipFile,RuntimeError,UnicodeError,base64.binascii.Error,NotImplementedError):raise ValueError('Некорректный архив GDTF.')

def gdtf_dmx(value,bits):
 match=re.fullmatch(r'(\d+)/(\d+)(s?)',value or '0/1')
 if not match:raise ValueError('Некорректное значение DMX в GDTF.')
 number,count=int(match[1]),int(match[2]);target=bits//8
 if not 1<=count<=4 or not 0<=number<256**count:raise ValueError('Некорректная разрядность DMX в GDTF.')
 if count>=target:return number//256**(count-target)
 if match[3]:return number*256**(target-count)
 return round(number*(256**target-1)/(256**count-1))

def merge_identical_modules(fixture):
 # Only display names and local module IDs are excluded. Channel order,
 # offsets, attributes, defaults, ranges and virtual channels must all match.
 modules=first(fixture,'Modules');instances=first(fixture,'Instances')
 if modules is None or instances is None:return
 def signature(node,root=False):
  ignored={'index','name'} if root else set()
  return (node.tag,tuple(sorted((k,v) for k,v in node.attrib.items() if k not in ignored)),(node.text or '').strip(),tuple(signature(c) for c in node))
 seen={};mapping={}
 for module in list(modules):
  key=signature(module,True);old=module.get('index')
  if key in seen:
   mapping[old]=seen[key];modules.remove(module)
  else:seen[key]=old;mapping[old]=old
 for instance in instances:
  old=instance.get('module_index')
  if old not in mapping:raise ValueError('Неизвестный модуль фикстуры.')
  instance.set('module_index',mapping[old])

def gdtf_to_ma(root,fixture,mode):
 import math
 geometries=fixture.find('Geometries')
 if geometries is None:raise ValueError('В GDTF отсутствует геометрия.')
 named={n.get('Name'):n for n in geometries.iter() if n.get('Name')}
 start=named.get(mode.get('Geometry'))
 if start is None:raise ValueError('Не найдена геометрия DMX-режима.')
 channels=mode.findall('./DMXChannels/DMXChannel')
 if not channels or len(channels)>1024:raise ValueError('Некорректное число каналов GDTF.')
 if mode.findall('./DMXRelations/Relation'):raise ValueError('DMX Relations в этом GDTF требуют отдельного сопоставления.')
 groups=[];visited=0
 def walk(node,shift=0,path=(),stack=()):
  nonlocal visited
  visited+=1
  if visited>2048 or len(stack)>32:raise ValueError('Слишком сложная геометрия GDTF.')
  name=node.get('Name','');key=path+(name,)
  if node.tag=='GeometryReference':
   target=named.get(node.get('Geometry'))
   if target is None or node.get('Geometry') in stack:raise ValueError('Циклическая или неизвестная геометрия GDTF.')
   breaks=node.findall('Break')
   if len(breaks)>1 or any(b.get('DMXBreak','1')!='1' for b in breaks):raise ValueError('Несколько DMX Break пока не поддерживаются.')
   delta=int(breaks[0].get('DMXOffset','1'))-1 if breaks else 0
   walk(target,shift+delta,key,stack+(node.get('Geometry'),));return
  own=[c for c in channels if c.get('Geometry')==name]
  if own:groups.append((key,shift,own))
  for child in node:
   if child.get('Name'):walk(child,shift,key,stack)
 walk(start)
 reached={id(c) for _,_,own in groups for c in own}
 if len(reached)!=len(channels):raise ValueError('Некоторые каналы GDTF не связаны с геометрией выбранного режима.')
 ma=E.Element(mq('MA'),major_vers='3',minor_vers='9',stream_vers='60');f=E.SubElement(ma,mq('FixtureType'),name=fixture.get('Name','GDTF'),manufacturer=fixture.get('Manufacturer',''),mode=mode.get('Name'))
 mods=E.SubElement(f,mq('Modules'));instances=E.SubElement(f,mq('Instances'));occupied=set();virtual=0;functions=0;sets=0;unmapped=set();built=[]
 aliases={'Dimmer':'DIM','Pan':'PAN','Tilt':'TILT','ColorAdd_R':'COLORRGB1','ColorAdd_G':'COLORRGB2','ColorAdd_B':'COLORRGB3','ColorAdd_W':'WHITE','ColorAdd_A':'AMBER','ColorAdd_RY':'COLORRGB4','ColorAdd_C':'CYAN','ColorSub_C':'CYAN','ColorSub_M':'MAGENTA','ColorSub_Y':'YELLOW','PositionMSpeed':'PANTILTSPEED','IntensityMSpeed':'DIMMERSPEED','Effects1':'EFFECT','ColorEffects1':'COLORMACRO','ColorMacro1Rate':'MACRORATE','StrobeMode':'SHUTTER1','Shutter1':'SHUTTER1','Shutter1Strobe':'SHUTTER1','NoFeature':'DUMMY','ColorMacro1Rate':'MACRORATE','ColorEffects1':'COLORMACRO','PanRotate':'PAN2','TiltRotate':'TILT2','Gobo1Pos':'GOBO1_POS','Gobo1PosRotate':'GOBO1_POS','Gobo2Pos':'GOBO2_POS','Gobo2PosRotate':'GOBO2_POS'}
 for key,shift,own in groups:
  physical=[]
  for c in own:
   offsets=[int(n.strip())+shift for n in c.get('Offset','').split(',') if n.strip() and n.strip()!='None']
   if c.get('DMXBreak','1') not in ('1','Overwrite'):raise ValueError('Несколько DMX Break пока не поддерживаются.')
   if len(offsets)>2 or len(offsets)==2 and offsets[1]!=offsets[0]+1:raise ValueError('Поддерживаются последовательные 8/16-битные каналы GDTF.')
   for address in offsets:
    if address in occupied or not 1<=address<=512:raise ValueError('Пересечение или выход за 512 DMX в GDTF.')
    occupied.add(address)
   physical.append((c,offsets))
  patch=min((n for _,offsets in physical for n in offsets),default=1)
  if not any(offsets for _,offsets in physical):raise ValueError('Отдельная геометрия только с виртуальными каналами требует сопоставления.')
  built.append((patch,key,physical))
 # Assign explicit NoFeature bytes to their nearest preceding active DMX
 # module before making module definitions. This affects editor and exports.
 def empty_channel(channel):
  logical=channel.findall('LogicalChannel')
  return bool(logical) and all(n.get('Attribute','NoFeature')=='NoFeature' and all(fn.get('Attribute','NoFeature')=='NoFeature' for fn in n.findall('ChannelFunction')) for n in logical)
 owners={address:i for i,(_,_,entries) in enumerate(built) for c,offsets in entries if not empty_channel(c) for address in offsets}
 active_addresses=sorted(owners)
 for i,(_,_,entries) in enumerate(built):
  for entry in list(entries):
   channel,offsets=entry
   if not offsets or not empty_channel(channel):continue
   before=[a for a in active_addresses if a<min(offsets)]
   after=[a for a in active_addresses if a>max(offsets)]
   if not before and not after:raise ValueError('Профиль состоит только из неиспользуемых DMX-каналов.')
   destination=owners[before[-1] if before else after[0]]
   if destination!=i:entries.remove(entry);built[destination][2].append(entry)
 normalized=[]
 for _,key,entries in built:
  if not entries:continue
  offsets=[a for _,values in entries for a in values]
  if not offsets:raise ValueError('Виртуальный модуль без физических каналов требует сопоставления.')
  entries.sort(key=lambda entry:min(entry[1]) if entry[1] else 513)
  normalized.append((min(offsets),key,entries))
 built=normalized
 for index,(patch,key,physical) in enumerate(sorted(built,key=lambda n:n[0])):
  module=E.SubElement(mods,mq('Module'),index=str(index),name=' / '.join(key),**{'class':'LED'})
  E.SubElement(instances,mq('Instance'),index=str(index),module_index=str(index),patch=str(patch))
  for ci,(channel,offsets) in enumerate(physical):
   logical=channel.findall('LogicalChannel')
   if len(logical)!=1:raise ValueError('Несколько LogicalChannel на одном адресе требуют сопоставления.')
   logical=logical[0];attr=logical.get('Attribute','NoFeature');attr=attr[8:] if attr.startswith('Vlighte_') else attr;mapped=aliases.get(attr,attr.upper())
   if attr not in aliases and not re.fullmatch(r'Zoom|Focus|Iris|Frost|CTO|CTB|Gobo\d+|Color\d+',attr):unmapped.add(attr)
   c=E.SubElement(module,mq('ChannelType'),index=str(ci),attribute=mapped)
   if offsets:
    c.set('coarse',str(offsets[0]-patch+1))
    if len(offsets)==2:c.set('fine',str(offsets[1]-patch+1))
   else:virtual+=1;c.set('virtual','true')
   bits=8*len(offsets) if offsets else 32;maximum=2**bits-1
   intervals=gdtf_function_intervals(logical,bits,mode)
   for fi,(fn,lo,hi,condition) in enumerate(intervals):
    if condition[0]:
     master=gdtf_master(mode,condition[0]);c.set('mode',gdtf_native_attribute(master.find('LogicalChannel').get('Attribute')))
    pf,pt=float(fn.get('PhysicalFrom','0')),float(fn.get('PhysicalTo','1'))
    if not all(math.isfinite(n) for n in (pf,pt)):raise ValueError('Некорректный физический диапазон GDTF.')
    scale=100 if mapped in ('DIM','COLORRGB1','COLORRGB2','COLORRGB3','WHITE','AMBER') else 1
    feature,preset=('POSITION','POSITION') if mapped in ('PAN','TILT') else ('DIMMER','DIMMER') if mapped=='DIM' else ('COLORRGB','COLOR') if mapped.startswith('COLORRGB') or mapped in ('WHITE','AMBER') else ('SHUTTER','BEAM') if mapped=='SHUTTER1' else ('CONTROL','CONTROL') if mapped=='DUMMY' else ('BEAM','BEAM')
    c.set('feature',feature);c.set('preset',preset)
    initial=channel.get('InitialFunction','').split('.')[-1]
    if fi==0 or fn.get('Name')==initial:c.set('default',str(gdtf_dmx(fn.get('Default','0/1'),bits)))
    attrs={'index':str(fi),'attribute':mapped,'subattribute':'RESERVED' if mapped=='DUMMY' else mapped,'attribute_user_name':fn.get('Name',attr),'feature':feature,'preset':preset,'min_dmx_24':str(round(lo*16777215/maximum)),'max_dmx_24':str(round(hi*16777215/maximum)),'physfrom':str(pf),'physto':str(pt),'from':str(pf*scale),'to':str(pt*scale)}
    if condition[0]:attrs.update(mode_dmx_start=str(condition[1]),mode_dmx_end=str(condition[2]))
    if intervals and all(item[0].get('Attribute',logical.get('Attribute'))=='NoFeature' for item in intervals):attrs.update(attribute='DUMMY',subattribute='RESERVED',feature='CONTROL',preset='CONTROL')
    out=E.SubElement(c,mq('ChannelFunction'),attrs);functions+=1
    channel_sets=fn.findall('ChannelSet');set_starts=[gdtf_dmx(s.get('DMXFrom','0/1'),bits) for s in channel_sets]
    if any(a>=b for a,b in zip(set_starts,set_starts[1:])) or any(not lo<=s<=hi for s in set_starts):raise ValueError('Некорректные ChannelSet в GDTF.')
    for si,s in enumerate(channel_sets):
     end=set_starts[si+1]-1 if si+1<len(set_starts) else hi
     E.SubElement(out,mq('ChannelSet'),index=str(si),name=s.get('Name',''),from_dmx=str(set_starts[si]),to_dmx=str(end));sets+=1
 if not occupied:raise ValueError('Нет физических каналов GDTF.')
 import_gdtf_wheels44(ma,fixture,mode)
 merge_identical_modules(f)
 validate_ma2(ma)
 ma.insert(0,E.Comment(GDTF_MARKER+base64.b64encode(json.dumps({'mode':mode.get('Name'),'descriptionXml':serialize(root)},ensure_ascii=True,separators=(',',':')).encode()).decode()))
 warnings=['Переносятся DMX-данные и поддерживаемые данные колёс; 3D-модели не включены.']
 if unmapped:warnings.append('Общие управляющие каналы, требующие проверки на пульте: '+', '.join(sorted(unmapped)))
 return ma,{'mode':mode.get('Name'),'footprint':max(occupied),'dmxSlots':len(occupied),'modules':len(list(mods)),'instances':len(built),'virtualChannels':virtual,'functions':functions,'channelSets':sets},warnings

def import_profile(payload):
 if 'gdtf' not in payload:
  root,system=convert(parse(payload['xml']));return {'xml':serialize(root),'system':system}
 root,fixture,modes=read_gdtf(payload['gdtf']);choices=[{'name':m.get('Name')} for m in modes]
 if payload.get('mode') is None:return {'system':'GDTF','modes':choices,'fixture':fixture.get('Name','GDTF')}
 mode=next((m for m in modes if m.get('Name')==payload['mode']),None)
 if mode is None:raise ValueError('Неизвестный DMX-режим GDTF.')
 ma,summary,warnings=gdtf_to_ma(root,fixture,mode);recovered=gdtf_ma_recovery(root)
 if recovered is not None:
  ma=recovered;summary['footprint']=validate_ma2(ma)
 source=ma2_serialize(ma);hog,_=convert(ma)
 hog=attach_ma2_roundtrip(serialize(normalize_compound_location(hog)),source,first(hog,'CompoundType').get('name'))
 if len(source.encode())>2_000_000 or len(hog.encode())>2_000_000:raise ValueError('Профиль GDTF превышает лимит XML после преобразования.')
 return {'xml':hog,'sourceXml':source,'system':'GDTF','modes':choices,'summary':summary,'warnings':warnings}


GDTF_EXPORT_MARKER='VLIGHTE_MA2_GDTF:'

def ma_channel_layout(root):
 fixture=first(root,'FixtureType');modules={n.get('index'):n for n in descendants(fixture,'Module')}
 def node(n):return [local(n),sorted((k,v) for k,v in n.attrib.items() if k!='index'),(n.text or '').strip(),[node(c) for c in n if isinstance(c.tag,str)]]
 return [(inst.get('patch','1'),[node(c) for c in children(modules[inst.get('module_index')],'ChannelType')]) for inst in descendants(fixture,'Instance')]

def gdtf_ma_recovery(root):
 for comment in root:
  if comment.tag is E.Comment and (comment.text or '').startswith(GDTF_EXPORT_MARKER):
   try:
    data=json.loads(base64.b64decode(comment.text[len(GDTF_EXPORT_MARKER):],validate=True))
    if data['hash']!=object_fingerprint(serialize(root)):raise ValueError('GDTF изменён: данные восстановления MA2 больше не соответствуют профилю.')
    ma=parse(data['ma2Xml']);validate_ma2(ma);return ma
   except (KeyError,TypeError,UnicodeError,json.JSONDecodeError):raise ValueError('Повреждены данные восстановления MA2 в GDTF.')
 return None

def gdtf_from_ma(ma_xml,name):
 import math
 ma=parse(ma_xml);validate_ma2(ma);fixture=first(ma,'FixtureType');warnings=[];root=None
 # Restore original channel definitions only while the generated MA layout
 # still matches the retained GDTF. Never reuse a stale source after edits.
 for comment in ma:
  if comment.tag is E.Comment and (comment.text or '').startswith(GDTF_MARKER):
   original=json.loads(base64.b64decode(comment.text[len(GDTF_MARKER):],validate=True));candidate=parse(original['descriptionXml']);of=first(candidate,'FixtureType');mode=next((n for n in descendants(of,'DMXMode') if n.get('Name')==original['mode']),None)
   if mode is not None:
    normalized,_,_=gdtf_to_ma(candidate,of,mode)
    if ma_channel_layout(ma)==ma_channel_layout(normalized):
     root=candidate;container=of.find('DMXModes')
     for other in list(container):
      if other is not mode:container.remove(other)
     break
 if root is None:
  identity='{1,0,0,0}{0,1,0,0}{0,0,1,0}{0,0,0,1}'
  root=E.Element('GDTF',DataVersion='1.2');f=E.SubElement(root,'FixtureType',Name='Fixture',ShortName='Fixture',LongName=name,Manufacturer=fixture.get('manufacturer','User Created'),Description='DMX profile generated by vlighte.ru',FixtureTypeID=str(uuid.uuid4()).upper(),CanHaveChildren='No')
  definitions=E.SubElement(f,'AttributeDefinitions');features=E.SubElement(definitions,'FeatureGroups');attributes=E.SubElement(definitions,'Attributes');defined=set();feature_groups={}
  geometries=E.SubElement(f,'Geometries');base=E.SubElement(geometries,'Geometry',Name='Base',Position=identity);modes=E.SubElement(f,'DMXModes');mode=E.SubElement(modes,'DMXMode',Name=fixture.get('mode') or str(validate_ma2(ma))+'ch',Geometry='Base');dmx=E.SubElement(mode,'DMXChannels')
  modules={n.get('index'):n for n in descendants(fixture,'Module')}
  aliases={'DIM':'Dimmer','PAN':'Pan','TILT':'Tilt','COLORRGB1':'ColorAdd_R','COLORRGB2':'ColorAdd_G','COLORRGB3':'ColorAdd_B','COLORRGB4':'ColorAdd_RY','COLORRGB5':'ColorAdd_W','COLORRGB7':'ColorAdd_W','WHITE':'ColorAdd_W','RED':'ColorAdd_R','GREEN':'ColorAdd_G','BLUE':'ColorAdd_B','AMBER':'ColorAdd_RY','COLORCMY1':'ColorSub_C','COLORCMY2':'ColorSub_M','COLORCMY3':'ColorSub_Y','DUMMY':'NoFeature','RESERVED':'NoFeature','SHUTTER1':'Shutter1','STROBE':'Shutter1Strobe','ZOOM':'Zoom','FOCUS':'Focus','IRIS':'Iris','FROST':'Frost','CTO':'CTO','CTB':'CTB','PANTILTSPEED':'PositionMSpeed','DIMMERSPEED':'IntensityMSpeed','MACRORATE':'ColorMacro1Rate','COLORMACRO':'ColorEffects1','EFFECT':'Effects1'}
  def attribute(value):
   if value in aliases:result=aliases[value]
   elif re.fullmatch(r'DIM\d+',value):result='Dimmer'+value[3:]
   elif re.fullmatch(r'GOBO\d+',value):result='Gobo'+value[4:]
   elif re.fullmatch(r'COLOR\d+',value):result='Color'+value[5:]
   else:
    result='Vlighte_'+re.sub(r'[^a-zA-Z0-9_]','_',value or 'Control');warnings.append(value+': пользовательский атрибут GDTF, требуется проверка назначения.')
   if result not in defined:
    defined.add(result)
    group='Dimmer' if result.startswith('Dimmer') else 'Position' if result in ('Pan','Tilt','PositionMSpeed') else 'Color' if result.startswith(('Color','CTO','CTB')) else 'Beam'
    if group not in feature_groups:
     fg=E.SubElement(features,'FeatureGroup',Name=group,Pretty=group);E.SubElement(fg,'Feature',Name=group);feature_groups[group]=True
    E.SubElement(attributes,'Attribute',Name=result,Pretty=value,Feature=group+'.'+group,PhysicalUnit='Angle' if result in ('Pan','Tilt') else 'None')
   return result
  for ii,inst in enumerate(descendants(fixture,'Instance')):
   module=modules[inst.get('module_index')];geometry='Module_'+str(ii);E.SubElement(base,'Geometry',Name=geometry,Position=identity);used={}
   for ci,c in enumerate(children(module,'ChannelType')):
    attr=attribute(c.get('attribute','DUMMY'));used[attr]=used.get(attr,0)+1;cg=geometry
    if used[attr]>1:
     cg=geometry+'_Channel_'+str(ci);E.SubElement(next(n for n in base if n.get('Name')==geometry),'Geometry',Name=cg,Position=identity)
    offsets=[int(inst.get('patch','1'))+int(c.get(key))-1 for key in ('coarse','fine') if key in c.attrib];bits=8*len(offsets) if offsets else 32;maximum=2**bits-1;resolution=bits//8
    dc=E.SubElement(dmx,'DMXChannel',DMXBreak='1',Offset=','.join(map(str,offsets)) if offsets else 'None',Geometry=cg,Highlight='None')
    lc=E.SubElement(dc,'LogicalChannel',Attribute=attr,Snap='No',Master='Grand' if attr.startswith('Dimmer') else 'None');functions=children(c,'ChannelFunction')
    if not functions:raise ValueError('У канала MA2 отсутствуют функции для GDTF.')
    starts=[]
    for fn in functions:starts.append(round(int(fn.get('min_dmx_24','0'))*maximum/16777215))
    if starts[0]!=0 or any(a>=b for a,b in zip(starts,starts[1:])):raise ValueError('Диапазоны MA2 не представимы в GDTF без потери данных.')
    for fi,fn in enumerate(functions):
     low=starts[fi];high=round(int(fn.get('max_dmx_24','16777215'))*maximum/16777215)
     if high!=(starts[fi+1]-1 if fi+1<len(starts) else maximum):raise ValueError('Разрывы диапазонов MA2 требуют сопоставления GDTF.')
     physical=[float(fn.get(k,'0' if k=='physfrom' else '1')) for k in ('physfrom','physto')]
     if not all(math.isfinite(v) for v in physical):raise ValueError('Некорректные физические диапазоны MA2.')
     fname='Function_'+str(fi);fa=attribute(fn.get('attribute',c.get('attribute','DUMMY')))
     default=float(c.get('default','0'))
     if not math.isfinite(default):raise ValueError('Некорректный Default MA2.')
     out=E.SubElement(lc,'ChannelFunction',Name=fname,Attribute=fa,OriginalAttribute=fn.get('attribute_user_name',fn.get('attribute','')),DMXFrom=f'{low}/{resolution}',Default=f'{max(0,min(maximum,round(default)))}/{resolution}',PhysicalFrom=str(physical[0]),PhysicalTo=str(physical[1]))
     if fi==0:dc.set('InitialFunction',cg+'_'+attr+'.'+attr+'.'+fname)
     channel_sets=children(fn,'ChannelSet');ranges=[]
     for si,s in enumerate(channel_sets):
      start=int(s.get('from_dmx',str(round(int(s.get('min_dmx_24',str(round(low*16777215/maximum))))*maximum/16777215))))
      end=int(s.get('to_dmx',str(round(int(s.get('max_dmx_24',str(round(high*16777215/maximum))))*maximum/16777215))))
      if not low<=start<=end<=high:raise ValueError('ChannelSet MA2 выходит за диапазон функции GDTF.')
      ranges.append((start,end,s))
     ranges.sort(key=lambda n:n[0]);cursor=low
     for start,end,s in ranges:
      if start<cursor:raise ValueError('Пересечение ChannelSet MA2.')
      if start>cursor:E.SubElement(out,'ChannelSet',Name='',DMXFrom=f'{cursor}/{resolution}')
      E.SubElement(out,'ChannelSet',Name=s.get('name',''),DMXFrom=f'{start}/{resolution}');cursor=end+1
     if ranges and cursor<=high:E.SubElement(out,'ChannelSet',Name='',DMXFrom=f'{cursor}/{resolution}')
 f=first(root,'FixtureType');safe_name=re.sub(r'[^\w -]','_',name).strip()[:200] or 'Fixture';f.set('Name',safe_name);f.set('ShortName',safe_name[:32]);f.set('LongName',name)
 # No external resources are available for HOG/MA inputs. Do not emit broken
 # thumbnail/model references when restoring an original GDTF description.
 for key in ('Thumbnail','ThumbnailOffsetX','ThumbnailOffsetY'):f.attrib.pop(key,None)
 for node in list(f):
  if node.tag=='Models':f.remove(node)
 for node in f.iter():
  node.attrib.pop('Model',None);node.attrib.pop('MediaFileName',None)
 for node in list(root):
  if node.tag is E.Comment:root.remove(node)
 bare=copy.deepcopy(ma)
 for parent in bare.iter():
  for child in list(parent):
   if child.tag is E.Comment and (child.text or '').startswith(('VLIGHTE_',)):parent.remove(child)
 recovery={'hash':object_fingerprint(serialize(root)),'ma2Xml':ma2_serialize(bare)}
 root.insert(0,E.Comment(GDTF_EXPORT_MARKER+base64.b64encode(json.dumps(recovery,separators=(',',':')).encode()).decode()))
 description=serialize(root).encode('utf-8')
 if len(description)>2_000_000:raise ValueError('GDTF description.xml превышает 2 МБ с данными восстановления.')
 buffer=io.BytesIO()
 with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
  info=zipfile.ZipInfo('description.xml',date_time=(2026,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;archive.writestr(info,description)
 warnings.append('GDTF содержит DMX-профиль без 3D-моделей и изображений.')
 return base64.b64encode(buffer.getvalue()).decode(),list(dict.fromkeys(warnings))

def validate_gdtf_export(encoded):
 root,_,_=read_gdtf(encoded)
 if gdtf_ma_recovery(root) is None:raise ValueError('GDTF не содержит данных проверки генерации.')
 return base64.b64decode(encoded,validate=True)

def download_gdtf(environ,start_response,path):
 match=re.fullmatch(r'/api/files/([a-f0-9]{32})/download/gdtf',path)
 if not match:return None
 folder=STORE/match[1];files=list(folder.glob('GDTF_*.gdtf'))
 if not (folder/'info.json').is_file() or len(files)!=1:return library_json(start_response,'404 Not Found',{'error':'GDTF не найден.'})
 from urllib.parse import quote
 body=files[0].read_bytes();filename=files[0].name[len('GDTF_'):]
 start_response('200 OK',[('Content-Type','application/octet-stream'),('Content-Length',str(len(body))),('Content-Disposition',"attachment; filename=fixture_vlighte_ru.gdtf; filename*=UTF-8''"+quote(filename,safe='')),('Cache-Control','no-store'),('X-Content-Type-Options','nosniff')]);return [body]


# Native HOG/MA channel conversion. No source snapshots or private XML extensions.
def real_number(value):
 import math
 result=float(value)
 if not math.isfinite(result):raise ValueError('Некорректное числовое значение диапазона.')
 return result

def native_scale(function):
 return 100 if function in ('Intensity','I Red','I Green','I Blue','I White','I Amber','BG Red','BG Green','BG Blue','BG White','Cyan','Magenta','Yellow') or function.startswith('Sub Intensity') else 1

_map_module_channels38=map_module_channels
def map_module_channels(target,source,module):
 _map_module_channels38(target,source,module)
 for channel,ma in zip(children(target,'Channel'),source):
  ranges=first(channel,'Ranges')
  if ranges is None or channel.get('unused')=='true':
   if 'default' in ma.attrib:
    value=real_number(ma.get('default'));maximum=65535 if 'fine' in ma.attrib else 255
    if not value.is_integer() or not 0<=value<=maximum:raise ValueError('Некорректное постоянное значение DMX.')
    if value!=0:channel.attrib.pop('unused',None);channel.set('constant',str(int(value)))
   continue
  originals=children(ranges,'Range');primary=originals[0];function=primary.get('function');feature=primary.get('feature');scale=native_scale(function);maximum=65535 if 'fine' in ma.attrib else 255
  # Keep an explicitly numbered native control stable across repeated imports.
  attr=ma.get('attribute','').upper()
  dim=re.fullmatch(r'DIM(?:MER)?(\d+)',attr)
  if dim and int(dim[1])>1:function='Sub Intensity '+str(int(dim[1])-1);feature='Intensity'
  shutter=re.fullmatch(r'SHUTTER([1-4])',attr)
  if shutter:function='Strobe'+(' '+shutter[1] if shutter[1]!='1' else '');feature='Rate'
  if re.fullmatch(r'MACRORATE\d*',attr):function='Macro'+(' '+attr[9:] if attr[9:] else '')+' Rate';feature='Rate'
  output=[]
  for fn in children(ma,'ChannelFunction'):
   low=round(int(fn.get('min_dmx_24','0'))*maximum/16777215);high=round(int(fn.get('max_dmx_24','16777215'))*maximum/16777215)
   if not 0<=low<=high<=maximum:raise ValueError('Некорректный диапазон MA2.')
   pf=real_number(fn.get('physfrom',str(real_number(fn.get('from','0'))/scale)))*scale
   pt=real_number(fn.get('physto',str(real_number(fn.get('to','100'))/scale)))*scale
   points={low,high+1}
   sets=[]
   for s in children(fn,'ChannelSet'):
    start=int(s.get('from_dmx',str(low)));end=int(s.get('to_dmx',str(high)))
    if not low<=start<=end<=high:raise ValueError('ChannelSet выходит за диапазон MA2.')
    if any(start<=b and a<=end for a,b in sets):raise ValueError('Пересечение ChannelSet MA2.')
    sets.append((start,end));points.update((start,end+1))
   points=sorted(points)
   for start,stop in zip(points,points[1:]):
    end=stop-1
    interpolate=lambda n:pf+(pt-pf)*(n-low)/(high-low) if high!=low else pf
    output.append(E.Element(q('Range'),dmxstart=str(start),dmxend=str(end),function=function,feature=feature,start=str(interpolate(start)),end=str(interpolate(end))))
  if not output:continue
  ranges.clear();ranges.extend(output)
  # Complementary colour controls must describe each same sub-range.
  inverse={'I Red':'Cyan','I Green':'Magenta','I Blue':'Yellow'}.get(function)
  if inverse and any(r.get('function')==inverse for r in originals):
   for r in output:
    attrs=dict(r.attrib);attrs.update(function=inverse,feature='Variable',dmxstart=r.get('dmxend'),dmxend=r.get('dmxstart'));E.SubElement(ranges,q('Range'),attrs)

def hog_type_section(t,name):
 section=next(iter(children(t,name)),None)
 if section is None:
  section=E.Element(q(name));order=['History','Origin','Icon','Interchange','Notes','Defaults','Virtuals','ColourMatcher','Protocols','Previewpackages'];rank=order.index(name);position=next((i for i,n in enumerate(t) if local(n) in order and order.index(local(n))>rank),len(t));t.insert(position,section)
 return section

_convert38=convert
def convert(root):
 hog,system=_convert38(root)
 if root.tag!=mq('MA') or read_hog_roundtrip(root) is not None:return hog,system
 fixture=first(root,'FixtureType');modules={n.get('index'):n for n in descendants(fixture,'Module')};types={t.get('name'):t for t in descendants(hog,'Type')}
 for mid,module in modules.items():
  t=types.get(f"{fixture.get('name','MA2')} M{mid}")
  if t is None:continue
  physical=sorted([c for c in children(module,'ChannelType') if 'coarse' in c.attrib],key=lambda c:int(c.get('coarse')))
  for c,ch in zip(physical,descendants(t,'Channel')):
   if 'default' not in c.attrib or 'constant' in ch.attrib or ch.get('unused')=='true':continue
   default=real_number(c.get('default'));maximum=65535 if 'fine' in c.attrib else 255
   if not 0<=default<=maximum:raise ValueError('Некорректное значение Default MA2.')
   default=round(default) # HOG range boundaries and DMX output are integer slots.
   candidates=[r for r in descendants(ch,'Range') if int(r.get('dmxstart'))<=default<=int(r.get('dmxend',r.get('dmxstart')))]
   if not candidates:raise ValueError('Default MA2 не входит в диапазоны HOG.')
   r=candidates[0]
   if r.get('feature')=='Slots':
    E.SubElement(hog_type_section(t,'Defaults'),q('Default'),function=r.get('function'),feature=r.get('feature'),value=r.get('start'));continue
   low=int(r.get('dmxstart'));high=int(r.get('dmxend',str(low)));start=real_number(r.get('start','0'));end=real_number(r.get('end',str(start)));value=start+(end-start)*(default-low)/(high-low) if high!=low else start
   E.SubElement(hog_type_section(t,'Defaults'),q('Default'),function=r.get('function'),feature=r.get('feature'),value=str(value))
  virtual=[c for c in children(module,'ChannelType') if 'coarse' not in c.attrib]
  if len(virtual)>1:raise ValueError('Несколько виртуальных органов в одном модуле требуют явных зависимостей; экспорт остановлен без расширения области DIM.')
  for c in virtual:
   if c.get('attribute') not in ('DIM','DIMMER'):raise ValueError('Требуется сопоставление виртуального канала '+c.get('attribute',''))
   affected=set()
   for source,ch in zip(physical,children(first(t,'Channels'),'Channel')):
    if not ma_virtual_emitter48(source):continue
    affected.update(hog_virtual_targets48(ch))
    if source.get('attribute')=='COLORRGB7':
     affected.update((r.get('function'),r.get('feature')) for r in descendants(ch,'Range') if r.get('function')=='Sub Intensity 1' and r.get('feature')=='Intensity')
   if not affected:raise ValueError('Виртуальный DIM не имеет цветовых каналов в своём модуле.')
   fns=children(c,'ChannelFunction')
   if len(fns)!=1:raise ValueError('Составная шкала виртуального DIM не представима одним штатным HOG Virtual.')
   fn=fns[0]
   if int(fn.get('min_dmx_24','0'))!=0 or int(fn.get('max_dmx_24','16777215'))!=16777215 or c.get('mode'):raise ValueError('Условная шкала виртуального DIM требует отдельного сопоставления.')
   low=real_number(fn.get('physfrom',str(real_number(fn.get('from','0'))/100)))*100
   high=real_number(fn.get('physto',str(real_number(fn.get('to','100'))/100)))*100
   if low>=high:raise ValueError('Некорректная шкала виртуального DIM.')
   v=E.SubElement(hog_type_section(t,'Virtuals'),q('Virtual'),function='Intensity',feature='Intensity',start=str(low),end=str(high))
   for function,feature in sorted(affected):E.SubElement(v,q('Affected'),function=function,feature=feature)
   if 'default' in c.attrib:
    value=real_number(c.get('default'))
    if not 0<=value<=255:raise ValueError('Некорректное 8-битное Default виртуального DIM MA2.')
    E.SubElement(hog_type_section(t,'Defaults'),q('Default'),function='Intensity',feature='Intensity',value=str(low+(high-low)*value/255))
 return hog,system

_ma2_attribute38=ma2_attribute
def ma2_attribute(function,feature):
 match=re.fullmatch(r'Macro(?: ([1-8]))?( Rate)?',function)
 if match:
  number=int(match[1] or 1)
  return (('MACRORATE'+(str(number) if number>1 else '')) if match[2] else ('GOBOWHEELS' if number==1 else 'GOBO'+str(number)),'GOBOALL','GOBO',1)
 match=re.fullmatch(r'Beam Fx(?: ([1-9]))?( <>)?',function)
 if match:
  number=int(match[1] or 1);return (('GOBOWHEELS' if number==1 else 'GOBO'+str(number))+('_POS' if match[2] else ''),'GOBOALL','GOBO',1)
 match=re.fullmatch(r'Strobe(?: ([2-4]))?',function)
 if match:return ('SHUTTER'+(match[1] or '1'),'SHUTTER','BEAM',1)
 extra={'CTO':('CTO','COLOR','COLOR',1),'CTB':('CTB','COLOR','COLOR',1),'Frost':('FROST','BEAM','BEAM',1),'Position Time':('PANTILTSPEED','POSITION','POSITION',1),'Colour':('COLOR1','COLOR','COLOR',1),'Gobo':('GOBO1','GOBO','GOBO',1),'I Amber':('AMBER','COLORRGB','COLOR',.01),'BG White':('WHITE','COLORRGB','COLOR',.01)}
 if function in extra:return extra[function]
 return _ma2_attribute38(function,feature)

def ma2_from_hog(payload,hog_xml):
 hog=parse(hog_xml);types={n.get('name'):n for n in descendants(hog,'Type')};compound=next((c for c in descendants(hog,'CompoundType') if c.get('name')==payload['name'].strip()),None)
 if compound is None:raise ValueError('Не найдена составная фикстура.')
 root=E.Element(mq('MA'),{'{http://www.w3.org/2001/XMLSchema-instance}schemaLocation':M+' http://schemas.malighting.de/grandma2/xml/3.1.2/MA.xsd','major_vers':'3','minor_vers':'1','stream_vers':'2'});E.SubElement(root,mq('Info'),datetime=datetime.datetime.now(datetime.timezone.utc).isoformat(),showfile='Vlighte')
 fixture=E.SubElement(root,mq('FixtureType'),index='0',name=payload['name'].strip(),mode='');E.SubElement(fixture,mq('short_name')).text=payload['name'].strip()[:32];modules=E.SubElement(fixture,mq('Modules'),index='0');instances=E.SubElement(fixture,mq('Instances'),index='1');ids={};cursor=1;warnings=[]
 for ii,part in enumerate(children(first(compound,'Parts'),'Part')):
  name=part.get('type');t=types[name];width=int(first(t,'Dmxfootprint').get('size'))
  if name not in ids:
   mid=str(len(ids));ids[name]=mid;module=E.SubElement(modules,mq('Module'),index=mid,name=name,**{'class':'LED','beamtype':'Wash'});occupied=set();attrs=set()
   for ch in descendants(t,'Channel'):
    coarse=int(ch.get('number'));attached=descendants(ch,'AttachedChannel');fine=int(attached[0].text) if attached else None
    if len(attached)>1 or fine is not None and fine!=coarse+1:raise ValueError('Неподдерживаемая точность канала HOG4 для MA2.')
    for offset in [coarse]+([fine] if fine else []):
     if not 1<=offset<=width or offset in occupied:raise ValueError('Некорректная структура DMX HOG4.')
     occupied.add(offset)
    maximum=65535 if fine else 255;rs=descendants(ch,'Range') if ch.get('unused')!='true' else []
    if any(children(r,'Escapes') for r in rs):raise ValueError('Escapes требуют отдельного сопоставления MA2.')
    inverse={'I Red':'Cyan','I Green':'Magenta','I Blue':'Yellow'}
    if rs:rs=[r for r in rs if r.get('function')!=inverse.get(rs[0].get('function'))]
    if not rs:rs=[E.Element('Range',dmxstart='0',dmxend=str(maximum),function='Unused',feature='Continuous',start='0',end='100')]
    rs=sorted(rs,key=lambda r:int(r.get('dmxstart','0')));label=rs[0].get('function','Unused')
    if any(r.get('function')!=label for r in rs):raise ValueError('Несколько функций в одном HOG4-канале требуют сопоставления MA2.')
    attr,feature,preset,scale=('DUMMY','CONTROL','CONTROL',1) if label=='Unused' else ma2_attribute(label,rs[0].get('feature',''))
    if attr in attrs and attr!='DUMMY':raise ValueError('Повтор атрибута MA2 в одном модуле: '+attr)
    attrs.add(attr);c=E.SubElement(module,mq('ChannelType'),index=str(len(children(module,'ChannelType'))),attribute=attr,feature=feature,preset=preset,coarse=str(coarse))
    if fine:c.set('fine',str(fine))
    previous={}
    for ri,r in enumerate(rs):
     low=int(r.get('dmxstart','0'));high=int(r.get('dmxend',str(low)))
     condition=native_qualifier_condition(t,r)
     if not 0<=low<=high<=maximum or low<=previous.get(condition,-1):raise ValueError('Пересечение диапазонов при одинаковом условии HOG4.')
     previous[condition]=high;start,end=hog_physical42(r)
     fn=E.SubElement(c,mq('ChannelFunction'),index=str(ri),**{'from':str(start),'to':str(end),'min_dmx_24':str(round(low*16777215/maximum)),'max_dmx_24':str(round(high*16777215/maximum)),'physfrom':str(start*scale),'physto':str(end*scale),'subattribute':'RESERVED' if attr=='DUMMY' else attr,'attribute':attr,'attribute_user_name':label,'feature':feature,'preset':preset})
     if condition:
      c.set('mode',condition[0]);fn.set('mode_dmx_start',str(condition[1]));fn.set('mode_dmx_end',str(condition[2]))
    default=hog_default42(t,ch,rs)
    if default is not None:c.set('default',str(default))
    if label.startswith(('Macro','Beam Fx')):warnings.append(label+': исходный атрибут неоднозначен; сохранены независимое управление и числовые диапазоны как '+attr+'.')
   for offset in range(1,width+1):
    if offset not in occupied:
     c=E.SubElement(module,mq('ChannelType'),index=str(len(children(module,'ChannelType'))),attribute='DUMMY',feature='CONTROL',preset='CONTROL',coarse=str(offset));E.SubElement(c,mq('ChannelFunction'),index='0',**{'from':'0','to':'100','min_dmx_24':'0','max_dmx_24':'16777215','physfrom':'0','physto':'100','subattribute':'RESERVED','attribute':'DUMMY','feature':'CONTROL','preset':'CONTROL'})
   for vi,v in enumerate(descendants(t,'Virtual')):
    validate_virtual_scope43(t,v)
    if v.get('function')!='Intensity' or v.get('feature')!='Intensity':raise ValueError('Неизвестный виртуальный канал HOG4.')
    if 'DIM' in attrs:raise ValueError('Физический и виртуальный DIM одного модуля требуют сопоставления.')
    attrs.add('DIM');c=E.SubElement(module,mq('ChannelType'),index=str(len(children(module,'ChannelType'))),attribute='DIM',feature='DIMMER',preset='DIMMER');start=real_number(v.get('start','0'));end=real_number(v.get('end','100'))
    E.SubElement(c,mq('ChannelFunction'),index='0',**{'from':str(start),'to':str(end),'min_dmx_24':'0','max_dmx_24':'16777215','physfrom':str(start/100),'physto':str(end/100),'subattribute':'DIM','attribute':'DIM','feature':'DIMMER','preset':'DIMMER'})
  E.SubElement(instances,mq('Instance'),index=str(ii),module_index=ids[name],patch=str(cursor),X=part.get('plotX','0'),Y=part.get('plotY','0'));cursor+=width
 for tag,idx in [('Wheels','2'),('VirtualFunctionBlocks','3'),('FixtureMacroCollect','5')]:E.SubElement(fixture,mq(tag),index=idx)
 E.SubElement(fixture,mq('Body'));E.SubElement(fixture,mq('AutoPresets'),index='4');fixture.set('mode',str(cursor-1)+'ch');validate_ma2(root)
 return ma2_serialize(root),sorted(set(warnings))


def expanded_gdtf_channels(fixture,mode):
 geometries=fixture.find('Geometries')
 if geometries is None:raise ValueError('В GDTF отсутствует геометрия.')
 named={n.get('Name'):n for n in geometries.iter() if n.get('Name')};start=named.get(mode.get('Geometry'));channels=mode.findall('./DMXChannels/DMXChannel');records=[];seen=set();visits=0
 if not channels or len(channels)>1024 or any(len(c.findall('LogicalChannel'))!=1 for c in channels):raise ValueError('Требуется один LogicalChannel на физический канал GDTF.')
 if start is None:raise ValueError('Не найдена геометрия режима GDTF.')
 def walk(node,offsets=None,path=(),stack=()):
  nonlocal visits
  visits+=1
  if visits>2048 or len(stack)>32:raise ValueError('Слишком сложная геометрия GDTF.')
  offsets=offsets or {};key=path+(node.get('Name',''),)
  if node.tag=='GeometryReference':
   target=named.get(node.get('Geometry'))
   if target is None or node.get('Geometry') in stack:raise ValueError('Циклическая или неизвестная геометрия GDTF.')
   local_offsets={}
   for b in node.findall('Break'):
    number=int(b.get('DMXBreak','1'));shift=int(b.get('DMXOffset','1'))-1
    if not 1<=number<=255 or not 0<=shift<=511 or number in local_offsets:raise ValueError('Некорректное определение DMX Break геометрии.')
    local_offsets[number]=shift
   inherited=dict(offsets)
   for number,shift in local_offsets.items():inherited[number]=offsets.get(number,0)+shift
   # Unused entries are valid: the same reference can serve different modes.
   target_names={n.get('Name') for n in target.iter() if n.get('Name')};fixed={int(c.get('DMXBreak','1')) for c in channels if c.get('Geometry') in target_names and c.get('DMXBreak','1')!='Overwrite'};available=set(local_offsets)-fixed
   inherited['overwrite']=next(iter(available)) if len(available)==1 else next(iter(local_offsets)) if len(local_offsets)==1 else offsets.get('overwrite')
   walk(target,inherited,key,stack+(node.get('Geometry'),));return
  for c in channels:
   if c.get('Geometry')!=node.get('Name'):continue
   rawbreak=c.get('DMXBreak','1')
   number=offsets.get('overwrite') if rawbreak=='Overwrite' else int(rawbreak)
   if number is None:raise ValueError('Для Overwrite требуется однозначный DMX Break в GeometryReference.')
   if not 1<=number<=255:raise ValueError('Некорректный номер DMX Break.')
   values=[int(n.strip())+offsets.get(number,0) for n in c.get('Offset','').split(',') if n.strip() and n.strip()!='None']
   if any(not 1<=v<=512 for v in values):raise ValueError('Адрес DMX Break выходит за 512.')
   records.append((key,c,number,values));seen.add(id(c))
   if len(records)>8192:raise ValueError('Слишком много развёрнутых каналов GDTF.')
  for child in node:
   if child.get('Name'):walk(child,offsets,key,stack)
 walk(start)
 if len(seen)!=len(channels):raise ValueError('Не все каналы связаны с геометрией режима GDTF.')
 return records

def gdtf_native_attribute(value):
 return {'PanRotate':'PAN2','TiltRotate':'TILT2','Gobo1Pos':'GOBO1_POS','Gobo1PosRotate':'GOBO1_POS','Gobo2Pos':'GOBO2_POS','Gobo2PosRotate':'GOBO2_POS','Gobo1':'GOBO1','Gobo2':'GOBO2'}.get(value,value[8:] if value.startswith('Vlighte_') else value.upper())

def normalize_gdtf_break(root,fixture,mode,selected=None):
 records=expanded_gdtf_channels(fixture,mode);active=sorted({b for _,_,b,values in records if values})
 if not active:raise ValueError('Нет физических DMX Break.')
 if selected is None and len(active)>1:raise ValueError('Несколько активных DMX Break: выберите отдельный профиль Break.')
 selected=selected or active[0]
 if selected not in active:raise ValueError('Выбранный DMX Break не используется в режиме.')
 needs=selected!=1 or len(active)>1 or any(len(n.findall('Break'))>1 for n in fixture.iter('GeometryReference')) or any(fn.get('ModeMaster') for c in mode.findall('./DMXChannels/DMXChannel') for fn in c.findall('./LogicalChannel/ChannelFunction'))
 if not needs:return root,fixture,mode,[]
 root=copy.deepcopy(root);fixture=root.find('FixtureType');original_name=mode.get('Name');mode=next(n for n in fixture.findall('./DMXModes/DMXMode') if n.get('Name')==original_name)
 geos=fixture.find('Geometries');geos.clear();base=E.SubElement(geos,'Geometry',Name='Break_Profile',Position='{1,0,0,0}{0,1,0,0}{0,0,1,0}{0,0,0,1}');mode.set('Geometry','Break_Profile');target=mode.find('DMXChannels');target.clear();grouped={};aliases={}
 conditional=any(fn.get('ModeMaster') for _,c,b,values in records if b==selected for fn in c.findall('./LogicalChannel/ChannelFunction'))
 # A conditional controller and its follower must live in the same HOG Type.
 for path,c,b,values in records:
  if b!=selected:continue
  key=('Conditional',) if conditional else path
  if key not in grouped:
   name='Group_'+str(len(grouped));grouped[key]=name;E.SubElement(base,'Geometry',Name=name,Position='{1,0,0,0}{0,1,0,0}{0,0,1,0}{0,0,0,1}')
  clone=copy.deepcopy(c);name=grouped[key];oldid=c.get('Geometry')+'_'+c.find('LogicalChannel').get('Attribute');newid=name+'_'+c.find('LogicalChannel').get('Attribute');aliases.setdefault(oldid,[]).append(newid);clone.set('Geometry',name);clone.set('DMXBreak','1');clone.set('Offset',','.join(map(str,values)) if values else 'None')
  initial=clone.get('InitialFunction')
  if initial:clone.set('InitialFunction',newid+'.'+'.'.join(initial.split('.')[1:]))
  target.append(clone)
 for c in target:
  for fn in c.findall('./LogicalChannel/ChannelFunction'):
   master=fn.get('ModeMaster')
   if not master:continue
   head,*tail=master.split('.');matches=aliases.get(head,[])
   if len(set(matches))!=1:raise ValueError('ModeMaster отсутствует в выбранном Break или неоднозначен.')
   fn.set('ModeMaster',matches[0]+('.'+'.'.join(tail) if tail else ''))
 modes=fixture.find('DMXModes')
 for other in list(modes):
  if other is not mode:modes.remove(other)
 warnings=[]
 if len(active)>1 or selected!=1:
  mode.set('Name',original_name+' [DMX Break '+str(selected)+']');warnings.append('Отдельный профиль DMX Break '+str(selected)+': адреса внутри Break сохранены; остальные Break выбираются отдельными профилями.')
 return root,fixture,mode,warnings

def gdtf_master(mode,reference):
 head=reference.split('.')[0]
 matches=[c for c in mode.findall('./DMXChannels/DMXChannel') if c.get('Geometry')+'_'+c.find('LogicalChannel').get('Attribute')==head]
 if len(matches)!=1:raise ValueError('Неоднозначный или неизвестный ModeMaster.')
 return matches[0]

def gdtf_function_intervals(logical,bits,mode):
 fs=logical.findall('ChannelFunction');maximum=2**bits-1;groups={}
 if not fs:raise ValueError('В GDTF отсутствуют функции канала.')
 for fn in fs:
  master=fn.get('ModeMaster','');lowmode=highmode=None
  if master:
   c=gdtf_master(mode,master);offsets=[n for n in c.get('Offset','').split(',') if n and n!='None'];mbits=len(offsets)*8 if offsets else 32;lowmode=gdtf_dmx(fn.get('ModeFrom','0/1'),mbits);highmode=gdtf_dmx(fn.get('ModeTo','0/1'),mbits)
   if not 0<=lowmode<=highmode<=2**mbits-1:raise ValueError('Некорректный диапазон ModeMaster.')
  groups.setdefault((master,lowmode,highmode),[]).append(fn)
 result=[]
 for condition,functions in groups.items():
  functions=sorted(functions,key=lambda fn:gdtf_dmx(fn.get('DMXFrom','0/1'),bits));starts=[gdtf_dmx(fn.get('DMXFrom','0/1'),bits) for fn in functions]
  if len(set(starts))!=len(starts):raise ValueError('Пересечение ChannelFunction при одном и том же условии ModeMaster.')
  if starts[0]!=0:raise ValueError('Начальный незаданный диапазон функции требует явного NoFeature.')
  for index,fn in enumerate(functions):result.append((fn,starts[index],starts[index+1]-1 if index+1<len(starts) else maximum,condition))
 for i,(a,lo,hi,cond) in enumerate(result):
  for b,bl,bh,bc in result[i+1:]:
   if max(lo,bl)>min(hi,bh):continue
   if not cond[0] or cond[0]!=bc[0] or max(cond[1],bc[1])<=min(cond[2],bc[2]):raise ValueError('Неоднозначное пересечение условных диапазонов ChannelFunction.')
 return result

_gdtf_to_ma39=gdtf_to_ma
def gdtf_to_ma(root,fixture,mode,selected_break=None):
 normalized,f,selected,warnings=normalize_gdtf_break(root,fixture,mode,selected_break);ma,summary,notes=_gdtf_to_ma39(normalized,f,selected)
 if any(c.get('mode') for c in descendants(ma,'ChannelType')):warnings.append('ModeMaster переносится в штатные Qualifiers HOG с физической шкалой входного профиля.')
 return ma,summary,notes+warnings

_import_profile39=import_profile
def import_profile(payload):
 if 'gdtf' not in payload:return _import_profile39(payload)
 root,fixture,modes=read_gdtf(payload['gdtf']);choices=[];bindings={}
 for mode in modes:
  active=sorted({b for _,_,b,values in expanded_gdtf_channels(fixture,mode) if values})
  for b in active:
   label=mode.get('Name') if len(active)==1 else mode.get('Name')+' [DMX Break '+str(b)+']'
   if label in bindings:raise ValueError('Неоднозначные названия профилей DMX Break.')
   bindings[label]=(mode,b);choices.append({'name':label})
   if len(choices)>512:raise ValueError('Слишком много профилей DMX Break.')
 if payload.get('mode') is None:return {'system':'GDTF','modes':choices,'fixture':fixture.get('Name','GDTF')}
 if payload['mode'] not in bindings:raise ValueError('Выберите отдельный профиль нужного DMX Break.')
 mode,number=bindings[payload['mode']];ma,summary,warnings=gdtf_to_ma(root,fixture,mode,number);recovered=gdtf_ma_recovery(root)
 if recovered is not None and len(choices)==1:ma=recovered;summary['footprint']=validate_ma2(ma)
 source=ma2_serialize(ma);hog,_=convert(ma);hog=attach_ma2_roundtrip(serialize(normalize_compound_location(hog)),source,first(hog,'CompoundType').get('name'))
 if len(source.encode())>2_000_000 or len(hog.encode())>2_000_000:raise ValueError('Профиль превышает лимит XML после преобразования.')
 return {'xml':hog,'sourceXml':source,'system':'GDTF','modes':choices,'summary':summary,'warnings':warnings}

_ma2_attribute39=ma2_attribute
def ma2_attribute(function,feature):
 match=re.fullmatch(r'(Pan|Tilt) ([1-6])',function)
 if match:return (match[1].upper()+match[2],'POSITION','POSITION',1)
 return _ma2_attribute39(function,feature)

_map_module_channels39=map_module_channels
def map_module_channels(target,source,module):
 prepared=[]
 for c in source:
  clone=copy.deepcopy(c)
  if re.fullmatch(r'(PAN|TILT)[2-6]',c.get('attribute','')):clone.set('attribute',c.get('attribute')[:-1])
  prepared.append(clone)
 _map_module_channels39(target,prepared,module)
 pairs=list(zip(children(target,'Channel'),source));controls={c.get('attribute'):ch for ch,c in pairs};masters={c.get('mode') for _,c in pairs if c.get('mode')}
 # HOG qualifiers address real-world parameter values. A mode controller is
 # exposed as a raw DMX selector so even constant/overlapping physical ranges
 # have an unambiguous native qualifier value. This is surfaced as a warning.
 for attr in masters:
  if attr not in controls:raise ValueError('ModeMaster должен находиться в одном модуле с управляемым каналом.')
  for r in descendants(controls[attr],'Range'):
   r.set('start',r.get('dmxstart'));r.set('end',r.get('dmxend',r.get('dmxstart')))
 for ch,c in pairs:
  master=c.get('mode')
  if not master:continue
  controller=first(controls[master],'Range')
  if controller is None:raise ValueError('ModeMaster не имеет рабочего диапазона.')
  ranges=[r for r in descendants(ch,'Range') if r.get('function') not in ('Cyan','Magenta','Yellow')];cursor=0
  for fn in children(c,'ChannelFunction'):
   maximum=65535 if 'fine' in c.attrib else 255;lo=round(int(fn.get('min_dmx_24','0'))*maximum/16777215);hi=round(int(fn.get('max_dmx_24','16777215'))*maximum/16777215);points={lo,hi+1}
   for s in children(fn,'ChannelSet'):points.update((int(s.get('from_dmx',str(lo))),int(s.get('to_dmx',str(hi)))+1))
   count=len(points)-1
   for r in ranges[cursor:cursor+count]:
    master_source=next(mc for _,mc in pairs if mc.get('attribute')==master);maximum_master='65535' if 'fine' in master_source.attrib else '255';qualifiers=E.SubElement(r,q('Qualifiers'),match='all');E.SubElement(qualifiers,q('Qualifier'),function=controller.get('function'),feature=controller.get('feature'),start=fn.get('mode_dmx_start','0'),end=fn.get('mode_dmx_end',maximum_master))
   cursor+=count
  if cursor!=len(ranges):raise ValueError('Не удалось сопоставить условные диапазоны HOG.')

def native_qualifier_condition(t,r):
 groups=children(r,'Qualifiers')
 if not groups:return None
 qualifiers=children(groups[0],'Qualifier')
 if len(groups)!=1 or groups[0].get('match')!='all' or len(qualifiers)!=1:raise ValueError('Поддерживается одно условие ModeMaster на диапазон.')
 qualifier=qualifiers[0];function=qualifier.get('function');feature=qualifier.get('feature');start=real_number(qualifier.get('start','0'));end=real_number(qualifier.get('end',str(start)))
 if end<start:raise ValueError('Обратный диапазон Qualifier требует сопоставления.')
 spans=[]
 for channel in descendants(t,'Channel'):
  for candidate in descendants(channel,'Range'):
   if candidate.get('function')!=function or candidate.get('feature')!=feature:continue
   a=real_number(candidate.get('start','0'));b=real_number(candidate.get('end',str(a)));low=int(candidate.get('dmxstart'));high=int(candidate.get('dmxend',str(low)))
   if a!=low or b!=high:raise ValueError('ModeMaster с нелинейной шкалой требует отдельного сопоставления.')
   left=max(start,a);right=min(end,b)
   if left<=right:spans.append((round(left),round(right),channel))
 if not spans:raise ValueError('Qualifier ссылается на неизвестный управляющий канал.')
 owners={id(c) for _,_,c in spans}
 if len(owners)!=1:raise ValueError('Qualifier неоднозначен между несколькими каналами.')
 intervals=sorted((a,b) for a,b,_ in spans);cursor=round(start)
 for a,b in intervals:
  if a>cursor:raise ValueError('Разрыв диапазона ModeMaster.')
  cursor=max(cursor,b+1)
 if cursor<=round(end):raise ValueError('Неполный диапазон ModeMaster.')
 return (ma2_attribute(function,feature)[0],round(start),round(end))

_gdtf_from_ma39=gdtf_from_ma
def gdtf_from_ma(ma_xml,name):
 ma=parse(ma_xml)
 if not any(c.get('mode') for c in descendants(ma,'ChannelType')):return _gdtf_from_ma39(ma_xml,name)
 bare=copy.deepcopy(ma)
 for parent in bare.iter():
  for child in list(parent):
   if child.tag is E.Comment:parent.remove(child)
 simplified=copy.deepcopy(bare)
 for c in descendants(simplified,'ChannelType'):
  if not c.get('mode'):continue
  functions=children(c,'ChannelFunction');firstcondition=(functions[0].get('mode_dmx_start','0'),functions[0].get('mode_dmx_end','255'))
  for fn in functions:
   if (fn.get('mode_dmx_start','0'),fn.get('mode_dmx_end','255'))!=firstcondition:c.remove(fn)
  c.attrib.pop('mode',None)
 encoded,warnings=_gdtf_from_ma39(ma2_serialize(simplified),name);root,fixture,modes=read_gdtf(encoded);mode=modes[0];dcs=mode.findall('./DMXChannels/DMXChannel');modules={n.get('index'):n for n in descendants(bare,'Module')};expanded=[(inst,c) for inst in descendants(bare,'Instance') for c in children(modules[inst.get('module_index')],'ChannelType')]
 if len(expanded)!=len(dcs):raise ValueError('Не удалось сопоставить условные каналы GDTF.')
 for index,(inst,c) in enumerate(expanded):
  if not c.get('mode'):continue
  masterindices=[j for j,(other,mc) in enumerate(expanded) if other is inst and mc.get('attribute')==c.get('mode')]
  if len(masterindices)!=1:raise ValueError('Неоднозначный ModeMaster MA2.')
  master=dcs[masterindices[0]];masterid=master.get('Geometry')+'_'+master.find('LogicalChannel').get('Attribute');lc=dcs[index].find('LogicalChannel');prototype=copy.deepcopy(lc.find('ChannelFunction'));lc.clear();lc.set('Attribute',prototype.get('Attribute'));lc.set('Master','None');bits=16 if 'fine' in c.attrib else 8 if 'coarse' in c.attrib else 32;maximum=2**bits-1;resolution=bits//8
  for fi,fn in enumerate(children(c,'ChannelFunction')):
   out=copy.deepcopy(prototype)
   for child in list(out):out.remove(child)
   out.set('Name','Function_'+str(fi));out.set('DMXFrom',str(round(int(fn.get('min_dmx_24','0'))*maximum/16777215))+'/'+str(resolution));out.set('PhysicalFrom',fn.get('physfrom','0'));out.set('PhysicalTo',fn.get('physto','1'));out.set('ModeMaster',masterid)
   mbits=16 if 'fine' in expanded[masterindices[0]][1].attrib else 8;out.set('ModeFrom',fn.get('mode_dmx_start','0')+'/'+str(mbits//8));out.set('ModeTo',fn.get('mode_dmx_end',str(2**mbits-1))+'/'+str(mbits//8))
   for s in children(fn,'ChannelSet'):E.SubElement(out,'ChannelSet',Name=s.get('name',''),DMXFrom=s.get('from_dmx','0')+'/'+str(resolution))
   lc.append(out)
  dcs[index].set('InitialFunction',dcs[index].get('Geometry')+'_'+lc.get('Attribute')+'.'+lc.get('Attribute')+'.Function_0')
 for child in list(root):
  if child.tag is E.Comment:root.remove(child)
 data={'hash':object_fingerprint(serialize(root)),'ma2Xml':ma2_serialize(bare)};root.insert(0,E.Comment(GDTF_EXPORT_MARKER+base64.b64encode(json.dumps(data,separators=(',',':')).encode()).decode()));description=serialize(root).encode()
 if len(description)>2_000_000:raise ValueError('GDTF превышает 2 МБ.')
 buf=io.BytesIO()
 with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:z.writestr('description.xml',description)
 return base64.b64encode(buf.getvalue()).decode(),warnings+['Условия ModeMaster и физические шкалы входного профиля перенесены в определения GDTF.']


# Preserve native numeric physical curves; no recovery metadata is added.
_gdtf_function_intervals40=gdtf_function_intervals
def gdtf_function_intervals(logical,bits,mode):
 original=_gdtf_function_intervals40(logical,bits,mode);output=[];resolution=bits//8
 for fn,low,high,condition in original:
  sets=fn.findall('ChannelSet')
  if not any('PhysicalFrom' in s.attrib or 'PhysicalTo' in s.attrib for s in sets):output.append((fn,low,high,condition));continue
  starts=[gdtf_dmx(s.get('DMXFrom','0/1'),bits) for s in sets]
  if len(set(starts))!=len(starts) or any(a>=b for a,b in zip(starts,starts[1:])):raise ValueError('Неупорядоченные физические ChannelSet.')
  spans=[(start,starts[i+1]-1 if i+1<len(starts) else high,s) for i,(start,s) in enumerate(zip(starts,sets))];points={low,high+1}
  for a,b,s in spans:
   if not low<=a<=b<=high:raise ValueError('Физический ChannelSet выходит за диапазон функции.')
   points.update((a,b+1))
  pf=real_number(fn.get('PhysicalFrom','0'));pt=real_number(fn.get('PhysicalTo','1'))
  for a,stop in zip(sorted(points),sorted(points)[1:]):
   b=stop-1;source=next(((x,y,s) for x,y,s in spans if x<=a<=y),None);clone=copy.deepcopy(fn)
   for s in list(clone.findall('ChannelSet')):clone.remove(s)
   if source and ('PhysicalFrom' in source[2].attrib or 'PhysicalTo' in source[2].attrib):
    x,y,s=source;left=real_number(s.get('PhysicalFrom',str(pf)));right=real_number(s.get('PhysicalTo',str(pt)));pa=left+(right-left)*(a-x)/(y-x) if y!=x else left;pb=left+(right-left)*(b-x)/(y-x) if y!=x else left
   else:pa=pf+(pt-pf)*(a-low)/(high-low) if high!=low else pf;pb=pf+(pt-pf)*(b-low)/(high-low) if high!=low else pf
   clone.set('PhysicalFrom',str(pa));clone.set('PhysicalTo',str(pb));clone.set('DMXFrom',str(a)+'/'+str(resolution))
   if source:
    label=copy.deepcopy(source[2]);label.set('DMXFrom',str(a)+'/'+str(resolution));label.attrib.pop('PhysicalFrom',None);label.attrib.pop('PhysicalTo',None);clone.append(label)
   output.append((clone,a,b,condition))
 return output

_map_module_channels40=map_module_channels
def map_module_channels(target,source,module):
 normalized=copy.deepcopy(source)
 # Display From/To is not a substitute for physical From/To. MA2 defaults
 # these physical bounds to 0 and 1 when either field is absent.
 for c in normalized:
  for fn in children(c,'ChannelFunction'):
   if 'physfrom' not in fn.attrib:fn.set('physfrom','0')
   if 'physto' not in fn.attrib:fn.set('physto','1')
 _map_module_channels40(target,normalized,module)
 for ch,c in zip(children(target,'Channel'),normalized):
  if c.get('attribute')!='COLORMIXMODE':continue
  # ColourMixMode is a selector, not a continuous physical measurement. A
  # native Colour Mode control with raw DMX values keeps every mode reachable
  # and makes the stored DMX Default uniquely invertible (e.g. 45 stays 45).
  for r in descendants(ch,'Range'):
   r.set('function','Colour Mode');r.set('feature','Continuous');r.set('start',r.get('dmxstart'));r.set('end',r.get('dmxend',r.get('dmxstart')))

_ma2_attribute40=ma2_attribute
def ma2_attribute(function,feature):
 if function=='Colour Mode':return ('COLORMIXMODE','COLOR','COLOR',1)
 return _ma2_attribute40(function,feature)

_gdtf_to_ma40=gdtf_to_ma
def gdtf_to_ma(root,fixture,mode,selected_break=None):
 ma,summary,warnings=_gdtf_to_ma40(root,fixture,mode,selected_break)
 # The underlying converter counts the expanded native functions/sets.
 if any(c.get('attribute')=='COLORMIXMODE' for c in descendants(ma,'ChannelType')):warnings.append('ColorMixMode переносится в штатный Colour Mode; нулевая физическая шкала использует дискретные Slots.')
 return ma,summary,warnings


def slot_number42(value):
 match=re.fullmatch(r'slot ([1-9]\d*)',value or '')
 return int(match[1]) if match else None

def hog_physical42(r):
 if r.get('feature')=='Slots':
  if r.get('function')=='Colour Mode':return 0.0,0.0
  if re.fullmatch(r'Gobo(?: [2-3])?',r.get('function','')):return 1.0,1.0
  raise ValueError('Физическая семантика дискретного параметра HOG требует сопоставления.')
 return real_number(r.get('start','0')),real_number(r.get('end',r.get('start','0')))

def range_slice42(r,low,high):
 original_low=int(r.get('dmxstart'));original_high=int(r.get('dmxend',str(original_low)));a,b=hog_physical42(r);out=copy.deepcopy(r);out.set('dmxstart',str(low));out.set('dmxend',str(high))
 for child in list(out):
  if local(child)=='Qualifiers':out.remove(child)
 interpolate=lambda n:a+(b-a)*(n-original_low)/(original_high-original_low) if original_high!=original_low else a
 out.set('start',str(interpolate(low)));out.set('end',str(interpolate(high)));return out

_ma2_attribute41=ma2_attribute
def ma2_attribute(function,feature):
 mapping={'Gobo':('GOBO1','GOBO1','GOBO',1),'Gobo 2':('GOBO2','GOBO2','GOBO',1),'Gobo <>':('GOBO1_POS','GOBO1','GOBO',1),'Gobo 2 <>':('GOBO2_POS','GOBO2','GOBO',1)}
 return mapping.get(function) or _ma2_attribute41(function,feature)

def slots_candidate42(c):
 return c.get('attribute')=='COLORMIXMODE' and all(real_number(f.get('physfrom','0'))==real_number(f.get('physto','1'))==0 for f in children(c,'ChannelFunction'))

_map_module_channels41=map_module_channels
def map_module_channels(target,source,module):
 # Start before the old raw-DMX substitution. Native Range retains physical
 # endpoints; Feature is part of parameter identity and disambiguates modes.
 prepared=copy.deepcopy(source)
 for c in prepared:
  if re.fullmatch(r'(PAN|TILT)[2-6]',c.get('attribute','')):c.set('attribute',c.get('attribute')[:-1])
  for fn in children(c,'ChannelFunction'):
   fn.set('physfrom',fn.get('physfrom','0'));fn.set('physto',fn.get('physto','1'))
 _map_module_channels39(target,prepared,module)
 pairs=list(zip(children(target,'Channel'),source));controls={c.get('attribute'):ch for ch,c in pairs};definitions={c.get('attribute'):c for _,c in pairs};masters={c.get('mode') for _,c in pairs if c.get('mode')}
 for ch,c in pairs:
  attr=c.get('attribute');controller=attr in masters;discrete=slots_candidate42(c)
  if not controller and not discrete:continue
  maximum=65535 if 'fine' in c.attrib else 255;cuts={0,maximum+1}
  for _,follower in pairs:
   if follower.get('mode')==attr:
    for fn in children(follower,'ChannelFunction'):cuts.update((int(fn.get('mode_dmx_start','0')),int(fn.get('mode_dmx_end',str(maximum)))+1))
  cuts=sorted(cuts)
  if len(cuts)>3:raise ValueError('Для более двух областей ModeMaster нужно дополнительное штатное сопоставление HOG.')
  ranges=first(ch,'Ranges')
  if ranges is None:raise ValueError('Режимный контроллер не имеет диапазонов.')
  old=children(ranges,'Range');new=[];slot=0;default=round(real_number(c.get('default','0')))
  for r in old:
   low=int(r.get('dmxstart'));high=int(r.get('dmxend',str(low)));points={low,high+1};points.update(n for n in cuts if low<n<=high)
   a,b=hog_physical42(r);make_slot=discrete or attr=='GOBO1' and a==b==1
   if make_slot and low<default<=high:points.update((default,default+1))
   points=sorted(points)
   for begin,stop in zip(points,points[1:]):
    end=stop-1;node=range_slice42(r,begin,end);area=max(i for i,start in enumerate(cuts[:-1]) if start<=begin)
    if discrete:node.set('function','Colour Mode')
    elif attr=='GOBO1':node.set('function','Gobo')
    elif attr=='GOBO2':node.set('function','Gobo 2')
    if make_slot:
     slot+=1
     if slot>48:raise ValueError('Дискретный контроллер превышает 48 штатных слотов HOG.')
     node.set('feature','Slots');node.set('start','slot '+str(slot));node.attrib.pop('end',None)
    elif attr.startswith(('PAN','TILT')):node.set('feature','Index' if area==0 else 'Rotate')
    else:node.set('feature','Continuous' if area==0 else 'Spin')
    new.append(node)
  ranges.clear();ranges.extend(new)
 # Follower controls use their native position identity instead of an
 # unrelated Macro slot, preventing collisions with the restored wheel.
 for ch,c in pairs:
  if c.get('attribute') in ('GOBO1_POS','GOBO2_POS'):
   for r in descendants(ch,'Range'):r.set('function','Gobo <>' if c.get('attribute')=='GOBO1_POS' else 'Gobo 2 <>')
 for ch,c in pairs:
  master=c.get('mode')
  if not master:continue
  if master not in controls:raise ValueError('ModeMaster отсутствует в модуле.')
  controller=controls[master];maximum=65535 if 'fine' in definitions[master].attrib else 255;followers=[r for r in descendants(ch,'Range') if r.get('function') not in ('Cyan','Magenta','Yellow')];index=0
  for fn in children(c,'ChannelFunction'):
   cmax=65535 if 'fine' in c.attrib else 255;lo=round(int(fn.get('min_dmx_24','0'))*cmax/16777215);hi=round(int(fn.get('max_dmx_24','16777215'))*cmax/16777215);points={lo,hi+1}
   for s in children(fn,'ChannelSet'):points.update((int(s.get('from_dmx',str(lo))),int(s.get('to_dmx',str(hi)))+1))
   count=len(points)-1;mode_lo=int(fn.get('mode_dmx_start','0'));mode_hi=int(fn.get('mode_dmx_end',str(maximum)));qs=[]
   for cr in descendants(controller,'Range'):
    left=max(mode_lo,int(cr.get('dmxstart')));right=min(mode_hi,int(cr.get('dmxend',cr.get('dmxstart'))))
    if left>right:continue
    if cr.get('feature')=='Slots':start=end=cr.get('start')
    else:
     cut=range_slice42(cr,left,right);a,b=hog_physical42(cut);start=str(min(a,b));end=str(max(a,b))
    attrs={'function':cr.get('function'),'feature':cr.get('feature'),'start':start,'end':end}
    if attrs not in qs:qs.append(attrs)
   if not qs:raise ValueError('Пустое условие ModeMaster.')
   for r in followers[index:index+count]:
    group=E.SubElement(r,q('Qualifiers'),match='any')
    for attrs in qs:E.SubElement(group,q('Qualifier'),attrs)
   index+=count
  if index!=len(followers):raise ValueError('Не сопоставлены диапазоны управляемого канала.')

def native_qualifier_condition(t,r):
 import math
 groups=children(r,'Qualifiers')
 if not groups:return None
 if len(groups)!=1:raise ValueError('Несколько групп Qualifiers требуют сопоставления.')
 group=groups[0];qualifiers=children(group,'Qualifier');allspans=[];owners=set();identity=None
 for qualifier in qualifiers:
  spans=[]
  for c in descendants(t,'Channel'):
   for candidate in descendants(c,'Range'):
    if any(candidate.get(k)!=qualifier.get(k) for k in ('function','feature')):continue
    low=int(candidate.get('dmxstart'));high=int(candidate.get('dmxend',str(low)));slot=slot_number42(candidate.get('start'))
    if candidate.get('feature')=='Slots':
     qslot=slot_number42(qualifier.get('start'));qend=slot_number42(qualifier.get('end',qualifier.get('start')))
     if qslot is None or qend!=qslot:raise ValueError('Неоднозначный дискретный Qualifier.')
     if slot!=qslot:continue
     left,right=low,high
    else:
     a,b=hog_physical42(candidate);start=real_number(qualifier.get('start','0'));end=real_number(qualifier.get('end',str(start)));qlo=max(min(a,b),start);qhi=min(max(a,b),end)
     if qlo>qhi+1e-9:continue
     if a==b:left,right=low,high
     else:
      da=low+(high-low)*(qlo-a)/(b-a);db=low+(high-low)*(qhi-a)/(b-a);left=math.ceil(min(da,db)-1e-6);right=math.floor(max(da,db)+1e-6)
    if left<=right:
     spans.append((left,right));owners.add(id(c));mapped=ma2_attribute(candidate.get('function'),candidate.get('feature'))[0];identity=identity or mapped
     if mapped!=identity:raise ValueError('Несколько режимных органов управления в одном условии.')
  if not spans:raise ValueError('Qualifier не соответствует ни одному интервалу контроллера.')
  allspans.append(spans)
 if len(owners)!=1:raise ValueError('Qualifier неоднозначен между каналами.')
 if group.get('match')=='all' and len(allspans)>1:
  spans=allspans[0]
  for alternatives in allspans[1:]:spans=[(max(a,x),min(b,y)) for a,b in spans for x,y in alternatives if max(a,x)<=min(b,y)]
 else:spans=[pair for part in allspans for pair in part]
 if not spans:raise ValueError('Пустое пересечение Qualifiers.')
 spans.sort();left,right=spans[0]
 for a,b in spans[1:]:
  if a>right+1:raise ValueError('Несвязное условие требует нескольких ModeMaster-интервалов MA2.')
  right=max(right,b)
 return identity,left,right

def hog_default42(t,ch,rs):
 if 'constant' in ch.attrib:return float(ch.get('constant'))
 for default in descendants(t,'Default'):
  for r in rs:
   if any(r.get(k)!=default.get(k) for k in ('function','feature')):continue
   low=int(r.get('dmxstart'));high=int(r.get('dmxend',str(low)))
   if r.get('feature')=='Slots':
    if r.get('start')==default.get('value'):return float(low)
    continue
   value=real_number(default.get('value','0'));a,b=hog_physical42(r)
   if min(a,b)-1e-9<=value<=max(a,b)+1e-9:return low+(high-low)*(value-a)/(b-a) if a!=b else float(low)
 return None



# Native controls, not stored source snapshots or encoded display labels.
CONTROL_MAP43={
 'PLAYMODE':('Play Mode','Continuous','CONTROL','CONTROL'),
 'CONTROL1':('Fixture Control','Continuous','CONTROL','CONTROL'),
 'LEDZONEMODE':('Zone','Continuous','CONTROL','CONTROL'),
 'PATTERNSELECTION':('Pattern','Continuous','GOBOALL','GOBO'),
 'PATTERNSIZE':('Pattern','Size','GOBOALL','GOBO'),
 'PATTERNROTATION':('Pattern <>','Continuous','GOBOALL','GOBO'),
 'PATTERNFADE':('Pattern Fade','Continuous','GOBOALL','GOBO'),
 'PATTERNTRANSITION':('Pattern Mode','Continuous','GOBOALL','GOBO'),
 'PATTERNCROSSFADE':('Macro Xfade','Continuous','GOBOALL','GOBO'),
 'COLORMACRO1':('Colour Fx','Continuous','COLOR','COLOR'),
 'XYZ_X':('X','Position','POSITION','POSITION'),
 'XYZ_Y':('Y','Position','POSITION','POSITION'),
 'XYZ_Z':('Z','Position','POSITION','POSITION'),
 'ROT_X':('X','Rotate','POSITION','POSITION'),
 'ROT_Y':('Y','Rotate','POSITION','POSITION'),
 'ROT_Z':('Z','Rotate','POSITION','POSITION'),
}
_ma2_attribute42=ma2_attribute

def ma2_attribute(function,feature):
 if function=='I Amber':return 'COLORRGB4','COLORRGB','COLOR',.01
 for attribute,definition in CONTROL_MAP43.items():
  if (function,feature)==definition[:2]:return attribute,definition[2],definition[3],1
 match=re.fullmatch(r'Beam Fx(?: ([1-9]))?( <>)?',function)
 if match:
  # A generic effect is not a gobo wheel. Keep its native effect bank and
  # position control distinct, including gaps in the numbering.
  suffix=match[1] or '';return 'EFFECT'+suffix+('_POS' if match[2] else ''),'BEAM','BEAM',1
 return _ma2_attribute42(function,feature)

_map_module_channels42=map_module_channels

def map_module_channels(target,source,module):
 normalized=copy.deepcopy(source)
 for c in normalized:
  if c.get('attribute') in ('COLORADD_RY','AMBER'):
   original=c.get('attribute');c.set('attribute','COLORRGB4')
   for fn in children(c,'ChannelFunction'):
    for key in ('attribute','subattribute'):
     if fn.get(key)==original:fn.set(key,'COLORRGB4')
 _map_module_channels42(target,normalized,module)
 for ch,c in zip(children(target,'Channel'),source):
  attr=c.get('attribute','');definition=CONTROL_MAP43.get(attr)
  match=re.fullmatch(r'EFFECT([2-9])?(_POS)?',attr)
  if match:definition=('Beam Fx'+(' '+match[1] if match[1] else '')+(' <>' if match[2] else ''),'Continuous','BEAM','BEAM')
  if not definition:continue
  if c.get('mode') or any(n.get('mode')==attr for n in source):raise ValueError('Условный управляющий параметр требует отдельного штатного сопоставления.')
  for r in descendants(ch,'Range'):
   r.set('function',definition[0]);r.set('feature',definition[1])

def ma_virtual_emitter48(c):
 attr=c.get('attribute','').upper()
 return bool(re.fullmatch(r'COLORRGB[0-9]+|(?:RED|GREEN|BLUE|WHITE|AMBER|CYAN|MAGENTA|YELLOW)[0-9]*',attr))


def hog_virtual_targets48(ch):
 ranges=descendants(ch,'Range');pairs={(r.get('function'),r.get('feature')) for r in ranges}
 result=set();inverse={'I Red':'Cyan','I Green':'Magenta','I Blue':'Yellow'}
 for r in ranges:
  fn=r.get('function');feature=r.get('feature')
  if feature!='Intensity' or r.get('dmxstart')==r.get('dmxend'):continue
  if not ma_virtual_emitter48(E.Element('ChannelType',attribute=ma2_attribute(fn,feature)[0])):continue
  alternate=(inverse.get(fn),'Variable')
  result.add(alternate if alternate in pairs else (fn,feature))
 return result


def validate_virtual_scope43(t,v):
 if v.get('function')!='Intensity' or v.get('feature')!='Intensity':raise ValueError('Неизвестный виртуальный орган управления.')
 affected={(n.get('function'),n.get('feature')) for n in children(v,'Affected')}
 eligible=set().union(*(hog_virtual_targets48(ch) for ch in descendants(t,'Channel')))
 if not affected or affected!=eligible:raise ValueError('Область действия виртуального диммера не представима автоматическим DIM MA2; требуется отдельный модуль. Экспорт остановлен без расширения области действия.')



# Typed mappings use native HOG parameters, not source snapshots or tags.
CONTROL_MAP43.update({
 'RECORDING':('Capture','Continuous','VIDEO','VIDEO'),
 'FLOWEREFFECT':('Beam Split','Rotate','BEAM','BEAM'),
})
for _bank44 in range(4,9):
 CONTROL_MAP43['MACRO'+str(_bank44)]=('Macro '+str(_bank44),'Continuous','GOBOALL','GOBO')
_ma2_attribute43=ma2_attribute

def ma2_attribute(function,feature):
 if function=='Colour Fx' and feature=='Index':return 'COLORMACRO1','COLOR','COLOR',1
 if function=='Beam Split' and feature in ('Off','Rotate'):return 'FLOWEREFFECT','BEAM','BEAM',1
 match=re.fullmatch(r'Beam Fx ([4-8])( <>| Offset)?',function)
 if match:
  bank=match[1];modifier=match[2] or ''
  if (modifier,feature) in (('','Index'),(' <>','Index'),(' Offset','Continuous')):
   attr='GOBO'+bank+('_POS' if modifier==' <>' else 'FORWARD' if modifier else '')
   return attr,'GOBO'+bank,'GOBO',1
 match=re.fullmatch(r'Macro ([4-8])',function)
 if match and feature=='Continuous':return 'MACRO'+match[1],'GOBOALL','GOBO',1
 return _ma2_attribute43(function,feature)

_map_module_channels43=map_module_channels

def map_module_channels(target,source,module):
 _map_module_channels43(target,source,module)
 for ch,c in zip(children(target,'Channel'),source):
  # The indexed wheel and a continuous rainbow may share 0..1 physical
  # values. Keep their native features separate, preserving both ramps.
  if c.get('attribute') in ('COLOR1','COLORMACRO1'):
   for r in descendants(ch,'Range'):
    fn,_=source_functions44(c,r,target)
    if fn is not None and fn.get('wheel') and any('slot_index' in s.attrib for s in children(fn,'ChannelSet')):
     wheels=getattr(_wheel_context44,'wheels',[]);wi=int(fn.get('wheel','0'))-1
     if 0<=wi<len(wheels) and any(tuple(int(s.get(k,'255')) for k in ('r','g','b'))!=(255,255,255) for s in children(wheels[wi],'Slot')):r.set('feature','Index')
  if c.get('attribute')=='FLOWEREFFECT':
   for r in descendants(ch,'Range'):
    if r.get('dmxstart')==r.get('dmxend')=='0' and hog_physical42(r)==(0.,0.):r.set('feature','Off')
  match=re.fullmatch(r'GOBO([4-8])(_POS|FORWARD)?',c.get('attribute',''))
  if not match:continue
  if c.get('mode') or any(n.get('mode')==c.get('attribute') for n in source):raise ValueError('Условный RGB-гобо требует отдельного сопоставления.')
  bank=match[1];modifier=match[2] or '';function='Beam Fx '+bank+(' <>' if modifier=='_POS' else ' Offset' if modifier else '');feature='Offset' if modifier=='FORWARD' else 'Index' if modifier=='_POS' else 'Continuous'
  for r in descendants(ch,'Range'):r.set('function',function);r.set('feature',feature)
 owners={}
 for ch in children(target,'Channel'):
  for r in descendants(ch,'Range'):
   key=(r.get('function'),r.get('feature'))
   if key in owners and owners[key] is not ch:raise ValueError('Коллизия независимых параметров HOG: '+' / '.join(key))
   owners[key]=ch

def safe_media_ref44(value):
 value=(value or '').replace('\\','/')
 if not value or value.startswith('/') or ':' in value or any(n in ('..','') for n in value.split('/')):return None
 return value

def rgb_from_xyY44(value):
 try:x,y,Y=map(float,value.split(','));Y/=100
 except (ValueError,AttributeError):return (255,255,255)
 if y<=0:return (0,0,0)
 X=x*Y/y;Z=(1-x-y)*Y/y
 raw=(3.2406*X-1.5372*Y-.4986*Z,-.9689*X+1.8758*Y+.0415*Z,.0557*X-.2040*Y+1.0570*Z)
 def encode(v):
  v=max(0,min(1,v));return round(255*(12.92*v if v<=.0031308 else 1.055*v**(1/2.4)-.055))
 return tuple(encode(v) for v in raw)

def xyz_from_rgb44(rgb):
 def decode(v):
  v=max(0,min(255,int(v)))/255;return v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4
 r,g,b=map(decode,rgb)
 return (.4124*r+.3576*g+.1805*b,.2126*r+.7152*g+.0722*b,.0193*r+.1192*g+.9505*b)

def rgb_from_xyz44(values):
 X,Y,Z=map(float,values);raw=(3.2406*X-1.5372*Y-.4986*Z,-.9689*X+1.8758*Y+.0415*Z,.0557*X-.2040*Y+1.0570*Z)
 return tuple(round(255*(12.92*max(0,min(1,v)) if v<=.0031308 else 1.055*max(0,min(1,v))**(1/2.4)-.055)) for v in raw)

def xyY_from_rgb44(rgb):
 X,Y,Z=xyz_from_rgb44(rgb);total=X+Y+Z
 return ','.join(format(n,'.9g') for n in ((X/total,Y/total,100*Y) if total else (0,0,0)))

def ma_physical_channels44(ma):
 fixture=first(ma,'FixtureType');mods={n.get('index'):n for n in children(first(fixture,'Modules'),'Module')};result={}
 for inst in children(first(fixture,'Instances'),'Instance'):
  for c in children(mods[inst.get('module_index')],'ChannelType'):
   if c.get('coarse'):result[int(inst.get('patch','1'))+int(c.get('coarse'))-1]=c
 return result

def import_gdtf_wheels44(ma,fixture,mode):
 source={w.get('Name'):w for w in fixture.findall('./Wheels/Wheel')};target=first(ma,'FixtureType');wheels=E.SubElement(target,mq('Wheels'),index='2');lookup={};channels=ma_physical_channels44(ma)
 for path,dc,brk,offsets in expanded_gdtf_channels(fixture,mode):
  if not offsets:continue
  c=channels.get(offsets[0])
  if c is None:continue
  logical=dc.find('LogicalChannel');intervals=gdtf_function_intervals(logical,len(offsets)*8,mode);functions=children(c,'ChannelFunction')
  if len(intervals)!=len(functions):raise ValueError('Не сопоставлены функции для переноса колёс.')
  for (original,_,_,_),fn in zip(intervals,functions):
   name=original.get('Wheel');wheel=source.get(name)
   if wheel is None:continue
   if name not in lookup:
    idx=len(lookup);lookup[name]=idx;out=E.SubElement(wheels,mq('Wheel'),index=str(idx),subattribute=fn.get('subattribute',c.get('attribute')),attribute=c.get('attribute'),feature=c.get('feature','BEAM'),preset=c.get('preset','BEAM'))
    for si,slot in enumerate(wheel.findall('Slot')):
     attrs={'index':str(si),'media_name':slot.get('Name','')};attrs.update(zip(('r','g','b'),map(str,rgb_from_xyY44(slot.get('Color','0.3127,0.329,100')))))
     media=safe_media_ref44(slot.get('MediaFileName'))
     if media:attrs['media_filename']='wheels/'+media+( '' if media.lower().endswith(('.png','.jpg','.jpeg','.bmp')) else '.png')
     E.SubElement(out,mq('Slot'),attrs)
   fn.set('wheel',str(lookup[name]+1));slots=wheel.findall('Slot')
   for original_set,result_set in zip(original.findall('ChannelSet'),children(fn,'ChannelSet')):
    index=int(original_set.get('WheelSlotIndex','0'))
    if index>len(slots):raise ValueError('Индекс слота вне определённого колеса GDTF.')
    if index>0:result_set.set('slot_index',str(index-1))

def source_functions44(c,r,t):
 maximum=65535 if c.get('fine') else 255;low=int(r.get('dmxstart'));high=int(r.get('dmxend',str(low)));condition=native_qualifier_condition(t,r)
 found=[]
 for fn in children(c,'ChannelFunction'):
  a=round(int(fn.get('min_dmx_24','0'))*maximum/16777215);b=round(int(fn.get('max_dmx_24','16777215'))*maximum/16777215)
  own=(c.get('mode'),int(fn.get('mode_dmx_start','0')),int(fn.get('mode_dmx_end','255'))) if c.get('mode') else None
  if a<=low<=high<=b and condition==own:found.append(fn)
 if len(found)!=1:return None,None
 fn=found[0];sets=[s for s in children(fn,'ChannelSet') if int(s.get('from_dmx',str(low)))<=low and int(s.get('to_dmx',str(high)))>=high]
 return fn,sets[0] if len(sets)==1 else None

def native_value44(r):
 if r.get('feature')=='Slots':return r.get('start')
 a,b=hog_physical42(r);return format((a+b)/2,'.15g')

def value_matches44(r,value):
 if r.get('feature')=='Slots':return value==r.get('start')
 try:p=float(value)
 except (TypeError,ValueError):return False
 a,b=hog_physical42(r);return min(a,b)-1e-9<=p<=max(a,b)+1e-9

_convert43=convert
_wheel_context44=threading.local()

def convert(root):
 previous=getattr(_wheel_context44,'wheels',[]);holder=first(root,'Wheels');_wheel_context44.wheels=children(holder,'Wheel') if holder is not None else []
 try:hog,system=_convert43(root)
 finally:_wheel_context44.wheels=previous
 if root.tag!=mq('MA') or read_hog_roundtrip(root) is not None:return hog,system
 fixture=first(root,'FixtureType');wheels=children(first(fixture,'Wheels'),'Wheel') if first(fixture,'Wheels') is not None else [];types={t.get('name'):t for t in descendants(hog,'Type')}
 for module in children(first(fixture,'Modules'),'Module'):
  t=types.get(f"{fixture.get('name','MA2')} M{module.get('index')}")
  if t is None:continue
  definitions={};colorrefs={};physical=sorted([c for c in children(module,'ChannelType') if c.get('coarse')],key=lambda c:int(c.get('coarse')))
  for ch,c in zip(descendants(t,'Channel'),physical):
   for r in descendants(ch,'Range'):
    if r.get('function') in ('Cyan','Magenta','Yellow') and any(other.get('function') in ('I Red','I Green','I Blue') for other in descendants(ch,'Range')):continue
    fn,s=source_functions44(c,r,t)
    if fn is None or s is None:continue
    slot=None;wi=int(fn.get('wheel','0'))-1;si=int(s.get('slot_index','-1'))
    if 0<=wi<len(wheels):
     ws=children(wheels[wi],'Slot')
     if 0<=si<len(ws):slot=ws[si]
    label=slot.get('media_name','') if slot is not None else s.get('name','')
    if not label:continue
    value=native_value44(r);identity=(r.get('function'),r.get('feature'));matches=[other for other in descendants(t,'Range') if (other.get('function'),other.get('feature'))==identity and value_matches44(other,value)]
    if len(matches)!=1:continue # No fabricated lookup for non-invertible physical scales.
    if identity not in definitions:
     package=first(t,'Previewpackages')
     if package is None:package=E.SubElement(t,q('Previewpackages'))
     holder=first(package,'Package')
     if holder is None:holder=E.SubElement(package,q('Package'),name='Fixture library')
     preview=E.SubElement(holder,q('Funcpreview'));definition=E.SubElement(preview,q('Definition'),name=c.get('attribute',''));E.SubElement(definition,q('Funcref'),function=identity[0],feature=identity[1]);definitions[identity]=preview
    entry=E.SubElement(definitions[identity],q('Entry'),value=value,name=label)
    if slot is not None:
     media=safe_media_ref44(slot.get('media_filename'))
     if media:entry.set('thumbnail',media)
     rgb=tuple(int(slot.get(k,'255')) for k in ('r','g','b'))
     if rgb!=(255,255,255):colorrefs.setdefault(identity,[]).append((value,rgb))
  if colorrefs:
   matcher=first(t,'ColourMatcher')
   if matcher is None:
    matcher=E.Element(q('ColourMatcher'),type='slotted',quality='estimated');pos=list(t).index(first(t,'Protocols'));t.insert(pos,matcher)
   for (function,feature),items in colorrefs.items():
    ref=E.SubElement(matcher,q('funcref'),function=function,feature=feature)
    for value,rgb in items:E.SubElement(ref,q('entry'),value=value,**dict(zip(('x','y','z'),map(str,xyz_from_rgb44(rgb)))))
 return hog,system

def parameter_bank44(function):
 return re.sub(r'(?: <>| Offset)$','',function or '')

_ma2_from_hog43=ma2_from_hog

def ma2_from_hog(payload,hog_xml):
 xml,warnings=_ma2_from_hog43(payload,hog_xml);ma=parse(xml);fixture=first(ma,'FixtureType');wheels=first(fixture,'Wheels');hog=parse(hog_xml);types={t.get('name'):t for t in descendants(hog,'Type')};mods={n.get('index'):n for n in children(first(fixture,'Modules'),'Module')};compound=next(c for c in descendants(hog,'CompoundType') if c.get('name')==payload['name'].strip());processed=set();registry={}
 for part,inst in zip(children(first(compound,'Parts'),'Part'),children(first(fixture,'Instances'),'Instance')):
  t=types[part.get('type')];module=mods[inst.get('module_index')]
  if id(module) in processed:continue
  processed.add(id(module));banks={};presets={}
  for preview in descendants(t,'Funcpreview'):
   refs=descendants(first(preview,'Definition'),'Funcref')
   if len(refs)!=1 or any(children(e,'Entry') for e in children(preview,'Entry')):continue
   identity=(refs[0].get('function'),refs[0].get('feature'));entries=children(preview,'Entry');palette=[];indices={};bindings=[]
   colors=[e for ref in descendants(t,'funcref') if (ref.get('function'),ref.get('feature'))==identity for e in children(ref,'entry')]
   for entry in entries:
    value=entry.get('value');color=next((x for x in colors if x.get('value')==value),None);rgb=rgb_from_xyz44([color.get(k,'0') for k in ('x','y','z')]) if color is not None else (255,255,255);media=safe_media_ref44(entry.get('thumbnail'));item=(entry.get('name',''),rgb,media)
    if item not in indices:indices[item]=len(palette);palette.append(item)
    bindings.append((value,indices[item]))
   if not palette:continue
   signature=tuple(palette)
   if signature not in registry:
    idx=len(children(wheels,'Wheel'));registry[signature]=idx;attribute,feature,preset,_=ma2_attribute(*identity);wheel=E.SubElement(wheels,mq('Wheel'),index=str(idx),attribute=attribute,subattribute=attribute,feature=feature,preset=preset)
    for si,(name,rgb,media) in enumerate(palette):
     attrs={'index':str(si),'media_name':name};attrs.update(zip(('r','g','b'),map(str,rgb)))
     if media:attrs['media_filename']=media
     E.SubElement(wheel,mq('Slot'),attrs)
   presets[identity]=(registry[signature]+1,bindings)
   if parameter_bank44(identity[0])==identity[0]:banks[parameter_bank44(identity[0])]=registry[signature]+1
  for ch in descendants(t,'Channel'):
   c=next((n for n in children(module,'ChannelType') if n.get('coarse')==ch.get('number')),None)
   if c is None:continue
   inverse={'I Red':'Cyan','I Green':'Magenta','I Blue':'Yellow'}.get(first(ch,'Range').get('function') if first(ch,'Range') is not None else '')
   ranges=[r for r in descendants(ch,'Range') if r.get('function')!=inverse]
   ranges.sort(key=lambda n:int(n.get('dmxstart')))
   for r,fn in zip(ranges,children(c,'ChannelFunction')):
    identity=(r.get('function'),r.get('feature'));choice=presets.get(identity)
    if choice:
     matches=[si for value,si in choice[1] if value_matches44(r,value)]
     if len(matches)==1:
      fn.set('wheel',str(choice[0]));s=E.SubElement(fn,mq('ChannelSet'),index='0',name=children(children(wheels,'Wheel')[choice[0]-1],'Slot')[matches[0]].get('media_name',''),from_dmx=r.get('dmxstart'),to_dmx=r.get('dmxend',r.get('dmxstart')),slot_index=str(matches[0]))
      if c.get('preset') in ('VIDEO','CONTROL'):s.set('auto_create','false')
    elif r.get('function','').endswith(' <>') and parameter_bank44(r.get('function')) in banks:fn.set('wheel',str(banks[parameter_bank44(r.get('function'))]))
 return ma2_serialize(ma),warnings

_gdtf_from_ma43=gdtf_from_ma

def gdtf_from_ma(ma_xml,name):
 encoded,warnings=_gdtf_from_ma43(ma_xml,name);ma=parse(ma_xml);fixture=first(ma,'FixtureType');native=children(first(fixture,'Wheels'),'Wheel') if first(fixture,'Wheels') is not None else []
 if not native:return encoded,warnings
 root,gf,modes=read_gdtf(encoded)
 # Unedited recovery can already contain the complete original wheel graph.
 if gf.find('Wheels') is not None:return encoded,warnings
 wheels=E.Element('Wheels');gf.insert(list(gf).index(gf.find('AttributeDefinitions'))+1,wheels);names=[]
 for index,w in enumerate(native):
  wheel_name='Wheel_'+str(index+1);names.append(wheel_name);wheel=E.SubElement(wheels,'Wheel',Name=wheel_name)
  for s in children(w,'Slot'):
   E.SubElement(wheel,'Slot',Name=s.get('media_name',''),Color=xyY_from_rgb44(tuple(int(s.get(k,'255')) for k in ('r','g','b'))))
   # XML alone cannot supply external media bytes. Never emit a dangling
   # GDTF MediaFileName here; resource packages are handled separately.
 mods={n.get('index'):n for n in descendants(fixture,'Module')};expanded=[c for inst in descendants(fixture,'Instance') for c in children(mods[inst.get('module_index')],'ChannelType')];dcs=modes[0].findall('./DMXChannels/DMXChannel')
 if len(expanded)!=len(dcs):raise ValueError('Не сопоставлены каналы для экспорта колёс.')
 for c,dc in zip(expanded,dcs):
  for fn,gfn in zip(children(c,'ChannelFunction'),dc.findall('./LogicalChannel/ChannelFunction')):
   wi=int(fn.get('wheel','0'))-1
   if not 0<=wi<len(names):continue
   gfn.set('Wheel',names[wi]);native_sets=children(fn,'ChannelSet');generated=gfn.findall('ChannelSet')
   for s in native_sets:
    if 'slot_index' not in s.attrib:continue
    bits=16 if c.get('fine') else 8;lo=int(s.get('from_dmx','0'));match=next((g for g in generated if gdtf_dmx(g.get('DMXFrom','0/1'),bits)==lo),None)
    if match is not None:match.set('WheelSlotIndex',str(int(s.get('slot_index'))+1))
 # Native wheel edits change description hashes; regenerate the ordinary
 # signed recovery comment rather than leaving a stale signature.
 for child in list(root):
  if child.tag is E.Comment:root.remove(child)
 data={'hash':object_fingerprint(serialize(root)),'ma2Xml':ma2_serialize(strip_recovery(ma))};root.insert(0,E.Comment(GDTF_EXPORT_MARKER+base64.b64encode(json.dumps(data,separators=(',',':')).encode()).decode()));description=serialize(root).encode()
 if len(description)>2_000_000:raise ValueError('GDTF превышает 2 МБ после переноса колёс.')
 buf=io.BytesIO()
 with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as archive:archive.writestr('description.xml',description)
 if any(s.get('media_filename') for w in native for s in children(w,'Slot')):warnings.append('Ссылки на изображения требуют пакета ресурсов; в одиночном XML/GDTF без исходных файлов текстуры отсутствуют.')
 return base64.b64encode(buf.getvalue()).decode(),list(dict.fromkeys(warnings))

RESOURCE_LIMIT44=6_000_000

def image_signature44(name,data):
 ext=name.rsplit('.',1)[-1].lower()
 if ext=='png':
  import zlib,struct
  if not data.startswith(b'\x89PNG\r\n\x1a\n'):return False
  pos=8;chunks=[]
  while pos+12<=len(data):
   length=int.from_bytes(data[pos:pos+4],'big');kind=data[pos+4:pos+8];end=pos+12+length
   if end>len(data) or zlib.crc32(data[pos+4:pos+8+length])&0xffffffff!=int.from_bytes(data[pos+8+length:end],'big'):return False
   if not chunks:
    if kind!=b'IHDR' or length!=13:return False
    width,height=struct.unpack('>II',data[pos+8:pos+16])
    if not width or not height or width*height>4_194_304:return False
   chunks.append(kind);pos=end
   if kind==b'IEND':return length==0 and pos==len(data) and b'IDAT' in chunks
  return False
 return (ext in ('jpg','jpeg') and data.startswith(b'\xff\xd8\xff') and data.endswith(b'\xff\xd9')) or (ext=='bmp' and len(data)>=54 and data.startswith(b'BM') and int.from_bytes(data[2:6],'little')==len(data))

def pack_resources44(resources,profile=None,filename='profile.xml'):
 out=io.BytesIO()
 with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
  if profile is not None:
   if not safe_media_ref44(filename) or '/' in filename or not filename.lower().endswith('.xml'):raise ValueError('Некорректное имя XML в пакете.')
   z.writestr(zipfile.ZipInfo(filename,date_time=(2026,1,1,0,0,0)),profile.encode())
  for name,data in sorted(resources.items()):
   info=zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;z.writestr(info,data)
 raw=out.getvalue()
 if len(raw)>RESOURCE_LIMIT44+(2_100_000 if profile is not None else 0):raise ValueError('Пакет ресурсов превышает 6 МБ.')
 return base64.b64encode(raw).decode()

def read_resources44(encoded,profile=False,gdtf=False):
 if not isinstance(encoded,str) or len(encoded)>28_000_000:raise ValueError('Некорректный пакет ресурсов.')
 try:raw=base64.b64decode(encoded,validate=True)
 except (ValueError,base64.binascii.Error):raise ValueError('Некорректная кодировка пакета ресурсов.')
 if len(raw)>(20_000_000 if gdtf else RESOURCE_LIMIT44+2_100_000):raise ValueError('Пакет ресурсов превышает лимит.')
 result={};seen=set();xml=None;total=0
 try:
  with zipfile.ZipFile(io.BytesIO(raw)) as z:
   if len(z.infolist())>2048:raise ValueError('Слишком много файлов в пакете.')
   for info in z.infolist():
    if info.is_dir():continue
    name=safe_media_ref44(info.filename)
    if not name or name.casefold() in seen or info.flag_bits&1:raise ValueError('Опасное имя, повтор файла или шифрование в пакете.')
    seen.add(name.casefold())
    if '/' not in name and name.lower().endswith('.xml') and profile:
     if xml is not None:raise ValueError('В ZIP должно быть ровно одно XML в корне.')
     if info.file_size>2_000_000:raise ValueError('XML в пакете превышает 2 МБ.')
     xml=z.read(info).decode('utf-8-sig');parse(xml);continue
    if name=='colorimetry.json' and not gdtf:
     if info.file_size>512_000:raise ValueError('colorimetry.json превышает 512 КБ.')
     data=z.read(info);color_manifest45(data);result[name]=data;continue
    if gdtf and not name.startswith('wheels/'):continue
    if name.rsplit('.',1)[-1].lower() not in ('png','jpg','jpeg','bmp'):
     if gdtf:continue
     raise ValueError('Пакет допускает только XML в корне и PNG/JPEG/BMP.')
    total+=info.file_size
    if info.file_size>4_000_000 or total>8_000_000:raise ValueError('Изображения в пакете превышают лимит.')
    data=z.read(info)
    if not image_signature44(name,data):raise ValueError('Тип изображения не соответствует содержимому.')
    result[name]=data
 except (zipfile.BadZipFile,RuntimeError,UnicodeError,NotImplementedError):raise ValueError('Повреждён ZIP-пакет.')
 if profile and xml is None:raise ValueError('В ZIP требуется один XML-профиль в корне.')
 # The smaller compressed resource limit is enforced independently of the
 # original archive's compression ratio and profile size.
 pack_resources44(result)
 return xml,result

_import_profile_wheels44=import_profile

def import_profile(payload):
 if payload.get('package'):
  xml,resources=read_resources44(payload['package'],profile=True);result=_import_profile_wheels44({'xml':xml});result.update(sourceXml=xml,resourceBase64=pack_resources44(resources) if resources else None,resourceCount=len(resources));return result
 result=_import_profile_wheels44(payload)
 if payload.get('gdtf') and payload.get('mode') is not None:
  _,resources=read_resources44(payload['gdtf'],gdtf=True);result.update(resourceBase64=pack_resources44(resources) if resources else None,resourceCount=len(resources))
 return result

def gdtf_with_resources44(encoded,ma_xml,resources):
 root,fixture,modes=read_gdtf(encoded);ma=parse(ma_xml);native=children(first(ma,'Wheels'),'Wheel') if first(ma,'Wheels') is not None else [];wheels=fixture.findall('./Wheels/Wheel');by_name={w.get('Name'):w for w in wheels};by_attr={}
 mods={n.get('index'):n for n in descendants(ma,'Module')};expanded=[c for inst in descendants(ma,'Instance') for c in children(mods[inst.get('module_index')],'ChannelType')];dcs=modes[0].findall('./DMXChannels/DMXChannel')
 for c,dc in zip(expanded,dcs):
  for fn,gfn in zip(children(c,'ChannelFunction'),dc.findall('./LogicalChannel/ChannelFunction')):
   wi=int(fn.get('wheel','0'))-1;target=by_name.get(gfn.get('Wheel'))
   if not 0<=wi<len(native) or target is None:continue
   for ns,gs in zip(children(native[wi],'Slot'),target.findall('Slot')):
    name=safe_media_ref44(ns.get('media_filename'))
    if name in resources and name.lower().endswith('.png') and name.startswith('wheels/'):
     gs.set('MediaFileName',name[len('wheels/'):-4])
 for child in list(root):
  if child.tag is E.Comment:root.remove(child)
 data={'hash':object_fingerprint(serialize(root)),'ma2Xml':ma2_serialize(strip_recovery(ma))};root.insert(0,E.Comment(GDTF_EXPORT_MARKER+base64.b64encode(json.dumps(data,separators=(',',':')).encode()).decode()));description=serialize(root).encode()
 if len(description)>2_000_000:raise ValueError('GDTF description.xml превышает 2 МБ.')
 out=io.BytesIO()
 with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
  z.writestr('description.xml',description)
  for name,data in sorted(resources.items()):z.writestr(name,data)
 return base64.b64encode(out.getvalue()).decode()

_generate_formats_wheels44=generate_formats

def generate_formats(payload):
 resources=read_resources44(payload['resourceBase64'])[1] if payload.get('resourceBase64') else {};result=_generate_formats_wheels44(payload)
 if resources:
  result['resourceBase64']=pack_resources44(resources);result['resourceCount']=len(resources)
  if result.get('gdtfBase64'):result['gdtfBase64']=gdtf_with_resources44(result['gdtfBase64'],result['ma2Xml'],resources)
  refs=[safe_media_ref44(s.get('media_filename')) for s in descendants(parse(result['ma2Xml']),'Slot') if s.get('media_filename')]
  if all(ref in resources and ref.lower().endswith('.png') for ref in refs):result['warnings']=[w for w in result.get('warnings',[]) if 'без 3D-моделей и изображений' not in w and not w.startswith('Ссылки на изображения требуют пакета ресурсов')]
  result['warnings'].append('Включено ресурсов изображений: '+str(len(resources))+'.')
 if payload.get('packageFormat') in ('hog','ma2'):
  refs=[n.get('thumbnail') for n in descendants(parse(result['xml']),'Entry') if n.get('thumbnail')]+[s.get('media_filename') for s in descendants(parse(result['ma2Xml']),'Slot') if s.get('media_filename')]
  if any(safe_media_ref44(ref) is None or safe_media_ref44(ref) not in resources for ref in refs):raise ValueError('Для ZIP не хватает файлов изображений. Загрузите исходный GDTF или ZIP с XML и ресурсами, либо выберите обычный XML.')
  result['packageBase64']=pack_resources44(resources,result['xml' if payload['packageFormat']=='hog' else 'ma2Xml'],export_filename(payload.get('sourceXml') or payload['xml'],payload['packageFormat'],payload['name']))
 return result

_save_pair_unlocked43=save_pair_unlocked

def save_pair_unlocked(payload,generated):
 resources=read_resources44(payload['resourceBase64'])[1] if payload.get('resourceBase64') else {};record=_save_pair_unlocked43(payload,generated)
 if record is None:return None
 try:return attach_resources44(record,resources)
 except Exception:
  folder=(STORE/record['id']).resolve()
  if re.fullmatch(r'[a-f0-9]{32}',record['id']) and folder.parent==STORE.resolve():shutil.rmtree(folder)
  refresh_library();raise

def attach_resources44(record,resources):
 if resources:
  folder=STORE/record['id'];temporary=folder/('.resources.'+uuid.uuid4().hex+'.new');temporary.write_bytes(base64.b64decode(pack_resources44(resources)));temporary.replace(folder/'.resources.zip')
  archive_path=folder/'bundle.zip'
  if archive_path.exists():
   with zipfile.ZipFile(archive_path) as z:items={n:z.read(n) for n in z.namelist()}
   items.update({record['folder']+'/'+name:data for name,data in resources.items()});temporary=folder/('bundle.'+uuid.uuid4().hex+'.new')
   with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as z:
    for name,data in items.items():z.writestr(name,data)
   temporary.replace(archive_path)
 return record

_complete_gdtf_record43=complete_gdtf_record

def complete_gdtf_record(info,payload):
 result=_complete_gdtf_record43(info,payload)
 sidecar=STORE/info['id']/'.resources.zip'
 if sidecar.is_file():attach_resources44(result,read_resources44(base64.b64encode(sidecar.read_bytes()).decode())[1])
 return result

def resource_identity_matches44(info,payload):
 if not payload.get('resourceBase64'):return True
 def signature(resources):return tuple((name,hashlib.sha256(data).hexdigest()) for name,data in sorted(resources.items()))
 _,incoming=read_resources44(payload['resourceBase64']);stored=STORE/info['id']/'.resources.zip'
 if not stored.is_file():return not incoming
 if stored.stat().st_size>RESOURCE_LIMIT44:return False
 try:_,previous=read_resources44(base64.b64encode(stored.read_bytes()).decode())
 except ValueError:return False
 return signature(incoming)==signature(previous)

_read_saved_xml43=read_saved_xml

def read_saved_xml(path):
 result=_read_saved_xml43(path)
 if result:
  match=re.fullmatch(r'/api/files/([a-f0-9]{32})/(source|hog|ma2)',path);resource=STORE/match[1]/'.resources.zip'
  if resource.is_file():
   encoded=base64.b64encode(resource.read_bytes()).decode();_,items=read_resources44(encoded);result['resourceBase64']=pack_resources44(items)
 return result

_validate_gdtf_export43=validate_gdtf_export

def validate_gdtf_export(encoded):
 raw=_validate_gdtf_export43(encoded);read_resources44(encoded,gdtf=True);return raw


"""Native XYZ transfer plus explicit, profile-bound MA2 ZIP color data."""
import math
COLOR_MANIFEST45='colorimetry.json'

def xyz45(value):
 try:x,y,Y=map(real_number,value.split(','))
 except (AttributeError,ValueError):raise ValueError('Некорректный цвет CIE xyY.')
 if x<0 or y<0 or x+y>1.000001 or not 0<=Y<=100:raise ValueError('Цвет CIE выходит за допустимые границы.')
 if y==0:
  if Y!=0:raise ValueError('Ненулевая яркость CIE при y=0.')
  return (0.,0.,0.)
 values=(x*Y/(100*y),Y/100,(1-x-y)*Y/(100*y))
 if any(not 0<=v<100 for v in values):raise ValueError('Цвет XYZ не представим в нативном HOG.')
 return values

def cie45(values):
 X,Y,Z=map(real_number,values);total=X+Y+Z
 if not total:return '0,0,0'
 return ','.join(format(v,'.17g') for v in (X/total,Y/total,100*Y))

def color_manifest45(raw):
 if len(raw)>512_000:raise ValueError('colorimetry.json превышает 512 КБ.')
 try:doc=json.loads(raw)
 except (UnicodeError,ValueError):raise ValueError('Повреждён colorimetry.json.')
 if not isinstance(doc,dict) or set(doc)!={'schema','profiles'} or doc['schema']!='vlighte.colorimetry/1' or not isinstance(doc['profiles'],list) or len(doc['profiles'])>2:raise ValueError('Неизвестная структура colorimetry.json.')
 seen=set()
 for p in doc['profiles']:
  if not isinstance(p,dict) or set(p)!={'format','fingerprint','bindings'} or p['format']!='MA2' or not isinstance(p['fingerprint'],str) or not re.fullmatch('[0-9a-f]{64}',p['fingerprint']) or p['fingerprint'] in seen:raise ValueError('Некорректная привязка цветовых данных.')
  seen.add(p['fingerprint'])
  if not isinstance(p['bindings'],list) or len(p['bindings'])>4096:raise ValueError('Слишком много цветовых привязок.')
  for b in p['bindings']:
   if not isinstance(b,dict) or set(b)!={'channel','dmxFrom','dmxTo','wheel','slot','name','rgb','xyz'}:raise ValueError('Некорректная запись цвета.')
   for k in ('channel','dmxFrom','dmxTo','wheel','slot'):
    if type(b[k]) is not int:raise ValueError('Цветовая привязка требует целых индексов.')
   if not 1<=b['channel']<=65535 or not 0<=b['dmxFrom']<=b['dmxTo']<=65535 or not 1<=b['wheel']<=4096 or not 0<=b['slot']<4096 or not isinstance(b['name'],str) or len(b['name'])>4096:raise ValueError('Цветовая привязка вне допустимых границ.')
   if not isinstance(b['rgb'],list) or len(b['rgb'])!=3 or any(type(v) is not int or not 0<=v<=255 for v in b['rgb']):raise ValueError('Некорректный RGB цвета.')
   if not isinstance(b['xyz'],list) or len(b['xyz'])!=3 or any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<100 for v in b['xyz']) or b['xyz'][1]>1:raise ValueError('Некорректный XYZ цвета.')
 return doc

def ma_color_bindings45(ma):
 holder=first(ma,'Wheels');wheels=children(holder,'Wheel') if holder is not None else [];result=[]
 for address,c in ma_physical_channels44(ma).items():
  for fn in children(c,'ChannelFunction'):
   wi=int(fn.get('wheel','0'))-1
   if not 0<=wi<len(wheels):continue
   slots=children(wheels[wi],'Slot')
   for s in children(fn,'ChannelSet'):
    si=int(s.get('slot_index','-1'))
    if not 0<=si<len(slots):continue
    slot=slots[si];result.append({'channel':address,'dmxFrom':int(s.get('from_dmx','0')),'dmxTo':int(s.get('to_dmx','255')),'wheel':wi+1,'slot':si,'name':slot.get('media_name',''),'rgb':[int(slot.get(k,'255')) for k in ('r','g','b')]})
 return result

def hog_color_bindings45(hog):
 types={t.get('name'):t for t in descendants(hog,'Type')};compound=first(hog,'CompoundType');result=[];cursor=1
 for part in children(first(compound,'Parts'),'Part'):
  t=types[part.get('type')]
  for preview in descendants(t,'Funcpreview'):
   refs=descendants(first(preview,'Definition'),'Funcref')
   if len(refs)!=1:continue
   identity=(refs[0].get('function'),refs[0].get('feature'))
   refs_colors=[e for ref in descendants(t,'funcref') if (ref.get('function'),ref.get('feature'))==identity for e in children(ref,'entry')]
   for entry in children(preview,'Entry'):
    colors=[n for n in refs_colors if n.get('value')==entry.get('value') and all(k in n.attrib for k in ('x','y','z'))]
    if len(colors)!=1:continue
    matches=[(c,r) for c in descendants(t,'Channel') for r in descendants(c,'Range') if (r.get('function'),r.get('feature'))==identity and value_matches44(r,entry.get('value'))]
    if len(matches)!=1:continue
    c,r=matches[0];result.append({'channel':cursor+int(c.get('number'))-1,'dmxFrom':int(r.get('dmxstart')),'dmxTo':int(r.get('dmxend',r.get('dmxstart'))),'name':entry.get('name',''),'xyz':[real_number(colors[0].get(k)) for k in ('x','y','z')],'_type':t,'_range':r,'_entry':entry})
  cursor+=int(first(t,'Dmxfootprint').get('size'))
 return result

def apply_hog_colors45(hog,bindings):
 # Existing native previews define the actual slot. We never encode a
 # source document or invent a color channel to transport metadata.
 types={t.get('name'):t for t in descendants(hog,'Type')};cursor=1;written={};layout=module_metadata(hog)
 for part_index,part in enumerate(children(first(first(hog,'CompoundType'),'Parts'),'Part')):
  t=types[part.get('type')]
  for preview in descendants(t,'Funcpreview'):
   refs=descendants(first(preview,'Definition'),'Funcref')
   if len(refs)!=1:continue
   identity=(refs[0].get('function'),refs[0].get('feature'))
   for entry in children(preview,'Entry'):
    matches=[(c,r) for c in descendants(t,'Channel') for r in descendants(c,'Range') if (r.get('function'),r.get('feature'))==identity and value_matches44(r,entry.get('value'))]
    if len(matches)!=1:continue
    c,r=matches[0];address=cursor+int(c.get('number'))-1;lo=int(r.get('dmxstart'));hi=int(r.get('dmxend',str(lo)))
    # During an import, compact editable modules can have interleaved DMX
    # patches. Resolve their existing layout before applying native colors.
    # Once generated, HOG parts are contiguous and need no import metadata.
    if layout:
     placement=layout['placements'][part_index];definition=layout['modules'][part.get('type')];spec=next(n for n in definition['channels'] if n['number']==int(c.get('number')));address=placement['patch']+spec['offset']-1
    colors=[b for b in bindings if b['channel']==address and b['dmxFrom']<=lo<=hi<=b['dmxTo'] and b['name']==entry.get('name','')]
    values={tuple(b['xyz']) for b in colors}
    if not values:continue
    if len(values)!=1:raise ValueError('Неоднозначная привязка CIE к HOG-превью.')
    xyz=next(iter(values));key=(id(t),identity,entry.get('value'))
    if key in written and written[key]!=xyz:raise ValueError('Общий модуль содержит разные цвета CIE; требуется отдельный модуль.')
    written[key]=xyz;cm=first(t,'ColourMatcher')
    if cm is None:cm=hog_type_section(t,'ColourMatcher');cm.set('type','slotted');cm.set('quality','chromaticity')
    refs_cm=[n for n in children(cm,'funcref') if (n.get('function'),n.get('feature'))==identity]
    ref=refs_cm[0] if refs_cm else E.SubElement(cm,q('funcref'),function=identity[0],feature=identity[1])
    entries=[n for n in children(ref,'entry') if n.get('value')==entry.get('value')]
    color=entries[0] if entries else E.SubElement(ref,q('entry'),value=entry.get('value'))
    for k,v in zip(('x','y','z'),xyz):color.set(k,format(v,'.17g'))
 return hog

def gdtf_color_bindings45(encoded,label):
 root,fixture,modes=read_gdtf(encoded);chosen=None
 for mode in modes:
  active=sorted({b for _,_,b,offsets in expanded_gdtf_channels(fixture,mode) if offsets})
  for brk in active:
   name=mode.get('Name') if len(active)==1 else mode.get('Name')+' [DMX Break '+str(brk)+']'
   if name==label:chosen=(mode,brk)
 if chosen is None:raise ValueError('Не найден режим для цветовых данных GDTF.')
 mode,selected=chosen;wheels={w.get('Name'):w for w in fixture.findall('./Wheels/Wheel')};result=[]
 for _,dc,brk,offsets in expanded_gdtf_channels(fixture,mode):
  if not offsets or brk!=selected:continue
  bits=len(offsets)*8
  for fn,lo,hi,_ in gdtf_function_intervals(dc.find('LogicalChannel'),bits,mode):
   wheel=wheels.get(fn.get('Wheel'));slots=wheel.findall('Slot') if wheel is not None else [];sets=fn.findall('ChannelSet')
   for i,s in enumerate(sets):
    si=int(s.get('WheelSlotIndex','0'))-1
    if not 0<=si<len(slots):continue
    a=gdtf_dmx(s.get('DMXFrom','0/1'),bits);b=gdtf_dmx(sets[i+1].get('DMXFrom','0/1'),bits)-1 if i+1<len(sets) else hi;slot=slots[si]
    result.append({'channel':offsets[0],'dmxFrom':a,'dmxTo':b,'name':slot.get('Name',''),'xyz':xyz45(slot.get('Color','0.3127,0.329,100'))})
 return result

def apply_gdtf_colors45(encoded,bindings):
 raw=base64.b64decode(encoded);root,fixture,modes=read_gdtf(encoded);wheels={w.get('Name'):w for w in fixture.findall('./Wheels/Wheel')};written={}
 for _,dc,_,offsets in expanded_gdtf_channels(fixture,modes[0]):
  if not offsets:continue
  bits=len(offsets)*8
  for fn,lo,hi,_ in gdtf_function_intervals(dc.find('LogicalChannel'),bits,modes[0]):
   wheel=wheels.get(fn.get('Wheel'));slots=wheel.findall('Slot') if wheel is not None else [];sets=fn.findall('ChannelSet')
   for i,s in enumerate(sets):
    si=int(s.get('WheelSlotIndex','0'))-1
    if not 0<=si<len(slots):continue
    slot=slots[si];a=gdtf_dmx(s.get('DMXFrom','0/1'),bits);b=gdtf_dmx(sets[i+1].get('DMXFrom','0/1'),bits)-1 if i+1<len(sets) else hi
    values={tuple(n['xyz']) for n in bindings if n['channel']==offsets[0] and n['dmxFrom']<=a<=b<=n['dmxTo'] and n['name']==slot.get('Name','')}
    if not values:continue
    if len(values)!=1:raise ValueError('Неоднозначная привязка CIE к слоту GDTF.')
    value=cie45(next(iter(values)))
    if id(slot) in written and written[id(slot)]!=value:raise ValueError('Один слот объединяет разные исходные цвета CIE.')
    written[id(slot)]=value;slot.set('Color',value)
 # Keep the existing ordinary generation receipt valid. This comment is
 # neither read nor needed by the color path, and audits strip it.
 for c in root:
  if c.tag is E.Comment and (c.text or '').startswith(GDTF_EXPORT_MARKER):
   data=json.loads(base64.b64decode(c.text[len(GDTF_EXPORT_MARKER):]));data['hash']=object_fingerprint(serialize(root));c.text=GDTF_EXPORT_MARKER+base64.b64encode(json.dumps(data,separators=(',',':')).encode()).decode()
 out=io.BytesIO()
 with zipfile.ZipFile(io.BytesIO(raw)) as old,zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
  for info in old.infolist():
   if info.filename!=COLOR_MANIFEST45:z.writestr(info,serialize(root).encode() if info.filename=='description.xml' else old.read(info))
 return base64.b64encode(out.getvalue()).decode()

_import_profile44=import_profile

def import_profile(payload):
 result=_import_profile44(payload)
 if 'xml' not in result:return result
 bindings=[]
 if payload.get('gdtf') and payload.get('mode') is not None:bindings=gdtf_color_bindings45(payload['gdtf'],payload['mode'])
 elif payload.get('package') or payload.get('resourceBase64'):
  if payload.get('package'):xml,resources=read_resources44(payload['package'],profile=True)
  else:xml=payload['xml'];_,resources=read_resources44(payload['resourceBase64'])
  source=parse(xml)
  if source.tag==mq('MA') and COLOR_MANIFEST45 in resources:
   doc=color_manifest45(resources[COLOR_MANIFEST45]);p=next((p for p in doc['profiles'] if p['fingerprint']==object_fingerprint(xml)),None)
   if p is None:raise ValueError('colorimetry.json не соответствует MA2 XML. Загрузите согласованный ZIP или обычный XML без точных цветовых данных.')
   native=ma_color_bindings45(source)
   for b in p['bindings']:
    guard={k:v for k,v in b.items() if k!='xyz'}
    if guard not in native:raise ValueError('Привязка CIE не соответствует слоту MA2.')
   bindings=p['bindings']
 if bindings:result['xml']=serialize(apply_hog_colors45(strip_recovery(parse(result['xml'])),bindings))
 result['preciseColors']=bool(hog_color_bindings45(parse(result['xml'])))
 return result

_generate_formats44=generate_formats

def generate_formats(payload):
 result=_generate_formats44(payload);hog=parse(result['xml']);bindings=hog_color_bindings45(hog)
 if not bindings:return result
 if result.get('gdtfBase64'):result['gdtfBase64']=apply_gdtf_colors45(result['gdtfBase64'],bindings)
 resources=read_resources44(payload['resourceBase64'])[1] if payload.get('resourceBase64') else {};resources.pop(COLOR_MANIFEST45,None);profiles={}
 for xml in [result['ma2Xml'],payload.get('sourceXml')]:
  if not xml or parse(xml).tag!=mq('MA'):continue
  native=ma_color_bindings45(parse(xml));records=[]
  for b in native:
   values={tuple(n['xyz']) for n in bindings if n['channel']==b['channel'] and n['dmxFrom']<=b['dmxFrom']<=b['dmxTo']<=n['dmxTo'] and n['name']==b['name']}
   if len(values)==1:records.append({**b,'xyz':list(next(iter(values)))})
  if records:
   fingerprint=object_fingerprint(xml);profiles[fingerprint]={'format':'MA2','fingerprint':fingerprint,'bindings':records}
 if profiles:
  data=json.dumps({'schema':'vlighte.colorimetry/1','profiles':list(profiles.values())},ensure_ascii=False,separators=(',',':')).encode();color_manifest45(data);resources[COLOR_MANIFEST45]=data
  result['warnings']=list(dict.fromkeys(result.get('warnings',[])+['Точные XYZ сохранены в GDTF/HOG и colorimetry.json пакета. В самой MA2 и отдельном MA2 XML используется приближённый RGB.']))
  result['resourceBase64']=pack_resources44(resources);result['resourceCount']=sum(n!=COLOR_MANIFEST45 for n in resources)
 if payload.get('packageFormat') in ('hog','ma2'):result['packageBase64']=pack_resources44(resources,result['xml' if payload['packageFormat']=='hog' else 'ma2Xml'],export_filename(payload.get('sourceXml') or payload['xml'],payload['packageFormat'],payload['name']))
 return result



# HOG 3.21.2: Type/Continuous, Offset/Offset and <>/Index are native
# Beam Fx bank features. A generic EFFECT uses Continuous too, so only a
# bank with its dedicated Offset control identifies an RGB-gobo group.
_ma2_attribute45_hog46=ma2_attribute
_ma2_from_hog45_hog46=ma2_from_hog

def ma2_attribute(function,feature):
 match=re.fullmatch(r'Beam Fx ([4-8])( Offset)?',function)
 if match:
  bank=match[1]
  if match[2] and feature=='Offset':return 'GOBO'+bank+'FORWARD','GOBO'+bank,'GOBO',1
 return _ma2_attribute45_hog46(function,feature)

def ma2_from_hog(payload,hog_xml):
 # Scope inference per Type: bank numbers can be reused in unrelated modules.
 root=parse(hog_xml)
 banks={}
 for t in descendants(root,'Type'):
  banks[t.get('name')]={r.get('function').split()[2] for r in descendants(t,'Range') if re.fullmatch(r'Beam Fx [4-8] Offset',r.get('function','')) and r.get('feature')=='Offset'}
 # Attribute conversion is called per channel without a Type parameter.
 # Temporarily normalize only identified RGB-gobo bank selectors to their
 # legacy identity for the existing converter; no legacy XML is emitted.
 for t in descendants(root,'Type'):
  for n in t.iter():
   if n.get('function','') in {'Beam Fx '+b for b in banks[t.get('name')]} and n.get('feature')=='Continuous':n.set('feature','Index')
 return _ma2_from_hog45_hog46(payload,serialize(root))



# Unknown MA physical endpoints are not a constant strobe frequency.
# These two native percentage controls keep raw shutter DMX independently
# addressable without assigning fabricated Hz or named strobe behaviours.
RAW_SHUTTER47={'SHUTTER2':('Strobe Mode','Continuous'),'SHUTTER3':('Out Strobe','Continuous')}
_map_module_channels46_shutter47=map_module_channels
_ma2_attribute46_shutter47=ma2_attribute
_ma2_from_hog46_shutter47=ma2_from_hog

def map_module_channels(target,source,module):
 _map_module_channels46_shutter47(target,source,module)
 for ch,c in zip(children(target,'Channel'),source):
  identity=RAW_SHUTTER47.get(c.get('attribute'))
  functions=children(c,'ChannelFunction')
  if not identity or not functions or all('physfrom' in f.attrib and 'physto' in f.attrib for f in functions):continue
  if c.get('mode') or any(other.get('mode')==c.get('attribute') for other in source):raise ValueError('SHUTTER с неполной физической шкалой и ModeMaster требует отдельного сопоставления.')
  for other in children(target,'Channel'):
   if other is not ch and any((r.get('function'),r.get('feature'))==identity for r in descendants(other,'Range')):raise ValueError('Коллизия независимого SHUTTER-контроллера: '+identity[0])
  ranges=first(ch,'Ranges');ranges.clear();maximum=65535 if 'fine' in c.attrib else 255
  for f in functions:
   low=round(int(f.get('min_dmx_24','0'))*maximum/16777215);high=round(int(f.get('max_dmx_24','16777215'))*maximum/16777215)
   a=real_number(f.get('from','0'));b=real_number(f.get('to','100'))
   if a==b:raise ValueError('SHUTTER без физической шкалы имеет неоднозначную пользовательскую шкалу.')
   cuts={low,high+1}
   for item in children(f,'ChannelSet'):cuts.update((int(item.get('from_dmx',low)),int(item.get('to_dmx',high))+1))
   cuts=sorted(cuts)
   for begin,stop in zip(cuts,cuts[1:]):
    end=stop-1
    interpolate=lambda n:a+(b-a)*(n-low)/(high-low) if high!=low else a
    E.SubElement(ranges,q('Range'),dmxstart=str(begin),dmxend=str(end),function=identity[0],feature=identity[1],start=str(interpolate(begin)),end=str(interpolate(end)))

def ma2_attribute(function,feature):
 for attr,identity in RAW_SHUTTER47.items():
  if (function,feature)==identity:return attr,'SHUTTER','BEAM',1
 return _ma2_attribute46_shutter47(function,feature)

def ma2_from_hog(payload,hog_xml):
 xml,warnings=_ma2_from_hog46_shutter47(payload,hog_xml)
 hog=parse(hog_xml);ma=parse(xml);types={t.get('name'):t for t in descendants(hog,'Type')};compound=next(c for c in descendants(hog,'CompoundType') if c.get('name')==payload['name'].strip());modules={c.get('index'):c for c in descendants(ma,'Module')}
 for part,inst in zip(children(first(compound,'Parts'),'Part'),descendants(ma,'Instance')):
  for ch in descendants(types[part.get('type')],'Channel'):
   if not any((r.get('function'),r.get('feature')) in RAW_SHUTTER47.values() for r in descendants(ch,'Range')):continue
   c=next(c for c in children(modules[inst.get('module_index')],'ChannelType') if c.get('coarse')==ch.get('number'))
   for f in children(c,'ChannelFunction'):
    f.attrib.pop('physfrom',None);f.attrib.pop('physto',None)
   warnings.append(c.get('attribute')+': сохранена процентная шкала управления; физические значения не заданы в HOG raw-контроллере.')
 return ma2_serialize(ma),sorted(set(warnings))



# The source virtual DIM controls COLORRGB7 independently of the RGB colour
# model. A native I White emitter switches colour models in HOG 3.21.2;
# use an auxiliary level (default 100%) multiplied by virtual Intensity.
_map_module_channels48_virtual49=map_module_channels
_ma2_from_hog48_virtual49=ma2_from_hog

def map_module_channels(target,source,module):
 _map_module_channels48_virtual49(target,source,module)
 if not any(c.get('attribute') in ('DIM','DIMMER') and 'coarse' not in c.attrib for c in children(module,'ChannelType')):return
 for ch,c in zip(children(target,'Channel'),source):
  if c.get('attribute')!='COLORRGB7':continue
  if any(r.get('function')=='Sub Intensity 1' for other in children(target,'Channel') if other is not ch for r in descendants(other,'Range')):raise ValueError('Независимый цветовой канал виртуального DIM конфликтует с Sub Intensity 1.')
  for r in descendants(ch,'Range'):
   if (r.get('function'),r.get('feature'))!=('I White','Intensity'):raise ValueError('Составной COLORRGB7 требует отдельного сопоставления виртуального DIM.')
   r.set('function','Sub Intensity 1')

def ma2_from_hog(payload,hog_xml):
 root=parse(hog_xml)
 for t in descendants(root,'Type'):
  affected=[a for v in descendants(t,'Virtual') for a in children(v,'Affected') if (a.get('function'),a.get('feature'))==('Sub Intensity 1','Intensity')]
  if not affected:continue
  if any(r.get('function')=='I White' for r in descendants(t,'Range')):raise ValueError('Виртуальный Sub Intensity 1 и I White требуют раздельных атрибутов MA2.')
  # MA's automatic DIM acts on emitter attributes, not physical DIM2.
  # Normalize only an explicitly affected auxiliary level; ordinary DIM2
  # remains a physical dimmer. No source snapshots or recovery comments.
  for r in descendants(t,'Range'):
   if (r.get('function'),r.get('feature'))==('Sub Intensity 1','Intensity'):r.set('function','I White')
  for a in affected:a.set('function','I White')
  for d in descendants(t,'Default'):
   if (d.get('function'),d.get('feature'))==('Sub Intensity 1','Intensity'):d.set('function','I White')
 return _ma2_from_hog48_virtual49(payload,serialize(root))


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
   if not 0<size<=(32_000_000 if path=='/api/archive' else 16_800_000 if path=='/api/generate' else 28_100_000):raise ValueError('Размер запроса превышает лимит.')
   if not environ.get('CONTENT_TYPE','').startswith('application/json'):raise ValueError('Ожидается JSON.')
   payload=json.loads(environ['wsgi.input'].read(size))
   if path=='/api/import':data=import_profile(payload)
   elif path=='/api/generate':
    data=generate_formats(payload) if payload.get('exportBoth') else {'xml':generate(payload)}
    data['originReceipt']=origin_receipt(capture_origin(environ),data['xml'],data.get('ma2Xml'),data.get('gdtfBase64'),data.get('resourceBase64'))
    existing=find_existing({**payload,'ma2Xml':data.get('ma2Xml')},data['xml'])
    if existing:data['existingId']=existing['id']
   else:
    if not payload.get('sourceXml'):raise ValueError('Отсутствует исходный XML.')
    generated=payload['xml'];validate_hog(parse(generated))
    if payload.get('ma2Xml'):validate_ma2(parse(payload['ma2Xml']))
    if payload.get('gdtfBase64'):validate_gdtf_export(payload['gdtfBase64'])
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
  attachment=download_gdtf(environ,start_response,path)
  if attachment is not None:return attachment
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
