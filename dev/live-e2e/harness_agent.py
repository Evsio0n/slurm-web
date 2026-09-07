"""Minimal agent-like Flask app exposing the fork's live views for gunicorn E2E."""

import json
import os
from pathlib import Path

from flask import Flask
from rfl.authentication.jwt import JWTManager

from slurmweb.version import get_version
from slurmweb.views import agent as views

STATE = Path(os.environ["HARNESS_STATE"])

app = Flask("harness-agent")
app.jwt = JWTManager.key(
    audience="slurm-web", algorithm="HS256", path=STATE / "jwt.key", create=True
)


class Policy:
    def allowed_user_action(self, user, action):
        return action == "jobs-view"


class Slurmrestd:
    def job(self, job):
        return json.loads((STATE / "job.json").read_text())


app.policy = Policy()
app.slurmrestd = Slurmrestd()
v = get_version()
app.add_url_rule(f"/v{v}/job/<int:job>/live", view_func=views.job_live, websocket=True)
app.add_url_rule(f"/v{v}/job/<int:job>/log", view_func=views.job_log)
app.add_url_rule(f"/v{v}/job/<int:job>/checks", view_func=views.job_checks)
app.add_url_rule(f"/v{v}/job/<int:job>/gpus", view_func=views.job_gpus)
