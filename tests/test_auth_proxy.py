"""The non-vector proxy hop: real Caddy -> Uvicorn -> ASGI."""

from __future__ import annotations

import hashlib
import shutil
import socket
import subprocess
import threading
import time

import httpx
import pytest
import uvicorn

from doorslip.api import create_app
from doorslip.auth import AUTH_HEADER
from doorslip.client import Agent
from doorslip.crypto import generate_keypair, sign
from doorslip.store import Store, connect

CADDY = shutil.which("caddy")


class _RawTargetProbe:
    """Expose the ASGI target for the paths the production app does not route."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        raw_path = scope.get("raw_path", b"")
        if scope["type"] == "http" and raw_path.startswith(b"/proxy-target/"):
            query = scope.get("query_string", b"")
            target = raw_path + (b"?" + query if query else b"")
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", str(len(target)).encode("ascii"))],
                }
            )
            await send({"type": "http.response.body", "body": target})
            return
        await self.app(scope, receive, send)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _frame(method: str, target: bytes, nonce: str, body: bytes = b"") -> bytes:
    """Independent frame construction for the proxy interoperability proof."""
    return b"\n".join(
        (
            b"doorslip-auth-v1",
            method.encode("ascii"),
            target,
            nonce.encode("ascii"),
            hashlib.sha256(body).hexdigest().encode("ascii"),
        )
    )


@pytest.mark.skipif(CADDY is None, reason="Caddy executable is not available")
def test_caddy_preserves_the_signed_target_through_uvicorn_and_asgi(tmp_path):
    db = connect(":memory:")
    app = _RawTargetProbe(create_app(Store(db)))
    upstream_socket = socket.socket()
    upstream_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        upstream_socket.bind(("127.0.0.1", 0))
    except PermissionError:
        upstream_socket.close()
        db.close()
        pytest.skip(
            "loopback socket creation is not permitted; real Caddy/Uvicorn "
            "integration cannot run in this sandbox"
        )
    upstream_socket.listen()
    upstream_port = upstream_socket.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", lifespan="off", access_log=False)
    )
    server_thread = threading.Thread(
        target=server.run, kwargs={"sockets": [upstream_socket]}, daemon=True
    )
    server_thread.start()

    caddy_port = _free_port()
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(
        "{\n\tadmin off\n\tauto_https off\n}\n"
        f"http://127.0.0.1:{caddy_port} {{\n"
        f"\treverse_proxy 127.0.0.1:{upstream_port}\n"
        "}\n",
        encoding="utf-8",
    )
    caddy = subprocess.Popen(
        [CADDY, "run", "--config", str(caddyfile), "--adapter", "caddyfile"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        deadline = time.monotonic() + 10
        base_url = f"http://127.0.0.1:{caddy_port}"
        while True:
            if caddy.poll() is not None:
                output = caddy.communicate()[0]
                pytest.fail(f"Caddy exited before becoming ready:\n{output}")
            try:
                with httpx.Client(base_url=base_url, timeout=0.2) as probe:
                    if probe.get("/stats").status_code == 200:
                        break
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                pytest.fail("Caddy did not become ready within 10 seconds")
            time.sleep(0.05)

        with httpx.Client(base_url=base_url, timeout=5.0) as client:
            for target in (
                b"/proxy-target/%2F?a=1&a=2",
                b"/proxy-target/%2f?a=1&a=2",
            ):
                request = client.build_request("GET", target.decode("ascii"))
                assert request.url.raw_path == target
                assert client.send(request).content == target

            keypair = generate_keypair()
            agent = Agent(
                client,
                handle="proxy@doorslip.test",
                label="integration",
                keypair=keypair,
            )
            agent.register()
            nonce = client.get("/nonce", params={"pubkey": keypair.public_key}).json()[
                "nonce"
            ]
            target = b"/contacts?a=1&a=2&upper=%2F&lower=%2f"
            signature = sign(_frame("GET", target, nonce), keypair.private_key)
            request = client.build_request("GET", target.decode("ascii"))
            assert request.url.raw_path == target
            request.headers[AUTH_HEADER] = f"{keypair.public_key}.{nonce}.{signature}"

            response = client.send(request)

        assert response.status_code == 200, response.text
    finally:
        caddy.terminate()
        try:
            caddy.wait(timeout=5)
        except subprocess.TimeoutExpired:
            caddy.kill()
            caddy.wait(timeout=5)
        server.should_exit = True
        server_thread.join(timeout=5)
        upstream_socket.close()
        db.close()
