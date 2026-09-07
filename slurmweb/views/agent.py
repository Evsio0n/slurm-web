# Copyright (c) 2023 Rackslab
#
# This file is part of Slurm-web.
#
# SPDX-License-Identifier: MIT

from typing import Any, Tuple
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import time

from flask import Response, current_app, jsonify, abort, request, stream_with_context
from werkzeug.exceptions import HTTPException
from rfl.web.tokens import rbac_action, check_jwt

from ..live import (
    CLOSE_BAD_REQUEST,
    CLOSE_FORBIDDEN,
    CLOSE_INTERNAL,
    CLOSE_NOT_FOUND,
    LIVE_CHANNELS,
    ConnectionClosed,
    LiveSessionError,
    accept_websocket,
    authenticate_websocket,
    finish_websocket,
    receive_json,
    send_json,
)

from ..version import get_version
from ..errors import SlurmwebCacheError, SlurmwebMetricsDBError

from ..slurmrestd import TERMINAL_JOB_STATES
from ..slurmrestd.errors import (
    SlurmrestdNotFoundError,
    SlurmrestdInvalidResponseError,
    SlurmrestConnectionError,
    SlurmrestdAuthenticationError,
    SlurmrestdInternalError,
)


logger = logging.getLogger(__name__)


def racksdb_get_version():
    """Get RacksDB version if available, or return 'N/A' if not installed."""
    try:
        from racksdb.version import get_version

        return get_version()
    except ModuleNotFoundError:
        return "N/A (not installed)"


def version():
    return Response(f"Slurm-web agent v{get_version()}\n", mimetype="text/plain")


def info():
    data = {
        "cluster": current_app.settings.service.cluster,
        "metrics": current_app.settings.metrics.enabled,
        "cache": current_app.settings.cache.enabled,
        "racksdb": {
            "enabled": current_app.racksdb_active,
            "infrastructure": current_app.settings.racksdb.infrastructure,
            "version": racksdb_get_version(),
        },
        "slurmdbd": {
            "jobs_max_hours": current_app.settings.slurmdbd.jobs_max_hours,
        },
        "version": get_version(),
    }
    return jsonify(data)


@check_jwt
def permissions():
    roles, actions = current_app.policy.roles_actions(request.user)
    return jsonify(
        {
            "roles": list(roles),
            "actions": list(actions),
        }
    )


def handle_slurmrestd_errors(func):
    """Wrapper function to handle slurmrestd-related exceptions consistently.

    Handles all slurmrestd exceptions and converts them to appropriate HTTP
    error responses. Also handles SlurmwebCacheError for cache-related issues.
    """

    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except SlurmrestdNotFoundError as err:
            msg = f"URL not found on slurmrestd: {err}"
            logger.error(msg)
            abort(404, msg)
        except SlurmrestdInvalidResponseError as err:
            msg = f"Invalid response from slurmrestd: {err}"
            logger.error(msg)
            abort(500, msg)
        except SlurmrestConnectionError as err:
            msg = f"Unable to connect to slurmrestd: {err}"
            logger.error(msg)
            abort(500, msg)
        except SlurmrestdAuthenticationError as err:
            msg = f"Authentication error on slurmrestd: {err}"
            logger.error(msg)
            abort(401, msg)
        except SlurmrestdInternalError as err:
            msg = f"slurmrestd error: {err.description} ({err.source})"
            if err.error != -1:
                msg += f" [{err.message}/{err.error}]"
            logger.error(msg)
            abort(500, msg)
        except SlurmwebCacheError as err:
            msg = f"Cache error: {str(err)}"
            logger.error(msg)
            abort(500, msg)

    return wrapper


@handle_slurmrestd_errors
def ping():
    """Ping endpoint that discovers slurmrestd API version and returns it along with
    Slurm version information."""
    # Discover and save both API version and Slurm version
    _, slurm_version, api_version = current_app.slurmrestd.discover()

    return jsonify(
        {
            "versions": {
                "slurm": slurm_version,
                "api": api_version,
            },
        }
    )


@handle_slurmrestd_errors
def slurmrest(method: str, *args: Tuple[Any, ...]):
    return getattr(current_app.slurmrestd, method)(*args)


