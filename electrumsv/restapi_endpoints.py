from aiohttp import web

routes = web.RouteTableDef()  # restapi.class_to_instance_methods() converts handlers


class DefaultEndpoints:

    def __init__(self):
        pass

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
