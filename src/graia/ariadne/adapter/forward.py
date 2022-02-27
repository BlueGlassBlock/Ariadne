"""正向 Adapter, 作为客户端连接至 mirai-api-http"""

import asyncio
import json
from asyncio import CancelledError, Future, Task
from typing import Any, List, Optional, Tuple, Union

import ujson
from aiohttp import (
    ClientConnectionError,
    ClientSession,
    ClientWebSocketResponse,
    FormData,
    WebSocketError,
    WSMsgType,
)
from graia.broadcast import Broadcast
from loguru import logger
from yarl import URL

from ..exception import InvalidSession
from ..model import CallMethod, DatetimeEncoder, MiraiSession
from ..util import yield_with_timeout
from . import Adapter
from .util import SyncIDManager, validate_response


class HttpAdapter(Adapter):
    """
    仅使用正向 HTTP 的适配器, 采用短轮询接收事件/消息.
    不推荐.
    """

    def __init__(
        self,
        broadcast: Broadcast,
        mirai_session: MiraiSession,
        fetch_interval: float = 0.5,
        count: int = 10,
    ) -> None:
        super().__init__(broadcast, mirai_session)
        self.fetch_interval = fetch_interval
        self.count = count

    async def authenticate(self) -> None:
        if not self.mirai_session.single_mode and not self.mirai_session.session_key:
            async with self.session.post(
                self.mirai_session.url_gen("verify"),
                data=ujson.dumps({"verifyKey": self.mirai_session.verify_key}),
            ) as response:
                response.raise_for_status()
                session_key: dict = (await response.json())["session"]
            async with self.session.post(
                self.mirai_session.url_gen("bind"),
                data=ujson.dumps({"sessionKey": session_key, "qq": self.mirai_session.account}),
            ) as response:
                response.raise_for_status()
                validate_response(await response.json())
            self.mirai_session.session_key = session_key

    async def fetch_cycle(self) -> None:
        await self.authenticate()
        async with ClientSession() as session:
            self.session = session
            while self.running:
                await asyncio.sleep(self.fetch_interval)
                async with self.session.get(
                    URL(self.mirai_session.url_gen("fetchMessage")).with_query(
                        {"sessionKey": self.mirai_session.session_key, "count": self.count}
                    )
                ) as response:
                    response.raise_for_status()
                    resp_json: dict = await response.json()
                    resp: List[dict] = validate_response(resp_json)
                for data in resp:
                    event = self.build_event(data)
                    await self.event_queue.put(event)
            self.mirai_session.session_key = None

    async def call_cycle(self) -> None:
        async for call in yield_with_timeout(self.call_queue.get, lambda: self.running):
            if not any(
                [
                    self.mirai_session.session_key,
                    self.mirai_session.single_mode,
                    call.meta and not getattr(self, "route", None),
                ]
            ):
                await self.call_queue.put(call)
                try:
                    await self.authenticate()
                except ClientConnectionError as e:
                    logger.error(e.__class__.__name__)
                    await asyncio.sleep(3)
                continue
            data = call.data
            action = call.action
            method = call.method
            if method in (CallMethod.GET, CallMethod.RESTGET):
                if isinstance(data, str):
                    data = json.loads(data)
                async with self.session.get(
                    URL(self.mirai_session.url_gen(action)).with_query(data)
                ) as response:
                    response.raise_for_status()
                    resp_json: dict = await response.json()

            elif method in (CallMethod.POST, CallMethod.RESTPOST):
                if not isinstance(data, str):
                    data = json.dumps(data, cls=DatetimeEncoder)
                async with self.session.post(self.mirai_session.url_gen(action), data=data) as response:
                    response.raise_for_status()
                    resp_json: dict = await response.json()

            else:  # MULTIPART
                if isinstance(data, FormData):
                    form = data
                elif isinstance(data, dict):
                    form = FormData(quote_fields=False)
                    for k, v in data.items():
                        v: Union[str, bytes, Tuple[Any, dict]]
                        if isinstance(v, tuple):
                            form.add_field(k, v[0], **v[1])
                        else:
                            form.add_field(k, v)
                async with self.session.post(self.mirai_session.url_gen(action), data=form) as response:
                    response.raise_for_status()
                    resp_json: dict = await response.json()

            val = validate_response(resp_json)
            if isinstance(val, Exception):
                if isinstance(val, InvalidSession):
                    self.mirai_session.session_key = None
                call.future.set_exception(val)
            else:
                call.future.set_result(val)


