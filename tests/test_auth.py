from types import SimpleNamespace

import pytest

from larkx.auth import AuthExpired, LarkAuth
from larkx.region import get_region


def auth_stub(tmp_path):
    return LarkAuth(path=tmp_path / "credentials.json")


def test_failed_cookie_import_keeps_the_working_credentials(tmp_path, monkeypatch):
    auth = auth_stub(tmp_path)
    auth.cookies = {"session": "GOOD"}
    auth.save()
    monkeypatch.setattr(LarkAuth, "validate", lambda self, fetch_profile=False: (False, "会话已被服务端注销"))
    ok, msg = auth.adopt_cookies({"session": "STALE"}, get_region("feishu"))
    assert ok is False
    # 校验没过就不能把还能用的凭证覆盖掉。
    assert auth.load() and auth.cookies == {"session": "GOOD"}


def test_network_failure_is_not_reported_as_expired_session(tmp_path, monkeypatch):
    import requests

    auth = auth_stub(tmp_path)
    auth.cookies = {"session": "x"}
    auth.device_id = "d"

    def boom(*args, **kwargs):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr("larkx.auth.requests.get", boom)
    # 网络问题要往上抛，require_valid 才能区分"没网"和"凭证过期"。
    with pytest.raises(requests.ConnectionError):
        auth.validate()