@rbac_action("stats-view")
def stats():
    total = 0
    running = 0

    for job in current_app.slurmrestd.jobs():
        total += 1
        if "RUNNING" in job["job_state"]:
            running += 1

    nodes = 0
    cores = 0
    memory = 0
    gpus = 0
    for node in slurmrest("nodes"):
        nodes += 1
        cores += node["cpus"]
        memory += node["real_memory"]
        gpus += current_app.slurmrestd.node_gres_extract_gpus(node["gres"])
    return jsonify(
        {
            "resources": {
                "nodes": nodes,
                "cores": cores,
                "memory": memory,
                "gpus": gpus,
            },
            "jobs": {"running": running, "total": total},
        }
    )


@check_jwt
def jobs():
    user = request.user
    if current_app.policy.allowed_user_action(user, "jobs-view"):
        own_only = False
    elif current_app.policy.allowed_user_action(user, "jobs-view-own"):
        own_only = True
    else:
        abort(403, "User is not allowed to perform action jobs-view or jobs-view-own")
    node = request.args.get("node")
    if node:
        jobs_list = slurmrest("jobs_by_node", node)
    else:
        jobs_list = slurmrest("jobs_current")
    if own_only:
        login = user.login.lower()
        jobs_list = [
            job for job in jobs_list if job.get("user_name", "").lower() == login
        ]
    return jsonify(jobs_list)


@check_jwt
def job(job: int):
    user = request.user
    if current_app.policy.allowed_user_action(user, "jobs-view"):
        own_only = False
    elif current_app.policy.allowed_user_action(user, "jobs-view-own"):
        own_only = True
    else:
        abort(403, "User is not allowed to perform action jobs-view or jobs-view-own")
    job_data = slurmrest("job", job)
    if own_only and job_data.get("user", "").lower() != user.login.lower():
        abort(404, "Job not found")
    return jsonify(job_data)


def _authorized_job(job: int):
    """Return one job after applying the same ownership checks as the detail view."""
    user = request.user
    if current_app.policy.allowed_user_action(user, "jobs-view"):
        own_only = False
    elif current_app.policy.allowed_user_action(user, "jobs-view-own"):
        own_only = True
    else:
        abort(403, "User is not allowed to perform action jobs-view or jobs-view-own")
    job_data = slurmrest("job", job)
    if own_only and job_data.get("user", "").lower() != user.login.lower():
        abort(404, "Job not found")
    return job_data


# Data roots are overridable so deployments and tests can relocate them without
# patching code. Defaults match the B100 container mounts.
def _log_roots():
    raw = os.environ.get("SLURMWEB_LOG_ROOTS", "/mnt/ai-data/jobs:/tank/ai/data/jobs")
    return tuple(Path(item) for item in raw.split(":") if item)


def _gpu_root() -> Path:
    return Path(os.environ.get("SLURMWEB_GPU_ROOT", "/mnt/ai-data/.slurm-web/gpu"))


def _checks_root() -> Path:
    return Path(os.environ.get("SLURMWEB_CHECKS_ROOT", "/mnt/ai-data/.slurm-web/checks"))


def _managed_job_log(path: str) -> Path:
    """Resolve a job log while preventing access outside configured data roots."""
    candidate = Path(path).resolve(strict=False)
    roots = _log_roots()
    if not any(candidate == root or root in candidate.parents for root in roots):
        abort(403, "Job output is outside managed log roots")
    return candidate


def _job_is_active(job_data) -> bool:
    states = job_data.get("state", {}).get("current", [])
    return not any(state in TERMINAL_JOB_STATES for state in states)


def _read_log_chunk(job_data, stream: str, offset: int, limit: int):
    """Read one incremental UTF-8 chunk of a job output file.

    Shared by the polling REST endpoint and the live WebSocket session so both
    transports expose exactly the same offsets and rotation semantics.
    """
    path = job_data.get(f"{stream}_expanded", "")
    if not path and stream == "stderr":
        path = job_data.get("stdout_expanded", "")
    running = "RUNNING" in job_data.get("state", {}).get("current", [])
    if not path:
        return {
            "path": "",
            "chunk": "",
            "offset": 0,
            "next_offset": 0,
            "waiting": True,
        }
    output = _managed_job_log(path)
    if not output.exists():
        return {
            "path": str(output),
            "chunk": "",
            "offset": offset,
            "next_offset": offset,
            "waiting": True,
        }
    size = output.stat().st_size
    rotated = offset > size
    if rotated:
        offset = 0
    with output.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read(limit)
    return {
        "path": str(output),
        "chunk": chunk.decode("utf-8", errors="replace"),
        "offset": offset,
        "next_offset": offset + len(chunk),
        "size": size,
        "rotated": rotated,
        "waiting": running,
    }


