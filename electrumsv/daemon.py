#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2015 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import ast
import base64
from typing import Optional, Tuple, Any, Dict
import os
import time

import jsonrpclib
from aiohttp import web

from .restapi import AiohttpServer
from .app_state import app_state
from .commands import known_commands, Commands
from .exchange_rate import FxTask
from .jsonrpc import VerifyingJSONRPCServer
from .logs import logs
from .network import Network
from .simple_config import SimpleConfig
from .storage import WalletStorage
from .util import json_decode, DaemonThread, to_string, random_integer, get_wallet_name_from_path
from .version import PACKAGE_VERSION
from .wallet import ParentWallet


logger = logs.get_logger("daemon")


def get_lockfile(config: SimpleConfig) -> str:
    return os.path.join(config.path, 'daemon')


def remove_lockfile(lockfile: str) -> None:
    logger.debug("removing lockfile")
    try:
        os.unlink(lockfile)
    except OSError:
        pass


def get_fd_or_server(config: SimpleConfig) -> Tuple[Optional[int], Optional[jsonrpclib.Server]]:
    '''Tries to create the lockfile, using O_EXCL to
    prevent races.  If it succeeds it returns the FD.
    Otherwise try and connect to the server specified in the lockfile.
    If this succeeds, the server is returned.  Otherwise remove the
    lockfile and try again.'''
    lockfile = get_lockfile(config)
    while True:
        try:
            return os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644), None
        except OSError:
            pass
        server = get_server(config)
        if server is not None:
            return None, server
        # Couldn't connect; remove lockfile and try again.
        remove_lockfile(lockfile)


def get_server(config: SimpleConfig) -> Optional[jsonrpclib.Server]:
    lockfile = get_lockfile(config)
    while True:
        create_time = None
        server_url = None
        try:
            with open(lockfile) as f:
                (host, port), create_time = ast.literal_eval(f.read())
                rpc_user, rpc_password = get_rpc_credentials(config)
                if rpc_password == '':
                    # authentication disabled
                    server_url = 'http://%s:%d' % (host, port)
                else:
                    server_url = 'http://%s:%s@%s:%d' % (
                        rpc_user, rpc_password, host, port)
                server = jsonrpclib.Server(server_url)
            # Test daemon is running
            server.ping()
            return server
        except ConnectionRefusedError:
            logger.warning("get_server could not connect to the rpc server, is it running?")
        except SyntaxError:
            if os.path.getsize(lockfile):
                logger.exception("RPC server lockfile exists, but is invalid")
            else:
                # Our caller 'get_fd_or_server' has created the empty file before we check.
                logger.warning("get_server could not connect to the rpc server, is it running?")
        except Exception:
            # We do not want the full stacktrace, this will limit it.
            logger.exception("attempt to connect to the RPC server failed")
        if not create_time or create_time < time.time() - 1.0:
            return None
        # Sleep a bit and try again; it might have just been started
        time.sleep(1.0)


