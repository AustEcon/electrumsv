import json
import logging
from json import JSONDecodeError

from aiohttp import web

routes = web.RouteTableDef()  # restapi.class_to_instance_methods() converts handlers


class DefaultEndpoints:

    def __init__(self):
        self.logger = logging.getLogger("default-endpoints")

    async def _decode_request(self, request):
        """Request validation"""
        if not request.content.is_eof():
            return {}
        body = await request.content.read()
        try:
            request_body = json.loads(body.decode('utf-8'))
        except JSONDecodeError as e:
            fault_message = "JSONDecodeError: " + str(e)
            response_obj = {'message': fault_message}
            self.logger.error(response_obj)
            return web.json_response(data=response_obj, status=400)
        return request_body

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
