from types import SimpleNamespace

from larkx.media import download_message_resource


def test_download_message_resource_passes_max_bytes(monkeypatch):
    calls = []
    monkeypatch.setattr("larkx.media.download", lambda *args, **kwargs: calls.append((args, kwargs)))
    auth = SimpleNamespace(region=SimpleNamespace(file_host="https://files.example"))
    msg = {"msg_id": "m", "chat_id": "c", "msg_type": 3, "content_data": {"key": "k"}}
    download_message_resource(auth, msg, out_path="out.bin", max_bytes=123)
    assert calls[0][1]["max_bytes"] == 123
