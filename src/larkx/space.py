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
- POST /space/api/box/upload/prepare|finish + box/stream/upload/merge_block   上传文件
- POST /space/api/import/create + GET import/result   上传的文件转成在线文档

导入文档那套 box 接口在文档服务上，只用登录 cookie 就能写，不需要 CSRF。
云空间的增删改移动（建目录、删文件、改名等）走 internal-api 的 explorer 接口，
要额外带 X-CSRFToken（值是 _csrf_token cookie，任意一次 space/api 读接口的响应
就会下发它），暂时没有封装。编辑已有文档的正文走编辑器的 box-stream 私有同步
协议，也没有封装。
"""
from __future__ import annotations

import json
import re
import time
import uuid
import zlib

import requests

from .auth import LarkAuth

# Etherpad 风格的属性操作串：*N=应用属性号 N（base36），+M=接下来 M 个字符（base36）
_OP_RE = re.compile(r"(\*[0-9a-z]+|\|[0-9a-z]+|\+[0-9a-z]+|=[0-9a-z]+|-[0-9a-z]+)")
_URL_RE = re.compile(r"^(https?:|mailto:|tel:|/)", re.I)
_CSS_COLOR_RE = re.compile(r"^(#[0-9a-f]{3,8}|rgba?\(|hsla?\()", re.I)
_LISTS = ("bullet", "ordered", "todo")
_HEADINGS = {"heading1": 1, "heading2": 2, "heading3": 3, "heading4": 4, "heading5": 5, "heading6": 6}


class SpaceError(RuntimeError):
    """云空间/文档接口返回了业务错误。"""


def _unwrap(path: str, body: dict) -> dict:
    if not isinstance(body, dict):
        raise SpaceError(f'{path} 返回了非 JSON 响应')
    if body.get('code'):
        raise SpaceError(f'{path} 失败: {body.get("msg") or body.get("code")}')
    return body.get('data') or {}


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


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
        if btype in _LISTS:
            marker = {"bullet": "- ", "ordered": "1. ", "todo": "- [ ] "}[btype]
            lines = [marker + _block_text(block)] if _block_text(block) else []
            for child in children(bid):
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
        # 相邻的列表块之间只换行，不然会被渲染成一个个独立的段落。
        groups: list[str] = []
        previous = None
        for bid in bids:
            btype = ((block_map.get(bid) or {}).get("data") or {}).get("type")
            for group in render(bid):
                if groups and btype in _LISTS and previous in _LISTS:
                    groups[-1] += "\n" + group
                else:
                    groups.append(group)
            previous = btype
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
    """云空间文件列表、云文档读写。鉴权用登录 cookie，和 LarkClient 通用。"""

    def __init__(self, auth: LarkAuth):
        self.auth = auth

    def _get(self, path: str, host: str = '', **params) -> dict:
        url = f'{host or self.auth.region.internal_api_host}{path}'
        response = requests.get(url, params=params, cookies=self.auth.cookies,
                                headers=self._headers(), timeout=30)
        response.raise_for_status()
        return _unwrap(path, response.json())

    def _post(self, path: str, *, host: str = '', json_body=None, data=None, params: dict = '',
              content_type: str = '', headers: dict = '', timeout: int = 30) -> dict:
        """POST 一个 space 接口。json_body 走 json 编码，data 是已经拼好的请求体（裸字节或 JSON 文本）。"""
        url = f'{host or self.auth.region.space_api_host}{path}'
        merged = self._headers()
        if data is not None:
            merged['content-type'] = content_type or 'application/octet-stream'
        merged.update(headers)
        response = requests.post(url, params=params or None,
                                 **({'data': data} if data is not None else {'json': json_body}),
                                 cookies=self.auth.cookies, headers=merged, timeout=timeout)
        response.raise_for_status()
        return _unwrap(path, response.json())

    def _headers(self) -> dict:
        return {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36',
            'origin': f'https://{self.auth.region.domain}',
            'referer': f'https://{self.auth.region.domain}/',
            'x-appid': self.auth.gateway_app_id,
        }

    # ---------- 文档写入 ----------

    def import_markdown(self, markdown: str, parent_token: str, *, name: str = '',
                        parent_type: str = 'wiki') -> dict:
        """把 markdown 上传并由服务端转成一篇飞书云文档，返回 {token, url, type}。

        parent_token 是目标位置：parent_type='wiki'（默认）传知识库节点 token，
        'space' 传云空间文件夹 token（服务端会校验，传空会失败）。
        name 决定文档标题和解析格式，缺省按 .md 处理。

        协议是四步：box/upload/prepare 申请上传 → box/stream/upload/merge_block 传字节
        → box/upload/finish 拿 file_token → import/create 建转换任务、轮询 import/result。
        """
        host = self.auth.region.space_api_host
        blob = markdown.encode('utf-8') if isinstance(markdown, str) else markdown
        extension = name.rsplit('.', 1)[-1].lower() if '.' in name else 'md'
        name = name or f'doc-{uuid.uuid4().hex[:8]}.{extension}'
        obj_type = {'md': 'docx', 'markdown': 'docx', 'mark': 'docx', 'txt': 'docx',
                    'doc': 'docx', 'docx': 'docx'}.get(extension, 'docx')

        prepare = self._post('/space/api/box/upload/prepare/', host=host,
                              params={'shouldBypassScsDialog': 'true'},
                              json_body={'mount_point': 'ccm_import', 'mount_node_token': parent_token,
                                         'name': name, 'size': len(blob),
                                         'extra': {'extra': _dumps({'obj_type': obj_type,
                                                                    'file_extension': extension})},
                                         'size_checker': True})
        upload_id, block_size = prepare['upload_id'], prepare.get('block_size') or len(blob) or 1
        blocks = [blob[i:i + block_size] for i in range(0, max(len(blob), 1), block_size)]
        uploaded = self._post('/space/api/box/stream/upload/merge_block/', host=host,
                              params={'shouldBypassScsDialog': 'true', 'upload_id': upload_id,
                                      'mount_point': 'ccm_import'},
                              data=b''.join(blocks), timeout=120,
                              headers={'x-seq-list': ','.join(str(i) for i in range(len(blocks))),
                                       'x-block-list-checksum': ','.join(
                                           str(zlib.adler32(block) & 0xffffffff) for block in blocks),
                                       'x-block-origin-size': str(block_size)})
        if len(uploaded.get('success_seq_list') or []) != len(blocks):
            raise SpaceError(f'上传分片失败: {uploaded}')
        finish = self._post('/space/api/box/upload/finish/', host=host,
                            params={'shouldBypassScsDialog': 'true'},
                            json_body={'upload_id': upload_id, 'num_blocks': len(blocks),
                                       'mount_point': 'ccm_import', 'push_open_history_record': 0})

        # import/create 虽然收 multipart，真实请求体其实是这段 JSON 文本，照抄前端。
        task = self._post('/space/api/import/create/', host=host, content_type='application/json',
                          data=_dumps({'file_token': finish['file_token'], 'type': obj_type,
                                       'file_extension': extension,
                                       'point': {'mount_type': 2 if parent_type == 'wiki' else 1,
                                                 'mount_key': parent_token},
                                       'event_source': '6', 'client_token': str(uuid.uuid4()),
                                       'passback': _dumps({'time_zone': next((n for n in time.tzname
                                                                               if n), 'UTC')})}))
        ticket, deadline = task['ticket'], time.time() + int(task.get('job_timeout') or 600)
        while time.time() < deadline:
            time.sleep(2)
            result = self._get(f'/space/api/import/result/{ticket}', host=host).get('result') or {}
            if result.get('token'):
                return {'token': result['token'], 'url': result.get('url') or '',
                        'type': result.get('type') or ''}
            if result.get('job_status') == 3:
                raise SpaceError(f'导入失败: {result.get("job_error_msg") or result}')
        raise SpaceError('导入超时')

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
