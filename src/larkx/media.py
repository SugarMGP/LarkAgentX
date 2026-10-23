import os
import re
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
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


def image_resources(content: dict) -> list[dict]:
    """从消息内容里取图片资源。

    content 是 decode_message_content 解出来的结构。新版图片在 imageV2 里：
    key 是图片 key（img_v3_…），crypto 是解密用的 {key, iv, aad}（没有就是明文图片），
    thumbnail 是消息里内联的缩略图字节（webp）。
    """
    out = []
    image = (content or {}).get("image") or {}
    v2 = (content or {}).get("imageV2") or {}
    if v2.get("imageKey"):
        cipher = ((v2.get("crypto") or {}).get("cipher")) or {}
        width = height = 0
        for variant in v2.get("variants") or []:
            size = variant.get("size") or {}
            width, height = int(size.get("width") or 0), int(size.get("height") or 0)
            break
        out.append({
            "key": str(v2.get("imageKey") or ""),
            "width": width,
            "height": height,
            "crypto": ({
                "key": cipher.get("key") or b"",
                "iv": cipher.get("iv") or b"",
                "aad": cipher.get("extra") or b"",
            } if cipher.get("key") else None),
            "thumbnail": v2.get("thumbnailInline") or b"",
        })
    elif image.get("imageKey") or image.get("key"):
        origin = image.get("origin") or {}
        out.append({
            "key": str(image.get("imageKey") or image.get("key") or ""),
            "width": int(origin.get("width") or 0),
            "height": int(origin.get("height") or 0),
            "crypto": None,
            "thumbnail": b"",
        })
    return out


def open_image(auth: LarkAuth, image_key: str):
    """打开图片的上游响应。字节是加密的（有 crypto 时要自己解密）。"""
    resp = requests.get(f'{auth.region.file_host}/static-resource/image/{image_key}',
                        cookies=auth.cookies, headers=_upload_headers(auth), stream=True, timeout=60)
    resp.raise_for_status()
    return resp


def decrypt_image(blob: bytes, crypto: dict) -> bytes:
    """用消息里的 crypto 解密图片字节（AES-256-GCM）。"""
    if not crypto:
        return blob
    return AESGCM(crypto["key"]).decrypt(crypto["iv"], blob, crypto.get("aad") or b"")


def sniff_image_type(blob: bytes) -> str:
    """看字节头猜图片类型。"""
    if blob.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if blob.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if blob.startswith(b'GIF8'):
        return 'image/gif'
    if blob[:4] == b'RIFF' and blob[8:12] == b'WEBP':
        return 'image/webp'
    if blob.startswith(b'BM'):
        return 'image/bmp'
    return 'application/octet-stream'


def message_attachments(message_type: int, content: dict) -> list[dict]:
    """把消息内容里的资源整理成统一的附件列表：[{kind, key, ...}]。

    各种消息类型带的资源不一样（图片是 key+宽高，文件是 key+名字+大小，视频还有时长），
    这里统一成 kind + 各自的字段，调用方不用认识飞书的字段名。
    """
    content = content or {}
    if message_type == 5:
        return [{"kind": "image", "key": item["key"], "width": item["width"], "height": item["height"]}
                for item in image_resources(content) if item.get("key")]
    if message_type in (3, 13):
        return [{"kind": "file", "key": content.get("key", ""), "name": content.get("name", ""),
                 "size": int(content.get("size") or 0), "mime": content.get("mime", "")}]
    if message_type == 15:
        return [{"kind": "video", "key": content.get("key", ""), "name": content.get("name", ""),
                 "size": int(content.get("size") or 0), "duration": int(content.get("duration") or 0)}]
    if message_type == 7:
        return [{"kind": "audio", "key": content.get("key", ""),
                 "duration": int(content.get("duration") or 0)}]
    if message_type == 10:
        return [{"kind": "sticker", "key": content.get("key", ""), "sticker_id": content.get("stickerId", "")}]
    if message_type == 20:
        return [{"kind": "location", "name": content.get("name", ""),
                 "latitude": content.get("latitude", ""), "longitude": content.get("longitude", "")}]
    if message_type == 23:
        return [{"kind": "user_card", "user_id": content.get("userId", ""), "name": content.get("name", "")}]
    if message_type == 2:
        return [{"kind": "post", "title": content.get("title", "")}]
    if message_type == 14:
        header = content.get("cardHeader") or {}
        return [{"kind": "card", "title": header.get("title") or header.get("mainTitle") or ""}]
    if message_type == 11:
        return [{"kind": "merge_forward", "count": len(content.get("messages") or [])}]
    if message_type == 9:
        chat = content.get("chat") or {}
        return [{"kind": "group_share", "chat_id": content.get("chatId") or chat.get("id", ""),
                 "name": chat.get("name", "")}]
    if message_type == 16:
        return [{"kind": "calendar_event", "attendees_count": int(content.get("attendeesCount") or 0)}]
    return []
