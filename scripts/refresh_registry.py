"""Run in OKE: renew a named OCIR image-pull Secret with a short-lived token."""
import base64
import json
import os
from pathlib import Path
import ssl
import urllib.request
import oci
from oci._vendor import requests

signer=oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
registry=os.environ["OCIR_REGISTRY"]
response=requests.get("https://"+registry+"/20180419/docker/token",auth=signer,
    headers={"Accept":"application/json"},timeout=(5,20),allow_redirects=False)
response.raise_for_status()
token=response.json()["token"]
docker={"auths":{registry:{"auth":base64.b64encode(("BEARER_TOKEN:"+token).encode()).decode()}}}
payload={"data":{".dockerconfigjson":base64.b64encode(json.dumps(docker).encode()).decode()}}
ca="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
bearer=Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text().strip()
host=os.environ["KUBERNETES_SERVICE_HOST"]
port=os.environ["KUBERNETES_SERVICE_PORT_HTTPS"]
request=urllib.request.Request(f"https://{host}:{port}/api/v1/namespaces/mcp-lab/secrets/mcp-ocir",
    method="PATCH",data=json.dumps(payload).encode(),headers={"Authorization":"Bearer "+bearer,
    "Content-Type":"application/merge-patch+json"})
with urllib.request.urlopen(request,context=ssl.create_default_context(cafile=ca),timeout=20) as result:
    result.read()
    print(json.dumps({"registry_secret_refreshed":True,"namespace":"mcp-lab","secret":"mcp-ocir",
        "credential_expires_in":response.json().get("expires_in"),"http_status":result.status}))
