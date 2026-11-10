"""Stateless MCP client; the public browser never receives the MCP credential."""
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
HOST = os.environ.get("MCP_HOST", "mcp.example.invalid")
REGIONS = json.loads(os.environ.get("MCP_REGION_IPS", '{"sa-saopaulo-1":"192.0.2.10","sa-vinhedo-1":"192.0.2.20"}'))


class Unavailable(RuntimeError):
    """The HTTP request was rejected before a tool could run; retry is safe."""


class MCPClient:
    def __init__(self):
        self.context = ssl.create_default_context()
        ca_file = Path(os.environ.get("MCP_CA_FILE", "/tls/ca.crt"))
        if ca_file.exists() and ca_file.stat().st_size:
            self.context.load_verify_locations(cafile=str(ca_file))
        self.token = os.environ["MCP_TOKEN"]

    def rpc(self, method, params=None, *, protocol="2025-03-26", notification=False):
        conn = http.client.HTTPSConnection(HOST, 443, timeout=70, context=self.context)
        request_id = str(uuid.uuid4())
        body = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = request_id
        if params is not None:
            body["params"] = params
        try:
            conn.request("POST", "/mcp", body=json.dumps(body), headers={
                "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": protocol})
            response = conn.getresponse()
            if response.status == 503:
                response.read(4096)
                raise Unavailable(f"HTTP {response.status} no endpoint global")
            if response.status >= 400:
                response.read(4096)
                raise RuntimeError(f"MCP recusou a requisição: HTTP {response.status}")
            if response.status == 202:
                response.read()
                return {}
            if "text/event-stream" in response.getheader("Content-Type", ""):
                data, size = [], 0
                while line := response.readline():
                    size += len(line)
                    if size > 2_000_000:
                        raise RuntimeError("Resposta MCP excedeu o limite")
                    line = line.decode().rstrip("\r\n")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        payload = json.loads("\n".join(data))
                        data = []
                        if payload.get("id") == request_id:
                            break
                else:
                    raise RuntimeError("MCP fechou o stream sem confirmar o resultado")
            else:
                payload = json.loads(response.read(2_000_001))
            if "error" in payload:
                raise RuntimeError("Erro de protocolo MCP: " + str(payload["error"].get("message", ""))[:300])
            return payload.get("result", {})
        finally:
            conn.close()

    def call_tool(self, name, arguments):
        started = time.monotonic()
        init = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "OCI Brazil Live Demo", "version": "1.0"}})
        protocol = init.get("protocolVersion", "2025-03-26")
        self.rpc("notifications/initialized", protocol=protocol, notification=True)
        result = self.rpc("tools/call", {"name": name, "arguments": arguments}, protocol=protocol)
        if result.get("isError"):
            raise RuntimeError("A ferramenta MCP retornou erro; resultado não confirmado")
        data = result.get("structuredContent")
        if not isinstance(data, dict):
            text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
            data = json.loads(text)
        return data, time.monotonic() - started

    def regional_health(self, region):
        started = time.monotonic()
        conn = http.client.HTTPSConnection(HOST, 443, context=self.context, timeout=4)
        try:
            raw = socket.create_connection((REGIONS[region], 443), timeout=4)
            conn.sock = self.context.wrap_socket(raw, server_hostname=HOST)
            conn.request("GET", "/readyz", headers={"Host": HOST})
            response = conn.getresponse()
            data = response.read(4096).decode()
            return {"region": region, "healthy": response.status == 200, "http_status": response.status,
                "seconds": round(time.monotonic() - started, 3), "source": "HTTPS /readyz", "body": data}
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            return {"region": region, "healthy": False, "http_status": None, "error": type(exc).__name__, "source": "HTTPS /readyz"}
        finally:
            conn.close()
