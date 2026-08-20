"""模型 Provider API Key 的本地私有凭据存储。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


class ModelProviderCredentialStore:
    """把模型 API Key 与普通 YAML 配置和配置历史分离。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            # Windows 不保证支持 POSIX 权限位，仍由数据目录和管理 API 保护。
            pass

    def configured(self, provider_id: str) -> bool:
        """返回指定 Provider 是否已经保存 API Key。"""

        return self._path(provider_id).is_file()

    def masked(self, provider_id: str) -> str | None:
        """返回不会泄露完整凭据的短掩码。"""

        if not self.configured(provider_id):
            return None
        return mask_api_key(self.reveal(provider_id))

    def reveal(self, provider_id: str) -> str:
        """按管理员明确请求读取单条 API Key。"""

        path = self._path(provider_id)
        if not path.is_file():
            raise KeyError(provider_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("模型 Provider 凭据文件损坏") from exc
        if not isinstance(value, dict) or value.get("provider_id") != provider_id:
            raise ValueError("模型 Provider 凭据身份不匹配")
        api_key = value.get("api_key")
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("模型 Provider 凭据为空")
        return api_key

    def replace(self, provider_id: str, api_key: str) -> None:
        """原子创建或替换指定 Provider 的 API Key。"""

        normalized_id = provider_id.strip()
        normalized_key = api_key.strip()
        if not normalized_id:
            raise ValueError("模型 Provider ID 不能为空")
        if not normalized_key:
            raise ValueError("API Key 不能为空")
        path = self._path(normalized_id)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.root,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    {"provider_id": normalized_id, "api_key": normalized_key},
                    handle,
                    ensure_ascii=False,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary_path, 0o600)
            except OSError:
                pass
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def delete(self, provider_id: str) -> None:
        """删除指定 Provider 的凭据；不存在时保持幂等。"""

        self._path(provider_id).unlink(missing_ok=True)

    def _path(self, provider_id: str) -> Path:
        """使用身份摘要生成固定且不可目录穿越的文件名。"""

        normalized = provider_id.strip()
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"


def mask_api_key(api_key: str) -> str:
    """显示短前缀与末尾四位，避免普通列表泄露完整 Key。"""

    if len(api_key) <= 4:
        return "****"
    return f"{api_key[:3]}****{api_key[-4:]}"
