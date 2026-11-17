"""BORG_ROUTE — Adaptive Recursive Agent Router.

Entry point for the FastAPI application.
Start with: uvicorn main:app --reload
"""
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.registry.capability_matrix import CapabilityMatrix

load_dotenv()

# Shared capability matrix — loaded at startup, reused by all API handlers.
matrix = CapabilityMatrix()
try:
    # Loaded at import so the engine, which shares this object, sees the YAML's
    # rows even before the lifespan runs (the lifespan reload is idempotent).
    matrix.load()
except Exception:
    pass  # Missing or bad YAML is non-fatal; the lifespan tries again.

# The engine routes on this same object, so Discover, Get Model Info and the
# checkbox take effect on the next run without a restart (R2).
from src.orchestrator.engine import reload_matrix as _share_matrix  # noqa: E402

_share_matrix(matrix)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: load capability matrix and initialise DB schema.
    try:
        matrix.load()
    except FileNotFoundError:
        pass  # Matrix will be empty until discovery runs; non-fatal.
    from src.db.schema import create_all
    try:
        await create_all()
    except Exception:
        pass  # DB init failure is non-fatal on startup; logged by caller.
    yield
    # Shutdown: nothing to clean up yet.


app = FastAPI(title="BORG_ROUTE", version="0.1.0", lifespan=lifespan)

# Serve static UI files if the ui/ directory exists. The Ask page is the front
# door (work order 4.2): `/`, `/ui`, `/ui/` and the retired chat page
# (`/ui/chat.html`, archived) all redirect to it. These routes are registered
# before the static mount so they win over it.
from fastapi.responses import RedirectResponse  # noqa: E402

ASK_PAGE = "/ui/ask.html"


@app.get("/", include_in_schema=False)
async def site_root():
    return RedirectResponse(ASK_PAGE)


if os.path.isdir("ui"):

    @app.get("/ui", include_in_schema=False)
    @app.get("/ui/", include_in_schema=False)
    @app.get("/ui/chat.html", include_in_schema=False)
    async def ui_root():
        return RedirectResponse(ASK_PAGE)

    app.mount("/ui", StaticFiles(directory="ui", html=True), name="ui")

# Register routers
from src.api.models import router as models_router  # noqa: E402
from src.api.chat_completions import router as chat_router  # noqa: E402
from src.api.tasks import router as tasks_router  # noqa: E402
from src.mcp.server import router as mcp_router  # noqa: E402
from src.api.admin import router as admin_router  # noqa: E402
from src.api.logs import router as logs_router  # noqa: E402
from src.api.skills import router as skills_router  # noqa: E402
from src.api.status import router as status_router  # noqa: E402

app.include_router(models_router)
app.include_router(chat_router)
app.include_router(tasks_router)
app.include_router(mcp_router)
app.include_router(admin_router)
app.include_router(logs_router)
app.include_router(skills_router)
app.include_router(status_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
