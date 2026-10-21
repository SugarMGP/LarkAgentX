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
    monkeypatch.setattr(c, "_gateway_post", lambda packet: reply)
    return c


def test_send_packet_only_supports_quoted_reply():
    req = parsed(builders.build_send_message_packet("hi", "c1", "req", reply_to="q1", cid="fixed"))
    assert (req.rootId, req.parentId, req.cid, req.isReplyInThread) == ("q1", "q1", "fixed", False)
    req = parsed(builders.build_send_message_packet("hi", "c1", "req", cid="fixed"))
    assert (req.rootId, req.parentId, req.isReplyInThread) == ("", "", False)


def test_group_and_p2p_chat_requests_differ_in_type(monkeypatch):
    auth = SimpleNamespace(require_valid=lambda: None, app_key="k", user_id="u",
                           region=SimpleNamespace(locale="zh_CN"))
    c = LarkClient(auth)
    packets = []
    def post(packet):
        packets.append(packet)
        return P.Packet().SerializeToString()
    monkeypatch.setattr(c, "_gateway_post", post)
    monkeypatch.setattr("larkx.client.generate_request_cid", lambda: "sdk-cid")
    assert c.create_group_chat(["1", "2"], group_name="项目群", description="描述") == ""
    assert c.create_p2p_chat("9") == ""
    group = P.PutChatRequest(); group.ParseFromString(packets[0].payload)
    p2p = P.PutChatRequest(); p2p.ParseFromString(packets[1].payload)
    assert (group.type, list(group.chatterIds), group.groupName, group.groupDesc, group.cid) == (2, ["1", "2"], "项目群", "描述", "sdk-cid")
    assert (p2p.type, list(p2p.chatterIds)) == (1, ["9"])


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



def test_mark_read_uses_the_chat_read_request(monkeypatch):
    calls = []
    c = make_client(monkeypatch)
    monkeypatch.setattr(c, "api", lambda type_name, data: calls.append((type_name, data)) or {"readPosition": "10"})
    assert c.mark_chat_read("c1", 10, ["m1"]) == {"readPosition": "10"}
    assert calls == [
        ("messages.PutReadMessagesRequest", {"chatId": "c1", "maxPosition": 10, "messageIds": ["m1"]}),
    ]


def test_recall_and_edit_messages_use_protocol_confirmations(monkeypatch):
    c = make_client(monkeypatch)
    replies = [{"message": "ok"}, {"message": {"id": "m-new"}}]
    calls = []
    monkeypatch.setattr(c, "api", lambda type_name, data: calls.append((type_name, data)) or replies.pop(0))
    c.recall_message("m1")
    c.edit_message("7", "new text", cid="cid")
    assert calls == [
        ("messages.DeleteMessageRequest", {"id": "m1"}),
        ("messages.EditMessageRequest", {
            "msgId": 7, "cid": "cid", "type": 4,
            "content": {"richText": calls[1][1]["content"]["richText"]},
        }),
    ]
    assert calls[1][1]["content"]["richText"]["innerText"] == "new text"


def test_build_ws_url_contains_ticket_and_request_id(monkeypatch):
    auth = SimpleNamespace(require_valid=lambda: None, app_key="k", user_id="u", device_id="dev",
                           get_ticket=lambda: "ticket-1",
                           region=SimpleNamespace(locale="zh_CN", ws_url="wss://example/ws"))
    c = LarkClient(auth)
    url = c.build_ws_url()
    assert url.startswith("wss://example/ws?")
    assert "ticket=ticket-1" in url and "request_id=" in url and "access_key=" in url
