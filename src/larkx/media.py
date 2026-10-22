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


def _upload_headers(auth: LarkAuth) -> dict:
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
                         headers=_upload_headers(auth), timeout=UPLOAD_TIMEOUT)
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
                         headers=_upload_headers(auth), timeout=UPLOAD_TIMEOUT)
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
