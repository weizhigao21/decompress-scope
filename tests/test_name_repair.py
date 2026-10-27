"""乱码文件名修复的守卫。

要守住的不是"能修好"，而是这几条边界：

- **正常名字一个都不许动**：中文包（GBK 字节、不带 UTF-8 标志）在中文 Windows
  上 7z 本来就解对了，任何改动都是破坏；正常日文名、ASCII 名同理。
- **修就必须修对**：Shift-JIS 字节被按 GBK 解出来的名字要还原成原文，
  目录名（含多级）也要一起还原。
- **改名绝不覆盖已有产物**：目标名被占用时挪后缀。
- **修完再修不出变化**（幂等）：跑重试、跑第二次不能把名字越改越乱。
- **拿不到确定性字节时不猜**：7z/rar、损坏包、分卷首卷都退回启发式，
  修不动就原样留着，绝不乱改。
"""
from __future__ import annotations

import io
import subprocess
import zipfile
from pathlib import Path

import pytest

from core.config import Config, detect_sevenzip
from core.name_repair import (
    _plausible,
    looks_like_mojibake,
    repair_name,
    repair_tree,
)
from core.pipeline import Pipeline
from core.store import TaskStore
from core.vault import PasswordVault


def _sevenzip_or_skip() -> Path:
    try:
        return detect_sevenzip()
    except FileNotFoundError:
        pytest.skip("未找到 7z.exe，跳过集成测试")


def _make_sjis_zip(folder: Path, name: str, entries: list[tuple[str, bytes]]) -> Path:
    """造一个「名字是 cp932 字节、又没置 UTF-8 标志」的包 —— 真实日文资源的样子。

    做法：先用等长 ASCII 占位名写进 zip，再把字节替换成 cp932 的。这样 general
    purpose bit 11 是 0（zipfile 只在名字含非 ASCII 时才置位），7z 就会按系统
    代码页去解，从而复现乱码。

    只保留「字节恰好也是合法 GBK」的条目：真出现非法字节对时 7z 会把整对替换成
    `_`，磁盘上的名字就不可逆了（那是另一个已知边界，见 name_repair 模块头）。
    """
    buf = io.BytesIO()
    planned: list[tuple[bytes, bytes]] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as archive:
        for index, (shown, data) in enumerate(entries):
            segments = [seg.encode("cp932") for seg in shown.split("/")]
            for seg in segments:
                seg.decode("cp936")          # 非合法 GBK 就抛，让测试失败得明确
            raw = b"/".join(segments)
            placeholder = f"Q{index}".encode() + b"Z" * (len(raw) - 2)
            planned.append((placeholder, raw))
            archive.writestr(placeholder.decode("ascii"), data)
    blob = buf.getvalue()
    for placeholder, raw in planned:
        assert len(placeholder) == len(raw)
        blob = blob.replace(placeholder, raw)
    target = folder / name
    target.write_bytes(blob)
    return target


def _make_gbk_zip(folder: Path, name: str, entries: list[tuple[str, bytes]]) -> Path:
    """造一个「中文名按 GBK 写、不带 UTF-8 标志」的包 —— 中文资源最常见的形态。"""
    buf = io.BytesIO()
    planned: list[tuple[bytes, bytes]] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as archive:
        for index, (shown, data) in enumerate(entries):
            raw = b"/".join(seg.encode("gbk") for seg in shown.split("/"))
            placeholder = f"R{index}".encode() + b"Y" * (len(raw) - 2)
            planned.append((placeholder, raw))
            archive.writestr(placeholder.decode("ascii"), data)
    blob = buf.getvalue()
    for placeholder, raw in planned:
        blob = blob.replace(placeholder, raw)
    target = folder / name
    target.write_bytes(blob)
    return target


