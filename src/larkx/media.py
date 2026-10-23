import mimetypes
import os
import re
from urllib.parse import urlsplit

import requests
from loguru import logger
from .auth import LarkAuth
from .config import load_config
from .region import get_region

UPLOAD_TIMEOUT = 300
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36'


def _web_headers(auth: LarkAuth) -> dict:
    return {
        'origin': auth.region.web_origin,
        'referer': f'{auth.region.web_origin}/',
        'user-agent': USER_AGENT,
        'x-source': 'web',
        'x-appid': auth.gateway_app_id,
    }


def upload_image(auth: LarkAuth, data: bytes, name: str='image.png') -> str:
    """上传图片，返回图片 key（img_v3_…）；发图片消息前先调它。"""
    resp = requests.post(f'{auth.region.file_host}/api/image',
                         files={'image': (name, data)}, cookies=auth.cookies,
                         headers=_web_headers(auth), timeout=UPLOAD_TIMEOUT)
    resp.raise_for_status()
    key = re.search(r'img_[\w-]+', resp.text)
    if not key:
        raise ValueError(f'图片上传失败: {resp.text[:200]}')
    logger.info(f'图片已上传: {key.group(0)} ({len(data)} 字节)')
    return key.group(0)


def upload_file(auth: LarkAuth, chat_id: str, data: bytes, name: str) -> dict:
    """上传文件到会话（文件归属于会话），返回 {key, name, size, mime}。"""
    resp = requests.post(f'{auth.region.file_host}/api/chats/{chat_id}/files',
                         files={'file': (name, data)}, cookies=auth.cookies,
                         headers=_web_headers(auth), timeout=UPLOAD_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    info = body.get('data') or {}
    if body.get('status') != 'success' or not info.get('key'):
        raise ValueError(f"文件上传失败: {body.get('message') or body.get('status') or '响应里没有文件 key'}")
    out = {'key': str(info['key']), 'name': info.get('name') or name,
           'size': int(info.get('size') or len(data)),
           'mime': info.get('mimetype') or 'application/octet-stream'}
    logger.info(f"文件已上传: {out['key']} ({out['size']} 字节)")
    return out


def download(auth: LarkAuth, url: str, out_path: str=None, max_bytes: int=200 * 1024 * 1024):
    """下载文件。登录 cookie 只发给本区域（feishu.cn / larksuite.com）的地址。"""
    host = urlsplit(url).netloc.lower()
    same_region = host == auth.region.domain or host.endswith('.' + auth.region.domain)
    with requests.get(url, cookies=auth.cookies if same_region else {}, timeout=60, stream=True) as r:
        r.raise_for_status()
        content_type = r.headers.get('content-type', '')
        if not out_path:
            chunks = []
            size = 0
            for chunk in r.iter_content(1 << 16):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError('文件过大')
                chunks.append(chunk)
            return (b''.join(chunks), content_type)
        n = 0
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, 'wb') as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
                n += len(chunk)
                if n > max_bytes:
                    raise ValueError('文件过大')
        return (out_path, content_type, n)


def image_keys(content: dict) -> list[str]:
    """从消息内容里取图片 key（img_v3_…）。新版图片在 imageV2 里，旧版在 image 里。"""
    content = content or {}
    v2 = content.get("imageV2") or {}
    if v2.get("imageKey"):
        return [str(v2["imageKey"])]
    image = content.get("image") or {}
    origin = image.get("origin") or {}
    key = origin.get("key") or image.get("imageKey") or image.get("key")
    return [str(key)] if key else []


def download_message_resource(auth: LarkAuth, key: str, message_id: str = '') -> tuple[bytes, str]:
    """按资源 key 取回消息里的原始字节（图片/文件/视频/语音通用，服务端返回明文）。

    图片的 key 绑在消息上，要带上那条消息的 message_id；文件/视频/语音只认 key。
    返回 (字节, content-type)：飞书对视频也回 text/plain，这里按文件名重猜一次。
    """
    url = f'{auth.region.file_host}/download/messages/{message_id}/keys/{key}'
    resp = requests.get(url, cookies=auth.cookies, headers=_web_headers(auth), timeout=60)
    if resp.status_code == 400:
        raise KeyError(f'飞书不认这个资源 key: {key}')
    resp.raise_for_status()
    name = _attachment_name(resp.headers.get('content-disposition', ''))
    content_type = mimetypes.guess_type(name)[0] or resp.headers.get('content-type') or 'application/octet-stream'
    return resp.content, content_type


def _attachment_name(disposition: str) -> str:
    """从 Content-Disposition 里取文件名。"""
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition or '')
    return match.group(1).strip() if match else ''


def message_attachments(message_type: int, content: dict) -> list[dict]:
    """把消息内容里的资源整理成统一的附件列表：[{kind, key, name, size}]。

    kind 区分资源类型（图片/文件/视频/语音），因为它们的 key 前缀和下载方式都不一样；
    位置、名片这类没有资源的类型不进附件。
    """
    content = content or {}
    if message_type == 5:
        return [{"kind": "image", "key": key, "name": "", "size": int(content.get("originSize") or 0)}
                for key in image_keys(content)]
    if message_type in (3, 13):
        return [{"kind": "file", "key": content.get("key", ""), "name": content.get("name", ""),
                 "size": int(content.get("size") or 0)}]
    if message_type == 15:
        return [{"kind": "video", "key": content.get("key", ""), "name": content.get("name", ""),
                 "size": int(content.get("size") or 0)}]
    if message_type == 7:
        return [{"kind": "audio", "key": content.get("key", ""), "name": "",
                 "size": int(content.get("size") or 0)}]
    return []