class WebsocketAdapter(Adapter):
    """
    正向 Websocket 适配器.
    """

    def __init__(
        self, broadcast: Broadcast, mirai_session: MiraiSession, ping: bool = True, log: bool = False
    ) -> None:
        super().__init__(broadcast, mirai_session)
        self.ping = ping
        self.ping_task: Optional[Task] = None
        self.websocket: Optional[ClientWebSocketResponse] = None
        self.query_dict = {"verifyKey": mirai_session.verify_key}
        self.id_manager = SyncIDManager()
        self.log = log
        if not mirai_session.single_mode:
            self.query_dict["qq"] = mirai_session.account

    async def ws_ping(self, interval: float = 30.0) -> None:
        """向 Mirai API HTTP 的 WebsocketAdapter 循环发送 ping.

        Args:
            interval (float, optional): ping 间隔 (s). 默认 30.0.
        """
        while self.running:
            try:
                try:
                    await self.websocket.ping()
                    if self.log:
                        logger.debug("websocket: ping")
                except Exception as e:
                    logger.exception(f"websocket: ping failed: {e!r}")
                else:
                    if self.log:
                        logger.debug(f"websocket: ping success, delay {interval}s")
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                if self.log:
                    logger.debug("websocket: pinger exit")
                break

    async def call_cycle(self):
        async for call in yield_with_timeout(self.call_queue.get, lambda: self.running):
            if (
                not any([self.mirai_session.session_key, self.mirai_session.single_mode, call.meta])
                or not self.websocket
            ):
                await self.call_queue.put(call)
                continue

            sync_id: int = self.id_manager.allocate(call.future)
            content = {
                "syncId": str(sync_id),
                "command": call.action.replace("/", "_"),
                "content": call.data,
            }
            if call.method == CallMethod.RESTGET:
                content["subCommand"] = "get"
            elif call.method == CallMethod.RESTPOST:
                content["subCommand"] = "update"
            elif call.method == CallMethod.MULTIPART:
                self.id_manager.free(
                    sync_id,
                    NotImplementedError(f"Unsupported operation for WebsocketAdapter: {call.method}"),
                    Future.set_exception,
                )
            await self.websocket.send_str(json.dumps(content, cls=DatetimeEncoder))

    async def fetch_cycle(self) -> None:
        self.running = True
        async with ClientSession() as session:
            self.session = session
            async with self.session.ws_connect(
                str(URL(self.mirai_session.url_gen("all")).with_query(self.query_dict)),
                autoping=False,
            ) as connection:
                logger.info("websocket: connected")
                self.websocket = connection

                if self.ping:
                    self.ping_task = self.broadcast.loop.create_task(
                        self.ws_ping(), name="ariadne_adapter_ws_ping"
                    )
                    logger.info("websocket: ping task created")

                try:
                    async for ws_message in yield_with_timeout(connection.receive, lambda: self.running):
                        if ws_message.type is WSMsgType.TEXT:
                            raw_data: dict = ujson.loads(ws_message.data)
                            sync_id: int = int(raw_data["syncId"] or -1)
                            data: dict = raw_data["data"]
                            if "session" in data:
                                self.mirai_session.session_key = data["session"]
                                continue
                            if not self.id_manager.free(sync_id, validate_response(data)):
                                await self.event_queue.put(self.build_event(data))
                        elif ws_message.type is WSMsgType.CLOSED:
                            logger.warning("websocket: connection has been closed.")
                            raise WebSocketError(1, "connection closed")
                        elif ws_message.type is WSMsgType.PONG:
                            if self.log:
                                logger.debug("websocket: received pong")
                        else:
                            logger.warning(f"websocket: unknown message type - {ws_message.type}")
                except CancelledError:
                    pass
                except Exception as e:
                    logger.exception(e)
                finally:
                    if self.ping_task:
                        self.ping_task.cancel()
                        self.ping_task = None
                        if self.log:
                            logger.debug("websocket: ping task complete")
                    logger.info("websocket: disconnected")
                    self.running = False
