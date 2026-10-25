import runpy,os,xml.etree.ElementTree as E
os.environ['VLIGHTE_GEO_BOOTSTRAP']='0'
m=runpy.run_path('app.py')
root=E.fromstring('<GDTF><FixtureType Name="Test"><Geometries><Geometry Name="Base"><Geometry Name="Padding"/><Geometry Name="Active"/></Geometry></Geometries><DMXModes><DMXMode Name="4ch" Geometry="Base"><DMXChannels/></DMXMode></DMXModes></FixtureType></GDTF>')
f=root.find('FixtureType');mode=f.find('./DMXModes/DMXMode');channels=mode.find('DMXChannels')
for address,attr in [(1,'NoFeature'),(2,'NoFeature'),(3,'Dimmer'),(4,'NoFeature')]:
 c=E.SubElement(channels,'DMXChannel',Offset=str(address),Geometry='Active' if attr=='Dimmer' else 'Padding')
 E.SubElement(E.SubElement(c,'LogicalChannel',Attribute=attr),'ChannelFunction',Attribute=attr,Name=attr,DMXFrom='0/1')
ma,summary,_=m['gdtf_to_ma'](root,f,mode)
assert summary['modules']==summary['instances']==1 and m['validate_ma2'](ma)==4
assert sorted(int(c.get('coarse')) for c in m['descendants'](ma,'ChannelType'))==[1,2,3,4]
channels[2].find('LogicalChannel').set('Attribute','NoFeature');channels[2].find('./LogicalChannel/ChannelFunction').set('Attribute','NoFeature')
try:m['gdtf_to_ma'](root,f,mode);raise AssertionError('All-empty fixture accepted')
except ValueError as error:assert 'неиспользуемых' in str(error)
print('PASS leading/trailing padding attaches to working module; all-empty profile rejected')
