"""BORG_ROUTE — Adaptive Recursive Agent Router.

Entry point for the FastAPI application.
Start with: uvicorn main:app --reload
"""
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: .env already loaded above at module level.
    yield
    # Shutdown: nothing to clean up yet.


app = FastAPI(title="BORG_ROUTE", version="0.1.0", lifespan=lifespan)

# Serve static UI files if the ui/ directory exists.
import os
if os.path.isdir("ui"):
    app.mount("/ui", StaticFiles(directory="ui"), name="ui")


@app.get("/health")
async def health():
    return {"status": "ok"}
