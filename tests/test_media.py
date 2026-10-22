from types import SimpleNamespace

import pytest

from larkx import media
from larkx.proto import builders, decoders, lark_all_pb2 as L, proto_pb2 as P


def anchor(href: str, content: str) -> bytes:
    prop = L.entities.RichTextElement.AnchorProperty(href=href, content=content)
    return prop.SerializeToString()


def rich_text(*elements) -> dict:
    dictionary = {}
    element_ids = []
    for index, (tag, prop) in enumerate(elements):
        element_id = f"e{index}"
        dictionary[element_id] = {"tag": tag, "property": prop}
        element_ids.append(element_id)
    return {"elementIds": element_ids, "elements": {"dictionary": dictionary}}


def test_link_element_is_rendered_with_url_and_text():
    text = decoders.extract_rich_text(rich_text(
        (1, P.TextProperty(content="看看这个").SerializeToString()),
        (6, anchor("https://example.com/a", "文档")),
    ))
    assert text == "看看这个文档(https://example.com/a)"


def test_link_without_different_text_shows_url_only():
    text = decoders.extract_rich_text(rich_text((6, anchor("https://example.com/b", "https://example.com/b"))))
    assert text == "https://example.com/b"


def test_image_and_file_packets_carry_the_uploaded_keys():
    image = builders.build_image_message_packet("img_v3_1", "c1", "req", cid="cid1")
    req = P.PutMessageRequest(); req.ParseFromString(image.payload)
    assert (req.type, req.chatId, req.cid, req.content.cryptoToken) == (5, "c1", "cid1", "img_v3_1")

    file = builders.build_file_message_packet("file_v3_1", "a.pdf", 12, "application/pdf", "c1", "req", cid="cid2")
    req = P.PutMessageRequest(); req.ParseFromString(file.payload)
    assert (req.type, req.content.fileKey, req.content.fileName, req.content.fileSize, req.content.fileMime) == (
        3, "file_v3_1", "a.pdf", 12, "application/pdf")
    assert req.cid == "cid2"


def test_reply_relations_use_root_and_parent_only():
    req = P.PutMessageRequest(); req.ParseFromString(
        builders.build_send_message_packet("hi", "c1", "req", reply_to="m1").payload)
    assert (req.rootId, req.parentId, req.isReplyInThread) == ("m1", "m1", False)


class FakeResponse:
    def __init__(self, text="", body=None):
        self.text = text
        self._body = body or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def auth_stub():
    return SimpleNamespace(
        cookies={"session": "x"},
        region=SimpleNamespace(file_host="https://file.example", web_origin="https://web.example"),
        gateway_app_id="app1",
    )


def test_upload_image_returns_key(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["files"] = kwargs["files"]
        return FakeResponse(text="0img_v3_abc-123")

    monkeypatch.setattr(media.requests, "post", fake_post)
    assert media.upload_image(auth_stub(), b"png-bytes", "a.png") == "img_v3_abc-123"
    assert captured["url"].endswith("/api/image")
    assert captured["files"]["image"][0] == "a.png"


def test_upload_file_returns_file_info(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        return FakeResponse(body={"status": "success", "data": {
            "key": "file_v3_1", "name": "a.pdf", "size": 12, "mimetype": "application/pdf"}})

    monkeypatch.setattr(media.requests, "post", fake_post)
    info = media.upload_file(auth_stub(), "c1", b"pdf-bytes", "a.pdf")
    assert info == {"key": "file_v3_1", "name": "a.pdf", "size": 12, "mime": "application/pdf"}
    assert captured["url"].endswith("/api/chats/c1/files")


def test_upload_file_reports_failure(monkeypatch):
    monkeypatch.setattr(media.requests, "post", lambda url, **kwargs: FakeResponse(body={"status": "fail", "message": "太大"}))
    with pytest.raises(ValueError, match="太大"):
        media.upload_file(auth_stub(), "c1", b"x", "a.pdf")
