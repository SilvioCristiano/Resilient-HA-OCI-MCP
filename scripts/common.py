"""Operator tooling: local configuration is explicit and never part of Git."""
import json
import os
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=ROOT/'.runtime'
REGIONS=('sa-saopaulo-1','sa-vinhedo-1')
LAB='Resilient-HA-OCI-MCP'


def private_json(path,value):
    path.parent.mkdir(mode=0o700,exist_ok=True)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(value,stream,indent=2)


def config(sample=False):
    path=ROOT/'config'/('lab.example.json' if sample else 'lab.json')
    if not path.exists():raise RuntimeError('Copie config/lab.example.json para config/lab.json e preencha os dados do seu ambiente.')
    cfg=json.loads(path.read_text())
    if not sample:
        for field in ('compartment_id','domain','mcp_host','dns_zone_id','model_id','vinhedo_routing_profile_id','registry_namespace'):
            value=cfg.get(field,'')
            if not value or 'REPLACE_ME' in value or value.endswith('.invalid'):
                raise ValueError('Preencha o campo '+field+' em config/lab.json')
    return cfg


def state():
    path=RUNTIME/'state.json'
    return json.loads(path.read_text()) if path.exists() else {}


def kubectl(cfg,region,*args,json_output=False):
    cmd=['kubectl','--context',cfg['regions'][region]['context'],*args]
    result=subprocess.run(cmd,check=True,text=True,capture_output=True)
    return json.loads(result.stdout) if json_output else result.stdout


def image(cfg,kind):
    return cfg['registry']+'/'+cfg['registry_namespace']+'/'+cfg['repository_'+kind]+':'+cfg['image_version']
