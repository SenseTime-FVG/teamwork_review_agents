"""解析 Git 的安全进度，并跨平台持续读取子进程管道。"""

from __future__ import annotations

import re
import threading
import time
from typing import BinaryIO


_STAGES = {
    "Enumerating objects": "枚举对象",
    "Counting objects": "统计对象",
    "Compressing objects": "压缩对象",
    "Receiving objects": "接收对象",
    "Resolving deltas": "解析差异",
    "Updating files": "检出文件",
    "Checking out files": "检出文件",
    "Filtering content": "过滤文件内容",
    "Writing objects": "写入对象",
}
_LINE = re.compile(
    r"^(?P<remote>remote: )?(?P<stage>" + "|".join(_STAGES) + r"): +"
    r"(?:(?P<percent>\d{1,3})% \((?P<current>\d{1,18})/(?P<total>\d{1,18})\)"
    r"|(?P<count>\d{1,18}))"
    r"(?:, (?P<size>\d{1,18}(?:\.\d{1,3})?) (?P<unit>bytes|B|KiB|MiB|GiB)"
    r"(?: \| (?P<speed>\d{1,18}(?:\.\d{1,3})?) (?P<speed_unit>bytes|B|KiB|MiB|GiB)/s)?)?"
    r"(?P<done>, done\.)? *$"
)
_UNITS = {"bytes": 1, "B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}


class GitProgressTracker:
    """只由可量化进度推进刷新时钟，重复日志和心跳不续期。"""

    def __init__(self, started: float) -> None:
        self._lock = threading.Lock()
        self._last_monotonic = started
        self._last_at: float | None = None
        self._progress: dict[str, object] | None = None
        self._high_water: dict[str, tuple[int, int, bool]] = {}
        self._pending = bytearray()
        self._dropping = False

    def feed(self, data: bytes) -> None:
        """保留最多一行，完整分隔后才解析，避免拆包或长行造成伪进度。"""

        for piece in re.split(rb"([\r\n])", data):
            if piece in (b"\r", b"\n"):
                if not self._dropping and self._pending:
                    self._parse(bytes(self._pending))
                self._pending.clear()
                self._dropping = False
            elif not self._dropping:
                self._pending.extend(piece)
                if len(self._pending) > 4096:
                    self._pending.clear()
                    self._dropping = True

    def finish(self) -> None:
        """处理正常结束时没有换行的最后一条输出。"""

        if self._pending and not self._dropping:
            self._parse(bytes(self._pending))
        self._pending.clear()

    def _parse(self, line: bytes) -> None:
        """只接受固定阶段和数值，不透传远端文本、路径或凭据。"""

        match = _LINE.fullmatch(line.decode("utf-8", errors="replace"))
        if match is None:
            return
        values = match.groupdict()
        current = int(values["current"] or values["count"])
        total = int(values["total"]) if values["total"] else None
        percent = int(values["percent"]) if values["percent"] else None
        if percent is not None and (percent > 100 or total is None or current > total):
            return
        size = int(float(values["size"]) * _UNITS[values["unit"]]) if values["size"] else 0
        speed = float(values["speed"]) * _UNITS[values["speed_unit"]] if values["speed"] else None
        done = values["done"] is not None
        stage = values["stage"]
        key = ("remote:" if values["remote"] else "") + stage
        with self._lock:
            previous = self._high_water.get(key)
            advanced = previous is None or current > previous[0] or size > previous[1] or (done and not previous[2])
            if previous is not None:
                self._high_water[key] = (max(current, previous[0]), max(size, previous[1]), done or previous[2])
            else:
                self._high_water[key] = (current, size, done)
            if advanced:
                self._last_monotonic = time.monotonic()
                self._last_at = time.time()
            # 速度可以刷新显示，但不能仅凭速度变化判定命令有进展。
            if advanced or (self._progress is not None and self._progress["stage"] == key):
                self._progress = {
                    "stage": key,
                    "label": ("远端 · " if values["remote"] else "") + _STAGES[stage],
                    "percent": percent,
                    "current": current,
                    "total": total,
                    "received_bytes": size if values["size"] else None,
                    "bytes_per_second": speed,
                }

    def snapshot(self) -> tuple[float, float | None, dict[str, object] | None]:
        """返回计时及进度副本，避免主线程和管道读取线程竞争。"""

        with self._lock:
            return self._last_monotonic, self._last_at, dict(self._progress) if self._progress else None


class GitOutputReader(threading.Thread):
    """持续排空管道；stderr 按完整记录限长，避免截断密钥后才脱敏。"""

    def __init__(self, stream: BinaryIO, *, finished: threading.Event, tracker: GitProgressTracker | None = None) -> None:
        super().__init__(daemon=True, name="git-output-reader")
        self.stream = stream
        self.tracker = tracker
        self.finished = finished
        self.data = bytearray()
        self._pending = bytearray()
        self._dropping = False
        self.error = False

    def run(self) -> None:
        """二进制读取兼容 Windows，保留 Git 使用的回车分隔符。"""

        try:
            while chunk := self.stream.read1(8192):
                if self.tracker is None:
                    self.data.extend(chunk)
                    continue
                self.tracker.feed(chunk)
                # 仅保留完整且有界的错误记录，超长单行整行丢弃。
                for piece in re.split(rb"([\r\n])", chunk):
                    if piece in (b"\r", b"\n"):
                        if not self._dropping:
                            self._append_record(bytes(self._pending))
                        self._pending.clear()
                        self._dropping = False
                    elif not self._dropping:
                        self._pending.extend(piece)
                        if len(self._pending) > 16384:
                            self._pending.clear()
                            self._dropping = True
            if self.tracker is not None:
                self.tracker.finish()
                if not self._dropping and self._pending:
                    self._append_record(bytes(self._pending))
        except (OSError, ValueError):
            self.error = True
        finally:
            self.stream.close()
            self.finished.set()

    def _append_record(self, record: bytes) -> None:
        """只淘汰完整旧记录，输出缓存不随下载时长无限增长。"""

        self.data.extend(record + b"\n")
        if len(self.data) > 65536:
            boundary = self.data.find(b"\n", len(self.data) - 65536)
            del self.data[:boundary + 1]

    def text(self) -> str:
        """在读取结束后返回与既有 Git 查询接口兼容的文本。"""

        return self.data.decode("utf-8", errors="replace").replace("\r\n", "\n")


def with_git_progress(arguments: list[str]) -> list[str]:
    """只给支持进度选项的命令加参数，不改变路径或已有显式选项。"""

    result = list(arguments)
    index = 0
    while index < len(result):
        item = result[index]
        if item in {"-C", "-c", "--git-dir", "--work-tree"}:
            index += 2
            continue
        if item.startswith("-"):
            index += 1
            continue
        if item in {"clone", "fetch", "checkout"}:
            options = result[index + 1:]
            options = options[:options.index("--")] if "--" in options else options
            if "--progress" not in options and "--no-progress" not in options:
                result.insert(index + 1, "--progress")
        break
    return result
