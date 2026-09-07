# Copyright (c) 2023 Rackslab
#
# This file is part of Slurm-web.
#
# SPDX-License-Identifier: MIT


class SlurmwebAppRoute:
    def __init__(self, endpoint: str, func, methods=None, websocket: bool = False):
        self.endpoint = endpoint
        self.func = func
        self.methods = methods
        # Werkzeug only matches WebSocket upgrade requests against rules declared
        # with websocket=True, so live endpoints must opt in explicitly.
        self.websocket = websocket
