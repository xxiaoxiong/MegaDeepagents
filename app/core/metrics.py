"""Lightweight application metrics without external dependencies.

Exposes a Prometheus-compatible text format at ``/metrics`` so that any
scrape-based monitoring system (Prometheus, Grafana Agent, Datadog) can
collect counters and gauges without adding ``prometheus_client`` to the
runtime image.

Thread-safe: all mutations go through a single ``threading.Lock``.
The cardinality is bounded by the label sets defined below, so the text
payload stays small even in long-running processes.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from app.core.logging import logger


class _Counter:
    """A monotonically increasing counter, optionally labelled."""

    __slots__ = ("_name", "_help", "_labels", "_lock", "_values")

    def __init__(self, name: str, help: str, label_names: tuple[str, ...] = ()) -> None:
        self._name = name
        self._help = help
        self._labels = label_names
        self._lock = threading.Lock()
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: Any) -> None:
        key = tuple(str(labels.get(ln, "")) for ln in self._labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def render(self) -> list[str]:
        lines = [f"# HELP {self._name} {self._help}", f"# TYPE {self._name} counter"]
        with self._lock:
            for key, value in sorted(self._values.items()):
                if self._labels:
                    label_str = ",".join(
                        f'{ln}="{kv}"' for ln, kv in zip(self._labels, key)
                    )
                    lines.append(f'{self._name}{{{label_str}}} {value}')
                else:
                    lines.append(f"{self._name} {value}")
        return lines


class _Gauge:
    """A gauge that can go up and down."""

    __slots__ = ("_name", "_help", "_labels", "_lock", "_values")

    def __init__(self, name: str, help: str, label_names: tuple[str, ...] = ()) -> None:
        self._name = name
        self._help = help
        self._labels = label_names
        self._lock = threading.Lock()
        self._values: dict[tuple[str, ...], float] = {}

    def set(self, value: float, **labels: Any) -> None:
        key = tuple(str(labels.get(ln, "")) for ln in self._labels)
        with self._lock:
            self._values[key] = value

    def inc(self, amount: float = 1.0, **labels: Any) -> None:
        key = tuple(str(labels.get(ln, "")) for ln in self._labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels: Any) -> None:
        self.inc(-amount, **labels)

    def render(self) -> list[str]:
        lines = [f"# HELP {self._name} {self._help}", f"# TYPE {self._name} gauge"]
        with self._lock:
            for key, value in sorted(self._values.items()):
                if self._labels:
                    label_str = ",".join(
                        f'{ln}="{kv}"' for ln, kv in zip(self._labels, key)
                    )
                    lines.append(f'{self._name}{{{label_str}}} {value}')
                else:
                    lines.append(f"{self._name} {value}")
        return lines


class _Histogram:
    """A simple histogram with fixed buckets for latency tracking."""

    __slots__ = ("_name", "_help", "_buckets", "_lock", "_counts", "_sum", "_total")

    def __init__(self, name: str, help: str, buckets: tuple[float, ...] = ()) -> None:
        self._name = name
        self._help = help
        self._buckets = buckets or (0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0, 30.0)
        self._lock = threading.Lock()
        self._counts: list[int] = [0] * (len(self._buckets) + 1)
        self._sum: float = 0.0
        self._total: int = 0

    def observe(self, value: float) -> None:
        with self._lock:
            self._sum += value
            self._total += 1
            for i, bound in enumerate(self._buckets):
                if value <= bound:
                    self._counts[i] += 1
                    return
            self._counts[-1] += 1

    def render(self) -> list[str]:
        lines = [f"# HELP {self._name} {self._help}", f"# TYPE {self._name} histogram"]
        with self._lock:
            cumulative = 0
            for i, bound in enumerate(self._buckets):
                cumulative += self._counts[i]
                lines.append(f'{self._name}_bucket{{le="{bound}"}} {cumulative}')
            cumulative += self._counts[-1]
            lines.append(f'{self._name}_bucket{{le="+Inf"}} {cumulative}')
            lines.append(f"{self._name}_sum {self._sum}")
            lines.append(f"{self._name}_count {self._total}")
        return lines


# ===== Metric instances =====

runs_total = _Counter(
    "megadeepagents_runs_total",
    "Total runs by final status",
    label_names=("status",),
)
active_runs = _Gauge(
    "megadeepagents_active_runs",
    "Currently active (non-terminal) runs",
)
tasks_total = _Counter(
    "megadeepagents_tasks_total",
    "Total tasks by final status",
    label_names=("status",),
)
agents_active = _Gauge(
    "megadeepagents_active_agents",
    "Currently registered agent instances",
)
http_requests_total = _Counter(
    "megadeepagents_http_requests_total",
    "HTTP requests by method, path template and status code",
    label_names=("method", "path", "status"),
)
http_request_duration = _Histogram(
    "megadeepagents_http_request_duration_seconds",
    "HTTP request latency in seconds",
)
llm_calls_total = _Counter(
    "megadeepagents_llm_calls_total",
    "LLM invocations by status",
    label_names=("status",),
)

_all_metrics = (
    runs_total, active_runs, tasks_total, agents_active,
    http_requests_total, http_request_duration, llm_calls_total,
)


def render_prometheus() -> str:
    """Render all metrics in Prometheus text exposition format."""
    lines: list[str] = []
    for metric in _all_metrics:
        lines.extend(metric.render())
    lines.append("")
    lines.append(f'# Process metrics generated at {time.time():.3f}')
    return "\n".join(lines)
