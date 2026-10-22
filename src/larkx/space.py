"""飞书云空间与云文档接口。

逆向结论：这套接口和 IM 网关不是一套协议——走 JSON over HTTP，域名也不同
（云空间在 internal-api.<domain>，文档/空间服务在 internal-api-space.<domain>），
路径来自文档页内嵌配置和前端 JS 里的接口表。

已用真实账号验证的能力：
- GET  /space/api/explorer/v3/children/list/          我的云空间文件列表
- GET  /space/api/explorer/folder/children/            某个目录下的文件
- GET  /space/api/explorer/root/                       云空间根目录
- GET  /space/api/wiki/v2/tree/get_node/               wiki 节点 → 真实文档 token
- GET  /space/api/docx/pages/client_vars               文档正文（块）

写操作（建目录、删文件、移动、改名、评论等）需要请求头 X-CSRFToken，
值取自 cookie _csrf_token；网页端登录的会话里没有这个 cookie，所以没有封装。
文档写入（markdown 写回）走编辑器的 box-stream 私有同步协议，也没有封装。
"""
from __future__ import annotations

import re

import requests

from .auth import LarkAuth

# Etherpad 风格的属性操作串：*N=应用属性号 N（base36），+M=接下来 M 个字符（base36）
_OP_RE = re.compile(r"(\*[0-9a-z]+|\|[0-9a-z]+|\+[0-9a-z]+|=[0-9a-z]+|-[0-9a-z]+)")
_URL_RE = re.compile(r"^(https?:|mailto:|tel:|/)", re.I)
_CSS_COLOR_RE = re.compile(r"^(#[0-9a-f]{3,8}|rgba?\(|hsla?\()", re.I)
_HEADINGS = {"heading1": 1, "heading2": 2, "heading3": 3, "heading4": 4, "heading5": 5, "heading6": 6}


class SpaceError(RuntimeError):
    """云空间/文档接口返回了业务错误。"""


def _make_node(text: str, attr_nums: list[int], num_to_attrib: dict) -> dict:
    """把一组属性号应用到一段文本，得到带样式/链接的节点。"""
    node = {"text": text}
    for num in attr_nums:
        pair = num_to_attrib.get(str(num))
        if not pair:
            continue
        key = (pair[0] or "").lower()
        val = pair[1]
        if "bold" in key:
            node["bold"] = True
        elif "italic" in key:
            node["italic"] = True
        elif "strike" in key:
            node["strike"] = True
        elif "underline" in key:
            node["underline"] = True
        elif "inline" in key and "code" in key or key == "code" or "codeinline" in key:
            node["inline_code"] = True
        elif "equation" in key:
            node["equation"] = val
        elif "inline-component" in key or "inline_component" in key:
            _apply_inline_component(node, val)
        elif not re.search(r"(^|[-_])id$", key) and ("link" in key or "href" in key or "url" in key):
            url = _decode_link(val)
            if url and _URL_RE.match(url):
                node["link"] = url
    return node


def _apply_inline_component(node: dict, raw: str) -> None:
    """行内组件（提及文档/用户）：正文里只是占位空格，标题和链接在这个 JSON 里。"""
    import json
    try:
        data = (json.loads(raw) or {}).get("data") or {}
    except ValueError:
        return
    title = str(data.get("title") or data.get("name") or "").strip()
    url = _decode_link(str(data.get("raw_url") or data.get("url") or data.get("link") or ""))
    if title:
        node["text"] = title
    if url and _URL_RE.match(url):
        node["link"] = url


def _decode_link(value: str) -> str:
    if not value:
        return ""
    try:
        from urllib.parse import unquote
        decoded = unquote(value)
    except Exception:
        decoded = value
    match = re.search(r"https?://[^\s\"']+", decoded)
    return match.group(0) if match else decoded


def _decode_line(text: str, attribs: str, num_to_attrib: dict) -> list[dict]:
    """解码一行的 attributed text。"""
    if not attribs:
        return [{"text": text}] if text else []
    nodes: list[dict] = []
    index = 0
    pending: list[int] = []
    for match in _OP_RE.finditer(attribs):
        op = match.group(0)
        code, num = op[0], op[1:]
        if code == "*":
            pending.append(int(num, 36))
        elif code == "+":
            length = int(num, 36)
            chunk = text[index:index + length]
            index += length
            if chunk:
                nodes.append(_make_node(chunk, pending, num_to_attrib))
            pending = []
        elif code != "|":
            pending = []
    if index < len(text):
        nodes.append(_make_node(text[index:], pending, num_to_attrib))
    return nodes


