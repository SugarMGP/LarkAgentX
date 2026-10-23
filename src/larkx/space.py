"""飞书云空间与云文档接口。

逆向结论：这套接口和 IM 网关不是一套协议——走 JSON over HTTP，域名也不同
（云空间在 internal-api.<domain>，文档/空间服务在 internal-api-space.<domain>），
路径来自文档页内嵌配置和前端 JS 里的接口表。

已用真实账号验证的能力：
- GET  /space/api/explorer/v3/children/list/          我的云空间文件列表（含"我的空间"根目录节点）
- GET  /space/api/explorer/folder/children/            某个目录下的文件
- GET  /space/api/wiki/v2/tree/get_node/               wiki 节点 → 真实文档 token
- GET  /space/api/docx/pages/client_vars               文档正文（块）
- POST /space/api/box/upload/prepare|finish + box/stream/upload/merge_block   上传文件
- POST /space/api/import/create + GET import/result   上传的文件转成在线文档
- POST /space/api/suite/permission/members/create/     给文档加协作者

导入文档那套 box 接口在文档服务上，只用登录 cookie 就能写，不需要 CSRF。
云空间的增删改移动（建目录、删文件、改名等）和加协作者走 explorer/suite 接口，
要额外带 X-CSRFToken（值是 _csrf_token cookie，任意一次 space/api 读接口的响应
就会下发它），已封装。编辑已有文档的正文走编辑器的 box-stream 私有同步协议，
没有封装。
"""
from __future__ import annotations

import json
import random
import re
import string
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
# 可插入/可切换的块类型：正文、1~9 级标题、列表、待办、引用、代码块、分割线
BLOCK_TYPES = frozenset({"text", "bullet", "ordered", "todo", "quote", "code", "divider"}
                        | {f"heading{level}" for level in range(1, 10)})
_BLOCK_ID_ALPHABET = string.ascii_letters + string.digits
# 云文档对象在空间接口里的 obj_type
OBJ_TYPE_DOC = 22
# 协作者权限位（飞书位掩码）：可阅读 1、可评论 2、可编辑 4
PERM_READABLE = 1
PERM_COMMENTABLE = 2
PERM_EDITABLE = 4


class SpaceError(RuntimeError):
    """云空间/文档接口返回了业务错误。"""


def _check_block_type(block_type: str) -> None:
    if block_type not in BLOCK_TYPES:
        raise SpaceError(f'不支持的块类型: {block_type}')


