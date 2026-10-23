from types import SimpleNamespace

import pytest

from larkx.client import LarkClient
from larkx.space import SpaceClient, SpaceError, _blocks_to_markdown, _decode_line


class FakeResponse:
    def __init__(self, body, status=200, cookies=None):
        self._body = body
        self.status_code = status
        self.text = str(body)
        self.cookies = cookies or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def auth_stub():
    return SimpleNamespace(
        cookies={"session": "x"},
        user_id="7690398870819343329",
        gateway_app_id="161471",
        region=SimpleNamespace(domain="feishu.cn", internal_api_host="https://internal-api.feishu.cn",
                               space_api_host="https://internal-api-space.feishu.cn"),
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


def test_import_markdown_uploads_then_polls(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url.rsplit("/space/api/", 1)[-1], kwargs))
        if url.endswith("upload/prepare/"):
            body = {"code": 0, "data": {"upload_id": "u1", "block_size": 2, "num_blocks": 2}}
        elif url.endswith("merge_block/"):
            body = {"code": 0, "data": {"success_seq_list": [0, 1]}}
        elif url.endswith("upload/finish/"):
            body = {"code": 0, "data": {"file_token": "ft1"}}
        else:
            body = {"code": 0, "data": {"ticket": "tk1", "job_timeout": 600}}
        return FakeResponse(body)

    def fake_get(url, **kwargs):
        calls.append((url.rsplit("/space/api/", 1)[-1], kwargs))
        return FakeResponse({"code": 0, "data": {"result": {"token": "doc1", "url": "u1"}}})

    monkeypatch.setattr("larkx.space.requests.post", fake_post)
    monkeypatch.setattr("larkx.space.requests.get", fake_get)
    monkeypatch.setattr("larkx.space.time.sleep", lambda *_: None)

    out = SpaceClient(auth_stub()).import_markdown("# hi", "wiki-node", name="报告.md")

    assert out == {"token": "doc1", "url": "u1", "type": ""}
    assert [name for name, _ in calls] == [
        "box/upload/prepare/", "box/stream/upload/merge_block/", "box/upload/finish/",
        "import/create/", "import/result/tk1",
    ]
    create = calls[3][1]["data"]
    assert '"type":"docx"' in create and '"mount_type":2' in create
    assert '"mount_key":"wiki-node"' in create and '"event_source":"6"' in create


def test_root_token_picks_space_root(monkeypatch):
    body = {"code": 0, "data": {"entities": {"nodes": {
        "n1": {"name": "文档", "type": 22, "token": "nodcn1", "obj_token": "doc1"},
        "n2": {"name": "我的空间", "type": 4, "token": "root1", "obj_token": "root1"},
    }}}}
    monkeypatch.setattr("larkx.space.requests.get", lambda url, **kwargs: FakeResponse(body))
    assert SpaceClient(auth_stub()).root_token() == "root1"


def test_root_token_missing_root_raises(monkeypatch):
    body = {"code": 0, "data": {"entities": {"nodes": {
        "n1": {"name": "文档", "type": 22, "token": "nodcn1", "obj_token": "doc1"},
    }}}}
    monkeypatch.setattr("larkx.space.requests.get", lambda url, **kwargs: FakeResponse(body))
    with pytest.raises(SpaceError, match="根目录"):
        SpaceClient(auth_stub()).root_token()