@check_jwt
def job_log(job: int):
    """Return an incremental UTF-8 log chunk for terminal-style live polling."""
    job_data = _authorized_job(job)
    stream = request.args.get("stream", "stdout")
    if stream not in ("stdout", "stderr"):
        abort(400, "stream must be stdout or stderr")
    if request.args.get("raw") in ("1", "true"):
        return _raw_log_response(job, job_data, stream)
    try:
        offset = max(0, int(request.args.get("offset", 0)))
        limit = max(1024, min(262144, int(request.args.get("limit", 65536))))
    except ValueError:
        abort(400, "offset and limit must be integers")
    return jsonify(_read_log_chunk(job_data, stream, offset, limit))


def _log_path(job_data, stream: str) -> str:
    path = job_data.get(f"{stream}_expanded", "")
    if not path and stream == "stderr":
        path = job_data.get("stdout_expanded", "")
    return path


def _raw_log_response(job: int, job_data, stream: str):
    """Stream the whole output file as text for download or raw view."""
    path = _log_path(job_data, stream)
    if not path:
        abort(404, "Job output path is not known yet")
    output = _managed_job_log(path)
    if not output.exists():
        abort(404, "Job output file does not exist")

    def generate():
        with output.open("rb") as handle:
            while True:
                block = handle.read(1 << 16)
                if not block:
                    break
                yield block

    return Response(
        generate(),
        mimetype="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="job-{job}-{stream}.log"',
            "Cache-Control": "no-cache, no-store",
        },
    )


ERROR_LINE = re.compile(
    r"(traceback \(most recent call last\)|\berror\b|exception|cuda out of memory|"
    r"out of memory|oom-kill|killed|segmentation fault|core dumped|assertion|"
    r"srun: error|slurmstepd: error|fatal|panic|command not found|no such file|"
    r"permission denied|non-zero exit)",
    re.IGNORECASE,
)


def _error_excerpt(job_data, max_lines: int = 40, tail_bytes: int = 256 * 1024):
    """Return the most relevant error block from stderr (or stdout) of a job.

    Scans the tail of the file for the last line matching ERROR_LINE and returns
    that line plus what follows, so a Python traceback or a CUDA OOM message is
    shown in full. Line numbers are absolute so the UI can jump to them.
    """
    for stream in ("stderr", "stdout"):
        path = _log_path(job_data, stream)
        if not path:
            continue
        try:
            output = _managed_job_log(path)
        except HTTPException:
            continue
        if not output.exists() or output.stat().st_size == 0:
            continue
        size = output.stat().st_size
        with output.open("rb") as handle:
            start = max(0, size - tail_bytes)
            head_lines = 0
            if start:
                # Count lines before the tail so numbers stay absolute.
                remaining = start
                while remaining:
                    block = handle.read(min(1 << 20, remaining))
                    if not block:
                        break
                    head_lines += block.count(b"\n")
                    remaining -= len(block)
                handle.seek(start)
                partial = handle.readline()  # drop the cut line
                head_lines += 1 if partial.endswith(b"\n") else 0
            tail = handle.read().decode("utf-8", errors="replace")
        lines = tail.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if not lines:
            continue
        hit = None
        for index in range(len(lines) - 1, -1, -1):
            if ERROR_LINE.search(lines[index]):
                hit = index
                # Prefer the start of a traceback block over its last line.
                for back in range(index - 1, max(-1, index - 60), -1):
                    if lines[back].startswith("Traceback (most recent call last)"):
                        hit = back
                        break
                break
        if hit is None:
            hit = max(0, len(lines) - 12)
            matched = False
        else:
            matched = True
        excerpt = lines[hit : hit + max_lines]
        return {
            "stream": stream,
            "path": str(output),
            "line": head_lines + hit + 1,
            "total_lines": head_lines + len(lines),
            "matched": matched,
            "lines": excerpt,
            "truncated": hit + max_lines < len(lines),
        }
    return None


