import asyncio
import os
import time
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.responses import JSONResponse
from app.oci_llm_client import OCILLMClient
from app.state import SessionStore

REGION = os.environ["OCI_REGION"]
START = time.monotonic()
STATE_BACKEND = "redis"
store = SessionStore(os.environ["REDIS_URL"])
llm = OCILLMClient()
token = os.environ["MCP_TOKEN"]
if len(token) < 32:
    raise ValueError("MCP_TOKEN must have at least 32 characters")
mcp = FastMCP("OCI Resilient MCP", auth=StaticTokenVerifier(
    tokens={token: {"client_id": "lab-client", "scopes": ["mcp"]}}))


@mcp.tool()
async def ask_oci_genai(prompt: str, session_id: str) -> dict:
    """Resume a shared conversation. Use an unguessable session_id unique to this client."""
    if not prompt.strip() or len(prompt) > 32000:
        raise ValueError("prompt length must be 1..32000 characters")
    raw, state = await asyncio.to_thread(store.load, session_id)
    history = state["history"] + [{"role": "USER", "text": prompt}]
    if sum(len(x["text"]) for x in history) > 200000:
        raise ValueError("Session context limit exceeded; archive and start a new session")
    result = await asyncio.to_thread(llm.chat, history)
    state["history"] = history + [{"role": "ASSISTANT", "text": result["answer"]}]
    state["revision"] += 1
    state["tool_context"]["last_inference"] = {k: v for k, v in result.items() if k != "answer"}
    await asyncio.to_thread(store.commit, session_id, raw, state)
    return dict(result, app_region=REGION, session_revision=state["revision"],
                history_messages=len(state["history"]))


@mcp.tool()
async def analyze_resilience_status() -> dict:
    """Return node and shared state health, with inference dependency made explicit."""
    return {"app_region": REGION, "uptime_seconds": time.monotonic() - START,
            "state_healthy": await asyncio.to_thread(store.health), "state_backend": STATE_BACKEND,
            "model_execution_region": "sa-saopaulo-1", "regional_inference_dr": False}


@mcp.custom_route("/livez", methods=["GET"])
async def livez(request):
    return JSONResponse({"live": True})


@mcp.custom_route("/readyz", methods=["GET"])
async def readyz(request):
    try:
        await asyncio.to_thread(store.health)
        return JSONResponse({"ready": True, "app_region": REGION})
    except Exception:
        return JSONResponse({"ready": False}, status_code=503)


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000, stateless_http=True)
