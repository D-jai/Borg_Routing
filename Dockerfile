# BORG_ROUTE image. Default command serves the app; compose.yaml picks replay or live.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install -e .

COPY main.py ./
COPY data/capability_matrix.yaml data/curated_models.yaml data/skills.yaml ./data/
COPY ui ./ui
COPY demo/runs/*.json ./demo/runs/

# data/ is a named volume in compose: a new volume starts as a copy of this folder.
RUN useradd --create-home borg && chown -R borg:borg /app/data
USER borg

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
