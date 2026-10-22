from types import SimpleNamespace

import pytest

from larkx.client import LarkClient
from larkx.space import SpaceClient, SpaceError


class FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status
        self.text = str(body)

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def auth_stub():
    return SimpleNamespace(
        cookies={"session": "x"},
        gateway_app_id="161471",
        region=SimpleNamespace(domain="feishu.cn", internal_api_host="https://internal-api.feishu.cn"),
    )


def test_files_returns_nodes(monkeypatch):
    body = {"code": 0, "msg": "Success", "data": {"entities": {"nodes": {
        "n1": {"name": "我的空间", "obj_token": "tok1"},
        "n2": {"name": "文档", "obj_token": "tok2"},
    }}}}
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        captured["headers"] = kwargs.get("headers")
        return FakeResponse(body)

    monkeypatch.setattr("larkx.space.requests.get", fake_get)
    files = SpaceClient(auth_stub()).files()
    assert [f["name"] for f in files] == ["我的空间", "文档"]
    assert captured["url"] == "https://internal-api.feishu.cn/space/api/explorer/v3/children/list/"
    assert captured["headers"]["x-appid"] == "161471"


def test_business_error_is_raised(monkeypatch):
    monkeypatch.setattr("larkx.space.requests.get",
                        lambda url, **kwargs: FakeResponse({"code": 2, "msg": "Parameter Error"}))
    with pytest.raises(SpaceError, match="Parameter Error"):
        SpaceClient(auth_stub()).files()


def test_document_by_url_returns_doc_info(monkeypatch):
    class Client(LarkClient):
        def __init__(self):
            pass

        def api(self, name, data):
            assert name == "docs.PullDocByURLsRequest"
            assert data == {"urls": ["https://x.feishu.cn/wiki/abc"]}
            return {"docs": {"https://x.feishu.cn/wiki/abc": {"token": "T1", "url": "u"}}}

    info = Client().document_by_url("https://x.feishu.cn/wiki/abc")
    assert info["token"] == "T1"


def test_document_by_url_raises_when_not_resolvable(monkeypatch):
    class Client(LarkClient):
        def __init__(self):
            pass

        def api(self, name, data):
            return {"docs": {}}

    with pytest.raises(ValueError):
        Client().document_by_url("https://x.feishu.cn/wiki/abc")