def get_rpc_credentials(config: SimpleConfig, is_restapi=False) \
        -> Tuple[Optional[str], Optional[str]]:
    rpc_user = config.get('rpcuser', None)
    rpc_password = config.get('rpcpassword', None)
    if rpc_user is None or rpc_password is None:
        rpc_user = 'user'
        nbits = 128
        pw_int = random_integer(nbits)
        pw_b64 = base64.b64encode(
            pw_int.to_bytes(nbits // 8, 'big'), b'-_')
        rpc_password = to_string(pw_b64, 'ascii')
        config.set_key('rpcuser', rpc_user)
        config.set_key('rpcpassword', rpc_password, save=True)
    elif rpc_password == '' and not is_restapi:
        logger.warning('No password set for RPC API. Access is therefore granted to any users.')
    elif rpc_password == '' and is_restapi:
        logger.warning('No password set for REST API. Access is therefore granted to any users.')
    return rpc_user, rpc_password


class Daemon(DaemonThread):

    def __init__(self, fd, is_gui: bool) -> None:
        super().__init__('daemon')
        app_state.daemon = self
        config = app_state.config
        self.config = config
        if config.get('offline'):
            self.network = None
            self.fx_task = None
        else:
            self.network = Network()
            app_state.fx = FxTask(app_state.config, self.network)
            self.fx_task = app_state.async_.spawn(app_state.fx.refresh_loop)
        self.wallets = {}
        # RPC API - (synchronous) - self.run()
        self.init_server(config, fd, is_gui)

        # REST API - (asynchronous) - async_ loop/thread
        self.init_restapi_server(config, fd)
        self.default_endpoints = {"/"    : self.status,
                                  "/ping": self.rest_ping}
        app_state.dapp_extensions_added = False
        self.configure_restapi_server(self.default_endpoints)

    # ----- Default External API ----- #

    async def status(self, request):
        return web.json_response({
            "status": "success",
        })

    async def rest_ping(self, request):
        return web.json_response({
            "value": "pong"
        })

    # -------------------------------- #

    def configure_restapi_server(self, extension_endpoints: Dict[str, Any]):
        self.rest_server.register_new_endpoints(extension_endpoints)
        self.logger.debug(f"added default rest api endpoints: {list(extension_endpoints.keys())}")
        pass

    def init_restapi_server(self, config: SimpleConfig, fd) -> None:
        host = config.get('rpchost', '127.0.0.1')
        port = 9999  # hard-code until added to config

        # Basic Auth not yet configured. Credentials shared with rpc currently.
        username, password = get_rpc_credentials(config, is_restapi=True)
        try:
            self.rest_server = AiohttpServer(host=host, port=port, username=username,
                password=password, extension_endpoints=None)

        except Exception as e:
            logger.error('Warning: cannot initialize REST server on host %s %s', host, e)
            self.rest_server = None
            # let the rpc server handle the fd for now (until we purge the jsonrpc server from ESV)
            return

    def init_server(self, config: SimpleConfig, fd, is_gui: bool) -> None:
        host = config.get('rpchost', '127.0.0.1')
        port = config.get('rpcport', 0)
        rpc_user, rpc_password = get_rpc_credentials(config)
        try:
            server = VerifyingJSONRPCServer((host, port), logRequests=False,
                                            rpc_user=rpc_user, rpc_password=rpc_password)
        except Exception as e:
            logger.error('Warning: cannot initialize RPC server on host %s %s', host, e)
            self.server = None
            os.close(fd)
            return
        os.write(fd, bytes(repr((server.socket.getsockname(), time.time())), 'utf8'))
        os.close(fd)
        self.server = server
        server.timeout = 0.1
        server.register_function(self.ping, 'ping')
        server.register_function(self.run_gui, 'gui')
        server.register_function(self.run_daemon, 'daemon')
        self.cmd_runner = Commands(self.config, None, self.network)
        for cmdname in known_commands:
            server.register_function(getattr(self.cmd_runner, cmdname), cmdname)
        server.register_function(self.run_cmdline, 'run_cmdline')

    def init_thread_watcher(self) -> None:
        import threading
        import sys
        import traceback

        def _watcher():
            while True:
                for th in threading.enumerate():
                    th_text = str(th)
                    # if "GUI" not in th_text:
                    #     continue
                    print(th)
                    traceback.print_stack(sys._current_frames()[th.ident])
                    print()
                time.sleep(5.0)

        t = threading.Thread(target=_watcher)
        t.setDaemon(True)
        t.start()

    def ping(self) -> bool:
        return True

    def run_daemon(self, config_options: dict) -> Any:
        config = SimpleConfig(config_options)
        sub = config.get('subcommand')
        assert sub in [None, 'start', 'stop', 'status', 'load_wallet', 'close_wallet']
        if sub in [None, 'start']:
            response = "Daemon already running"
        elif sub == 'load_wallet':
            path = config.get_wallet_path()
            wallet = self.load_wallet(path, config.get('password'))
            self.cmd_runner.parent_wallet = wallet
            response = True
        elif sub == 'close_wallet':
            path = WalletStorage.canonical_path(config.get_wallet_path())
            if path in self.wallets:
                self.stop_wallet_at_path(path)
                response = True
            else:
                response = False
        elif sub == 'status':
            if self.network:
                response = self.network.status()
                response.update({
                    'fee_per_kb': self.config.fee_per_kb(),
                    'path': self.config.path,
                    'version': PACKAGE_VERSION,
                    'wallets': {k: w.is_synchronized() for k, w in self.wallets.items()},
                })
            else:
                response = "Daemon offline"
        elif sub == 'stop':
            self.stop()
            response = "Daemon stopped"
        return response

    def run_gui(self, config_options: dict) -> str:
        config = SimpleConfig(config_options)
        if hasattr(app_state, 'windows'):
            config.open_last_wallet()
            path = config.get_wallet_path()
            app_state.app.new_window(path, config.get('url'))
            return "ok"

        return "error: ElectrumSV is running in daemon mode; stop the daemon first."

    def load_wallet(self, wallet_filepath: str, password: Optional[str]) -> ParentWallet:
        # wizard will be launched if we return
        if wallet_filepath in self.wallets:
            wallet = self.wallets[wallet_filepath]
            return wallet
        if not WalletStorage.files_are_matched_by_path(wallet_filepath):
            return
        storage = WalletStorage(wallet_filepath, manual_upgrades=True)
        if storage.is_encrypted():
            if not password:
                return
            storage.decrypt(password)
        if storage.requires_split():
            return
        if storage.requires_upgrade():
            return

        parent_wallet = ParentWallet(storage)
        self.start_wallet(parent_wallet)
        return parent_wallet

    def get_wallet(self, path: str) -> ParentWallet:
        wallet_filepath = WalletStorage.canonical_path(path)
        return self.wallets.get(wallet_filepath)

    def start_wallet(self, parent_wallet: ParentWallet) -> None:
        # We expect the storage path to be exact, including the database extension. So it should
        # match the canonical path used elsewhere.
        self.wallets[parent_wallet.get_storage_path()] = parent_wallet
        parent_wallet.start(self.network)

    def stop_wallet_at_path(self, path: str) -> None:
        wallet_filepath = WalletStorage.canonical_path(path)
        # Issue #659 wallet may already be stopped.
        if wallet_filepath in self.wallets:
            parent_wallet = self.wallets.pop(wallet_filepath)
            parent_wallet.stop()

    def stop_wallets(self):
        for path in list(self.wallets.keys()):
            self.stop_wallet_at_path(path)

    def run_cmdline(self, config_options: dict) -> Any:
        password = config_options.get('password')
        new_password = config_options.get('new_password')
        config = SimpleConfig(config_options)
        cmdname = config.get('cmd')
        cmd = known_commands[cmdname]
        if cmd.requires_wallet:
            path = WalletStorage.canonical_path(config.get_wallet_path())
            parent_wallet = self.wallets.get(path)
            if parent_wallet is None:
                return {'error': 'Wallet "%s" is not loaded. Use "electrum-sv daemon load_wallet"'
                        % get_wallet_name_from_path(path)}
        else:
            parent_wallet = None
        # arguments passed to function
        args = [config.get(x) for x in cmd.params]
        # decode json arguments
        args = [json_decode(i) for i in args]
        # options
        kwargs = {}
        for x in cmd.options:
            kwargs[x] = (config_options.get(x) if x in ['password', 'new_password']
                         else config.get(x))
        cmd_runner = Commands(config, parent_wallet, self.network)
        func = getattr(cmd_runner, cmd.name)
        result = func(*args, **kwargs)
        return result

    def on_stop(self):
        if self.rest_server.is_alive:
            app_state.async_.spawn_and_wait(self.rest_server.stop)
        self.logger.debug("stopped.")

    def run(self) -> None:
        while self.is_running():
            self.server.handle_request() if self.server else time.sleep(0.1)
            if app_state.has_app():
                # running server 'freezes' routes - must register dapp endpoints first
                # https://github.com/aio-libs/aiohttp/issues/3238
                if app_state.dapp_extensions_added and not self.rest_server.is_alive:
                    # This won't run for gui because 'dapp_extensions' never get added.
                    self._restapi_future = app_state.async_.spawn(self.rest_server.launcher)
                    self.rest_server.is_alive = True
                else:
                    continue
        logger.warning("no longer running")
        if self.network:
            logger.warning("wait for network shutdown")
            self.fx_task.cancel()
            app_state.async_.spawn_and_wait(self.network.shutdown_wait)
        self.on_stop()

    def stop(self) -> None:
        logger.warning("stopping")
        super().stop()
        self.stop_wallets()
        remove_lockfile(get_lockfile(self.config))
