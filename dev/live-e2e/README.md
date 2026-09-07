# Job live WebSocket end-to-end test

Runs the fork's `/job/<id>/live` agent session and gateway relay under real
Gunicorn `gthread` workers, with stub Slurm data, and drives the protocol with
`simple_websocket.Client`: auth (header and first frame), subscribe, cursor
resume, log append/switch/pause/resume, check events, GPU change detection, job
completion, heartbeat, close codes and concurrency.

```console
$ FORK=$PWD PYTHONPATH=$PWD .venv/bin/python dev/live-e2e/live_e2e.py
```

Requires `gunicorn` and `simple-websocket` in the virtualenv.
