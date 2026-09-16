"""只用于启动提示的访问地址发现，不改变监听、路由或认证。"""

from __future__ import annotations

import ipaddress
import socket

import psutil


def http_url(host: str, port: int) -> str:
    """格式化 IPv4、主机名和 IPv6 URL，兼容带作用域的显式 IPv6 地址。"""

    if ":" in host:
        host = f"[{host.strip('[]').replace('%', '%25')}]"
    return f"http://{host}:{port}"


def local_interface_addresses(family: int) -> list[tuple[str, str]]:
    """读取启用网卡的非回环候选；失败只影响提示，不影响服务启动。"""

    try:
        addresses = psutil.net_if_addrs()
        states = psutil.net_if_stats()
    except (OSError, psutil.Error, RuntimeError):
        return []
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in sorted(addresses, key=str.casefold):
        state = states.get(name)
        if state is None or not state.isup:
            continue
        for item in addresses[name]:
            if item.family != family:
                continue
            try:
                address = ipaddress.ip_address(item.address.split("%", 1)[0])
            except ValueError:
                continue
            if address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local:
                continue
            if address.version != (4 if family == socket.AF_INET else 6):
                continue
            # 全网广播不是可访问端点；IPv6 映射地址不用于猜测双栈监听能力。
            if str(address) == "255.255.255.255" or getattr(address, "ipv4_mapped", None) is not None:
                continue
            normalized = str(address)
            if normalized not in seen:
                seen.add(normalized)
                candidates.append(("".join(char for char in name if char.isprintable()), normalized))
    return sorted(candidates, key=lambda item: (item[0].casefold(), int(ipaddress.ip_address(item[1]))))


def service_access_message(host: str, port: int, *, admin_token_required: bool | None = None) -> str:
    """按实际绑定范围展示候选地址，认证标记不包含凭据内容。"""

    endpoint = http_url(host, port).removeprefix("http://")
    lines = [f"监听地址：{endpoint}"]
    if host in {"0.0.0.0", "::"}:
        loopback = "127.0.0.1" if host == "0.0.0.0" else "::1"
        lines.append(f"本机访问：{http_url(loopback, port)}")
        family = socket.AF_INET if host == "0.0.0.0" else socket.AF_INET6
        candidates = local_interface_addresses(family)
        for name, address in candidates:
            lines.append(f"局域网访问候选（{name}）：{http_url(address, port)}")
        if not candidates:
            lines.append("局域网访问：未检测到可用候选地址，请检查本机网卡配置")
        lines.append("提示：候选来自本机网卡，可能包含 VPN/虚拟网卡；实际访问受防火墙、路由和 WSL 网络配置影响。")
    else:
        try:
            loopback_only = ipaddress.ip_address(host.strip("[]")).is_loopback
        except ValueError:
            loopback_only = host.casefold() == "localhost"
        label = "本机访问" if loopback_only else "访问地址"
        lines.append(f"{label}：{http_url(host, port)}")
        if loopback_only:
            lines.append("访问范围：仅本机，不开放局域网访问")
    if admin_token_required is True:
        lines.append("认证方式：需要管理员 Token（本机与局域网相同）")
    elif admin_token_required is False:
        lines.append("认证方式：未配置管理员 Token")
    else:
        lines.append("认证方式：以服务的管理员 Token 配置为准")
    return "\n".join(lines)
