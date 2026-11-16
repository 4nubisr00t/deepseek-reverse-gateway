from app.api.auth_routes import router as auth_router
from fastapi import APIRouter, Request
from curl_cffi import requests as cffi_requests
import asyncio

solver_proxy_router = APIRouter(prefix="/api/solver", tags=["solver"])

FLARESOLVERR_URL = "http://127.0.0.1:8191/v1"
SERVER_PROXIES = [
    "http://127.0.0.1:7890",
    "http://127.0.0.1:7891"
]

@solver_proxy_router.post("/v1")
async def forward_solver_request(req_body: dict):
    # Ensure proxy is set to local mihomo proxy on server if not supplied
    if "proxy" not in req_body or not req_body["proxy"]:
        req_body["proxy"] = {"url": SERVER_PROXIES[0]}

    loop = asyncio.get_running_loop()
    def _do_post():
        r = cffi_requests.post(FLARESOLVERR_URL, json=req_body, timeout=120)
        return r.json()
    return await loop.run_in_executor(None, _do_post)

@solver_proxy_router.get("/health")
async def solver_health():
    loop = asyncio.get_running_loop()
    def _do_get():
        try:
            r = cffi_requests.get("http://127.0.0.1:8191/", timeout=5)
            return r.json()
        except Exception as e:
            return {"status": "error", "message": str(e)}
    return await loop.run_in_executor(None, _do_get)
