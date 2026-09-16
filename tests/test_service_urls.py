"""使用虚构网卡验证启动地址提示，不访问外网或修改网络配置。"""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from teamwork_review_agents import cli, service_urls
from teamwork_review_agents.process_manager import management_url


@pytest.fixture
def interfaces(monkeypatch):
    """覆盖多网卡、重复 IP、停用网卡和不可作为访问目标的地址。"""

    def address(ip, family=socket.AF_INET):
        """只构造地址发现实际读取的两个字段。"""

        return SimpleNamespace(address=ip, family=family)

    addresses = {
        "lo": [address("127.0.0.1"), address("::1", socket.AF_INET6)],
        "Wi-Fi": [address("192.168.1.20"), address("fd00::20", socket.AF_INET6)],
        "Ethernet": [address("10.2.0.3"), address("10.2.0.3"), address("fd00::3", socket.AF_INET6)],
        "VPN": [address("10.8.0.2")],
        "disabled": [address("192.168.2.30")],
        "unknown-state": [address("192.168.3.40")],
        "invalid": [address("0.0.0.0"), address("224.0.0.1"), address("169.254.1.2"),
                    address("255.255.255.255"), address("invalid-ip"), address("00:11:22:33:44:55", -1),
                    address("fe80::1%invalid", socket.AF_INET6), address("::", socket.AF_INET6),
                    address("ff02::1", socket.AF_INET6), address("::ffff:192.168.1.20", socket.AF_INET6)],
    }
    states = {name: SimpleNamespace(isup=name != "disabled") for name in addresses if name != "unknown-state"}
    addrs_mock, stats_mock = Mock(return_value=addresses), Mock(return_value=states)
    monkeypatch.setattr(service_urls.psutil, "net_if_addrs", addrs_mock)
    monkeypatch.setattr(service_urls.psutil, "net_if_stats", stats_mock)
    return addrs_mock, stats_mock


def test_ipv4_wildcard_lists_all_enabled_candidates(interfaces):
    """所有候选标注网卡并带实际端口，不把 IPv6、停用或重复地址混入 IPv4。"""

    message = service_urls.service_access_message("0.0.0.0", 9091, admin_token_required=True)
    assert "监听地址：0.0.0.0:9091" in message
    assert "本机访问：http://127.0.0.1:9091" in message
    assert "候选（Ethernet）：http://10.2.0.3:9091" in message
    assert "候选（Wi-Fi）：http://192.168.1.20:9091" in message
    assert "候选（VPN）：http://10.8.0.2:9091" in message
    assert message.count("http://10.2.0.3:9091") == 1
    for excluded in ("fd00", "disabled", "unknown-state", "169.254", "224.0", "invalid-ip", "255.255"):
        assert excluded not in message
    assert "需要管理员 Token（本机与局域网相同）" in message
    assert "防火墙" in message and "WSL" in message


def test_ipv6_wildcard_does_not_claim_ipv4_listener(interfaces):
    """IPv6 使用方括号，不把 IPv4 网卡或带作用域的链路本地地址当作双栈可达。"""

    message = service_urls.service_access_message("::", 8090)
    assert "监听地址：[::]:8090" in message
    assert "本机访问：http://[::1]:8090" in message
    assert "http://[fd00::20]:8090" in message
    assert "http://[fd00::3]:8090" in message
    assert "192.168" not in message and "127.0.0.1" not in message and "fe80" not in message
    assert management_url("::", 8090) == "http://[::1]:8090"
    assert service_urls.http_url("fe80::1%en0", 8090) == "http://[fe80::1%25en0]:8090"


@pytest.mark.parametrize("host,url,label", [
    ("127.0.0.1", "http://127.0.0.1:9000", "本机访问"),
    ("localhost", "http://localhost:9000", "本机访问"),
    ("::1", "http://[::1]:9000", "本机访问"),
    ("192.168.1.20", "http://192.168.1.20:9000", "访问地址"),
    ("fd00::20", "http://[fd00::20]:9000", "访问地址"),
    ("review.internal", "http://review.internal:9000", "访问地址"),
])
def test_specific_binding_never_advertises_other_interfaces(interfaces, host, url, label):
    """具体绑定只展示该端点，尤其不能谎报回环地址或其他网卡也能访问。"""

    message = service_urls.service_access_message(host, 9000, admin_token_required=False)
    assert f"{label}：{url}" in message
    assert "局域网访问候选" not in message
    assert "未配置管理员 Token" in message
    assert ("仅本机" in message) is (label == "本机访问")
    interfaces[0].assert_not_called()
    interfaces[1].assert_not_called()


@pytest.mark.parametrize("function", ["net_if_addrs", "net_if_stats"])
@pytest.mark.parametrize("error", [OSError("internal-detail"), psutil.AccessDenied(), RuntimeError("internal-detail")])
def test_interface_failure_is_only_a_display_fallback(interfaces, monkeypatch, function, error):
    """网卡接口报错不泄漏异常正文，也不阻断启动成功提示。"""

    monkeypatch.setattr(service_urls.psutil, function, Mock(side_effect=error))
    message = service_urls.service_access_message("0.0.0.0", 8080)
    assert "http://127.0.0.1:8080" in message
    assert "未检测到可用候选地址" in message
    assert "internal-detail" not in message


def test_no_lan_interface_keeps_loopback_message(monkeypatch):
    """离线或只有回环网卡时不编造地址。"""

    monkeypatch.setattr(service_urls.psutil, "net_if_addrs", lambda: {})
    monkeypatch.setattr(service_urls.psutil, "net_if_stats", lambda: {})
    message = service_urls.service_access_message("0.0.0.0", 8080)
    assert "未检测到可用候选地址" in message
    assert "http://127.0.0.1:8080" in message


@pytest.mark.parametrize("ready", [True, False])
async def test_foreground_prints_only_after_server_is_ready(monkeypatch, tmp_path, capsys, interfaces, ready):
    """前台端口覆盖和认证说明沿用实际参数，启动失败前不输出可访问提示。"""

    config = SimpleNamespace(web=SimpleNamespace(host="0.0.0.0", port=8080, admin_token_env="FIXTURE_ADMIN_TOKEN"))
    monkeypatch.setenv("FIXTURE_ADMIN_TOKEN", "never-print-fixture-secret")
    monkeypatch.setattr(cli, "load_config", lambda *args: config)
    monkeypatch.setattr(cli, "validate_runtime_files", lambda *args: [])
    monkeypatch.setattr(cli, "create_app", lambda *args: object())

    class Server:
        """模拟 Uvicorn 就绪标志，不占用真实端口或执行后台调度。"""

        def __init__(self, settings):
            self.started = False
            self.should_exit = False
            assert settings.host == "0.0.0.0" and settings.port == 9092

        async def serve(self):
            """给报告协程运行机会，先核对未就绪时没有地址输出。"""

            await asyncio.sleep(0.06)
            assert "本机访问" not in capsys.readouterr().out
            self.started = ready
            await asyncio.sleep(0.12)
            self.should_exit = True

    monkeypatch.setattr(cli.uvicorn, "Server", Server)
    assert await cli._serve(tmp_path / "config.yaml", None, 9092) == 0
    output = capsys.readouterr().out
    if ready:
        assert "本机访问：http://127.0.0.1:9092" in output
        assert "http://192.168.1.20:9092" in output
        assert "需要管理员 Token" in output
    else:
        assert "本机访问" not in output
    assert "never-print-fixture-secret" not in output
