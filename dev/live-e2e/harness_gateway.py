"""Minimal gateway-like Flask app exposing the fork's live relay for gunicorn E2E."""
import os
from pathlib import Path

from flask import Flask
from rfl.authentication.jwt import JWTManager

from slurmweb.version import get_version
from slurmweb.views import gateway as views

STATE = Path(os.environ["HARNESS_STATE"])

app = Flask("harness-gateway")
app.jwt = JWTManager.key(
    audience="slurm-web", algorithm="HS256", path=STATE / "jwt.key", create=False
)


class Agent:
    url = f"http://127.0.0.1:{os.environ['HARNESS_AGENT_PORT']}"
    version = get_version()


app.agents = {"test": Agent()}
app.add_url_rule("/api/agents/<cluster>/job/<int:job>/live", view_func=views.job_live, websocket=True)
