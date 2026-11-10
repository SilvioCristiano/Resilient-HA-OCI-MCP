"""Explicit operator phases; render locally before applying to each OKE context."""
import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
import yaml
from common import ROOT,RUNTIME,REGIONS,config,state,private_json,kubectl,image
from rendering import build


def initialize():
    path=RUNTIME/'secrets.json'
    if path.exists():print('Segredos locais já existem; nenhuma credencial foi substituída.');return
    password=secrets.token_urlsafe(24)
    values={name:secrets.token_urlsafe(48) for name in ('MCP_TOKEN','REDIS_MCP_PASSWORD','REDIS_WEB_PASSWORD','WEB_SESSION_SECRET','CONTROL_SECRET')}
    values['PRESENTER_PASSWORD_HASH']=hashlib.sha256(password.encode()).hexdigest()
    private_json(path,values)
    fd=os.open(RUNTIME/'presenter-access.txt',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:stream.write('Senha do apresentador: '+password+'\nCompartilhe apenas com o responsável pelo laboratório.\n')
    print('Segredos gerados em .runtime/ (modo 600). A senha fica em .runtime/presenter-access.txt; não será impressa.')


def render(cfg,region):
    keys=json.loads((RUNTIME/'secrets.json').read_text())
    tls={'tls.crt':(RUNTIME/'tls/fullchain.pem').read_bytes(),'tls.key':(RUNTIME/'tls/server.key').read_bytes()}
    registry=(RUNTIME/'registry-config.json').read_text()
    docs=build(cfg,region,state(),keys,tls,registry)
    path=RUNTIME/'rendered'/region/'all.yaml'
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as stream:yaml.safe_dump_all(docs,stream,sort_keys=False)
    print('Manifestos privados gerados:',path.relative_to(ROOT))
    return path


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['init','check','render','deploy','discover','build','status'])
    p.add_argument('--region',choices=REGIONS)
    p.add_argument('--apply',action='store_true',help='Required for Kubernetes changes')
    args=p.parse_args()
    if args.action=='init':initialize();return
    cfg=config()
    if args.action=='check':
        for command in (['oci','--version'],['kubectl','version','--client=true'],['docker','version']):
            subprocess.run(command,check=True)
        print('Configuração local preenchida. Confira identidade, compartment e os dois contextos antes de aplicar.')
    elif args.action=='build':
        for kind in ('mcp','web'):
            subprocess.run(['docker','build','--platform','linux/amd64','--target',kind,'-t',image(cfg,kind),'.'],cwd=ROOT,check=True)
            subprocess.run(['docker','--config',str(RUNTIME/'docker'),'push',image(cfg,kind)],check=True)
    elif args.action in ('render','deploy'):
        if not args.region:p.error('--region é obrigatório')
        path=render(cfg,args.region)
        if args.action=='deploy':
            if not args.apply:raise ValueError('Revise os manifestos locais e adicione --apply para aplicar.')
            print(kubectl(cfg,args.region,'apply','-f',str(path)))
            for app in ('mcp-server','mcp-web','mcp-edge'):
                print(kubectl(cfg,args.region,'rollout','status','deployment/'+app,'-n','mcp-lab','--timeout=300s'))
    elif args.action=='discover':
        observed=state();observed.setdefault('ips',{})
        for region in REGIONS:
            try:data=kubectl(cfg,region,'get','service','mcp-public','-n','mcp-lab','-o','json',json_output=True)
            except subprocess.CalledProcessError:
                print('Service ainda indisponível no contexto '+cfg['regions'][region]['context']);continue
            entries=data.get('status',{}).get('loadBalancer',{}).get('ingress',[])
            if entries and entries[0].get('ip'):observed['ips'][region]=entries[0]['ip']
        private_json(RUNTIME/'state.json',observed)
        print(json.dumps({'ips':observed['ips']},indent=2))
    elif args.action=='status':
        for region in REGIONS:
            print(region)
            print(kubectl(cfg,region,'get','deploy,svc,pods','-n','mcp-lab','-o','wide'))


if __name__=='__main__':
    try:main()
    except (ValueError,RuntimeError,FileNotFoundError,subprocess.CalledProcessError) as exc:
        print('Não concluído:',str(exc),file=sys.stderr);sys.exit(1)