def _unwrap(path: str, body: dict) -> dict:
    if not isinstance(body, dict):
        raise SpaceError(f'{path} 返回了非 JSON 响应')
    if body.get('code'):
        raise SpaceError(f'{path} 失败: {body.get("msg") or body.get("code")}')
    return body.get('data') or {}


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _b36(number: int) -> str:
    """base36 小写：飞书 changeset 里数字的编码方式。"""
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
    if number < 36:
        return digits[number]
    return _b36(number // 36) + digits[number % 36]


def _new_block_id(taken: dict) -> str:
    """飞书块 id 是 27 位大小写字母数字；跟文档里已有的撞了就重摇。"""
    while True:
        candidate = ''.join(random.choices(_BLOCK_ID_ALPHABET, k=27))
        if candidate not in taken:
            return candidate


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
        elif code == "|":
            # insertAttribute：换成这个样式，不是叠加。
            pending = [int(num, 36)]
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
        self._csrf = ''

    def _get(self, path: str, host: str = '', **params) -> dict:
        url = f'{host or self.auth.region.internal_api_host}{path}'
        response = requests.get(url, params=params, cookies=self.auth.cookies,
                                headers=self._headers(), timeout=30)
        self._raise_for_status(response, path)
        self._remember_csrf(response)
        return _unwrap(path, response.json())

    @staticmethod
    def _raise_for_status(response, path: str) -> None:
        """HTTP 错误统一抛 SpaceError，调用方才能按业务错误统一处理。"""
        if response.status_code >= 400:
            raise SpaceError(f'{path} HTTP {response.status_code}: {response.text[:200]}')

    def _remember_csrf(self, response) -> None:
        """记下 _csrf_token：写操作要带的 X-CSRFToken 就是它，读接口的响应会下发。"""
        token = response.cookies.get('_csrf_token')
        if token:
            self._csrf = token

    def _csrf_headers(self) -> dict:
        if not self._csrf:
            self._get('/space/api/explorer/v3/children/list/', host=self.auth.region.space_api_host)
        return {'X-CSRFToken': self._csrf}

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
        self._raise_for_status(response, path)
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

    def root_token(self) -> str:
        """我的云空间根目录 token：导入文档默认落在这里。"""
        for node in self.files():
            # 根目录节点的 token 和 obj_token 相同；普通子节点的节点 token 与对象 token 不同。
            if node.get('type') == 4 and node.get('token') == node.get('obj_token'):
                return str(node.get('token') or '')
        raise SpaceError('未找到云空间根目录')

    def folder_children(self, folder_token: str) -> list[dict]:
        """某个目录下的文件。"""
        data = self._get('/space/api/explorer/folder/children/', folder_token=folder_token)
        nodes = ((data.get('entities') or {}).get('nodes') or {})
        return list(nodes.values())

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
        skipped = list(data.get('skip_blocks') or [])
        # 长文档要按 cursor 翻页，否则只拿到前 239 块。
        while data.get('has_more') and data.get('cursor'):
            page = self._get('/space/api/docx/pages/client_vars', **dict(params, cursor=data['cursor']))
            cursor = page.get('cursor')
            if not page.get('block_map') or cursor == data.get('cursor'):
                break
            data.setdefault('block_map', {}).update(page.get('block_map') or {})
            data['block_sequence'] = list(data.get('block_sequence') or []) + list(page.get('block_sequence') or [])
            skipped += list(page.get('skip_blocks') or [])
            data['cursor'] = cursor
            data['has_more'] = page.get('has_more')
        # 表格的单元格块不在首屏（表格块被 skip_blocks 跳过），按表格块再拉一次。
        for table_id in skipped:
            extra = dict(params, mode=4, block_id=table_id)
            try:
                more = self._get('/space/api/docx/pages/client_vars', **extra)
            except SpaceError:
                continue
            data.setdefault('block_map', {}).update(more.get('block_map') or {})
        return data

    def _doc_token(self, url_or_token: str) -> tuple[str, str, str]:
        """(真实文档 token, space_id, wiki_token)：wiki 链接、docx 链接或 token 都行。"""
        # 分享链接常带 ?from= 或 #，文档 token 不能带查询串。
        url_or_token = url_or_token.strip().split('?', 1)[0].split('#', 1)[0]
        token = url_or_token
        space_id, wiki_token = '', ''
        if '/wiki/' in url_or_token:
            wiki_token = url_or_token.rstrip('/').rsplit('/', 1)[-1]
            node = self.wiki_node(wiki_token)
            token = node['obj_token']
            space_id = node['space_id']
        elif url_or_token.startswith('http'):
            token = url_or_token.rstrip('/').rsplit('/', 1)[-1]
        elif not token.startswith('doxcn'):
            # 可能是知识库节点 token：节点里的文档要用真实文档 token 查正文和权限。
            try:
                node = self.wiki_node(token)
            except SpaceError:
                node = {}
            if node.get('obj_token'):
                token, space_id, wiki_token = node['obj_token'], node.get('space_id', ''), token
        return token, space_id, wiki_token

    def document_markdown(self, url_or_token: str) -> str:
        """读一篇文档并转成 markdown。传 wiki 链接、docx 链接或文档 token 都行。"""
        token, space_id, wiki_token = self._doc_token(url_or_token)
        data = self.document_blocks(token, space_id=space_id, wiki_token=wiki_token)
        block_map = data.get('block_map') or {}
        sequence = [str(x) for x in (data.get('block_sequence') or [])]
        return _blocks_to_markdown(block_map, sequence)

    # ---------- 编辑正文 ----------

    def document_outline(self, url_or_token: str) -> dict:
        """文档的章节结构：{token, title, sections:[{id, level, title, blocks:[{id, type, text}]}]}。

        用来定位"要改哪一段"：每个标题带上它下面的正文块，块 id 可以直接传给 update_block_text。
        """
        token, _, _ = self._doc_token(url_or_token)
        data = self.document_blocks(token)
        blocks = data.get('block_map') or {}
        order = [b for b in (data.get('block_sequence') or list(blocks)) if b in blocks]
        title = ''
        sections: list[dict] = []
        current: dict | None = None
        for block_id in order:
            meta = blocks[block_id].get('data') or {}
            kind = str(meta.get('type') or '')
            text = _block_text(blocks[block_id])
            if kind == 'page':
                title = text
            elif kind.startswith('heading'):
                current = {"id": block_id, "level": int(kind[7:] or 1), "title": text, "blocks": []}
                sections.append(current)
            elif kind == 'text' and current is not None:
                current["blocks"].append({"id": block_id, "type": kind, "text": text})
        return {"token": token, "title": title, "sections": sections}

    def update_block_text(self, url_or_token: str, block_id: str, text: str, *, append: bool = False) -> dict:
        """改一个正文块的内容（append=True 时在原文后面追加），返回服务端的响应。

        协议：先读块拿到当前正文和版本，再造 changeset 调
        POST /space/api/docx/blocks/user_change。changeset 形如
        ``Z:<旧长度><长度差><操作串>*0+<新长度>$<新正文>``。
        """
        token, _, _ = self._doc_token(url_or_token)
        block = self.document_blocks(token).get('block_map', {}).get(block_id)
        if block is None:
            raise SpaceError(f'文档里没有这个块: {block_id}')
        old = _block_text(block)
        version = int(block.get('version') or 1)
        keep = len(old) if append else 0
        ops = f"={_b36(keep)}" + ("" if append else f"-{_b36(len(old))}")
        return self._post('/space/api/docx/blocks/user_change',
                          json_body={"member_id": "10000000000000", "uuid": str(uuid.uuid4()),
                                     "page_id": token,
                                     "change_map": {block_id: {"id": block_id, "version": version,
                                                             "payload": {"ops": [
                                                                 self._text_op(len(old), text, ops)]}}}},
                          headers=self._csrf_headers())

    def _text_op(self, old_len: int, text: str, keep_ops: str) -> dict:
        """把块正文改成 text 的一条 changeset 操作（text 之前的保留/删除由 keep_ops 描述）。"""
        length = len(text)
        delta = length - old_len
        diff = f">{_b36(delta)}" if delta >= 0 else f"<{_b36(-delta)}"
        changeset = f"Z:{_b36(old_len)}{diff}{keep_ops}*0+{_b36(length)}${text}"
        return {"p": ["text"], "subType": {"t": "easysync", "o": {
            "zone_changesets": {"0": changeset},
            "apool": {"numToAttrib": {"0": ["author", self.auth.user_id]}, "nextNum": 1,
                      "attribToNum": {f"author,{self.auth.user_id}": 0}}}}}

    def insert_block(self, url_or_token: str, text: str = '', *, block_type: str = 'text',
                     parent_id: str = '', index: int | None = None, after: str = '') -> dict:
        """插入一个新块，返回 {"block_id", "parent_id", "index"}。

        协议：一次 user_change 带两条 change_map —— 新块自己（version 0，一条
        ``{"p": [], "action": {"oi": 块数据}}``）和它的父块
        （``{"p": ["children", 序号], "action": {"li": 新块 id}}``）。

        block_type 取 BLOCK_TYPES 里的值。位置三选一：after 某个块（插在它后面）、
        给 index、都不给就追加到父块末尾；parent_id 不给就是文档本身（顶层块）。
        """
        _check_block_type(block_type)
        token, _, _ = self._doc_token(url_or_token)
        blocks = self.document_blocks(token).get('block_map') or {}
        if after:
            host = blocks.get(after)
            if host is None:
                raise SpaceError(f'文档里没有这个块: {after}')
            parent_id = str((host.get('data') or {}).get('parent_id') or '')
            siblings = (blocks.get(parent_id, {}).get('data') or {}).get('children') or []
            index = list(siblings).index(after) + 1
        parent = blocks.get(parent_id or token)
        if parent is None:
            raise SpaceError(f'文档里没有这个父块: {parent_id}')
        children = list((parent.get('data') or {}).get('children') or [])
        index = len(children) if index is None else max(0, min(int(index), len(children)))
        block_id = _new_block_id(blocks)
        new_block = {"parent_id": parent.get('id') or token, "type": block_type,
                     "children": [], "comments": [], "revisions": [],
                     "author": self.auth.user_id,
                     # 新块只能空着建，正文用同一次请求里的 changeset 写（带文字会被判 schema mismatch）。
                     "text": {"initialAttributedTexts": {"text": {"0": ""}, "attribs": {"0": ""}},
                              "apool": {"numToAttrib": {}, "nextNum": 0}},
                     "folded": False}
        ops = [{"p": [], "action": {"oi": new_block}}]
        if text:
            ops.append(self._text_op(0, text, ''))
        response = self._post_block_op(token, {
            block_id: {"id": block_id, "version": 0, "payload": {"ops": ops}},
            parent["id"]: {"id": parent["id"], "version": int(parent.get('version') or 1),
                           "payload": {"ops": [
                               {"p": ["children", index], "action": {"li": block_id}}]}}})
        return {"block_id": block_id, "parent_id": parent.get('id') or token, "index": index,
                "response": response}

    def set_block_type(self, url_or_token: str, block_id: str, block_type: str) -> dict:
        """把已有块换成另一种类型（段↔标题↔列表↔待办↔引用↔代码块），返回服务端响应。

        协议：在块上发一条 ``{"p": ["type"], "action": {"od": 旧类型, "oi": 新类型}}``。
        """
        _check_block_type(block_type)
        token, _, _ = self._doc_token(url_or_token)
        block = self.document_blocks(token).get('block_map', {}).get(block_id)
        if block is None:
            raise SpaceError(f'文档里没有这个块: {block_id}')
        old_type = str((block.get('data') or {}).get('type') or 'text')
        op = {"p": ["type"], "action": {"od": old_type, "oi": block_type}}
        return self._post_block_op(token, {block_id: {"id": block_id, "version": int(block.get('version') or 1),
                                                      "payload": {"ops": [op]}}})

    def delete_block(self, url_or_token: str, block_id: str) -> dict:
        """删掉一个块（连同它的子块）。返回服务端的响应。

        协议：在父块上发一条块树操作 {"p": ["children", 序号], "action": {"ld": 块 id}}。
        """
        token, _, _ = self._doc_token(url_or_token)
        data = self.document_blocks(token)
        blocks = data.get('block_map') or {}
        block = blocks.get(block_id)
        if block is None:
            raise SpaceError(f'文档里没有这个块: {block_id}')
        parent_id = str((block.get('data') or {}).get('parent_id') or '')
        parent = blocks.get(parent_id) or {}
        children = (parent.get('data') or {}).get('children') or []
        if block_id not in children:
            raise SpaceError(f'这个块不在父块的子块列表里: {block_id}')
        index = children.index(block_id)
        op = {"p": ["children", index], "action": {"ld": block_id}}
        return self._post_block_op(token, {parent_id: {"id": parent_id,
                                                      "version": int(parent.get('version') or 1),
                                                      "payload": {"ops": [op]}}})

    def delete_section(self, url_or_token: str, heading_block_id: str) -> list[dict]:
        """删掉一个章节：标题块和它下面的正文块。返回每个被删块的响应。"""
        token, _, _ = self._doc_token(url_or_token)
        data = self.document_blocks(token)
        blocks = data.get('block_map') or {}
        order = [b for b in (data.get('block_sequence') or list(blocks)) if b in blocks]
        if heading_block_id not in order:
            raise SpaceError(f'文档里没有这个标题: {heading_block_id}')
        heading = blocks.get(heading_block_id) or {}
        level = int(str((heading.get('data') or {}).get('type') or 'heading1')[7:] or 1)
        victims = [heading_block_id]
        for block_id in order[order.index(heading_block_id) + 1:]:
            meta = blocks.get(block_id, {}).get('data') or {}
            kind = str(meta.get('type') or '')
            if kind.startswith('heading') and int(kind[7:] or 1) <= level:
                break
            victims.append(block_id)
        return [self.delete_block(token, block_id) for block_id in victims]

    def _post_block_op(self, token: str, change_map: dict) -> dict:
        return self._post('/space/api/docx/blocks/user_change',
                          json_body={"member_id": "10000000000000", "uuid": str(uuid.uuid4()),
                                     "page_id": token, "change_map": change_map},
                          headers=self._csrf_headers())

    # ---------- 文档元数据与权限 ----------

    def document_token(self, url_or_token: str) -> str:
        """真实文档 token：wiki 链接或知识库节点 token 会被换成文档 token（权限接口只认它）。"""
        return self._doc_token(url_or_token)[0]

    def document_meta(self, url_or_token: str) -> dict:
        """文档元数据：标题、所有者、创建/最后修改人、时间、密级等。

        wiki 链接会自动换成真实文档 token（这些接口只认文档 token）。
        """
        token, _, _ = self._doc_token(url_or_token)
        return self._get('/space/api/meta/', token=token, type=OBJ_TYPE_DOC)

    def document_path(self, url_or_token: str) -> dict:
        """文档所在位置：云空间/知识库的层级路径，nodes 里是各级节点。"""
        token, _, _ = self._doc_token(url_or_token)
        return self._get('/space/api/explorer/v2/obj/path/', obj_token=token, obj_type=OBJ_TYPE_DOC)

    def document_members(self, url_or_token: str) -> dict:
        """文档的协作者（可访问人）列表：members 是人，entities.users 是他们的资料。"""
        token, _, _ = self._doc_token(url_or_token)
        return self._get('/space/api/suite/permission/members.v2/', token=token, type=OBJ_TYPE_DOC)

    def remove_member(self, token: str, owner_id: str, owner_type: int = 1,
                      obj_type: int = OBJ_TYPE_DOC) -> dict:
        """把协作者从文档上移除。owner_id 是飞书数字 id，owner_type：1=用户、2=群组。"""
        return self._post('/space/api/suite/permission/members/delete/',
                           data={'token': token, 'type': str(obj_type),
                                 'owner_id': str(owner_id), 'owner_type': str(owner_type)},
                           content_type='application/x-www-form-urlencoded',
                           headers=self._csrf_headers())

    def update_title(self, url_or_token: str, title: str) -> dict:
        """改文档标题。"""
        token, _, _ = self._doc_token(url_or_token)
        return self._post('/space/api/doc.v2/update_title',
                          json_body={'token': token, 'obj_type': OBJ_TYPE_DOC, 'title': title},
                          headers=self._csrf_headers())

    # ---------- 协作权限 ----------

    def search_user(self, query: str) -> list[dict]:
        """按关键词搜租户里的人/群，候选带 owner_id、owner_name。"""
        data = self._get('/space/api/search/permission_candidates/',
                         host=self.auth.region.space_api_host, query=query)
        return list(data.get('candidates') or [])

    def add_member(self, token: str, user_ids: list[str], permission: int = PERM_EDITABLE,
                   obj_type: int = OBJ_TYPE_DOC, owner_type: int = 1) -> dict:
        """给文档加协作者：user_ids 是飞书数字 id，permission 是权限位掩码。

        owner_type：1=用户、2=群组。只有文档所有者能加协作者。这里 notify_lark=0
        表示不通知对方，链接由调用方自己发出去。
        """
        owners = [{'owner_id': user_id, 'owner_type': owner_type, 'permission': permission}
                  for user_id in user_ids]
        result = self._post('/space/api/suite/permission/members/create/',
                             data={'token': token, 'type': str(obj_type), 'owners': _dumps(owners),
                                   'notify_lark': '0', 'send_lark_im': 'false', 'lark_im_text': ''},
                             content_type='application/x-www-form-urlencoded',
                             headers=self._csrf_headers())
        if (result.get('success_count') or 0) != len(user_ids):
            raise SpaceError(f'加协作者失败: {result}')
        return result
