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
    # Startup: load capability matrix from disk.
    try:
        matrix.load()
    except FileNotFoundError:
        pass  # Matrix will be empty until discovery runs; non-fatal.
    yield
    # Shutdown: nothing to clean up yet.


app = FastAPI(title="BORG_ROUTE", version="0.1.0", lifespan=lifespan)

# Serve static UI files if the ui/ directory exists.
if os.path.isdir("ui"):
    app.mount("/ui", StaticFiles(directory="ui"), name="ui")

# Register routers
from src.api.models import router as models_router  # noqa: E402

app.include_router(models_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
