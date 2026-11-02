"""A switch in front of the primary provider, for the failover drill (part 6 of the phase 8 plan).

The service reaches the primary through this small forwarder. Stopping its container takes the
primary away from the service exactly as a network failure would, with no fault code in the service
and no effect on any other provider, and starting it again brings the primary back:

    docker compose -f deploy/docker-compose.yml -f deploy/failover-drill.yml stop primary-gate

It forwards each request unchanged, the API key among its headers, and logs none of them.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import FastAPI, Request, Response

UPSTREAM = "https://api.anthropic.com"
PORT = 9000
# These describe one connection or one encoding of the body, so they do not pass through.
LOCAL_HEADERS = {
    "connection",
    "content-encoding",
    "content-length",
    "host",
    "keep-alive",
    "transfer-encoding",
}


def create_gate(upstream: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(title="primary gate")

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def forward(path: str, request: Request) -> Response:
        reply = await upstream.request(
            request.method,
            f"/{path}",
            params=request.query_params,
            content=await request.body(),
            headers={k: v for k, v in request.headers.items() if k.lower() not in LOCAL_HEADERS},
        )
        kept = {k: v for k, v in reply.headers.items() if k.lower() not in LOCAL_HEADERS}
        return Response(reply.content, status_code=reply.status_code, headers=kept)

    return app


def main() -> None:
    import uvicorn

    logging.getLogger("httpx").setLevel(logging.WARNING)
    upstream = httpx.AsyncClient(base_url=UPSTREAM, timeout=120.0)
    uvicorn.run(create_gate(upstream), host="0.0.0.0", port=PORT, access_log=False)


if __name__ == "__main__":
    main()
