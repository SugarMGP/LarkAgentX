from types import SimpleNamespace

import pytest

from larkx.client import LarkClient
from larkx.space import SpaceClient, SpaceError, _blocks_to_markdown


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


def test_document_by_url_returns_doc_info():
    class Client(LarkClient):
        def __init__(self):
            pass

        def api(self, name, data):
            assert name == "docs.PullDocByURLsRequest"
            assert data == {"urls": ["https://x.feishu.cn/wiki/abc"]}
            return {"docs": {"https://x.feishu.cn/wiki/abc": {"token": "T1", "url": "u"}}}

    assert Client().document_by_url("https://x.feishu.cn/wiki/abc")["token"] == "T1"


def test_document_by_url_raises_when_not_resolvable():
    class Client(LarkClient):
        def __init__(self):
            pass

        def api(self, name, data):
            return {"docs": {}}

    with pytest.raises(ValueError):
        Client().document_by_url("https://x.feishu.cn/wiki/abc")


def text_block(text, attribs="", num_to_attrib=None):
    return {"id": "b1", "data": {"type": "text", "text": {
        "apool": {"numToAttrib": num_to_attrib or {}},
        "initialAttributedTexts": {"text": {"0": text}, "attribs": {"0": attribs}},
    }}}


def test_attributed_text_decodes_bold_and_link():
    # "*0+1*1+2" = 前 1 个字符用属性 0（粗体），后 2 个字符用属性 1（链接）
    num_to_attrib = {"0": ["bold", "true"], "1": ["link", "https://x.dev"]}
    block = text_block("AB", "*0+1*1+1", num_to_attrib)
    markdown = _blocks_to_markdown({"b1": block}, ["b1"])
    assert markdown == "**A**[B](https://x.dev)"


def test_block_types_render_to_markdown():
    blocks = {
        "root": {"id": "root", "data": {"type": "page", "children": ["h", "p", "c"], "text": {
            "apool": {"numToAttrib": {}},
            "initialAttributedTexts": {"text": {"0": "标题"}, "attribs": {"0": ""}}}}},
        "h": {"id": "h", "data": {"type": "heading2", "children": [], "text": {
            "apool": {"numToAttrib": {}},
            "initialAttributedTexts": {"text": {"0": "小节"}, "attribs": {"0": ""}}}}},
        "p": {"id": "p", "data": {"type": "text", "children": [], "text": {
            "apool": {"numToAttrib": {}},
            "initialAttributedTexts": {"text": {"0": "正文"}, "attribs": {"0": ""}}}}},
        "c": {"id": "c", "data": {"type": "code", "language": "JSON", "children": [], "text": {
            "apool": {"numToAttrib": {}},
            "initialAttributedTexts": {"text": {"0": "{}"}, "attribs": {"0": ""}}}}},
    }
    markdown = _blocks_to_markdown(blocks, ["root", "h", "p", "c"])
    assert markdown == "# 标题\n\n## 小节\n\n正文\n\n```JSON\n{}\n```"


def test_table_cells_come_from_children():
    blocks = {
        "root": {"id": "root", "data": {"type": "page", "children": ["t"]}},
        "t": {"id": "t", "data": {"type": "table", "columns_id": ["c1", "c2"], "rows_id": ["r1"],
                                   "cell_set": {"r1c1": {"block_id": "cell1"}, "r1c2": {"block_id": "cell2"}}}},
        "cell1": {"id": "cell1", "data": {"type": "table_cell", "children": ["x1"]}},
        "x1": {"id": "x1", "data": {"type": "text", "children": [], "text": {
            "apool": {"numToAttrib": {}},
            "initialAttributedTexts": {"text": {"0": "甲"}, "attribs": {"0": ""}}}}},
        "cell2": {"id": "cell2", "data": {"type": "table_cell", "children": ["x2"]}},
        "x2": {"id": "x2", "data": {"type": "text", "children": [], "text": {
            "apool": {"numToAttrib": {}},
            "initialAttributedTexts": {"text": {"0": "乙"}, "attribs": {"0": ""}}}}},
    }
    markdown = _blocks_to_markdown(blocks, ["root", "t"])
    assert "| 甲 | 乙 |" in markdown
    assert "| --- | --- |" in markdown


def test_document_blocks_merges_table_cells(monkeypatch):
    responses = {
        "/space/api/docx/pages/client_vars": [
            {"code": 0, "data": {"block_map": {"t": {"data": {"type": "table"}}}, "skip_blocks": ["t"]}},
            {"code": 0, "data": {"block_map": {"cell": {"data": {"type": "table_cell"}}}}},
        ],
    }
    calls = []

    def fake_get(url, **kwargs):
        calls.append(kwargs.get("params"))
        body = responses["/space/api/docx/pages/client_vars"][min(len(calls) - 1, 1)]
        return FakeResponse(body)

    monkeypatch.setattr("larkx.space.requests.get", fake_get)
    data = SpaceClient(auth_stub()).document_blocks("T1")
    assert "cell" in data["block_map"]
    assert calls[1]["mode"] == 4 and calls[1]["block_id"] == "t"