def _exit_summary(exit_code):
    if not isinstance(exit_code, dict):
        return None
    return {
        "status": exit_code.get("status", []),
        "return_code": exit_code.get("return_code", {}).get("number")
        if exit_code.get("return_code", {}).get("set")
        else None,
        "signal": exit_code.get("signal", {}).get("id", {}).get("number")
        if exit_code.get("signal", {}).get("id", {}).get("set")
        else None,
        "signal_name": exit_code.get("signal", {}).get("name", ""),
    }


def _job_diagnostics(job_data):
    """Failure summary for terminal jobs: exit codes, failed steps and error excerpt."""
    states = job_data.get("state", {}).get("current", [])
    exit_code = _exit_summary(job_data.get("exit_code"))
    failed_steps = [
        {
            "id": step.get("step", {}).get("id"),
            "name": step.get("step", {}).get("name"),
            "state": step.get("state", []),
            "exit_code": _exit_summary(step.get("exit_code")),
            "nodes": step.get("nodes", {}).get("range", ""),
        }
        for step in job_data.get("steps", []) or []
        if any(state in ("FAILED", "CANCELLED", "OUT_OF_MEMORY", "TIMEOUT", "NODE_FAIL") for state in step.get("state", []))
        or (step.get("exit_code", {}).get("return_code", {}).get("number") or 0) != 0
    ]
    failed = any(state in ("FAILED", "OUT_OF_MEMORY", "TIMEOUT", "NODE_FAIL", "CANCELLED", "BOOT_FAIL", "DEADLINE", "PREEMPTED") for state in states)
    nonzero = bool(exit_code and ((exit_code.get("return_code") or 0) != 0 or exit_code.get("signal")))
    if not (failed or nonzero or failed_steps):
        return None
    return {
        "states": states,
        "reason": job_data.get("state", {}).get("reason", ""),
        "exit_code": exit_code,
        "derived_exit_code": _exit_summary(job_data.get("derived_exit_code")),
        "failed_steps": failed_steps,
        "excerpt": _error_excerpt(job_data),
    }


@check_jwt
def job_diagnostics(job: int):
    job_data = _authorized_job(job)
    return jsonify(_job_diagnostics(job_data))


def _gpu_telemetry(job: int, job_data):
    """Return fresh per-GPU snapshots correlated with the selected Slurm job."""
    nodes = set(job_data.get("nodes", "").replace("[", "").replace("]", "").split(","))
    root = _gpu_root()
    snapshots = []
    now = time.time()
    for path in sorted(root.glob("*.json")):
        try:
            snapshot = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        # Snapshot files carry exact node names. Keep all files when the Slurm hostlist
        # is compressed; process-level job correlation below remains authoritative.
        if nodes and snapshot.get("node") not in nodes and "[" not in job_data.get("nodes", ""):
            continue
        snapshot["age_seconds"] = round(max(0, now - float(snapshot.get("timestamp", 0))), 1)
        snapshot["stale"] = snapshot["age_seconds"] > 8
        snapshot["gpus"] = [
            gpu for gpu in snapshot.get("gpus", []) if str(job) in gpu.get("job_ids", [])
        ]
        if snapshot["gpus"] or not snapshot["stale"]:
            snapshots.append(snapshot)
    gpus = [gpu for snapshot in snapshots if not snapshot["stale"] for gpu in snapshot["gpus"]]
    return {
        "nodes": snapshots,
        "summary": {
            "count": len(gpus),
            "utilization": round(sum(gpu.get("utilization_gpu", 0) for gpu in gpus) / len(gpus), 1) if gpus else 0,
            "memory_used_mb": sum(gpu.get("memory_used_mb", 0) for gpu in gpus),
            "memory_total_mb": sum(gpu.get("memory_total_mb", 0) for gpu in gpus),
            "power_watts": round(sum(gpu.get("power_watts", 0) for gpu in gpus), 1),
            "temperature_max": max((gpu.get("temperature", 0) for gpu in gpus), default=0),
        },
    }


