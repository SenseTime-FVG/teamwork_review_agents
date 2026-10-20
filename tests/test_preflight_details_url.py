"""本地 CI 详情地址的配置、安全边界及网卡回退，不访问真实网络。"""

from __future__ import annotations

import socket

import pytest
from pydantic import ValidationError

from teamwork_review_agents.config import WebConfig
from teamwork_review_agents import service_urls


@pytest.mark.parametrize("value,expected", [
    (None, None), ("  ", None),
    (" https://review.example.com/ ", "https://review.example.com"),
    ("https://review.example.com/teamwork/", "https://review.example.com/teamwork"),
    ("http://[fd00::20]:8090/", "http://[fd00::20]:8090"),
])
def test_public_url_normalizes_http_base(value, expected):
    """允许 HTTPS 反代和路径前缀，空值使用监听地址推导。"""

    assert WebConfig(public_url=value).public_url == expected


@pytest.mark.parametrize("value", [
    "javascript:alert(1)", "file:///tmp/log", "//review.example.com",
    "https://user:secret@review.example.com", "https://user@review.example.com",
    "https://review.example.com/?token=secret", "https://review.example.com/#token=secret",
    "https://review.example.com/?", "https://review.example.com/#",
    "http://0.0.0.0:8080", "http://[::]:8080", "http://review.example.com:99999",
    "http://review.example.com:0", "https://review.example.com/log\nsecret",
    "https://review.example.com/has space", "https://review.example.com\\secret",
])
def test_public_url_rejects_credentials_and_non_browser_addresses(value):
    """配置阶段拒绝把凭据或非浏览器目标发布到远端状态。"""

    with pytest.raises(ValidationError, match="对外访问地址"):
        WebConfig(public_url=value)


def test_explicit_public_url_skips_interface_detection(monkeypatch):
    """显式域名不受 VPN、监听通配或内部端口影响，链接只携带运行 ID。"""

    def unexpected(_family):
        """显式地址不应触发网卡查询。"""

        pytest.fail("显式地址不应查询网卡")

    monkeypatch.setattr(service_urls, "local_interface_addresses", unexpected)
    web = WebConfig(host="0.0.0.0", port=8080, public_url="https://review.example.com/teamwork/")
    assert service_urls.preflight_details_url(web, "run-123") == "https://review.example.com/teamwork/#preflight=run-123"


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", "http://127.0.0.1:9090/#preflight=run-123"),
    ("review.internal", "http://review.internal:9090/#preflight=run-123"),
    ("fd00::20", "http://[fd00::20]:9090/#preflight=run-123"),
])
def test_specific_host_uses_configured_port(host, expected):
    """具体监听地址沿用已配置端口，不把任意网卡当作可访问地址。"""

    assert service_urls.preflight_details_url(WebConfig(host=host, port=9090), "run-123") == expected


@pytest.mark.parametrize("host,family,address,expected", [
    ("0.0.0.0", socket.AF_INET, "10.2.0.3", "http://10.2.0.3:8080/#preflight=run-123"),
    ("::", socket.AF_INET6, "fd00::20", "http://[fd00::20]:8080/#preflight=run-123"),
])
def test_wildcard_uses_only_unambiguous_interface(monkeypatch, host, family, address, expected):
    """相同地址出现在多张网卡仍可确定目标，IPv4/IPv6 严格按监听族选择。"""

    def interfaces(actual_family):
        """只返回指定监听族对应的虚构网卡。"""

        assert actual_family == family
        return [("first", address), ("duplicate", address)]

    monkeypatch.setattr(service_urls, "local_interface_addresses", interfaces)
    assert service_urls.preflight_details_url(WebConfig(host=host), "run-123") == expected


@pytest.mark.parametrize("addresses", [[], [("lan", "10.2.0.3"), ("vpn", "10.8.0.2")]])
def test_ambiguous_or_missing_interfaces_do_not_publish_guessed_link(monkeypatch, addresses):
    """没有唯一地址时保持无详情链接，不改变本地 CI 状态发布。"""

    monkeypatch.setattr(service_urls, "local_interface_addresses", lambda _family: addresses)
    assert service_urls.preflight_details_url(WebConfig(host="0.0.0.0"), "run-123") is None
