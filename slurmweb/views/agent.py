# Copyright (c) 2023 Rackslab
#
# This file is part of Slurm-web.
#
# SPDX-License-Identifier: MIT

from typing import Any, Tuple
import json
import logging
from pathlib import Path
import time

from flask import Response, current_app, jsonify, abort, request
from rfl.web.tokens import rbac_action, check_jwt

from ..version import get_version
from ..errors import SlurmwebCacheError, SlurmwebMetricsDBError

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


def _managed_job_log(path: str) -> Path:
    """Resolve a job log while preventing access outside configured data roots."""
    candidate = Path(path).resolve(strict=False)
    roots = (Path("/mnt/ai-data/jobs"), Path("/tank/ai/data/jobs"))
    if not any(candidate == root or root in candidate.parents for root in roots):
        abort(403, "Job output is outside managed log roots")
    return candidate


@check_jwt
def job_log(job: int):
    """Return an incremental UTF-8 log chunk for terminal-style live polling."""
    job_data = _authorized_job(job)
    stream = request.args.get("stream", "stdout")
    if stream not in ("stdout", "stderr"):
        abort(400, "stream must be stdout or stderr")
    path = job_data.get(f"{stream}_expanded", "")
    if not path and stream == "stderr":
        path = job_data.get("stdout_expanded", "")
    if not path:
        return jsonify({"path": "", "chunk": "", "offset": 0, "next_offset": 0, "waiting": True})
    output = _managed_job_log(path)
    try:
        offset = max(0, int(request.args.get("offset", 0)))
        limit = max(1024, min(262144, int(request.args.get("limit", 65536))))
    except ValueError:
        abort(400, "offset and limit must be integers")
    if not output.exists():
        return jsonify({"path": str(output), "chunk": "", "offset": offset, "next_offset": offset, "waiting": True})
    size = output.stat().st_size
    rotated = offset > size
    if rotated:
        offset = 0
    with output.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read(limit)
    return jsonify(
        {
            "path": str(output),
            "chunk": chunk.decode("utf-8", errors="replace"),
            "offset": offset,
            "next_offset": offset + len(chunk),
            "size": size,
            "rotated": rotated,
            "waiting": "RUNNING" in job_data.get("state", {}).get("current", []),
        }
    )


@check_jwt
def job_gpus(job: int):
    """Return fresh per-GPU snapshots correlated with the selected Slurm job."""
    job_data = _authorized_job(job)
    nodes = set(job_data.get("nodes", "").replace("[", "").replace("]", "").split(","))
    root = Path("/mnt/ai-data/.slurm-web/gpu")
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
    return jsonify(
        {
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
    )


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
