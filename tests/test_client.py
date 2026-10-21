from types import SimpleNamespace

import pytest

from larkx.client import LarkClient, NetworkError, SendUnconfirmed
from larkx.proto import builders, proto_pb2 as P


def parsed(packet, cls=P.PutMessageRequest):
    msg = cls(); msg.ParseFromString(packet.payload); return msg


def make_client(monkeypatch, reply=P.Packet().SerializeToString()):
    auth = SimpleNamespace(require_valid=lambda: None, app_key="k", user_id="u",
                           region=SimpleNamespace(locale="zh_CN"))
    c = LarkClient(auth)
    monkeypatch.setattr(c, "is_thread_chat", lambda chat_id: False)
    monkeypatch.setattr(c, "_gateway_post", lambda packet: reply)
    return c


def test_send_packet_distinguishes_thread_root_and_quoted_reply():
    req = parsed(builders.build_send_message_packet("hi", "c1", "req", root_id="r1", reply_to="q1", cid="fixed"))
    assert (req.rootId, req.parentId, req.cid, req.isReplyInThread) == ("r1", "q1", "fixed", True)
    req = parsed(builders.build_send_message_packet("hi", "c1", "req", reply_to="q1", cid="fixed"))
    assert (req.rootId, req.parentId, req.isReplyInThread) == ("q1", "q1", False)


def test_create_chat_packet_supports_members_description_and_topic(monkeypatch):
    auth = SimpleNamespace(require_valid=lambda: None, app_key="k", user_id="u",
                           region=SimpleNamespace(locale="zh_CN"))
    c = LarkClient(auth)
    packets = []
    def post(packet):
        packets.append(packet)
        return P.Packet().SerializeToString()
    monkeypatch.setattr(c, "_gateway_post", post)
    monkeypatch.setattr("larkx.client.generate_request_cid", lambda: "sdk-cid")
    assert c.create_chat(["1", "2"], group_name="项目群", description="描述", topic=True) == ""
    raw = P.PutChatRequest(); raw.ParseFromString(packets[0].payload)
    assert (raw.type, list(raw.chatterIds), raw.groupName, raw.groupDesc, raw.cid) == (2, ["1", "2"], "项目群", "描述", "sdk-cid")


def test_send_msg_uses_generated_cid_and_raises_unconfirmed(monkeypatch):
    import larkx.client as client_module
    monkeypatch.setattr(client_module, "generate_request_cid", lambda: "sdk-cid")
    c = make_client(monkeypatch, reply=P.Packet().SerializeToString())
    with pytest.raises(SendUnconfirmed) as error:
        c.send_msg("hello", "c1")
    assert error.value.cid == "sdk-cid"


def test_network_errors_are_separate(monkeypatch):
    import requests
    auth = SimpleNamespace(require_valid=lambda: None, app_key="k", user_id="u", gateway_app_id="1", cookies={},
                           region=SimpleNamespace(locale="zh_CN", web_origin="https://example", gateway_url="https://gateway"))
    c = LarkClient(auth)

    def fail(*args, **kwargs):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "post", fail)
    with pytest.raises(NetworkError):
        c._gateway_post(P.Packet(cmd=1))



def test_mark_read_is_split_into_chat_and_thread_requests(monkeypatch):
    calls = []
    c = make_client(monkeypatch)
    monkeypatch.setattr(c, "api", lambda type_name, data: calls.append((type_name, data)))
    assert c.mark_chat_read("c1", 10, ["m1"]) is True
    assert c.mark_thread_read("c1", "t1", 20) is True
    assert calls == [
        ("messages.PutReadMessagesRequest", {"chatId": "c1", "maxPosition": 10, "messageIds": ["m1"]}),
        ("messages.PutReadMessagesRequest", {"chatId": "c1", "threadId": "t1", "threadMaxPosition": 20}),
    ]


def test_recall_and_edit_messages_use_protocol_confirmations(monkeypatch):
    c = make_client(monkeypatch)
    replies = [{"message": "ok"}, {"message": {"id": "m-new"}}]
    calls = []
    monkeypatch.setattr(c, "api", lambda type_name, data: calls.append((type_name, data)) or replies.pop(0))
    c.recall_message("m1")
    c.edit_message("m2", "new text", cid="cid")
    assert calls == [
        ("messages.DeleteMessageRequest", {"id": "m1"}),
        ("messages.EditMessageRequest", {
            "msgId": "m2", "cid": "cid", "type": 4,
            "content": {"richText": calls[1][1]["content"]["richText"]},
        }),
    ]
    assert calls[1][1]["content"]["richText"]["innerText"] == "new text"