def test_add_member_posts_editable_owners_with_csrf(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        return FakeResponse({"code": 0, "data": {"entities": {"nodes": {}}}},
                            cookies={"_csrf_token": "csrf1"})

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse({"code": 0, "data": {"success_count": 2}})

    monkeypatch.setattr("larkx.space.requests.get", fake_get)
    monkeypatch.setattr("larkx.space.requests.post", fake_post)

    SpaceClient(auth_stub()).add_member("doc1", ["u1", "u2"])

    url, kwargs = calls[0]
    assert url.endswith("/space/api/suite/permission/members/create/")
    assert kwargs["headers"]["X-CSRFToken"] == "csrf1"
    assert kwargs["data"]["owners"] == '[{"owner_id":"u1","owner_type":1,"permission":4},{"owner_id":"u2","owner_type":1,"permission":4}]'
    assert kwargs["data"]["notify_lark"] == "0"


def test_add_member_raises_when_not_all_granted(monkeypatch):
    monkeypatch.setattr("larkx.space.requests.get",
                        lambda url, **kwargs: FakeResponse({"code": 0, "data": {}},
                                                         cookies={"_csrf_token": "csrf1"}))
    monkeypatch.setattr("larkx.space.requests.post",
                        lambda url, **kwargs: FakeResponse({"code": 0, "data": {"success_count": 0}}))
    with pytest.raises(SpaceError, match="加协作者失败"):
        SpaceClient(auth_stub()).add_member("doc1", ["u1"])


def test_search_user_returns_candidates(monkeypatch):
    body = {"code": 0, "data": {"candidates": [{"owner_id": "u1", "owner_name": "张三", "owner_type": 1}]}}
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return FakeResponse(body)

    monkeypatch.setattr("larkx.space.requests.get", fake_get)
    out = SpaceClient(auth_stub()).search_user("san@dewu.com")
    assert out == [{"owner_id": "u1", "owner_name": "张三", "owner_type": 1}]
    assert captured["url"].endswith("/space/api/search/permission_candidates/")
    assert captured["params"] == {"query": "san@dewu.com"}


def test_document_blocks_follows_the_cursor(monkeypatch):
    pages = [
        {"code": 0, "data": {"block_map": {"b1": {}}, "block_sequence": ["b1"], "has_more": True, "cursor": "c1"}},
        {"code": 0, "data": {"block_map": {"b2": {}}, "block_sequence": ["b2"], "has_more": False, "cursor": "c2"}},
    ]
    calls = []

    def fake_get(url, **kwargs):
        calls.append(kwargs.get("params"))
        return FakeResponse(pages[min(len(calls) - 1, 1)])

    monkeypatch.setattr("larkx.space.requests.get", fake_get)
    data = SpaceClient(auth_stub()).document_blocks("T1")
    assert sorted(data["block_map"]) == ["b1", "b2"]
    assert data["block_sequence"] == ["b1", "b2"]
    assert calls[0].get("cursor") is None and calls[1]["cursor"] == "c1"


def test_http_error_becomes_space_error(monkeypatch):
    monkeypatch.setattr("larkx.space.requests.get",
                        lambda url, **kwargs: FakeResponse("boom", status=500))
    with pytest.raises(SpaceError, match="HTTP 500"):
        SpaceClient(auth_stub()).files()


def test_document_markdown_drops_query_and_fragment(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(kwargs.get("params"))
        return FakeResponse({"code": 0, "data": {"block_map": {}, "block_sequence": []}})

    monkeypatch.setattr("larkx.space.requests.get", fake_get)
    SpaceClient(auth_stub()).document_markdown("https://x.feishu.cn/docx/AbC123?from=copy#frag")
    assert calls[0]["id"] == "AbC123"


def test_pipe_op_replaces_the_style_set():
    nodes = _decode_line("abcdef", "*0+3|1+2", {"0": ["bold", "true"], "1": ["italic", "true"]})
    assert [(n["text"], n.get("bold"), n.get("italic")) for n in nodes] == [
        ("abc", True, None), ("de", None, True), ("f", None, None)]


def test_update_block_text_replaces_the_body(monkeypatch):
    blocks = {"code": 0, "data": {"block_map": {"b1": {
        "id": "b1", "version": 3,
        "data": {"type": "text", "text": {"initialAttributedTexts": {"text": {"0": "old"}}}}}}}}
    posts = []

    def fake_get(url, **kwargs):
        return FakeResponse(blocks)

    def fake_post(url, **kwargs):
        posts.append((url, kwargs.get("json"), kwargs.get("headers")))
        return FakeResponse({"code": 0, "data": {}})

    monkeypatch.setattr("larkx.space.requests.get", fake_get)
    monkeypatch.setattr("larkx.space.requests.post", fake_post)
    SpaceClient(auth_stub()).update_block_text("doc1", "b1", "new text")

    url, body, headers = posts[0]
    assert url.endswith("/space/api/docx/blocks/user_change")
    assert body["page_id"] == "doc1"
    change = body["change_map"]["b1"]
    assert change["version"] == 3
    # 旧正文 3 个字换成 8 个字：长度差 +5，删掉旧的 3 个，插入新的 8 个。
    assert change["payload"]["ops"][0]["subType"]["o"]["zone_changesets"]["0"] == "Z:3>5=0-3*0+8$new text"
    assert "X-CSRFToken" in headers


def test_insert_block_creates_the_block_and_links_it_into_the_parent(monkeypatch):
    blocks = {"code": 0, "data": {"block_map": {"doc1": {
        "id": "doc1", "version": 4,
        "data": {"type": "page", "children": ["h1", "p1"]}}, "h1": {
        "id": "h1", "version": 1,
        "data": {"type": "heading1", "parent_id": "doc1", "children": ["p1"]}}, "p1": {
        "id": "p1", "version": 1,
        "data": {"type": "text", "parent_id": "doc1", "children": []}}}}}
    posts = []

    def fake_get(url, **kwargs):
        return FakeResponse(blocks)

    def fake_post(url, **kwargs):
        posts.append((url, kwargs.get("json")))
        return FakeResponse({"code": 0, "data": {}})

    monkeypatch.setattr("larkx.space.requests.get", fake_get)
    monkeypatch.setattr("larkx.space.requests.post", fake_post)
    out = SpaceClient(auth_stub()).insert_block("doc1", "新标题", block_type="heading1", after="p1")

    url, body = posts[0]
    assert url.endswith("/space/api/docx/blocks/user_change")
    assert body["page_id"] == "doc1"
    assert out["parent_id"] == "doc1" and out["index"] == 2
    new_block = body["change_map"][out["block_id"]]
    assert new_block["version"] == 0
    assert new_block["payload"]["ops"][0]["action"]["oi"] == {
        "parent_id": "doc1", "type": "heading1", "children": [], "comments": [], "revisions": [],
        "author": "7690398870819343329",
        "text": {"initialAttributedTexts": {"text": {"0": ""}, "attribs": {"0": ""}},
                 "apool": {"numToAttrib": {}, "nextNum": 0}},
        "folded": False}
    # 正文跟在建块后面，一次请求写完：空块 0 字 → 3 个字
    assert new_block["payload"]["ops"][1]["subType"]["o"]["zone_changesets"]["0"] == "Z:0>3*0+3$新标题"
    parent_op = body["change_map"]["doc1"]["payload"]["ops"][0]
    assert parent_op == {"p": ["children", 2], "action": {"li": out["block_id"]}}


def test_insert_block_defaults_to_the_end_and_rejects_unknown_types(monkeypatch):
    blocks = {"code": 0, "data": {"block_map": {"doc1": {
        "id": "doc1", "version": 1,
        "data": {"type": "page", "children": ["p1"]}}, "p1": {
        "id": "p1", "version": 1,
        "data": {"type": "text", "parent_id": "doc1", "children": []}}}}}
    posts = []

    def fake_get(url, **kwargs):
        return FakeResponse(blocks)

    def fake_post(url, **kwargs):
        posts.append(kwargs.get("json"))
        return FakeResponse({"code": 0, "data": {}})

    monkeypatch.setattr("larkx.space.requests.get", fake_get)
    monkeypatch.setattr("larkx.space.requests.post", fake_post)
    space = SpaceClient(auth_stub())
    out = space.insert_block("doc1", "追加段")
    assert out["index"] == 1
    empty = space.insert_block("doc1")
    assert empty["index"] == 1
    # 没有正文就只建空块，不带 changeset
    assert len(posts[1]["change_map"][empty["block_id"]]["payload"]["ops"]) == 1
    with pytest.raises(SpaceError, match="不支持的块类型"):
        space.insert_block("doc1", "x", block_type="heading0")


def test_set_block_type_swaps_the_type_field(monkeypatch):
    blocks = {"code": 0, "data": {"block_map": {"b1": {
        "id": "b1", "version": 7, "data": {"type": "text"}}}}}
    posts = []

    def fake_get(url, **kwargs):
        return FakeResponse(blocks)

    def fake_post(url, **kwargs):
        posts.append(kwargs.get("json"))
        return FakeResponse({"code": 0, "data": {}})

    monkeypatch.setattr("larkx.space.requests.get", fake_get)
    monkeypatch.setattr("larkx.space.requests.post", fake_post)
    SpaceClient(auth_stub()).set_block_type("doc1", "b1", "bullet")

    change = posts[0]["change_map"]["b1"]
    assert change["version"] == 7
    assert change["payload"]["ops"] == [{"p": ["type"], "action": {"od": "text", "oi": "bullet"}}]


def test_document_meta_and_members_use_the_document_token(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs.get("params")))
        if 'wiki' in url:
            return FakeResponse({"code": 0, "data": {"node": {"obj_token": "T1", "space_id": "S1"}}})
        if 'meta' in url:
            return FakeResponse({"code": 0, "data": {"title": "周报", "owner_id": "u1"}})
        return FakeResponse({"code": 0, "data": {"members": [{"owner_id": "u1", "is_owner": True}]}})

    monkeypatch.setattr("larkx.space.requests.get", fake_get)
    space = SpaceClient(auth_stub())
    assert space.document_meta("https://x.feishu.cn/wiki/W1")["title"] == "周报"
    assert space.document_members("https://x.feishu.cn/wiki/W1")["members"][0]["owner_id"] == "u1"
    # wiki 链接先换成真实文档 token，这些接口只认文档 token
    assert [p for url, p in calls if 'meta' in url] == [{"token": "T1", "type": 22}]
    assert [p for url, p in calls if 'members' in url] == [{"token": "T1", "type": 22}]


def test_remove_member_posts_owner_and_type(monkeypatch):
    posts = []

    def fake_get(url, **kwargs):
        return FakeResponse({"code": 0, "data": {}})

    def fake_post(url, **kwargs):
        posts.append((url, kwargs.get("data"), kwargs.get("headers")))
        return FakeResponse({"code": 0, "data": {"ret": True}})

    monkeypatch.setattr("larkx.space.requests.get", fake_get)
    monkeypatch.setattr("larkx.space.requests.post", fake_post)
    SpaceClient(auth_stub()).remove_member("doc1", "g1", owner_type=2)

    url, data, headers = posts[0]
    assert url.endswith("/space/api/suite/permission/members/delete/")
    assert data == {"token": "doc1", "type": "22", "owner_id": "g1", "owner_type": "2"}
    assert "X-CSRFToken" in headers


def test_update_title_posts_the_new_title(monkeypatch):
    posts = []

    def fake_get(url, **kwargs):
        return FakeResponse({"code": 0, "data": {}})

    def fake_post(url, **kwargs):
        posts.append((url, kwargs.get("json")))
        return FakeResponse({"code": 0, "data": {}})

    monkeypatch.setattr("larkx.space.requests.get", fake_get)
    monkeypatch.setattr("larkx.space.requests.post", fake_post)
    SpaceClient(auth_stub()).update_title("doc1", "新标题")

    url, body = posts[0]
    assert url.endswith("/space/api/doc.v2/update_title")
    assert body == {"token": "doc1", "obj_type": 22, "title": "新标题"}


def test_add_member_supports_groups(monkeypatch):
    posts = []

    def fake_get(url, **kwargs):
        return FakeResponse({"code": 0, "data": {}})

    def fake_post(url, **kwargs):
        posts.append(kwargs.get("data"))
        return FakeResponse({"code": 0, "data": {"success_count": 1}})

    monkeypatch.setattr("larkx.space.requests.get", fake_get)
    monkeypatch.setattr("larkx.space.requests.post", fake_post)
    SpaceClient(auth_stub()).add_member("doc1", ["g1"], owner_type=2)

    assert '"owner_type":2' in posts[0]["owners"]


def test_document_token_resolves_wiki_nodes(monkeypatch):
    def fake_get(url, **kwargs):
        if 'wiki' in url:
            return FakeResponse({"code": 0, "data": {"node": {"obj_token": "T1", "space_id": "S1"}}})
        return FakeResponse({"code": 0, "data": {}})

    monkeypatch.setattr("larkx.space.requests.get", fake_get)
    space = SpaceClient(auth_stub())
    assert space.document_token("https://x.feishu.cn/wiki/W1") == "T1"
    assert space.document_token("W1") == "T1"
    assert space.document_token("doxcn1") == "doxcn1"