def _decode_text(text_data: dict) -> list[dict]:
    """解码块的正文（data.text）为行内节点列表。"""
    attributed = text_data.get("initialAttributedTexts") or {}
    num_to_attrib = (text_data.get("apool") or {}).get("numToAttrib") or {}

    def lines(value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [value.get(key, "") for key in sorted(value, key=lambda k: int(k))]

    texts = lines(attributed.get("text"))
    attribs = lines(attributed.get("attribs"))
    out: list[dict] = []
    for index, text in enumerate(texts):
        if index:
            out.append({"text": "\n"})
        out.extend(_decode_line(text, attribs[index] if index < len(attribs) else "", num_to_attrib))
    return out


def _render_inline(nodes: list[dict]) -> str:
    """行内节点渲染成 markdown。"""
    out = []
    for node in nodes:
        text = node.get("text", "")
        if text == "\n":
            out.append("  \n")
            continue
        if node.get("equation"):
            out.append(f"${node['equation'].strip()}$")
            continue
        if node.get("inline_code"):
            out.append("`" + text.replace("`", "\\`") + "`")
            continue
        if node.get("bold"):
            text = f"**{text}**"
        if node.get("italic"):
            text = f"*{text}*"
        if node.get("strike"):
            text = f"~~{text}~~"
        if node.get("link"):
            text = f"[{text}]({node['link']})"
        out.append(text)
    return "".join(out)


def _block_text(block: dict) -> str:
    return _render_inline(_decode_text((block.get("data") or {}).get("text") or {}))


def _blocks_to_markdown(block_map: dict, sequence: list[str]) -> str:
    """把块渲染成 markdown。返回的每一项是一个「块组」，组内用换行、组间用空行。"""
    def children(bid: str) -> list[str]:
        return list(((block_map.get(bid) or {}).get("data") or {}).get("children") or [])

    def render(bid: str) -> list[str]:
        block = block_map.get(bid) or {}
        data = block.get("data") or {}
        btype = data.get("type") or "text"
        if btype == "page":
            return render_children(children(bid))
        if btype in _HEADINGS:
            return [f"{'#' * _HEADINGS[btype]} {_block_text(block)}".rstrip()]
        if btype == "code":
            language = data.get("language") or ""
            return [f"```{language}\n{_block_text(block)}\n```"]
        if btype in ("bullet", "ordered", "todo"):
            marker = {"bullet": "- ", "ordered": "1. ", "todo": "- [ ] "}[btype]
            lines = []
            for child in children(bid):
                child_data = (block_map.get(child) or {}).get("data") or {}
                if child_data.get("type") in ("text", "todo"):
                    lines.append(marker + _block_text(block_map[child]))
                    lines.extend(render_children(children(child)))
                else:
                    lines.extend(render(child))
            return ["\n".join(lines)] if lines else []
        if btype == "quote":
            return ["\n".join("> " + line for line in _block_text(block).split("\n"))]
        if btype == "divider":
            return ["---"]
        if btype == "table":
            return _render_table(block, block_map)
        if btype == "image":
            return ["[图片]"]
        if btype in ("file", "drive"):
            return ["[文件]"]
        if btype == "whiteboard":
            return ["[画板]"]
        if btype == "callout":
            return [_block_text(block)]
        text = _block_text(block)
        if not text:
            return render_children(children(bid))
        return [text] if not children(bid) else [text] + render_children(children(bid))

    def render_children(bids: list[str]) -> list[str]:
        groups: list[str] = []
        for bid in bids:
            groups.extend(render(bid))
        return groups

    def _render_table(block: dict, block_map: dict) -> list[str]:
        data = block.get("data") or {}
        rows = data.get("rows_id") or []
        columns = data.get("columns_id") or []
        cells = data.get("cell_set") or {}
        if not rows or not columns:
            return []

        def cell_text(cell: dict) -> str:
            # 单元格的正文在它的 children 里（cell 本身是 table_cell 容器）。
            cell_block = block_map.get(cell.get("block_id")) or {}
            parts = [_block_text(block_map[child]) for child in
                     ((cell_block.get("data") or {}).get("children") or []) if child in block_map]
            text = " ".join(part for part in parts if part) or _block_text(cell_block)
            return text.replace("|", "\\|").replace("\n", " ")

        lines = []
        for index, row in enumerate(rows):
            values = [cell_text(cells.get(f"{row}{column}") or {}) for column in columns]
            if index == 0:
                lines.append("| " + " | ".join(values) + " |")
                lines.append("| " + " | ".join(["---"] * len(columns)) + " |")
            else:
                lines.append("| " + " | ".join(values) + " |")
        return ["\n".join(lines)]

    root = next((bid for bid in sequence
                 if ((block_map.get(bid) or {}).get("data") or {}).get("type") == "page"), None)
    order = children(root) if root else list(sequence)
    groups = render_children(order)
    if root:
        title = _block_text(block_map[root]).strip()
        if title:
            groups = [f'# {title}'] + groups
    return "\n\n".join(group for group in groups if group)


class SpaceClient:
    """云空间文件列表与云文档内容读取。鉴权用登录 cookie，和 LarkClient 通用。"""

    def __init__(self, auth: LarkAuth):
        self.auth = auth

    def _get(self, path: str, **params) -> dict:
        url = f'{self.auth.region.internal_api_host}{path}'
        response = requests.get(url, params=params, cookies=self.auth.cookies,
                                headers=self._headers(), timeout=30)
        response.raise_for_status()
        body = response.json()
        if body.get('code'):
            raise SpaceError(f'{path} 失败: {body.get("msg") or body.get("code")}')
        return body.get('data') or {}

    def _headers(self) -> dict:
        return {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36',
            'origin': f'https://{self.auth.region.domain}',
            'referer': f'https://{self.auth.region.domain}/',
            'x-appid': self.auth.gateway_app_id,
        }

    # ---------- 云空间 ----------

    def files(self) -> list[dict]:
        """我的云空间文件列表（根目录下的节点）。"""
        data = self._get('/space/api/explorer/v3/children/list/')
        nodes = ((data.get('entities') or {}).get('nodes') or {})
        return list(nodes.values())

    def folder_children(self, folder_token: str) -> list[dict]:
        """某个目录下的文件。"""
        data = self._get('/space/api/explorer/folder/children/', folder_token=folder_token)
        nodes = ((data.get('entities') or {}).get('nodes') or {})
        return list(nodes.values())

    def root(self) -> dict:
        """云空间根目录信息。"""
        return self._get('/space/api/explorer/root/')

    # ---------- 云文档 ----------

    def wiki_node(self, wiki_token: str) -> dict:
        """wiki 节点信息：真实文档 token、空间 id、标题、文档类型。"""
        data = self._get('/space/api/wiki/v2/tree/get_node/', wiki_token=wiki_token)
        node = data.get('node') or data
        return {'obj_token': node.get('obj_token') or wiki_token,
                'space_id': node.get('space_id') or '',
                'title': node.get('title') or '',
                'obj_type': node.get('obj_type')}

    def document_blocks(self, obj_token: str, *, space_id: str = '', wiki_token: str = '') -> dict:
        """文档正文的所有块：{block_map, block_sequence}。"""
        params = {'id': obj_token, 'mode': 1, 'limit': 239}
        if wiki_token:
            params.update({'wiki_space_id': space_id, 'container_type': 'wiki2.0', 'container_id': wiki_token})
        data = self._get('/space/api/docx/pages/client_vars', **params)
        # 表格的单元格块不在首屏（表格块被 skip_blocks 跳过），按表格块再拉一次。
        for table_id in data.get('skip_blocks') or []:
            extra = dict(params, mode=4, block_id=table_id)
            extra.pop('cursor', None)
            try:
                more = self._get('/space/api/docx/pages/client_vars', **extra)
            except SpaceError:
                continue
            data.setdefault('block_map', {}).update(more.get('block_map') or {})
        return data

    def document_markdown(self, url_or_token: str) -> str:
        """读一篇文档并转成 markdown。传 wiki 链接、docx 链接或文档 token 都行。"""
        token = url_or_token
        space_id, wiki_token = '', ''
        if '/wiki/' in url_or_token:
            wiki_token = url_or_token.rstrip('/').rsplit('/', 1)[-1]
            node = self.wiki_node(wiki_token)
            token = node['obj_token']
            space_id = node['space_id']
        elif url_or_token.startswith('http'):
            token = url_or_token.rstrip('/').rsplit('/', 1)[-1]
        data = self.document_blocks(token, space_id=space_id, wiki_token=wiki_token)
        block_map = data.get('block_map') or {}
        sequence = [str(x) for x in (data.get('block_sequence') or [])]
        return _blocks_to_markdown(block_map, sequence)
