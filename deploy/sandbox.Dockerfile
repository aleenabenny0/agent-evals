# The code agent's sandbox (docs/plans/phase-3-code-agent.md, part 3). It holds only boto3, moto and
# the runner, at the versions pinned through uv.lock, and runs as a user with no rights. The network,
# the writable filesystem and the limits are taken away when a container starts (sandbox.py).
#   uv export --only-group sandbox --no-emit-project --format requirements-txt -o deploy/sandbox-requirements.txt
FROM python:3.12-slim
COPY deploy/sandbox-requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --no-deps -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt
COPY src/agent_evals/agents/code/runner.py /runner/runner.py
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sandbox
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1
USER 10001
ENTRYPOINT ["python", "-I", "/runner/runner.py"]
