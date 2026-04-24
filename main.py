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

# Serve static UI files if the ui/ directory exists. html=True makes
# /ui/ resolve to ui/index.html (BUG-005 fix) so operators landing on the
# root UI path see the directory page instead of a 404.
if os.path.isdir("ui"):
    app.mount("/ui", StaticFiles(directory="ui", html=True), name="ui")

# Register routers
from src.api.models import router as models_router  # noqa: E402
from src.api.chat_completions import router as chat_router  # noqa: E402
from src.api.tasks import router as tasks_router  # noqa: E402
from src.mcp.server import router as mcp_router  # noqa: E402
from src.api.admin import router as admin_router  # noqa: E402
from src.api.logs import router as logs_router  # noqa: E402
from src.api.skills import router as skills_router  # noqa: E402

app.include_router(models_router)
app.include_router(chat_router)
app.include_router(tasks_router)
app.include_router(mcp_router)
app.include_router(admin_router)
app.include_router(logs_router)
app.include_router(skills_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
