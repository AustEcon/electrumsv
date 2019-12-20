import asyncio
from typing import Any, Dict, Optional, ClassVar
from aiohttp import web
import logging

from aiohttp.web_urldispatcher import UrlDispatcher


def class_to_instance_methods(klass: ClassVar, routes: web.RouteTableDef) -> UrlDispatcher:
    """Allows @routes.get("/") decorator syntax on instance methods which keeps contextually
    relevant http method, path and handler together in one place."""
    instance = klass()
    router = UrlDispatcher()
    http_methods = [route.method for route in routes._items]
    handlers = [route.handler.__name__ for route in routes._items]
    paths = [route.path for route in routes._items]

    for path, handler, http_method in zip(paths, handlers, http_methods):
        instance_method = getattr(instance, handler)
        adder = getattr(router, "add_" + http_method.lower())
        adder(path=path, handler=instance_method)
    return router


class BaseAiohttpServer:

    def __init__(self, host: str = "localhost", port: int = 9999):
        self.runner = None
        self.is_alive = False
        self.app = web.Application()
        self.app.on_startup.append(self.on_startup)
        self.app.on_shutdown.append(self.on_shutdown)
        self.host = host
        self.port = port
        self.logger = logging.getLogger("aiohttp-rest-api")

    async def on_startup(self, app):
        self.logger.debug("starting...")

    async def on_shutdown(self, app):
        self.logger.debug("cleaning up...")
        self.is_alive = False
        self.logger.debug("stopped.")

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()

    async def stop(self):
        await self.runner.cleanup()


class AiohttpServer(BaseAiohttpServer):

    def __init__(self, host: str="localhost", port: int=9999, username: Optional[str]=None,
            password: str=None, extension_endpoints: Dict[str, Any]=None) -> None:
        super().__init__(host=host, port=port)
        self.username = username
        self.password = password

    def add_routes(self, routes):
        self.app.router.add_routes(routes)

    async def launcher(self):
        await self.start()
        self.is_alive = True
        self.logger.debug("started on http://%s:%s", self.host, self.port)
        while True:
            await asyncio.sleep(0.5)

    def register_routes(self, endpoints_class: ClassVar, routes: web.RouteTableDef):
        transformed_router = class_to_instance_methods(klass=endpoints_class, routes=routes)
        for resource in transformed_router.resources():
            self.app.router.register_resource(resource)
