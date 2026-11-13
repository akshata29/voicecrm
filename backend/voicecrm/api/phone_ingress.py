"""The phone ingress: a second, tiny listener that serves ONLY the phone webhooks.

The VoiceCRM API has no sign-in (it is a local demo app), so it must never be put on the internet. Azure, however, has to
reach the webhooks. This listener is that door: it runs in the same process (so it shares the pipeline), binds to
localhost, serves nothing but `/api/v1/webhooks/phone/*`, has no docs and no access log (the Event Grid URL carries a
secret), and is the only port a dev tunnel should forward.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import uvicorn
from fastapi import FastAPI
from uvicorn.protocols.http.h11_impl import H11Protocol

from .routes import phone_webhooks

if TYPE_CHECKING:
    from ..container import Container

logger = logging.getLogger(__name__)
_uvicorn = logging.getLogger("uvicorn.error")            # where uvicorn writes its own messages, and what shows at INFO
_in_ingress: contextvars.ContextVar[bool] = contextvars.ContextVar("voicecrm_phone_ingress", default=False)


def create_ingress_app(container: Container) -> FastAPI:
    app = FastAPI(title="VoiceCRM phone ingress", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.container = container
    app.include_router(phone_webhooks.router, prefix="/api/v1")
    return app


class _QuietConfig(uvicorn.Config):
    """uvicorn.Config rewrites the logging of the whole process when it is created (the levels and handlers of the shared
    ``uvicorn.*`` loggers). The main server owns those: an embedded one that touched them would silence its "Application
    startup complete" and its access log, and the app would look hung."""

    def configure_logging(self) -> None:
        return None


class _NoAccessLog(H11Protocol):
    """No access log for the ingress, whatever the shared logger does: its URLs carry the webhook secret in the query string."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.access_log = False


class _MainServerSpeaks(logging.Filter):
    """The embedded server would repeat uvicorn's banner ("Started server process", "Uvicorn running on", "Shutting down"),
    and the console would look as if two apps had started. Only its warnings and errors (a port that cannot be bound) show;
    the ingress announces itself in one line instead. It tells the two apart by the task that is logging, not by the text."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (_in_ingress.get() and record.levelno < logging.WARNING)


_MAIN_SERVER_SPEAKS = _MainServerSpeaks()


class _EmbeddedServer(uvicorn.Server):
    """A uvicorn server that runs inside another program: it must not take over Ctrl+C from the main server."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class IngressServer:
    def __init__(self, app: FastAPI, *, host: str, port: int) -> None:
        self._server = _EmbeddedServer(_QuietConfig(app, host=host, port=port, http=_NoAccessLog, lifespan="off"))
        self._task: asyncio.Task[None] | None = None
        self.host, self.port = host, port
        _uvicorn.addFilter(_MAIN_SERVER_SPEAKS)           # one shared instance: adding it again changes nothing

    async def _serve(self) -> None:
        _in_ingress.set(True)                             # this task's own context: the main server's is untouched
        try:
            await self._server.serve()
        except SystemExit:                    # uvicorn exits the process when the port cannot be bound; the app must live on
            logger.debug("the phone ingress server asked to exit")
        except Exception:
            logger.exception("the phone ingress stopped unexpectedly")

    async def start(self, *, wait_seconds: float = 10.0) -> bool:
        """Starts listening. Returns False (and logs why) when the port cannot be used."""
        self._task = asyncio.create_task(self._serve(), name="phone-ingress")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds
        while not self._server.started:
            if self._task.done() or loop.time() > deadline:
                logger.error("the phone ingress could not start on %s:%s (is the port already in use?)", self.host, self.port)
                await self.stop()
                return False
            await asyncio.sleep(0.05)
        _uvicorn.info("Phone ingress listening on http://%s:%s (tunnel this port; it serves only the phone webhooks)",
                      self.host, self.port)
        return True

    async def stop(self) -> None:
        if self._task is None:
            return
        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, 5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        except Exception:
            logger.debug("phone ingress did not stop cleanly", exc_info=True)
        self._task = None
