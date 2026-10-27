"""云端密码库（云解密）客户端：按文件 MD5 向「解压缩全能王」的接口要密码。

协议来源：`E:/code/逆向/app/rev/uncompress/API_README.md`（自有设备实测）。
本模块只负责「算校验值 + 发一个 POST + 解析响应」，不做任何策略判断：

- 什么时候查、查几次、查不到怎么办，全部由 `core.pipeline` 决定；
- 依赖为零（urllib + hashlib + gzip），core 层不许引入第三方库或 Qt；
- 网络传输经由可替换的 `transport` 注入，测试永不真的发包。

接口形状（两端点 query/body 完全相同，只换路径）::

    POST https://apps.cscjapp.cn/compressnew/yun.php?md5=<大写hex>&ycs=1&...
         Content-Type: application/x-www-form-urlencoded
         size=<字节数>&name=<文件名>

只打 `yun.php` 不打 `checkFile.php`：预检与取密码的参数、body、失败语义
（201 + 文案）完全一样，预检拿不到任何额外信息，只是把请求数翻倍。

几个已实测的坑，写在这里免得以后"顺手优化"改回去：

1. `md5` 必须**大写**。
2. `key` 只依赖时间戳、服务端**不校验**，随便给 32 位 hex 即可（见报告 §6）；
   这里用 `secrets.token_hex(16)`，不回传任何真实签名。
3. `his` 是 `<启动时间戳>.<当前时间戳>`，前半段随便给一个早于 `tm` 的值即可；
   这里取进程导入时刻，符合"App 启动多久了"的语义。
4. 响应可能是 gzip，`urllib` 不会自动解压 —— 必须自己看 `Content-Encoding`。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import platform
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

API_BASE = "https://apps.cscjapp.cn/compressnew"
YUN_PATH = "/yun.php"

# 客户端自报的版本/设备信息。服务端不校验这些值（报告 §6.2 的四组探测），
# 但保持与 App 一致能少踩"看起来不像正常客户端"的风控。
APP_VER = "4.9.4"
APP_VERCODE = "119"
APP_PACK = "com.cjtec.uncompress"
APP_CHANNEL = "baidu"
DEVICE_MODEL = "LGE-AN10"
DEVICE_BRAND = "HONOR"
DEVICE_OS = "32"
YCS = "1"

DEFAULT_TIMEOUT = 10.0
_MAX_BODY = 256 * 1024       # 响应只为取一个密码，超出这个量级一律视为脏数据
_HASH_CHUNK = 4 * 1024 * 1024
_BOOT_TS = int(time.time())  # 进程"启动时间"，用于 his 前半段

# 状态值：调用方按这三档分流，不要拿 message 做字符串判断（文案随时会改）
STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"

# 响应里可能装密码的键。data 既可能是字符串（同 App 其他接口的惯例，
# 见报告 §4.4），也可能是字典 —— 两种都往下挖一层。
_PASSWORD_KEYS: tuple[str, ...] = (
    "password", "pwd", "pass", "passwd", "mima", "密码", "解压密码",
    "unzip_password", "unzippassword", "data",
)


class CloudCancelled(Exception):
    """算校验值的过程中用户取消。"""


@dataclass(frozen=True)
class CloudResult:
    """一次云端查询的结论。password 只在 STATUS_OK 时非空。"""

    password: str | None
    message: str
    status: str = STATUS_ERROR
    code: str = ""

    @property
    def found(self) -> bool:
        return bool(self.password)


def default_device_id() -> str:
    """由本机固定信息派生的 32 位小写 hex 设备标识。

    报告 §5 明确：`newudid` 不必与 App 真机一致，**复用同一个值维持身份**即可；
    换掉它反而可能触发设备指纹风控。所以这里用机器名+用户名派生 —— 同一台机器
    每次运行都得到同一个值，不需要额外落盘，也不存在"配置文件丢了就变设备"。
    用户想换身份时在设置里显式填一个即可（`cloud_unlock_udid`）。
    """
    seed = "|".join((
        platform.node() or "",
        os.environ.get("USERNAME", "") or os.environ.get("USER", "") or "",
        os.environ.get("COMPUTERNAME", "") or "",
    ))
    return hashlib.md5(f"{seed}|jieya-kaijing-cloud".encode("utf-8")).hexdigest()


def file_md5(path: Path, *, on_progress=None, should_cancel=None) -> str:
    """流式计算大写 MD5 hex。取消时抛 `CloudCancelled`，读不了返回空串。

    大写是接口要求（报告 §4.3），小写会直接查不到 —— 这个细节放在这里，
    免得调用方各写一次 `digest.upper()`，漏一处就是"云端永远查不到"。
    """
    digest = hashlib.md5()
    try:
        total = Path(path).stat().st_size or 1
        done = 0
        with Path(path).open("rb") as stream:
            while True:
                if should_cancel is not None and should_cancel():
                    raise CloudCancelled
                block = stream.read(_HASH_CHUNK)
                if not block:
                    break
                digest.update(block)
                done += len(block)
                if on_progress is not None:
                    on_progress(min(99, done * 100 // total))
    except OSError:
        return ""
    if on_progress is not None:
        on_progress(100)
    return digest.hexdigest().upper()


def build_query(md5: str, *, udid: str, tm: int | None = None,
                boot_ts: int = _BOOT_TS) -> str:
    """拼 query 串（不含域名与路径）。参数顺序与实测请求保持一致。"""
    tm = int(tm if tm is not None else time.time())
    key = secrets.token_hex(16)          # 服务端不校验，见模块头第 2 条
    pairs = [
        ("md5", md5.upper()),
        ("ycs", YCS),
        ("isvip", "false"),
        ("openid", ""),
        ("unionid", ""),
        ("newudid", udid or default_device_id()),
        ("ver", APP_VER),
        ("his", f"{int(boot_ts)}.{tm}"),
        ("device_model", DEVICE_MODEL),
        ("os", DEVICE_OS),
        ("device_brand", DEVICE_BRAND),
        ("tm", str(tm)),
        ("vercode", APP_VERCODE),
        ("pack", APP_PACK),
        ("key", key),
        ("chanel", APP_CHANNEL),
    ]
    return urllib.parse.urlencode(pairs)


def build_body(size: int, name: str) -> bytes:
    """表单 body：size 与 name 在 body 里（报告 §4.3）。"""
    return urllib.parse.urlencode({"size": int(size), "name": name}).encode("utf-8")


def _dig(payload, depth: int = 0) -> str | None:
    """在响应里挖密码：字符串直接用，字典按已知键递归，列表取第一个命中的。"""
    if depth > 3:
        return None
    if isinstance(payload, str):
        text = payload.strip().strip('"').strip()
        return text or None
    if isinstance(payload, bool) or payload is None:
        return None
    if isinstance(payload, (int, float)):
        return str(payload)
    if isinstance(payload, dict):
        for key in _PASSWORD_KEYS:
            if key in payload:
                found = _dig(payload[key], depth + 1)
                if found:
                    return found
        return None
    if isinstance(payload, (list, tuple)):
        for item in payload:
            found = _dig(item, depth + 1)
            if found:
                return found
    return None


def parse_answer(raw: bytes) -> CloudResult:
    """把响应体解析成 CloudResult。绝不抛异常 —— 脏数据只降级为 error。"""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return CloudResult(None, "云端返回空响应", STATUS_ERROR)
    if len(text) > _MAX_BODY:
        text = text[:_MAX_BODY]
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return CloudResult(None, f"云端返回无法解析：{text[:120]}", STATUS_ERROR)
    if not isinstance(payload, dict):
        # 少数接口会直接返回裸字符串/数组，按"有没有密码"处理
        found = _dig(payload)
        return (CloudResult(found, "云端密码库命中", STATUS_OK) if found
                else CloudResult(None, "云端返回结构未知", STATUS_ERROR))

    code = str(payload.get("code", "")).strip()
    note = str(payload.get("message") or payload.get("msg") or "").strip()
    found = _dig({k: v for k, v in payload.items() if k not in ("code", "message", "msg")})
    if found:
        return CloudResult(found, f"云端密码库命中：{found}", STATUS_OK, code)
    if code in ("1", "200"):
        return CloudResult(None, f"云端响应里没有密码字段（code={code} {note}）", STATUS_ERROR, code)
    # 201 = 未收录 / 没有找到密码；其余业务码同样当成"这次没拿到密码"，
    # 不做二次请求（重试只会多烧一次配额）。
    detail = f"{note}" if note else "未收录"
    return CloudResult(None, f"云端密码库未收录该文件（{detail}）", STATUS_NOT_FOUND, code)


def _decode_body(raw: bytes, encoding: str) -> bytes:
    """按 Content-Encoding 解压。服务端即使没被要求也可能发 gzip。"""
    enc = (encoding or "").lower()
    if "gzip" in enc:
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    return raw


def _http_post(url: str, body: bytes, headers: dict, timeout: float) -> tuple[int, bytes, str]:
    """默认传输层。返回 (HTTP 状态码, 原始 body, Content-Encoding)。

    独立成函数是为了让测试用 `transport=` 整个替换掉 —— 单测绝不发真包。
    """
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        raw = response.read(_MAX_BODY + 1)
        return int(getattr(response, "status", 200)), raw, response.headers.get("Content-Encoding", "")


class CloudUnlocker:
    """云端密码库客户端。一个实例可反复查询（内部无状态）。"""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, udid: str = "",
                 transport=None, base: str = API_BASE):
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self.udid = (udid or "").strip() or default_device_id()
        self._transport = transport or _http_post
        self._base = base.rstrip("/")

    def query(self, archive: Path, *, name: str = "", size: int | None = None,
              on_progress=None, should_cancel=None) -> CloudResult:
        """按文件 MD5 要密码。任何异常都收敛为 CloudResult(status=error)。

        只有调用方（pipeline）知道"这个包是不是分卷、是不是内嵌恢复出来的"，
        所以体积/文件名的判断留在那边；这里只保证"给什么算什么"。
        """
        path = Path(archive)
        display_name = name or path.name
        if size is None:
            try:
                size = path.stat().st_size
            except OSError as exc:
                return CloudResult(None, f"读不到文件大小：{exc}", STATUS_ERROR)
        try:
            digest = file_md5(path, on_progress=on_progress, should_cancel=should_cancel)
        except CloudCancelled:
            return CloudResult(None, "已取消", STATUS_SKIPPED)
        if not digest:
            return CloudResult(None, "计算文件校验值失败（文件读不了）", STATUS_ERROR)
        if size <= 0:
            return CloudResult(None, "文件大小异常（0 字节）", STATUS_ERROR)

        url = f"{self._base}{YUN_PATH}?{build_query(digest, udid=self.udid)}"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "okhttp/3.12.0",
            "Accept-Encoding": "gzip",
        }
        try:
            status, raw, encoding = self._transport(
                url, build_body(size, display_name), headers, self.timeout)
        except urllib.error.HTTPError as exc:
            return CloudResult(None, f"云端返回 HTTP {exc.code}", STATUS_ERROR, str(exc.code))
        except Exception as exc:  # noqa: BLE001 网络层什么都可能抛
            return CloudResult(None, f"连不上云端密码库：{exc.__class__.__name__}: {exc}",
                               STATUS_ERROR)
        if status >= 400:
            return CloudResult(None, f"云端返回 HTTP {status}", STATUS_ERROR, str(status))
        result = parse_answer(_decode_body(raw, encoding))
        return result


def build_unlocker(cfg) -> CloudUnlocker | None:
    """按运行参数决定是否启用云解密。

    单一入口：UI 与 CLI 都不去 `if pref.cloud_unlock:` 里各写一遍构造逻辑，
    否则迟早出现"设置里关了、某个入口还在查"。
    """
    if not getattr(cfg, "cloud_unlock", False):
        return None
    return CloudUnlocker(
        timeout=float(getattr(cfg, "cloud_unlock_timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT),
        udid=str(getattr(cfg, "cloud_unlock_udid", "") or ""),
    )


class CloudQueryJob:
    """一次**后台**云端查询：算 MD5 + 发包在另一个线程里跑，调用方随时来取。

    为什么要有线程：这一步和本地密码尝试是**并行**的。云端排第一不是为了
    "先等它"，恰恰是为了"不必等" —— 它从第一秒就在跑，本地该试什么继续试，
    谁先出结果用谁。大包算 MD5 要几秒到几十秒（2.6 GB 实测约 5 s），放在
    主流程里就是把本地候选全堵在它后面。

    跨线程的纪律（很重要）：
    - 线程里只做哈希与网络，**只**往 `self._percent` / `self._result` 写值；
    - 绝不回调调用方的进度函数、更不发 Qt 信号 —— 从非 Qt 线程发信号会让
      PySide6 随机 `Fatal Python error: Aborted`；
    - `should_cancel` 必须是线程安全的（生产上传的是 `threading.Event.is_set`）。
    进度与结论一律由调用方在**自己的线程**里 `poll()` / `wait()` 取。
    """

    def __init__(self, unlocker: CloudUnlocker, archive: Path, *,
                 should_cancel=None):
        self._unlocker = unlocker
        self._archive = Path(archive)
        self._should_cancel = should_cancel
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._result: CloudResult | None = None
        self._percent = 0
        self._thread: threading.Thread | None = None

    def start(self) -> "CloudQueryJob":
        self._thread = threading.Thread(
            target=self._run, name="cloud-unlock", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            result = self._unlocker.query(
                self._archive,
                on_progress=self._note_progress,
                should_cancel=self._should_cancel,
            )
        except Exception as exc:  # noqa: BLE001 任何异常都收敛成结论，不往外抛
            result = CloudResult(
                None, f"云端查询异常：{exc.__class__.__name__}: {exc}", STATUS_ERROR)
        with self._lock:
            self._result = result
        self._done.set()

    def _note_progress(self, pct: int) -> None:
        """线程内调用：只写一个整数，别的什么都不做。"""
        with self._lock:
            self._percent = int(pct)

    @property
    def percent(self) -> int:
        """校验值的计算进度（0–100）。调用方在自己的线程里读。"""
        with self._lock:
            return self._percent

    @property
    def running(self) -> bool:
        return not self._done.is_set()

    def poll(self) -> CloudResult | None:
        """非阻塞取结论；还没好返回 None。"""
        with self._lock:
            return self._result

    def wait(self, timeout: float | None = None) -> CloudResult | None:
        """阻塞等结论（本地候选都试完之后才用）。超时返回 None。"""
        self._done.wait(timeout)
        return self.poll()
