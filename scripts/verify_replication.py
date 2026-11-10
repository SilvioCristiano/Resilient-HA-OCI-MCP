"""Read one known session from Vinhedo Cache; never write to or promote the replica."""
import argparse
import json
import subprocess
import uuid
from common import ROOT,RUNTIME,REGIONS,config,state,kubectl,image

p=argparse.ArgumentParser();p.add_argument('--session-id',required=True);p.add_argument('--min-revision',type=int,default=1);args=p.parse_args()
if args.min_revision < 1: p.error('--min-revision deve ser pelo menos 1')
sid=str(uuid.UUID(args.session_id));cfg=config();secondary=state()['cache'][REGIONS[1]]['endpoint']
source='''import os,json,hashlib,time
from urllib.parse import urlsplit,urlunsplit
import redis
u=urlsplit(os.environ['REDIS_URL'])
url=urlunsplit((u.scheme,u.netloc.split('@')[0]+'@'+os.environ['SECONDARY_HOST']+':6379',u.path,'',''))
r=redis.Redis.from_url(url,decode_responses=True,ssl_cert_reqs='required',socket_connect_timeout=3,socket_timeout=3)
key='mcp:session:'+hashlib.sha256(os.environ['SESSION_ID'].encode()).hexdigest()
for attempt in range(30):
 raw=r.get(key)
 if raw:
  s=json.loads(raw)
  if s['revision'] < int(os.environ['MIN_REVISION']):
   time.sleep(2)
   continue
  print(json.dumps({'secondary_read':True,'history_messages':len(s['history']),'revision':s['revision'],'redis_role':r.info('replication').get('role')}))
  break
 time.sleep(2)
else: raise RuntimeError('Sessao ainda nao observada na replica; confira lag e o session_id.')
'''
name='mcp-replication-check-'+uuid.uuid4().hex[:8]
job={'apiVersion':'batch/v1','kind':'Job','metadata':{'name':name,'namespace':'mcp-lab'},'spec':{
    'ttlSecondsAfterFinished':300,'activeDeadlineSeconds':120,'backoffLimit':0,'template':{'spec':{
    'restartPolicy':'Never','automountServiceAccountToken':False,'imagePullSecrets':[{'name':'mcp-ocir'}],
    'securityContext':{'runAsNonRoot':True,'runAsUser':10001},'containers':[{'name':'check','image':image(cfg,'mcp'),
        'command':['python','-c',source],'env':[{'name':'MIN_REVISION','value':str(args.min_revision)},{'name':'SESSION_ID','value':sid},{'name':'SECONDARY_HOST','value':secondary},
            {'name':'REDIS_URL','valueFrom':{'secretKeyRef':{'name':'mcp-secrets','key':'REDIS_URL'}}}],
        'resources':{'requests':{'cpu':'25m','memory':'64Mi'},'limits':{'cpu':'250m','memory':'128Mi'}},
        'securityContext':{'readOnlyRootFilesystem':True,'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}}}]}}}}
path=RUNTIME/'replication-check.json';path.write_text(json.dumps(job))
print(kubectl(cfg,REGIONS[1],'apply','-f',str(path)))
try:
    print(kubectl(cfg,REGIONS[1],'wait','--for=condition=complete','job/'+name,'-n','mcp-lab','--timeout=130s'))
finally:
    print(kubectl(cfg,REGIONS[1],'logs','job/'+name,'-n','mcp-lab'))
    print(kubectl(cfg,REGIONS[1],'delete','job',name,'-n','mcp-lab','--wait=false'))