def _extract(exe: Path, archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [str(exe), "x", f"-o{dest}", str(archive), "-y", "-sccUTF-8", "-spd"],
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------- 判定：什么时候才允许动手 ----------

@pytest.mark.parametrize("name", ["擔杮岅偺僼傽僀儖柤.mp3", "僒儞僾儖.txt", "傾僀僪儖"])
def test_sjis_garbled_names_are_suspicious(name):
    assert looks_like_mojibake(name) is True


@pytest.mark.parametrize("name", [
    "测试文件.mp3",              # 正常中文
    "恋爱活动合集",
    "新建文件夹",
    "【汉化】作品名",
    "日本語のファイル名.mp3",     # 正常日文（有假名兜着）
    "あいうえお.txt",
    "ダウンロード",
    "柿昴.mp3",                  # 纯汉字日文名
    "ReadMe.txt",               # ASCII
])
def test_normal_names_are_not_suspicious(name):
    assert looks_like_mojibake(name) is False


def test_names_with_lost_bytes_are_left_alone():
    """含替换字符/半角问号 = 字节已经丢了，怎么解都是猜，一律不动。"""
    assert looks_like_mojibake("擔�巵.mp3") is False
    assert looks_like_mojibake("擔?巵.mp3") is False


# ---------- 单名修复 ----------

@pytest.mark.parametrize("garbled,expected", [
    ("擔杮岅偺僼傽僀儖柤.mp3", "日本語のファイル名.mp3"),
    ("傾僀僪儖", "アイドル"),
    ("僒儞僾儖.txt", "サンプル.txt"),
])
def test_sjis_name_is_restored(garbled, expected):
    assert repair_name(garbled) == expected


@pytest.mark.parametrize("name", ["测试文件.mp3", "恋爱活动合集", "日本語のファイル名.mp3",
                                  "ReadMe.txt", "柿昴.mp3"])
def test_repair_keeps_normal_names(name):
    assert repair_name(name) == name


def test_repair_rejects_candidates_with_path_separators():
    """cp932 的 0x5C 解出来是反斜杠 —— 拿去 rename 会被当成路径分隔符。"""
    assert _plausible("a/b", "擔杮") is False
    assert _plausible("a\\b", "擔杮") is False
    assert _plausible("a:b", "擔杮") is False


def test_repair_rejects_halfwidth_katakana_result():
    """中文名被误当日文解码时产物是半角片假名混排，这条挡住它。"""
    assert _plausible("ｲ簗ﾔﾎﾄｼ.mp3", "鏃ユ湰瑕炪") is False


def test_rare_chinese_name_passes_gate_one_but_is_never_changed():
    """两道防线是串联的：第一道会把生僻字中文名判成「可疑」，第二道必须拦下来。

    这条专门守住「第一道误判时第二道兜得住」。样本是挑过的 —— 它必须真的走到
    第二道：GBK 编码得通、按 cp932 也解得出东西（`ﾁ按GﾂQ` 这种半角假名混排），
    否则测到的只是「编码层面走不通」这个偶然结果，护不住真正的误判路径。
    真实世界里生僻字人名/地名并不罕见，只靠 GB2312 覆盖率一刀切会把它们改坏。
    """
    name = "翀翯翾.mp3"
    assert looks_like_mojibake(name) is True, "前提：第一道确实会怀疑它"
    assert repair_name(name) == name, "不能把生僻字中文名改成日文乱码"


# ---------- 树修复 ----------

def test_tree_restores_files_and_directories(tmp_path):
    exe = _sevenzip_or_skip()
    archive = _make_sjis_zip(tmp_path, "jp.zip", [
        ("日本語ファイル.txt", b"1"),
        ("同人誌/サンプル.mp3", b"2"),
        ("同人誌/あいうえお.txt", b"3"),
    ])
    out = tmp_path / "out"
    _extract(exe, archive, out)
    # 前提：7z 确实按 GBK 解成乱码了（否则这条测试测的是空气）
    assert (out / "擔杮岅僼傽僀儖.txt").exists()

    result = repair_tree(out, archive=archive)

    assert result.renamed == 4, result.describe()
    assert result.encoding == "Shift-JIS"
    assert (out / "日本語ファイル.txt").read_bytes() == b"1"
    assert (out / "同人誌" / "サンプル.mp3").read_bytes() == b"2"
    assert (out / "同人誌" / "あいうえお.txt").read_bytes() == b"3"
    assert not (out / "摨恖帍").exists(), "目录名没跟着改"


def test_chinese_zip_is_untouched(tmp_path):
    """最关键的一条：中文包本来就解对了，一个字节都不许动。"""
    exe = _sevenzip_or_skip()
    archive = _make_gbk_zip(tmp_path, "cn.zip", [
        ("测试文件.txt", b"a"),
        ("恋爱活动合集/说明文档.mp3", b"b"),
        ("新建文件夹/图片.jpg", b"c"),
    ])
    out = tmp_path / "out"
    _extract(exe, archive, out)
    before = sorted(str(p.relative_to(out)) for p in out.rglob("*"))

    result = repair_tree(out, archive=archive)

    assert result.renamed == 0, result.describe()
    assert sorted(str(p.relative_to(out)) for p in out.rglob("*")) == before


def test_utf8_flagged_zip_is_untouched(tmp_path):
    """带 UTF-8 标志的包 7z 本来就解对了（zipfile 写非 ASCII 名会自动置位）。"""
    exe = _sevenzip_or_skip()
    archive = tmp_path / "utf8.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("日本語のファイル名.mp3", b"x")
        zf.writestr("中文目录/测试.txt", b"y")
    out = tmp_path / "out"
    _extract(exe, archive, out)
    before = sorted(str(p.relative_to(out)) for p in out.rglob("*"))

    result = repair_tree(out, archive=archive)

    assert result.renamed == 0
    assert sorted(str(p.relative_to(out)) for p in out.rglob("*")) == before


def test_repair_is_idempotent(tmp_path):
    exe = _sevenzip_or_skip()
    archive = _make_sjis_zip(tmp_path, "jp.zip", [("サンプル.mp3", b"x")])
    out = tmp_path / "out"
    _extract(exe, archive, out)

    assert repair_tree(out, archive=archive).renamed == 1
    assert repair_tree(out, archive=archive).renamed == 0


def test_rename_never_overwrites_existing_file(tmp_path):
    """目标名已被占用：挪后缀，绝不覆盖（覆盖就是产物丢失）。"""
    root = tmp_path / "out"
    root.mkdir()
    (root / "擔杮岅偺僼傽僀儖.txt").write_text("garbled", encoding="utf-8")
    (root / "日本語のファイル.txt").write_text("已存在", encoding="utf-8")

    result = repair_tree(root)

    assert result.renamed == 1
    assert (root / "日本語のファイル.txt").read_text(encoding="utf-8") == "已存在"
    assert (root / "日本語のファイル (2).txt").read_text(encoding="utf-8") == "garbled"


def test_zip_path_repairs_files_without_the_heuristic(tmp_path, monkeypatch):
    """中央目录这条路必须自己站得住 —— 把启发式关掉，**文件**名仍然能修回来。

    不加这条的话，`test_tree_restores_files_and_directories` 在 ZIP 路径整段
    失效时依然会绿（启发式能兜住同样的结果），那条路就等于没被测过。

    注意分工：`zipfile` 写文件时不会额外生成目录条目，所以「同人誌」这个名字
    压根不在中央目录里，只能靠启发式从磁盘上的乱码名反推。两条路是互补的。
    """
    import core.name_repair as nr

    monkeypatch.setattr(nr, "_from_heuristic", lambda *a, **kw: None)
    exe = _sevenzip_or_skip()
    archive = _make_sjis_zip(tmp_path, "jp.zip", [
        ("同人誌/サンプル.mp3", b"2"),
        ("あいうえお.txt", b"3"),
    ])
    out = tmp_path / "out"
    _extract(exe, archive, out)

    result = repair_tree(out, archive=archive)

    assert result.renamed == 2, result.describe()
    # 目录名仍是乱码（中央目录里没有它，启发式又被关掉了）—— 正好说明这一条
    # 测的确实只是 ZIP 那条路，没有别的路径在偷偷兜底
    assert (out / "摨恖帍" / "サンプル.mp3").read_bytes() == b"2"
    assert (out / "あいうえお.txt").read_bytes() == b"3"


def test_missing_root_is_a_noop(tmp_path):
    assert repair_tree(tmp_path / "nope").renamed == 0


def test_corrupt_zip_falls_back_without_raising(tmp_path):
    """伪装成 .zip 的垃圾文件：读不了中央目录，绝不能把整轮运行带崩。"""
    archive = tmp_path / "broken.zip"
    archive.write_bytes(b"this is not a zip")
    root = tmp_path / "out"
    root.mkdir()
    (root / "擔杮岅偺僼傽僀儖柤.txt").write_text("x", encoding="utf-8")

    result = repair_tree(root, archive=archive)

    assert result.renamed == 1, "中央目录读不到时应退回启发式"


def test_non_zip_archive_uses_heuristic(tmp_path):
    """7z 包没有「cp437 名字」这回事：走启发式，名字本来就对时零改动。"""
    exe = _sevenzip_or_skip()
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "サンプル.txt").write_text("x", encoding="utf-8")
    archive = folder / "pack.7z"
    proc = subprocess.run([str(exe), "a", str(archive), "サンプル.txt"],
                          cwd=str(folder), capture_output=True)
    assert proc.returncode == 0
    (folder / "サンプル.txt").unlink()
    out = tmp_path / "out"
    _extract(exe, archive, out)

    assert repair_tree(out, archive=archive).renamed == 0


