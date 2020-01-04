import asyncio
import json
from base64 import b64decode
from json import JSONDecodeError
from typing import Optional, ClassVar, Dict, Union, Any, Tuple
from aiohttp import web
from aiohttp.web_urldispatcher import UrlDispatcher
import logging
from .util import to_bytes, to_string, constant_time_compare
from .app_state import app_state


def class_to_instance_methods(klass: ClassVar, routes: web.RouteTableDef) -> Union[UrlDispatcher,
                                                                                   object]:
    """Allows @routes.get("/") decorator syntax on instance methods and all of the benefits
    associated with that (regex, dynamic resources / url paths, code readability etc."""
    instance = klass()
    router = UrlDispatcher()
    http_methods = [route.method for route in routes._items]
    handlers = [route.handler.__name__ for route in routes._items]
    paths = [route.path for route in routes._items]

    for path, handler, http_method in zip(paths, handlers, http_methods):
        instance_method = getattr(instance, handler)
        adder = getattr(router, "add_" + http_method.lower())
        adder(path=path, handler=instance_method)
    return router, instance


def get_network_type():
    if app_state.config.get('testnet'):
        return 'test'
    if app_state.config.get('scalingtestnet'):
        return 'stn'
    else:
        return 'main'


def bad_request(code: int, message: str) -> web.Response:
    response_obj = {'code': code,
                    'message': message}
    return web.json_response(data=response_obj, status=400)


def unauthorized(code: int, message: str) -> web.Response:
    response_obj = {'code': code,
                    'message': message}
    return web.json_response(data=response_obj, status=401)


def forbidden(code: int, message: str) -> web.Response:
    response_obj = {'code': code,
                    'message': message}
    return web.json_response(data=response_obj, status=403)


def not_found(code: int, message: str) -> web.Response:
    response_obj = {'code': code,
                    'message': message}
    return web.json_response(data=response_obj, status=404)


def internal_server_error(code: int, message: str) -> web.Response:
    response_obj = {'code': code,
                    'message': message}
    return web.json_response(data=response_obj, status=500)


def good_response(response: Dict) -> web.Response:
    return web.Response(text=json.dumps(response, indent=2), content_type="application/json")


async def decode_request_body(request) -> [Dict[Any, Any]]:
    """Request validation"""
    body = await request.content.read()
    if body == b"":
        error = {'code': Errors.EMPTY_REQUEST_BODY_CODE,
                 'message': Errors.EMPTY_REQUEST_BODY_MESSAGE}
        return error
    try:
        request_body = json.loads(body.decode('utf-8'))
    except JSONDecodeError as e:
        # caller needs to check for 'code' key indicating an error occured
        error = {'code' : Errors.JSON_DECODE_ERROR_CODE,
                 'message': str(e)}
        return error
    return request_body


class Errors:
    """Error codes to facilitate client side troubleshooting of application-specific issues."""
    # http 400 bad requests
    GENERIC_BAD_REQUEST = 40000
    URL_INVALID_NETWORK_CODE = 40001
    URL_NETWORK_MISMATCH_CODE = 40002
    JSON_DECODE_ERROR_CODE = 40003  # message generated from exception
    FAULT_LOAD_BEFORE_GET_CODE = 400004
    EMPTY_REQUEST_BODY_CODE = 40005

    # http 401 unauthorized
    AUTH_CREDENTIALS_MISSING_CODE = 40102
    AUTH_UNSUPPORTED_TYPE_CODE = 40103

    # http 402 - 102xx series
    # http 403 - 103xx series
    AUTH_CREDENTIALS_INVALID_CODE = 40301

    # http 404 not found
    WALLET_NOT_FOUND_CODE = 40401
    HEADER_VAR_NOT_PROVIDED_CODE = 40402
    BODY_VAR_NOT_PROVIDED_CODE = 40403

    AUTH_CREDENTIALS_INVALID_MESSAGE = "Authentication failed (bad credentials)."
    AUTH_CREDENTIALS_MISSING_MESSAGE = "Authentication failed (missing credentials)."
    AUTH_UNSUPPORTED_TYPE_MESSAGE = "Authentication failed (only basic auth is supported)."
    URL_INVALID_NETWORK_MESSAGE = "Only {} networks are supported. You entered: '{}' network."
    URL_NETWORK_MISMATCH_MESSAGE = "Wallet is on '{}' network. You requested: '{}' network."
    WALLET_NOT_FOUND_MESSAGE = "Wallet: '{}' does not exist."
    FAULT_LOAD_BEFORE_GET_MESSAGE = "Must load wallet on the daemon via POST request prior to 'GET'"
    EMPTY_REQUEST_BODY_MESSAGE = "Request body was empty"
    HEADER_VAR_NOT_PROVIDED_MESSAGE = "Required header variable: {} was not provided."
    BODY_VAR_NOT_PROVIDED_MESSAGE = "Required body variable: {} was not provided."


class Fault(Exception):
    """Restapi error class"""

    def __init__(self, code=Errors.GENERIC_BAD_REQUEST, message='Server error'):
        self.code = code
        self.message = message

    def __repr__(self):
        return "Fault(%s, '%s')" % (self.code, self.message)


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
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port, reuse_address=True)
        await site.start()

    async def stop(self):
        await self.runner.cleanup()


class AiohttpServer(BaseAiohttpServer):

    def __init__(self, host: str="localhost", port: int=9999, username: Optional[str]=None,
            password: str=None) -> None:
        super().__init__(host=host, port=port)
        self.username = username
        self.password = password
        self.network = get_network_type()
        self.app.middlewares.extend([web.normalize_path_middleware(append_slash=False,
            remove_slash=True), self.authenticate, self.check_network])

    @web.middleware
    async def check_network(self, request, handler):
        supported_networks = ['main', 'stn', 'test']
        network = request.match_info.get('network', None)

        # paths without {network} are okay
        if network is None:
            response = await handler(request)
            return response

        # check if supported network
        else:
            if network not in supported_networks:
                code =    Errors.URL_INVALID_NETWORK_CODE
                message = Errors.URL_INVALID_NETWORK_MESSAGE.format(supported_networks, network)
                return bad_request(code, message)

        # check if network matches daemon
        if self.network != network:
            code =    Errors.URL_NETWORK_MISMATCH_CODE
            message = Errors.URL_NETWORK_MISMATCH_MESSAGE.format(self.network, network)
            return bad_request(code, message)

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
            return unauthorized(Errors.AUTH_CREDENTIALS_MISSING_CODE,
                                Errors.AUTH_CREDENTIALS_MISSING_MESSAGE)

        (basic, _, encoded) = auth_string.partition(' ')
        if basic != 'Basic':
            return unauthorized(Errors.AUTH_UNSUPPORTED_TYPE_CODE,
                                Errors.AUTH_UNSUPPORTED_TYPE_MESSAGE)

        encoded = to_bytes(encoded, 'utf8')
        credentials = to_string(b64decode(encoded), 'utf8')
        (username, _, password) = credentials.partition(':')
        if not (constant_time_compare(username, self.username)
                and constant_time_compare(password, self.password)):
            await asyncio.sleep(0.050)
            return forbidden(Errors.AUTH_CREDENTIALS_INVALID_CODE,
                                Errors.AUTH_CREDENTIALS_INVALID_MESSAGE)

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

    def register_routes(self, endpoints_class: ClassVar) -> Tuple[UrlDispatcher, object]:
        transformed_router, instance = class_to_instance_methods(klass=endpoints_class,
                                                                 routes=endpoints_class.routes)
        for resource in transformed_router.resources():
            self.app.router.register_resource(resource)
        return transformed_router, instance
