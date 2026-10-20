"""Readiness identifies this application and this running server instance."""

import uuid

_INSTANCE_ID = uuid.uuid4().hex


def make_health():
    return {"ok": True, "app": __package__, "instance_id": _INSTANCE_ID}
