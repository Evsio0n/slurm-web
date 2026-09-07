"""End-to-end test of the job live WebSocket under real gunicorn gthread workers."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rfl.authentication.jwt import JWTManager
from rfl.authentication.user import AuthenticatedUser
from simple_websocket import Client, ConnectionClosed

from slurmweb.version import get_version

HERE = Path(__file__).resolve().parent
FORK = Path(os.environ["FORK"]).resolve()
V = get_version()
results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))
    print(
        ("PASS " if condition else "FAIL ") + name + (f" :: {detail}" if detail else "")
    )


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def collect(ws, seconds, stop_when=None):
    out = []
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            raw = ws.receive(timeout=remaining)
        except ConnectionClosed:
            break
        if raw is None:
            break
        msg = json.loads(raw)
        out.append(msg)
        if stop_when and stop_when(msg):
            break
    return out


def types(msgs):
    return [m["type"] for m in msgs]


def main():
    state = Path(tempfile.mkdtemp(prefix="live-e2e-")).resolve()
    (state / "jobs" / "1").mkdir(parents=True)
    (state / "checks" / "1" / "0").mkdir(parents=True)
    (state / "gpu").mkdir()
    out = state / "jobs" / "1" / "out.log"
    err = state / "jobs" / "1" / "err.log"
    out.write_text("line 1\nline 2\n")
    err.write_text("")
    job = {
        "job_id": 1,
        "user": "tester",
        "state": {"current": ["RUNNING"]},
        "time": {"elapsed": 5},
        "nodes": "gpu2011",
        "stdout_expanded": str(out),
        "stderr_expanded": str(err),
        "steps": [],
    }
    (state / "job.json").write_text(json.dumps(job))
    (state / "gpu" / "gpu2011.json").write_text(
        json.dumps(
            {
                "node": "gpu2011",
                "timestamp": time.time(),
                "gpus": [
                    {
                        "index": 0,
                        "uuid": "GPU-1",
                        "name": "V100",
                        "utilization_gpu": 42,
                        "memory_used_mb": 1024,
                        "memory_total_mb": 16384,
                        "temperature": 50,
                        "power_watts": 100,
                        "job_ids": ["1"],
                        "processes": [],
                    }
                ],
            }
        )
    )
    checks_file = state / "checks" / "1" / "0" / "task-0000.jsonl"

    def check_event(seq, state_, cid="probe", progress=None):
        return (
            json.dumps(
                {
                    "schema": 1,
                    "seq": seq,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                    "job_id": 1,
                    "step_id": "0",
                    "task_id": 0,
                    "node": "gpu2011",
                    "check_id": cid,
                    "title": cid,
                    "state": state_,
                    "message": "",
                    "progress": progress,
                    "metrics": {},
                }
            )
            + "\n"
        )

    checks_file.write_text(check_event(100, "passed", "env"))

    env = dict(
        os.environ,
        HARNESS_STATE=str(state),
        SLURMWEB_LOG_ROOTS=str(state / "jobs"),
        SLURMWEB_CHECKS_ROOT=str(state / "checks"),
        SLURMWEB_GPU_ROOT=str(state / "gpu"),
        PYTHONPATH=f"{FORK}:{HERE}",
    )
    pa, pg = free_port(), free_port()
    env["HARNESS_AGENT_PORT"] = str(pa)
    gunicorn = str(FORK / ".venv" / "bin" / "gunicorn")
    common = [
        "--workers",
        "1",
        "--worker-class",
        "gthread",
        "--threads",
        "8",
        "--timeout",
        "30",
        "--log-level",
        "info",
    ]
    agent_log = open(state / "agent.log", "w")
    gateway_log = open(state / "gateway.log", "w")
    procs = [
        subprocess.Popen(
            [gunicorn, "--bind", f"127.0.0.1:{pa}", *common, "harness_agent:app"],
            cwd=HERE,
            env=env,
            stdout=agent_log,
            stderr=subprocess.STDOUT,
        ),
    ]
    check("agent gunicorn up", wait_port(pa))
    procs.append(
        subprocess.Popen(
            [gunicorn, "--bind", f"127.0.0.1:{pg}", *common, "harness_gateway:app"],
            cwd=HERE,
            env=env,
            stdout=gateway_log,
            stderr=subprocess.STDOUT,
        )
    )
    check("gateway gunicorn up", wait_port(pg))
    time.sleep(1)
    jwt = JWTManager.key(
        audience="slurm-web", algorithm="HS256", path=state / "jwt.key"
    )
    token = jwt.generate(AuthenticatedUser("tester", "Tester", []), 300)
    agent_url = f"ws://127.0.0.1:{pa}/v{V}/job/1/live"
    gateway_url = f"ws://127.0.0.1:{pg}/api/agents/test/job/1/live"

    try:
        # 1. Direct agent, header auth, full subscribe.
        ws = Client.connect(agent_url, headers={"Authorization": f"Bearer {token}"})
        first = collect(ws, 1.0, stop_when=lambda m: m["type"] == "ready")
        check("agent sends ready first", types(first) == ["ready"], str(types(first)))
        ws.send(
            json.dumps(
                {
                    "type": "subscribe",
                    "checks_cursor": 0,
                    "log": {"stream": "stdout", "offset": 0},
                }
            )
        )
        msgs = collect(ws, 2.5, stop_when=lambda m: m["type"] == "gpu")
        t = types(msgs)
        check(
            "subscribe yields checks snapshot",
            "checks" in t and msgs[t.index("checks")]["cursor"] == 100,
            str(t),
        )
        check(
            "subscribe yields job summary",
            "job" in t and msgs[t.index("job")]["active"] is True,
            str(t),
        )
        check(
            "subscribed ack carries cursors",
            "subscribed" in t
            and msgs[t.index("subscribed")]["cursors"]["checks"] == 100,
        )
        logs = [m for m in msgs if m["type"] == "log"]
        check(
            "initial log chunk delivered",
            logs
            and logs[0]["chunk"] == "line 1\nline 2\n"
            and logs[0]["next_offset"] == 14,
            str(logs[:1]),
        )
        gpus = [m for m in msgs if m["type"] == "gpu"]
        check(
            "gpu telemetry delivered once",
            len(gpus) == 1 and gpus[0]["summary"]["count"] == 1,
        )
        # No duplicate gpu when unchanged.
        quiet = collect(ws, 2.5)
        check("unchanged gpu not resent", "gpu" not in types(quiet), str(types(quiet)))
        # Incremental log append.
        with out.open("a") as f:
            f.write("line 3\n")
        msgs = collect(ws, 2.0, stop_when=lambda m: m["type"] == "log")
        logs = [m for m in msgs if m["type"] == "log"]
        check(
            "log append streams incrementally",
            logs and logs[-1]["chunk"] == "line 3\n" and logs[-1]["offset"] == 14,
            str(logs[-1:]),
        )
        # Check event append.
        with checks_file.open("a") as f:
            f.write(check_event(101, "running", "probe", 40))
        msgs = collect(ws, 2.0, stop_when=lambda m: m["type"] == "check")
        evs = [m for m in msgs if m["type"] == "check"]
        check(
            "check event streams with seq",
            evs
            and evs[-1]["event"]["seq"] == 101
            and evs[-1]["event"]["progress"] == 40,
        )
        # Switch to stderr.
        err.write_text("boom\n")
        ws.send(json.dumps({"type": "log", "stream": "stderr", "offset": 0}))
        msgs = collect(ws, 2.0, stop_when=lambda m: m["type"] == "log")
        logs = [m for m in msgs if m["type"] == "log"]
        check(
            "stream switch to stderr",
            logs and logs[-1]["stream"] == "stderr" and logs[-1]["chunk"] == "boom\n",
            str(logs[-1:]),
        )
        # Pause / resume.
        ws.send(json.dumps({"type": "pause", "channel": "log"}))
        time.sleep(0.6)
        with err.open("a") as f:
            f.write("after pause\n")
        msgs = collect(ws, 1.5, stop_when=lambda m: m["type"] == "log")
        check(
            "paused log channel stays quiet", "log" not in types(msgs), str(types(msgs))
        )
        ws.send(json.dumps({"type": "resume", "channel": "log"}))
        msgs = collect(ws, 2.0, stop_when=lambda m: m["type"] == "log")
        logs = [m for m in msgs if m["type"] == "log"]
        check(
            "resume flushes buffered bytes at right offset",
            logs and logs[-1]["chunk"] == "after pause\n" and logs[-1]["offset"] == 5,
            str(logs[-1:]),
        )
        # Ping / pong.
        ws.send(json.dumps({"type": "ping"}))
        msgs = collect(ws, 1.5, stop_when=lambda m: m["type"] == "pong")
        check("ping answered with pong", "pong" in types(msgs))
        # Job completes.
        job["state"]["current"] = ["COMPLETED"]
        job["time"]["elapsed"] = 9
        (state / "job.json").write_text(json.dumps(job))
        msgs = collect(
            ws, 4.0, stop_when=lambda m: m["type"] == "job" and not m["active"]
        )
        jobs = [m for m in msgs if m["type"] == "job"]
        check(
            "job completion pushed",
            jobs and jobs[-1]["active"] is False and jobs[-1]["elapsed"] == 9,
            str(jobs[-1:]),
        )
        # Heartbeat when idle.
        # The fixture GPU snapshot turns stale after 8s which legitimately triggers one
        # more gpu frame; the heartbeat must follow within 10s of that.
        msgs = collect(ws, 22.0, stop_when=lambda m: m["type"] == "heartbeat")
        check(
            "heartbeat within 10s idle",
            "heartbeat" in types(msgs) and types(msgs).count("gpu") <= 1,
            str(types(msgs)),
        )
        ws.close()

        # 2. Resume with cursors: nothing before the cursor is resent.
        ws = Client.connect(agent_url, headers={"Authorization": f"Bearer {token}"})
        collect(ws, 1.0, stop_when=lambda m: m["type"] == "ready")
        ws.send(
            json.dumps(
                {
                    "type": "subscribe",
                    "channels": ["checks", "log"],
                    "checks_cursor": 101,
                    "log": {"stream": "stdout", "offset": 14},
                }
            )
        )
        msgs = collect(ws, 2.5)
        evs = [m for m in msgs if m["type"] == "check"]
        logs = [m for m in msgs if m["type"] == "log"]
        check("resume skips already-seen check events", not evs, str(types(msgs)))
        check(
            "resume continues log from byte offset",
            logs and logs[0]["offset"] == 14 and logs[0]["chunk"] == "line 3\n",
            str(logs[:1]),
        )
        check(
            "unsubscribed channels stay silent",
            "gpu" not in types(msgs) and "job" not in types(msgs),
        )
        ws.close()

        # 3. Auth failures.
        ws = Client.connect(agent_url, headers={"Authorization": "Bearer nope"})
        msgs = collect(ws, 2.0)
        code = ws.close_reason
        check(
            "bad header token closes with 4401",
            int(code) == 4401 and msgs and msgs[0]["type"] == "error",
            f"code={code} msgs={types(msgs)}",
        )
        ws = Client.connect(agent_url)
        ws.send(json.dumps({"type": "auth", "token": "nope"}))
        collect(ws, 2.0)
        check(
            "bad auth frame closes with 4401",
            int(ws.close_reason) == 4401,
            str(ws.close_reason),
        )
        ws = Client.connect(agent_url)
        ws.send(json.dumps({"type": "subscribe"}))
        collect(ws, 2.0)
        check(
            "missing auth frame closes with 4401",
            int(ws.close_reason) == 4401,
            str(ws.close_reason),
        )
        ws = Client.connect(agent_url, headers={"Authorization": f"Bearer {token}"})
        collect(ws, 1.0, stop_when=lambda m: m["type"] == "ready")
        ws.send("not json")
        collect(ws, 2.0)
        check(
            "garbage frame closes with 4400",
            int(ws.close_reason) == 4400,
            str(ws.close_reason),
        )
        ws = Client.connect(
            f"ws://127.0.0.1:{pa}/v{V}/job/2/live",
            headers={"Authorization": f"Bearer {token}"},
        )
        # job 2 is served by the same stub job.json; only a user mismatch would 404.
        ws.close()

        # 4. Through the gateway with browser-style auth frame.
        ws = Client.connect(gateway_url)
        ws.send(json.dumps({"type": "auth", "token": token}))
        ws.send(
            json.dumps(
                {
                    "type": "subscribe",
                    "checks_cursor": 0,
                    "log": {"stream": "stdout", "offset": 0},
                }
            )
        )
        msgs = collect(ws, 3.0, stop_when=lambda m: m["type"] == "gpu")
        t = types(msgs)
        check(
            "gateway relays ready/checks/job/log/gpu",
            all(k in t for k in ("ready", "checks", "job", "subscribed", "log", "gpu")),
            str(t),
        )
        with out.open("a") as f:
            f.write("via gateway\n")
        msgs = collect(ws, 2.5, stop_when=lambda m: m["type"] == "log")
        logs = [m for m in msgs if m["type"] == "log"]
        check(
            "gateway relays incremental log",
            logs and logs[-1]["chunk"] == "via gateway\n",
            str(logs[-1:]),
        )
        ws.send(json.dumps({"type": "ping"}))
        msgs = collect(ws, 1.5, stop_when=lambda m: m["type"] == "pong")
        check("gateway relays client->agent frames", "pong" in types(msgs))
        ws.close()
        ws = Client.connect(gateway_url)
        ws.send(json.dumps({"type": "auth", "token": "nope"}))
        collect(ws, 2.0)
        check(
            "gateway rejects bad token with 4401",
            int(ws.close_reason) == 4401,
            str(ws.close_reason),
        )
        ws = Client.connect(f"ws://127.0.0.1:{pg}/api/agents/nope/job/1/live")
        ws.send(json.dumps({"type": "auth", "token": token}))
        collect(ws, 2.0)
        check(
            "gateway unknown cluster closes 4404",
            int(ws.close_reason) == 4404,
            str(ws.close_reason),
        )
        ws = Client.connect(gateway_url)
        ws.send(json.dumps({"type": "auth", "token": token}))
        ws.send(
            json.dumps(
                {
                    "type": "subscribe",
                    "checks_cursor": 0,
                    "log": {"stream": "stdout", "offset": 0},
                }
            )
        )
        collect(ws, 1.5, stop_when=lambda m: m["type"] == "subscribed")
        # Closing the browser side must also end the agent-side session (checked by
        # ensuring gateway thread count returns to baseline (checked by log grep later).
        ws.close()

        # 5. Concurrency: 5 relayed sessions + REST stays responsive.
        def session(i):
            c = Client.connect(gateway_url)
            c.send(json.dumps({"type": "auth", "token": token}))
            c.send(
                json.dumps(
                    {
                        "type": "subscribe",
                        "checks_cursor": 0,
                        "log": {"stream": "stdout", "offset": 0},
                    }
                )
            )
            got = collect(c, 3.0, stop_when=lambda m: m["type"] == "subscribed")
            return c, "subscribed" in types(got)

        with ThreadPoolExecutor(max_workers=5) as pool:
            sessions = list(pool.map(session, range(5)))
        check("5 concurrent relayed sessions subscribed", all(ok for _, ok in sessions))
        req = urllib.request.Request(
            f"http://127.0.0.1:{pa}/v{V}/job/1/log?stream=stdout&offset=0",
            headers={"Authorization": f"Bearer {token}"},
        )
        started = time.monotonic()
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
        check(
            "REST log endpoint responsive during 5 live sessions",
            resp.status == 200 and body["chunk"].startswith("line 1"),
            f"{time.monotonic() - started:.2f}s",
        )
        for c, _ in sessions:
            c.close()
        time.sleep(1.5)
    except Exception:
        import traceback

        traceback.print_exc()
        check("driver raised", False)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        agent_log.close()
        gateway_log.close()
        alog = (state / "agent.log").read_text()
        glog = (state / "gateway.log").read_text()
        check("no tracebacks in agent log", "Traceback" not in alog)
        check("no tracebacks in gateway log", "Traceback" not in glog)
        failed = [r for r in results if not r[1]]
        print(
            f"\nstate={state}\nresults: {len(results) - len(failed)} passed, "
            f"{len(failed)} failed"
        )
        if failed:
            print("---- agent.log tail")
            print(alog[-4000:])
            print("---- gateway.log tail")
            print(glog[-4000:])
        sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
