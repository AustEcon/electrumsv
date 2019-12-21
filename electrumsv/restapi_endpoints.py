import logging
from aiohttp import web


class DefaultEndpoints:

    routes = web.RouteTableDef()  # restapi.class_to_instance_methods() converts handlers

    def __init__(self):
        self.logger = logging.getLogger("default-endpoints")

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