# ---------- 接进流水线 ----------

def test_pipeline_delivers_repaired_names(tmp_path):
    """端到端：乱码包进来，产物落到交付目录时名字已经是日文，并且 report 有说明。"""
    exe = _sevenzip_or_skip()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    # 顶层故意放两项：只有一个顶层目录时 _sweep_empty_dirs 会把它上提一层，
    # 那样目录名本身就被抹掉了，测不到"目录名也要修"。
    _make_sjis_zip(inputs, "pack.zip", [
        ("同人誌/サンプル.mp3", b"payload"),
        ("日本語ファイル.txt", b"note"),
    ])
    db = tmp_path / "app.db"
    cfg = Config.create(sevenzip=str(exe), workdir=tmp_path / "wd")
    vault, store = PasswordVault(db), TaskStore(db)
    try:
        pipe = Pipeline(cfg, vault, store)
        events: list[dict] = []
        report = pipe.run([inputs], on_event=events.append)

        assert report.done == 1, report.warnings
        assert report.output_dirs, "没有交付目录"
        delivered = Path(report.output_dirs[0])
        assert (delivered / "同人誌" / "サンプル.mp3").read_bytes() == b"payload"
        assert any("已在" in n or "修正" in n for n in report.notes), report.notes
        notices = [e for e in events if e["kind"] == "notice"]
        assert notices and "修正" in notices[0]["message"]
    finally:
        vault.close()
        store.close()


