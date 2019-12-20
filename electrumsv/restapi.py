import asyncio
import json
from base64 import b64decode
from typing import Optional, ClassVar
from aiohttp import web
import logging
from aiohttp.web_urldispatcher import UrlDispatcher
from .util import to_bytes, to_string, constant_time_compare
from .app_state import app_state

AUTH_CREDENTIALS_INVALID_CODE = 10000
AUTH_CREDENTIALS_MISSING_CODE = 10001
AUTH_UNSUPPORTED_TYPE_CODE = 10002
URL_INVALID_NETWORK_CODE = 10003
URL_NETWORK_MISMATCH_CODE = 10004
AUTH_CREDENTIALS_INVALID_MESSAGE = "Authentication failed (bad credentials)."
AUTH_CREDENTIALS_MISSING_MESSAGE = "Authentication failed (missing credentials)."
AUTH_UNSUPPORTED_TYPE_MESSAGE = "Authentication failed (only basic auth is supported)."
URL_INVALID_NETWORK_MESSAGE = "Only {} networks are supported. You entered: '{}' network."
URL_NETWORK_MISMATCH_MESSAGE = "Wallet is on '{}' network. You requested: '{}' network."


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


def get_network_type():
    if app_state.config.get('testnet'):
        return 'test'
    if app_state.config.get('scalingtestnet'):
        return 'stn'
    else:
        return 'main'


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

    def _bad_request(self, code, message):
        response_obj = {'code': code,
                        'message': message}
        return web.json_response(data=response_obj, status=400)

    def _unauthorized(self, code, message):
        response_obj = {'code': code,
                        'message': message}
        return web.json_response(data=response_obj, status=401)

    def _forbidden(self, code, message):
        response_obj = {'code': code,
                        'message': message}
        return web.json_response(data=response_obj, status=403)

    def _good_response(self, response):
        return web.Response(text=json.dumps(response, indent=2), content_type = "application/json")


class AiohttpServer(BaseAiohttpServer):

    def __init__(self, host: str="localhost", port: int=9999, username: Optional[str]=None,
            password: str=None) -> None:
        super().__init__(host=host, port=port)
        self.username = username
        self.password = password
        self.network = get_network_type()
        self.app.middlewares.extend([self.authenticate, self.check_network])

    @web.middleware
    async def check_network(self, request, handler):
        supported_networks = ['main', 'stn', 'test']
        network = request.match_info.get('network', None)
        # some urls don't have the network in the path
        if network is None:
            response = await handler(request)
            return response
        # check if supported network
        else:
            if network not in supported_networks:
                code = URL_INVALID_NETWORK_CODE
                message = URL_INVALID_NETWORK_MESSAGE.format(supported_networks, network)
                return self._bad_request(code, message)
        # check if current wallet is running on this network
        if self.network != network:
            code = URL_NETWORK_MISMATCH_CODE
            message = URL_NETWORK_MISMATCH_MESSAGE.format(self.network, network)
            return self._bad_request(code, message)

        response = await handler(request)
        return response

    @web.middleware
    async def authenticate(self, request, handler):

        if self.password == '':
            # authentication is disabled
            response = await handler(request)
            return response

        auth_string = request.headers.get('Authorization', None)
        if auth_string is None:
            return self._unauthorized(AUTH_CREDENTIALS_INVALID_CODE,
                                     AUTH_CREDENTIALS_INVALID_MESSAGE)

        (basic, _, encoded) = auth_string.partition(' ')
        if basic != 'Basic':
            return self._unauthorized(AUTH_UNSUPPORTED_TYPE_CODE,
                                     AUTH_UNSUPPORTED_TYPE_MESSAGE)

        encoded = to_bytes(encoded, 'utf8')
        credentials = to_string(b64decode(encoded), 'utf8')
        (username, _, password) = credentials.partition(':')
        if not (constant_time_compare(username, self.username)
                and constant_time_compare(password, self.password)):
            await asyncio.sleep(0.050)
            return self._forbidden(AUTH_CREDENTIALS_INVALID_CODE, AUTH_CREDENTIALS_INVALID_MESSAGE)

        # passed authentication
        response = await handler(request)
        return response

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
