from __future__ import annotations

import json
import logging
import queue
import threading
from collections import defaultdict

from flask import Blueprint, Response, current_app, request, stream_with_context
from flask_login import login_required

log = logging.getLogger(__name__)
sse_bp = Blueprint("sse", __name__)


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, list[queue.Queue]] = defaultdict(list)

    def subscribe(self, manifest_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subs[manifest_id].append(q)
        return q

    def unsubscribe(self, manifest_id: str, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subs[manifest_id].remove(q)
            except ValueError:
                pass

    def publish(self, manifest_id: str, event: dict) -> None:
        with self._lock:
            qs = list(self._subs.get(manifest_id, []))
        dead: list[queue.Queue] = []
        for q in qs:
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(q)
        if dead:
            with self._lock:
                for q in dead:
                    try:
                        self._subs[manifest_id].remove(q)
                    except ValueError:
                        pass


@sse_bp.route("/api/events")
@login_required
def stream():
    manifest_id = request.args.get("manifest", "")
    if not manifest_id:
        return {"error": "manifest parameter required"}, 400

    event_bus: EventBus = current_app.extensions["event_bus"]
    q = event_bus.subscribe(manifest_id)

    def generate():
        try:
            while True:
                try:
                    event = q.get(timeout=20)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            event_bus.unsubscribe(manifest_id, q)

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