@check_jwt
def job_gpus(job: int):
    job_data = _authorized_job(job)
    return jsonify(_gpu_telemetry(job, job_data))


def _job_check_events(job: int):
    root = _checks_root() / str(job)
    events = []
    if not root.is_dir():
        return events
    for path in sorted(root.glob("*/task-*.jsonl")):
        try:
            # Keep a runaway task from forcing unbounded API reads.
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 2_000_000))
                if stream.tell():
                    stream.readline()
                for raw in stream:
                    try:
                        event = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        continue
                    if event.get("job_id") != job or not isinstance(event.get("seq"), int):
                        continue
                    events.append(event)
        except OSError:
            continue
    events.sort(key=lambda event: event["seq"])
    return events[-10000:]


def _checks_snapshot(job: int):
    events = _job_check_events(job)
    latest = {}
    for event in events:
        key = (event.get("step_id"), event.get("task_id"), event.get("check_id"))
        latest[key] = dict(event)
    now = time.time()
    for event in latest.values():
        if event.get("state") != "running":
            continue
        timestamp = event.get("timestamp", "")
        try:
            updated = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            continue
        if now - updated > 15:
            event["state"] = "stalled"
            event["stale_seconds"] = round(now - updated, 1)
    checks = sorted(latest.values(), key=lambda event: event["seq"])
    return {
        "job_id": job,
        "cursor": max((event["seq"] for event in events), default=0),
        "generated_at": now,
        "checks": checks,
    }


@check_jwt
def job_checks(job: int):
    _authorized_job(job)
    return jsonify(_checks_snapshot(job))


@check_jwt
def job_check_events(job: int):
    """Stream structured task check events with resumable sequence cursors.

    Kept as a fallback transport for clients that cannot open the WebSocket.
    """
    _authorized_job(job)
    try:
        cursor = max(0, int(request.args.get("cursor", 0)))
    except ValueError:
        abort(400, "cursor must be an integer")

    @stream_with_context
    def generate():
        nonlocal cursor
        started = time.monotonic()
        heartbeat = started
        yield "retry: 1000\n\n"
        while time.monotonic() - started < 30:
            events = [event for event in _job_check_events(job) if event["seq"] > cursor]
            for event in events:
                cursor = max(cursor, event["seq"])
                yield f"id: {cursor}\nevent: check\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
            if time.monotonic() - heartbeat >= 10:
                yield f"event: heartbeat\ndata: {{\"cursor\":{cursor}}}\n\n"
                heartbeat = time.monotonic()
            time.sleep(0.5)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )


def _job_summary(job_data):
    """Compact job state pushed on the live ``job`` channel."""
    steps = []
    for step in job_data.get("steps", []) or []:
        time_ = step.get("time", {})
        steps.append(
            {
                "id": step.get("step", {}).get("id"),
                "name": step.get("step", {}).get("name"),
                "state": step.get("state", []),
                "elapsed": time_.get("elapsed", 0),
                "start": time_.get("start", {}).get("number") if time_.get("start", {}).get("set") else None,
                "end": time_.get("end", {}).get("number") if time_.get("end", {}).get("set") else None,
                "exit_code": _exit_summary(step.get("exit_code")),
                "nodes": step.get("nodes", {}).get("range", ""),
                "node_count": step.get("nodes", {}).get("count", 0),
                "tasks": step.get("tasks", {}).get("count", 0),
            }
        )
    active = _job_is_active(job_data)
    time_ = job_data.get("time", {})
    return {
        "state": job_data.get("state", {}).get("current", []),
        "reason": job_data.get("state", {}).get("reason", ""),
        "elapsed": time_.get("elapsed", 0),
        "start": time_.get("start"),
        "end": time_.get("end"),
        "nodes": job_data.get("nodes", ""),
        "exit_code": _exit_summary(job_data.get("exit_code")),
        "derived_exit_code": _exit_summary(job_data.get("derived_exit_code")),
        "steps": steps,
        "active": active,
        # Diagnostics read log files; only computed once the job is over.
        "diagnostics": None if active else _job_diagnostics(job_data),
    }


