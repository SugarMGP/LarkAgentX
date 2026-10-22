from google.protobuf.json_format import MessageToDict

from . import proto_pb2 as P
from .ids import generate_request_cid


def wrap_packet(cmd: int, payload_msg, request_id: str) -> P.Packet:
    pkt = P.Packet()
    pkt.payloadType = 1
    pkt.cmd = cmd
    pkt.cid = request_id
    pkt.payload = payload_msg.SerializeToString()
    return pkt


def build_message_request(msg_type: int, chat_id: str, text: str = '', reply_to: str = None,
                          cid: str = None) -> P.PutMessageRequest:
    """组装一条消息请求；要自己往正文里插真实 AT 节点时用这个。"""
    req = _new_message_request(msg_type, chat_id, cid)
    _set_reply(req, reply_to)
    if text:
        fill_text_rich_text(req.content.richText, text)
    return req


def build_send_message_packet(text: str, chat_id: str, request_id: str,
                              reply_to: str=None, cid: str=None) -> P.Packet:
    """发文字。reply_to 是引用回复：rootId=parentId=被回复消息，且不设 isReplyInThread，
    普通群里就是普通引用样式（isReplyInThread 会让消息进话题，本库不支持话题）。"""
    req = _new_message_request(4, chat_id, cid)
    _set_reply(req, reply_to)
    fill_text_rich_text(req.content.richText, text)
    return wrap_packet(5, req, request_id)


def build_image_message_packet(image_key: str, chat_id: str, request_id: str,
                               reply_to: str=None, cid: str=None) -> P.Packet:
    """发图片。image_key 由 media.upload_image 上传得到，放在 content.cryptoToken。"""
    req = _new_message_request(5, chat_id, cid)
    _set_reply(req, reply_to)
    req.content.cryptoToken = str(image_key)
    return wrap_packet(5, req, request_id)


def build_file_message_packet(file_key: str, file_name: str, file_size: int, file_mime: str,
                              chat_id: str, request_id: str, reply_to: str=None, cid: str=None) -> P.Packet:
    """发文件。文件先经 media.upload_file 上传到该会话。"""
    req = _new_message_request(3, chat_id, cid)
    _set_reply(req, reply_to)
    req.content.fileKey = str(file_key)
    req.content.fileName = str(file_name)
    req.content.fileSize = int(file_size)
    req.content.fileMime = str(file_mime)
    return wrap_packet(5, req, request_id)


def _new_message_request(msg_type: int, chat_id: str, cid: str=None) -> P.PutMessageRequest:
    req = P.PutMessageRequest()
    req.type = msg_type
    req.chatId = str(chat_id)
    req.cid = str(cid) if cid else generate_request_cid()
    req.isNotified = 1
    req.version = 1
    return req


def _set_reply(req: P.PutMessageRequest, reply_to: str=None) -> None:
    """引用回复：rootId=parentId=被回复消息，不设 isReplyInThread。"""
    if reply_to:
        req.rootId = str(reply_to)
        req.parentId = str(reply_to)


def fill_text_rich_text(rich, text: str) -> None:
    """网页协议纯文本必须带一个 TEXT 元素；inner_text 是展示文本。"""
    element_id = generate_request_cid()
    rich.elementIds.append(element_id)
    rich.innerText = str(text)
    rich.elements.dictionary[element_id].tag = 1
    tp = P.TextProperty()
    tp.content = str(text)
    rich.elements.dictionary[element_id].property = tp.SerializeToString()


def build_text_rich_text(text: str) -> dict:
    """编辑消息用 api() 的 richText protobuf JSON。"""
    rich = P.RichText()
    fill_text_rich_text(rich, text)
    return MessageToDict(rich, preserving_proto_field_name=True)


def decode_put_message_response(content: bytes) -> str:
    pkt = P.Packet()
    pkt.ParseFromString(content)
    resp = P.PutMessageResponse()
    resp.ParseFromString(pkt.payload)
    return resp.message.id


