#!/usr/bin/env python3
"""Publish job-correlated NVIDIA GPU snapshots for Slurm-web.

This optional collector has no Python dependencies. Run it as a regular user on
compute nodes with a shared output directory mounted on the Slurm-web agent.
"""

import csv
import io
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

OUTPUT = Path(os.environ.get("SLURMWEB_GPU_SNAPSHOT_DIR", "/mnt/ai-data/.slurm-web/gpu"))
INTERVAL = max(1.0, float(os.environ.get("SLURMWEB_GPU_SNAPSHOT_INTERVAL", "2")))


def run(args):
    return subprocess.run(args, text=True, capture_output=True, timeout=8, check=True).stdout


def number(value):
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return 0.0


def process_map():
    result = {}
    fields = "gpu_uuid,pid,process_name,used_memory"
    try:
        rows = csv.reader(io.StringIO(run(["nvidia-smi", f"--query-compute-apps={fields}", "--format=csv,noheader,nounits"])))
    except (subprocess.SubprocessError, FileNotFoundError):
        return result
    for row in rows:
        if len(row) < 4:
            continue
        uuid, pid, name, memory = (part.strip() for part in row[:4])
        job_id = ""
        try:
            cgroup = Path(f"/proc/{int(pid)}/cgroup").read_text()
            match = re.search(r"(?:^|/)job[_-](\d+)(?:/|$)", cgroup)
            if match:
                job_id = match.group(1)
        except (OSError, ValueError):
            pass
        result.setdefault(uuid, []).append({"pid": int(pid), "name": name, "memory_used_mb": number(memory), "job_id": job_id})
    return result


def snapshot():
    processes = process_map()
    fields = "index,uuid,name,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,power.draw,power.limit"
    rows = csv.reader(io.StringIO(run(["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"])))
    gpus = []
    for row in rows:
        if len(row) < 10:
            continue
        index, uuid, name = (part.strip() for part in row[:3])
        gpu_processes = processes.get(uuid, [])
        gpus.append({
            "index": int(index), "uuid": uuid, "name": name,
            "utilization_gpu": number(row[3]), "utilization_memory": number(row[4]),
            "memory_used_mb": number(row[5]), "memory_total_mb": number(row[6]),
            "temperature": number(row[7]), "power_watts": number(row[8]),
            "power_limit_watts": number(row[9]),
            "job_ids": sorted({item["job_id"] for item in gpu_processes if item["job_id"]}),
            "processes": gpu_processes,
        })
    return {"schema": 1, "node": socket.gethostname(), "timestamp": time.time(), "gpus": gpus}


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    target = OUTPUT / f"{socket.gethostname()}.json"
    while True:
        started = time.monotonic()
        try:
            temporary = OUTPUT / f".{socket.gethostname()}.{os.getpid()}.tmp"
            temporary.write_text(json.dumps(snapshot(), separators=(",", ":")) + "\n")
            os.replace(temporary, target)
        except Exception as error:
            print(f"gpu_snapshot_error={error}", flush=True)
        time.sleep(max(0.25, INTERVAL - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