class JobLiveSession:
    """Server side of the multiplexed job live session.

    The loop blocks on ``receive`` for one tick, handles any client control
    message, then services every subscribed channel. Each channel keeps its own
    cursor so the client can resume after a reconnect:

    * ``checks``: event sequence number.
    * ``log``: byte offset per stream; only new bytes are ever sent.
    * ``gpu``: latest value only, sent when it changed. Nothing is buffered.
    * ``job``: state/elapsed summary, sent when it changed.
    """

    ACTIVE_TICK = 0.5
    IDLE_TICK = 2.0
    LOG_LIMIT = 131072
    GPU_INTERVAL = 2.0
    JOB_ACTIVE_INTERVAL = 2.0
    JOB_IDLE_INTERVAL = 15.0
    HEARTBEAT_INTERVAL = 10.0

    def __init__(self, ws, job: int, job_data):
        self.ws = ws
        self.job = job
        self.job_data = job_data
        self.channels = set()
        self.checks_cursor = 0
        # One cursor per output stream. Legacy clients subscribe to a single
        # stream; the workbench subscribes to both so tabs switch instantly.
        self.log_streams = {"stdout": {"offset": 0, "path": None}}
        self.log_paused = False
        self.last_gpu = None
        self.last_job = None
        self.next_gpu = 0.0
        self.next_job = time.monotonic() + self.JOB_ACTIVE_INTERVAL
        self.last_sent = time.monotonic()

    # Transport helpers

    def send(self, payload):
        send_json(self.ws, payload)
        self.last_sent = time.monotonic()

    def run(self):
        self.send({"type": "ready", "job_id": self.job, "channels": list(LIVE_CHANNELS)})
        while True:
            tick = self.ACTIVE_TICK if _job_is_active(self.job_data) else self.IDLE_TICK
            message = receive_json(self.ws, tick)
            if message is not None:
                self.handle(message)
            self.service()

    # Client control messages

    def handle(self, message):
        kind = message["type"]
        if kind == "subscribe":
            self.subscribe(message)
        elif kind == "log":
            self.log_streams = self._log_streams_from(message)
            self.log_paused = False
            self.channels.add("log")
        elif kind == "pause":
            if message.get("channel", "log") == "log":
                self.log_paused = True
        elif kind == "resume":
            if message.get("channel", "log") == "log":
                self.log_paused = False
        elif kind == "ping":
            self.send({"type": "pong", "ts": time.time()})
        elif kind == "auth":
            # Already authenticated during the handshake; ignore duplicates.
            pass
        else:
            raise LiveSessionError(CLOSE_BAD_REQUEST, f"unknown message type {kind}")

    def subscribe(self, message):
        channels = message.get("channels") or list(LIVE_CHANNELS)
        if not isinstance(channels, list) or any(ch not in LIVE_CHANNELS for ch in channels):
            raise LiveSessionError(CLOSE_BAD_REQUEST, "unknown channel in subscribe")
        self.channels = set(channels)
        self.checks_cursor = _coerce_offset(message.get("checks_cursor", 0))
        self.log_streams = self._log_streams_from(message.get("log") or {})
        self.log_paused = False
        if "checks" in self.channels:
            snapshot = _checks_snapshot(self.job)
            # A resuming client already holds every event up to its cursor. Send the
            # full snapshot anyway: it is small and it carries stalled states.
            self.checks_cursor = max(self.checks_cursor, snapshot["cursor"])
            self.send({"type": "checks", **snapshot})
        if "job" in self.channels:
            self.push_job(force=True)
        self.next_gpu = 0.0
        self.send(
            {
                "type": "subscribed",
                "channels": sorted(self.channels),
                "cursors": {
                    "checks": self.checks_cursor,
                    "log": {name: state["offset"] for name, state in self.log_streams.items()},
                },
            }
        )

    # Channel servicing

    def service(self):
        if not self.channels:
            return
        now = time.monotonic()
        if now >= self.next_job:
            self.refresh_job()
        if "checks" in self.channels:
            self.push_checks()
        if "log" in self.channels and not self.log_paused:
            self.push_log()
        if "gpu" in self.channels and now >= self.next_gpu:
            self.push_gpu()
            self.next_gpu = now + self.GPU_INTERVAL
        if time.monotonic() - self.last_sent >= self.HEARTBEAT_INTERVAL:
            self.send({"type": "heartbeat", "ts": time.time(), "cursor": self.checks_cursor})

    def refresh_job(self):
        try:
            self.job_data = slurmrest("job", self.job)
        except HTTPException as err:
            self.send({"type": "error", "code": err.code, "message": err.description, "transient": True})
        interval = self.JOB_ACTIVE_INTERVAL if _job_is_active(self.job_data) else self.JOB_IDLE_INTERVAL
        self.next_job = time.monotonic() + interval
        if "job" in self.channels:
            self.push_job()

    def push_job(self, force=False):
        summary = _job_summary(self.job_data)
        if force or summary != self.last_job:
            self.last_job = summary
            self.send({"type": "job", **summary})

    def push_checks(self):
        for event in _job_check_events(self.job):
            if event["seq"] <= self.checks_cursor:
                continue
            self.checks_cursor = event["seq"]
            self.send({"type": "check", "event": event})

    @staticmethod
    def _log_streams_from(message):
        """Accept ``{"streams": {"stdout": 0, "stderr": 0}}`` or legacy ``stream``/``offset``."""
        streams = message.get("streams")
        if streams is None:
            stream = message.get("stream", "stdout")
            streams = {stream: message.get("offset", 0)}
        if not isinstance(streams, dict) or not streams:
            raise LiveSessionError(CLOSE_BAD_REQUEST, "log.streams must map stream names to offsets")
        parsed = {}
        for name, offset in streams.items():
            if name not in ("stdout", "stderr"):
                raise LiveSessionError(CLOSE_BAD_REQUEST, "stream must be stdout or stderr")
            parsed[name] = {"offset": _coerce_offset(offset), "path": None}
        return parsed

    def push_log(self):
        stdout_path = _log_path(self.job_data, "stdout")
        for name, state in self.log_streams.items():
            if name == "stderr" and stdout_path and _log_path(self.job_data, "stderr") == stdout_path:
                # Slurm merges stderr into stdout unless --error is given. Say so once
                # instead of streaming the same file twice.
                if state["path"] != "merged":
                    state["path"] = "merged"
                    self.send({"type": "log", "stream": name, "path": stdout_path, "chunk": "", "offset": 0, "next_offset": 0, "waiting": False, "merged": True})
                continue
            try:
                chunk = _read_log_chunk(self.job_data, name, state["offset"], self.LOG_LIMIT)
            except HTTPException as err:
                self.send({"type": "error", "code": err.code, "message": err.description, "transient": True})
                self.log_paused = True
                return
            changed = chunk["chunk"] or chunk.get("rotated") or chunk["path"] != state["path"]
            if not changed:
                continue
            state["path"] = chunk["path"]
            state["offset"] = chunk["next_offset"]
            self.send({"type": "log", "stream": name, **chunk})

    def push_gpu(self):
        telemetry = _gpu_telemetry(self.job, self.job_data)
        # Only the freshest value matters; drop it entirely when unchanged so idle
        # sessions cost nothing on the wire. age_seconds ticks on every read and is
        # excluded from the comparison; stale flips are still delivered.
        fingerprint = json.dumps(
            [
                {key: value for key, value in node.items() if key != "age_seconds"}
                for node in telemetry["nodes"]
            ],
            sort_keys=True,
        )
        if fingerprint != self.last_gpu:
            self.last_gpu = fingerprint
            self.send({"type": "gpu", **telemetry})


