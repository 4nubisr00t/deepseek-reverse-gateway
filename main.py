"""
ds-gateway 启动入口
"""

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.config import config_manager
from app.core.security import ensure_default_key, flush_pending
from app.api.openai_routes import router as openai_router
from app.api.claude_routes import router as claude_router
from app.api.responses_routes import router as responses_router
from app.api.admin_routes import router as admin_router
from app.api.auth_routes import router as auth_router
from app.api.solver_routes import solver_proxy_router
from contextlib import asynccontextmanager



@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动 / 关闭钩子。

    原先是 `@app.on_event("startup")` + `@app.on_event("shutdown")` —— 这两个装饰器
    已被 FastAPI 标记为 deprecated，每次跑 pytest 都会打 DeprecationWarning
    （指向 main.py 的这两行），把真正有用的告警淹没掉。

    改成 lifespan 时**逐字保留**了原有关闭顺序与异常处理：
    `flush_pending()` 不吞异常（它是 keystore 的最后一次落盘，失败必须显式暴露），
    后两个节流存储各自 try/except 并打印 —— 不能因为迁移把语义改掉。
    """
    print("\n" + "=" * 60)
    print("🚀 Anubis DeepSeek Gateway (ds-gateway) 启动中...")
    print(f"📊 管理后台: http://localhost:{config_manager.config.port}/admin")
    print(f"🔌 OpenAI 兼容接口: http://localhost:{config_manager.config.port}/v1/chat/completions")
    print(f"🔌 Claude 兼容接口: http://localhost:{config_manager.config.port}/v1/messages")
    print(f"🔌 Responses 兼容接口: http://localhost:{config_manager.config.port}/v1/responses")
    print("=" * 60)
    # 首次启动自动初始化 Schema 并无损迁移存量 JSON 资产
    try:
        from app.db.migrator import auto_migrate_if_needed
        auto_migrate_if_needed()
    except Exception as e:
        print(f"[DB-Init] 数据库初始化或迁移失败（已忽略）: {e}")
    ensure_default_key()
    try:
        from app.core.security import sanitize_key_bindings
        cleaned = sanitize_key_bindings()
        if cleaned > 0:
            print(f"[Startup] 🧹 已完成僵尸号归属清洗，共清理 {cleaned} 条隔离垃圾号绑定")
    except Exception as e:
        print(f"[Startup] 清洗僵尸绑定异常（已忽略）: {e}")

    # 会话保活：每 N 分钟让前端 silent_refresh 续期 token，避免走到 40003 后
    # 再登录被官方回 biz_code=14（设备信任随会话失效，回放 profile 也救不回来）
    try:
        from app.engine import keepalive
        keepalive.start()
    except Exception as e:
        print(f"[Startup] 会话保活启动异常（已忽略）: {e}")

    yield

    # T2/T25: last_used_at 与按 key 用量都改为节流落盘（30s），
    # 重启前各补一次，避免丢掉最后的观测数据
    flush_pending()
    try:
        from app.engine.key_usage import key_usage_engine
        key_usage_engine.flush(force=True)
    except Exception as e:
        print(f"[Shutdown] 刷新 key 用量失败（已忽略）: {e}")
    # T1: 会话绑定同样是节流落盘。重启前补一次 ——
    # 否则最近 30 秒内建立的会话亲和会在重启后全部丢失。
    try:
        from app.engine.session_tree import session_tracker
        session_tracker.flush_bindings(force=True)
    except Exception as e:
        print(f"[Shutdown] 刷新会话绑定失败（已忽略）: {e}")


app = FastAPI(
    title="Anubis DeepSeek Gateway",
    description="High-resilience OpenAI/Anthropic gateway with session affinity and anti-detection pipeline",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件挂载
STATIC_DIR = Path(__file__).resolve().parent / "app" / "static"
if STATIC_DIR.exists():
    app.mount("/admin/static", StaticFiles(directory=str(STATIC_DIR)), name="admin_static")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 路由挂载
app.include_router(auth_router)
app.include_router(solver_proxy_router)
app.include_router(admin_router)
app.include_router(openai_router)
app.include_router(claude_router)
app.include_router(responses_router)

# Admin SPA 静态产物挂载与回退
ADMIN_DIST = Path(__file__).resolve().parent / "frontend" / "dist"
if ADMIN_DIST.exists():
    from fastapi import Request
    from fastapi.responses import FileResponse, JSONResponse

    assets_dir = ADMIN_DIST / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="root_assets")

    HTML_NO_CACHE_HEADERS = {
        "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    @app.get("/login", include_in_schema=False)
    @app.get("/admin/{subpath:path}", include_in_schema=False)
    async def serve_admin_spa(subpath: str = ""):
        return FileResponse(str(ADMIN_DIST / "index.html"), headers=HTML_NO_CACHE_HEADERS)


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=config_manager.config.host,
        port=config_manager.config.port,
        reload=False,
    )
