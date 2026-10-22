"""飞书云空间（文件/文档管理）接口。

逆向结论：这套接口和 IM 网关不是一套协议——走 JSON over HTTP，域名也不同
（云空间在 internal-api.<domain>，文档/空间服务在 internal-api-space.<domain>），
路径来自文档页内嵌配置和前端 JS 里的接口表。

只封装已用真实账号验证过的读接口：
- GET /space/api/explorer/v3/children/list/    我的云空间文件列表
- GET /space/api/explorer/folder/children/      某个目录下的文件
- GET /space/api/explorer/root/                 云空间根目录

写操作（建目录、删文件、移动、改名、评论等）需要请求头 X-CSRFToken，
值取自 cookie _csrf_token；网页端登录的会话里没有这个 cookie，所以没有封装。
文档正文（段落级增删改）走编辑器的 box-stream 私有同步协议，没有 REST 等价接口。
"""
from __future__ import annotations

import requests

from .auth import LarkAuth


class SpaceError(RuntimeError):
    """云空间接口返回了业务错误。"""


class SpaceClient:
    """云空间文件列表查询。鉴权用登录 cookie，和 LarkClient 通用。"""

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
