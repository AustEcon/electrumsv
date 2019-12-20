import logging
import os
from typing import Optional, Union, List

import aiorpcx
from aiohttp import web
from electrumsv.wallet import Abstract_Wallet, ParentWallet
from .logs import logs
from .app_state import app_state
from .restapi import Fault, bad_request, Errors, decode_request

class DefaultEndpoints:

    routes = web.RouteTableDef()

    def __init__(self):
        self.all_wallets = None
        self.logger = logs.get_logger("restapi-default-endpoints")
        self.wallets_path = os.path.join(app_state.config.electrum_path(), "wallets")

    # ----- External API ----- #

    @routes.get("/")
    async def status(self, request):
        return web.json_response({
            "status": "success",
        })

    @routes.get("/v1/{network}/ping")
    async def rest_ping(self, request):
        return web.json_response({
            "value": "pong"
        })
