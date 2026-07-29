"""Prometheus-compatible ``/metrics`` endpoint.

Exposes the lightweight counters and gauges defined in ``app.core.metrics``
in text exposition format.  No external dependency is required; any
Prometheus-compatible scraper can consume the output.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app.core.metrics import render_prometheus

router = APIRouter()


@router.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    return render_prometheus()