# ---------- 汇报措辞：界面列只放得下一句话 ----------

def test_summary_leaves_encoding_and_example_to_describe():
    """界面列只拿"修了几个"；编码与例子必须留给 describe()。

    实测：默认 1000px 窗口下信息列只有约 190px，带「（Shift-JIS）」那句要
    203px，界面上会被 elide 成省略号——连"修了几个"都看不全（用户反馈的
    就是这个）。所以 summary() 不许再带编码。
    """
    from core.name_repair import NameRepair

    r = NameRepair(renamed=12, encoding="Shift-JIS",
                   samples=[("ｲ簗ﾔﾎﾄｼ.txt", "日本語.txt")])

    assert r.summary() == "已修正 12 个乱码文件名"
    assert "Shift-JIS" not in r.summary(), "编码应只出现在 describe() 里"

    detail = r.describe()
    assert detail.startswith(r.summary()), "tooltip 要去掉首句就等于藏信息"
    assert "Shift-JIS" in detail
    assert "日本語.txt" in detail
    assert "例如" in detail


def test_empty_repair_says_nothing():
    """没改名就不许说话：否则每个非日语包都会挂一条"已修正 0 个"。"""
    from core.name_repair import NameRepair

    r = NameRepair()
    assert r.summary() == ""
    assert r.describe() == ""
