"""7z.exe 子进程封装。

所有调用统一附加 -y（防交互）、-sccUTF-8（统一输出编码）、-spd（关闭通配符，
防止文件名里的 [密码:xxx] 方括号被误解析）。
extract 用 Popen 流式读取输出：-bsp1 输出进度百分比，on_progress 增量回调；
should_cancel() 返回 True 时杀掉 7z 进程（用于 UI 取消/退出）。
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

_PROGRESS_RE = re.compile(r"(\d{1,3})%")
_POLL_INTERVAL = 0.5


def _creation_flags() -> int:
    """GUI 启动 7z 时不弹出控制台；其他平台保持默认行为。"""
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class SevenZipError(RuntimeError):
    pass


class SevenZipCancelled(SevenZipError):
    """用户取消导致的解压中断。"""


class SevenZip:
    def __init__(self, exe: Path, list_timeout: float = 120.0, extract_timeout: float = 3600.0):
        self.exe = str(exe)
        self.list_timeout = list_timeout
        self.extract_timeout = extract_timeout

    @staticmethod
    def _decode(data: bytes) -> str:
        for enc in ("utf-8", "gbk"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    def _run(self, args: list[str], timeout: float) -> tuple[int, str, str]:
        cmd = [self.exe, *args, "-y", "-sccUTF-8", "-spd"]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=timeout,
                creationflags=_creation_flags(),
            )
        except subprocess.TimeoutExpired:
            raise SevenZipError(f"7z 执行超时({timeout:.0f}s): {args[0]} ...")
        except OSError as exc:
            raise SevenZipError(f"无法启动 7z: {exc}")
        return proc.returncode, self._decode(proc.stdout), self._decode(proc.stderr)

    def version(self) -> str:
        code, out, err = self._run([], 30.0)
        line = out.strip().splitlines()[0] if out.strip() else ""
        if code != 0 or not line:
            raise SevenZipError(f"7z 版本探测失败(exit={code}) {err[:200]}")
        return line

    def list_raw(self, archive: Path, password: str = "") -> tuple[int, str, str]:
        return self._run(["l", "-slt", f"-p{password}", str(archive)], self.list_timeout)

    def test(self, archive: Path, password: str = "") -> int:
        code, _, _ = self._run(["t", f"-p{password}", str(archive)], self.list_timeout)
        return code

    def extract(
        self,
        archive: Path,
        dest: Path,
        password: str = "",
        on_progress=None,
        should_cancel=None,
    ) -> tuple[int, str, str]:
        """流式解压。on_progress(percent) 增量回调；should_cancel() 为 True 时
        杀掉 7z 并抛 SevenZipCancelled。返回 (退出码, 合并输出, "")。"""
        dest.mkdir(parents=True, exist_ok=True)
        cmd = [self.exe, "x", f"-o{dest}", f"-p{password}", str(archive),
               "-y", "-sccUTF-8", "-spd", "-bsp1"]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=_creation_flags(),
            )
        except OSError as exc:
            raise SevenZipError(f"无法启动 7z: {exc}")

        chunks: list[bytes] = []
        last_percent = -1

        def reader() -> None:
            nonlocal last_percent
            assert proc.stdout is not None
            while True:
                try:
                    data = proc.stdout.read1(4096)
                except Exception:
                    break
                if not data:
                    break
                chunks.append(data)
                if on_progress is None:
                    continue
                for match in _PROGRESS_RE.finditer(data.decode("utf-8", errors="ignore")):
                    pct = int(match.group(1))
                    if 0 <= pct <= 100 and pct != last_percent:
                        last_percent = pct
                        try:
                            on_progress(pct)
                        except Exception:
                            pass

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()

        deadline = time.monotonic() + self.extract_timeout
        code: int | None = None
        while True:
            if should_cancel is not None and should_cancel():
                proc.kill()
                proc.wait(timeout=30)
                raise SevenZipCancelled("用户取消解压")
            try:
                code = proc.wait(timeout=_POLL_INTERVAL)
                break
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait(timeout=30)
                raise SevenZipError(
                    f"7z 执行超时({self.extract_timeout:.0f}s): extract {archive.name}"
                )
        reader_thread.join(timeout=10)
        return code, self._decode(b"".join(chunks)), ""
