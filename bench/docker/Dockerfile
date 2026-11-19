# Image for running job B's generated tests with no network (bench/checks.py).
# Build once, with network:   docker build -t borg_bench_py bench/docker
# The tests then run with:    docker run --rm --network none ... borg_bench_py python -m pytest
FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi httpx pytest pyyaml