def _coerce_offset(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        raise LiveSessionError(CLOSE_BAD_REQUEST, "cursors must be integers")


def job_live(job: int):
    """Multiplexed live WebSocket for one job: checks, log, gpu and job state."""
    ws = accept_websocket()
    try:
        authenticate_websocket(ws)
        try:
            job_data = _authorized_job(job)
        except HTTPException as err:
            code = {403: CLOSE_FORBIDDEN, 404: CLOSE_NOT_FOUND}.get(err.code, CLOSE_INTERNAL)
            raise LiveSessionError(code, err.description)
        JobLiveSession(ws, job, job_data).run()
    except ConnectionClosed:
        return finish_websocket(ws)
    except LiveSessionError as err:
        logger.info("Live session for job %s closed: %s", job, err.message)
        return finish_websocket(ws, err)
    except Exception as err:  # pragma: no cover - defensive, keeps the worker alive
        logger.exception("Live session for job %s failed", job)
        return finish_websocket(ws, LiveSessionError(CLOSE_INTERNAL, str(err)))
    return finish_websocket(ws)


@check_jwt
def jobs_past():
    user = request.user
    if current_app.policy.allowed_user_action(user, "jobs-view-past"):
        own_only = False
    elif current_app.policy.allowed_user_action(user, "jobs-view-past-own"):
        own_only = True
    else:
        abort(
            403,
            "User is not allowed to perform action "
            "jobs-view-past or jobs-view-past-own",
        )
    settings = current_app.settings.slurmdbd
    if "hours" not in request.args:
        abort(400, "Missing hours query parameter")
    try:
        hours = int(request.args.get("hours"))
    except (TypeError, ValueError):
        abort(400, "Invalid hours query parameter")
    hours = max(1, min(hours, settings.jobs_max_hours))
    # Always fetch all past jobs from slurmrestd (shared cache); own-only filter
    # is applied here so restricted users never receive other users' jobs.
    jobs_list = slurmrest("jobs_past", hours)
    if own_only:
        login = user.login.lower()
        jobs_list = [job for job in jobs_list if job.get("user", "").lower() == login]
    return jsonify(jobs_list)


@rbac_action("nodes-view")
def nodes():
    return jsonify(slurmrest("nodes"))


@rbac_action("nodes-view")
def node(name: str):
    return jsonify(slurmrest("node", name))


@rbac_action("partitions-view")
def partitions():
    return jsonify(slurmrest("partitions"))


@rbac_action("qos-view")
def qos():
    return jsonify(slurmrest("qos"))


@rbac_action("reservations-view")
def reservations():
    return jsonify(slurmrest("reservations"))


@rbac_action("accounts-view")
def accounts():
    return jsonify(slurmrest("accounts"))


@rbac_action("associations-view")
def associations():
    return jsonify(slurmrest("associations"))


@rbac_action("cache-view")
def cache_stats():
    if current_app.cache is None:
        error = "Cache service is disabled, unable to query cache statistics"
        logger.warning(error)
        abort(501, error)
    (cache_hits, cache_misses, total_hits, total_misses) = current_app.cache.metrics()
    return jsonify(
        {
            "hit": {"keys": cache_hits, "total": total_hits},
            "miss": {"keys": cache_misses, "total": total_misses},
        }
    )


@rbac_action("cache-reset")
def cache_reset():
    if current_app.cache is None:
        error = "Cache service is disabled, unable to reset cache"
        logger.warning(error)
        abort(501, error)

    # Reset values in caching service
    current_app.cache.reset()

    # Return fresh values right after reset
    (cache_hits, cache_misses, total_hits, total_misses) = current_app.cache.metrics()
    return jsonify(
        {
            "hit": {"keys": cache_hits, "total": total_hits},
            "miss": {"keys": cache_misses, "total": total_misses},
        }
    )


@check_jwt
def metrics(metric):
    if current_app.metrics_db is None:
        error = "Metrics are disabled, unable to query values"
        logger.warning(error)
        abort(501, error)

    # Dictionnary of metrics and required policy actions associations
    metrics_policy_actions = {
        "nodes": "nodes-view",
        "cores": "nodes-view",
        "gpus": "nodes-view",
        "jobs": "jobs-view",
        "cache": "cache-view",
    }

    # Check metric is supported or send HTTP/404
    if metric not in metrics_policy_actions.keys():
        abort(404, f"Metric {metric} not found")

    # Check permission to request metric or send HTTP/403
    action = metrics_policy_actions[metric]
    if not current_app.policy.allowed_user_action(request.user, action):
        logger.warning(
            "Unauthorized access from user %s to %s metric (missing permission on %s)",
            request.user,
            metric,
            action,
        )
        abort(403, f"Access to {metric} metric not permitted")

    # Send metrics from DB

    try:
        return jsonify(
            current_app.metrics_db.request(metric, request.args.get("range", "hour"))
        )
    except SlurmwebMetricsDBError as err:
        logger.warning(str(err))
        abort(500, str(err))
