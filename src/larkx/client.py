import asyncio
import time
from urllib.parse import urlencode
import requests
import websockets
from loguru import logger


class NetworkError(RuntimeError):
    pass


class HttpError(RuntimeError):
    pass


class SendUnconfirmed(RuntimeError):
    def __init__(self, message: str, cid: str):
        super().__init__(message)
        self.cid = cid


class MessageActionUnconfirmed(RuntimeError):
    pass
from .auth import LarkAuth
from .proto import builders, decoders, proto_pb2 as P
from .proto.ids import generate_access_key, generate_long_request_id, generate_request_cid, generate_request_id


class LarkClient:
    def __init__(self, auth: LarkAuth):
        self.auth = auth
        self.auth.require_valid()
        if not self.auth.app_key or not self.auth.user_id:
            self.auth.validate(fetch_profile=True)
            self.auth.save()

    def build_ws_url(self) -> str:
        device_id = self.auth.device_id
        access_key = generate_access_key(f'2{self.auth.app_key}{device_id}f8a69f1719916z')
        ticket = self.auth.get_ticket()
        params = {
            'accept_encoding': 'gzip',
            'sdk_version': '7.72.8',
            'version_code': '7.72.8',
            'type': 'sdk_main',
            'platform': '',
            'terminal_type': '2',
            'device_model': '',
            'lark_version': '7.72.0',
            'net_status': '5',
            'lark_env': 'online',
            'request_id': generate_request_id(),
            'aid': '1',
            'fpid': '2',
            'device_id': device_id,
            'access_key': access_key,
            'ticket': ticket,
        }
        return f'{self.auth.region.ws_url}?{urlencode(params)}'

    @staticmethod
    def _wrap_frame(packet: P.Packet) -> bytes:
        frame = P.Frame()
        seconds = int(time.time())
        frame.seqid = seconds
        frame.logid = seconds
        frame.service = 1
        frame.method = 1
        entry = P.ExtendedEntry()
        entry.key = 'x-request-time'
        entry.value = str(int(time.time() * 1000))
        frame.headers.append(entry)
        frame.payloadType = 'pb'
        frame.payload = packet.SerializeToString()
        return frame.SerializeToString()

    async def send_ack(self, ws, sid: str):
        pkt = P.Packet()
        pkt.cmd = 1
        pkt.payloadType = 1
        pkt.sid = sid
        pkt.payload = b'\x08\x00'
        await ws.send(self._wrap_frame(pkt))

    async def send_heartbeat(self, ws):
        pkt = P.Packet()
        pkt.cmd = 4
        pkt.payloadType = 1
        await ws.send(self._wrap_frame(pkt))

    async def heartbeat_loop(self, ws, interval=30):
        try:
            while True:
                await asyncio.sleep(interval)
                await self.send_heartbeat(ws)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f'心跳停止: {e}')

    async def connect_websocket(self, on_message):
        url = await asyncio.to_thread(self.build_ws_url)
        async with websockets.connect(url, open_timeout=15) as ws:
            logger.info('WS 已连接')
            heartbeat_task = asyncio.create_task(self.heartbeat_loop(ws))
            chain = asyncio.sleep(0)
            try:
                async for raw in ws:
                    try:
                        _, packet = decoders.parse_ws_frame(raw)
                        if packet.get("sid") is not None:
                            await self.send_ack(ws, packet["sid"])
                        if packet.get("cmd") != 6:
                            continue
                        for msg in decoders.decode_push_messages(raw):
                            if msg.get("from_id"):
                                chain = asyncio.create_task(self._chain(chain, msg, on_message))
                    except Exception as e:
                        logger.debug(f'跳过异常帧: {e}')
            finally:
                heartbeat_task.cancel()
                if asyncio.iscoroutine(chain):
                    chain = asyncio.create_task(chain)
                await asyncio.gather(heartbeat_task, chain, return_exceptions=True)

    async def _chain(self, chain, msg, on_message):
        await chain
        try:
            await on_message(msg)
        except Exception as e:
            logger.error(f'消息回调异常: {e}')

    def _gateway_post(self, packet: P.Packet) -> bytes:
        headers = {
            'content-type': 'application/x-protobuf',
            'accept': '*/*',
            'origin': self.auth.region.web_origin,
            'referer': self.auth.region.web_origin + '/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36',
            'x-appid': self.auth.gateway_app_id,
            'x-command': str(packet.cmd),
            'x-command-version': '5.7.0',
            'x-lgw-os-type': '1',
            'x-lgw-terminal-type': '2',
            'x-source': 'web',
            'x-web-version': '3.9.32',
            'x-request-id': generate_long_request_id(),
        }
        try:
            resp = requests.post(self.auth.region.gateway_url, headers=headers, cookies=self.auth.cookies,
                                 data=packet.SerializeToString(), timeout=15)
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise HttpError(str(e)) from e
        except requests.RequestException as e:
            raise NetworkError(str(e)) from e
        return resp.content

    def send_msg(self, text: str, chat_id: str, cid: str=None, reply_to: str=None):
        """发送文本；reply_to 是引用回复（被回复消息 id）；cid 是幂等键，由调用方生成并在重试时复用。"""
        packet_cid = cid or generate_request_cid()
        pkt = builders.build_send_message_packet(
            text, str(chat_id), generate_long_request_id(), reply_to=reply_to, cid=packet_cid,
        )
        content = self._gateway_post(pkt)
        try:
            msg_id = builders.decode_put_message_response(content)
        except Exception as e:
            raise SendUnconfirmed(f'服务端已接受发送请求，但响应解析失败: {e}', packet_cid) from e
        if not msg_id:
            raise SendUnconfirmed('服务端已接受发送请求，但响应里没有消息 id', packet_cid)
        return msg_id

    def search(self, query: str):
        pkt = builders.build_search_packet(query, generate_long_request_id(), self.auth.region.locale)
        return builders.decode_search_response(self._gateway_post(pkt))

    def create_group_chat(self, member_ids, group_name: str=None, description: str=None,
                          cid: str=None) -> str:
        """建群并拉人，返回 chat_id；cid 是幂等键，由调用方生成并在重试时复用。"""
        packet_cid = cid or generate_request_cid()
        data = {"type": 2, "chatterIds": [str(member_id) for member_id in member_ids], "cid": packet_cid}
        if group_name:
            data["groupName"] = str(group_name)
        if description:
            data["groupDesc"] = str(description)
        response = self.api("chats.PutChatRequest", data)
        return str(((response or {}).get("chat") or {}).get("id") or "")

    def create_p2p_chat(self, user_id: str) -> str:
        """和某个用户的单聊：已有则返回原会话，没有则服务端新建。"""
        response = self.api("chats.PutChatRequest", {"type": 1, "chatterIds": [str(user_id)]})
        return str(((response or {}).get("chat") or {}).get("id") or "")

    def get_user_name(self, user_id: str, chat_id: str):
        try:
            pkt = builders.build_user_info_packet(str(user_id), str(chat_id), generate_long_request_id())
            return builders.decode_user_info_response(self._gateway_post(pkt), self.auth.region.locale)
        except Exception:
            return None

    def recall_message(self, msg_id: str) -> None:
        """撤回消息；服务端没有明确返回 ok 时不视为成功。"""
        response = self.api('messages.DeleteMessageRequest', {'id': str(msg_id)})
        if (response or {}).get('message') != 'ok':
            raise MessageActionUnconfirmed(f'撤回失败: {(response or {}).get("message") or "服务端没有确认"}')

    def edit_message(self, msg_id: str, text: str, cid: str=None) -> None:
        """编辑自己发的纯文本消息；cid 是幂等键，由调用方生成并在重试时复用。"""
        packet_cid = cid or generate_request_cid()
        response = self.api('messages.EditMessageRequest', {
            'msgId': int(msg_id), 'cid': packet_cid, 'type': 4,
            'content': {'richText': builders.build_text_rich_text(text)},
        })
        if not ((response or {}).get('message') or {}).get('id'):
            raise MessageActionUnconfirmed('编辑失败: 服务端没有确认')

    def mark_chat_read(self, chat_id: str, max_position: int, message_ids=None) -> bool:
        data = {'chatId': str(chat_id), 'maxPosition': int(max_position)}
        if message_ids:
            data['messageIds'] = [str(m) for m in message_ids]
        self.api('messages.PutReadMessagesRequest', data)
        return True

    def get_group_name(self, chat_id: str):
        try:
            pkt = builders.build_group_info_packet(str(chat_id), generate_long_request_id())
            return builders.decode_group_info_response(self._gateway_post(pkt))
        except Exception:
            return None

    def api(self, type_or_cmd, data: dict=None, raw: bytes=None):
        from .proto import gateway
        cmd, req_cls, resp_cls = gateway.resolve_cmd(type_or_cmd)
        req_msg = None
        if req_cls is not None:
            req_msg = gateway.request_from_dict(req_cls, data or {})
        pkt = gateway.build_packet(cmd, generate_long_request_id(), req_msg=req_msg, req_bytes=raw if req_msg is None else None)
        content = self._gateway_post(pkt)
        resp_pkt = P.Packet()
        resp_pkt.ParseFromString(content)
        if resp_cls is not None and resp_pkt.payload:
            return gateway.response_to_dict(resp_cls, resp_pkt.payload)
        from protobuf_to_dict import protobuf_to_dict
        return protobuf_to_dict(resp_pkt)

    def get_chat_info(self, chat_id: str):
        resp = self.api('chats.PullChatsByIdsRequest', {'chatIds': [str(chat_id)]})
        chats = (resp or {}).get('chats') or {}
        for _, chat in chats.items():
            return chat
        return None

    def pull_history(self, chat_id: str, positions, save_storage=None):
        resp = self.api('messages.PullMessagesByPositionsRequest', {'chatId': str(chat_id), 'positions': [int(p) for p in positions]})
        messages = (resp or {}).get('messages') or {}
        out = []
        for pos_key, v in sorted(messages.items(), key=lambda kv: int(kv[0])):
            message_type = decoders.enum_to_int(v.get('type', 0))
            chat_type = decoders.enum_to_int(v.get('chatType', 0))
            summary, data = decoders.decode_message_content(message_type, v.get('content', b''))
            root_id = v.get('rootId') or ''
            msg = {'msg_id': v.get('id'), 'msg_type': message_type, 'msg_type_name': decoders.MSG_TYPE_NAMES.get(message_type, str(message_type)), 'from_id': v.get('fromId'), 'chat_id': v.get('chatId') or v.get('channelId') or str(chat_id), 'chat_type': chat_type, 'chat_type_name': decoders.CHAT_TYPE_NAMES.get(chat_type, str(chat_type)), 'at_me': False, 'root_id': root_id, 'parent_id': v.get('parentId') or '', 'cid': v.get('cid') or '', 'position': v.get('position') or int(pos_key), 'create_time': v.get('createTime'), 'content': summary, 'content_data': data, **decoders.message_flags(v)}
            out.append(msg)
            if save_storage is not None:
                try:
                    save_storage.save_message(msg)
                except Exception:
                    pass
        return out
