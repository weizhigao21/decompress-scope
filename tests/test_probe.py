"""`7z l -slt` 解析层的守卫。

这里喂**离线样本**、不跑 7z：解析是纯函数，把它的行为钉在样本上才有意义。
"7z 真的会这么报"这件事由 test_pipeline_integration 的真 7z 用例守着。

样本里的分卷段是真实抓取的（7-Zip 26.01，`l -slt p.zip.001`），
不是手编的——手编的样本很容易编出比现实更规整的输入。
"""
from pathlib import Path

from core.models import ArchiveInfo
from core.probe import _with_format, parse_slt

# 分卷首卷的真实输出。关键在**两行 Type**：
#   上面那行 Split 是外层容器，下面那行 zip 才是里面真正的格式。
SPLIT_SLT = """\
7-Zip 26.01 (x64) : Copyright (c) 1999-2026 Igor Pavlov : 2026-04-27

Scanning the drive for archives:
1 file, 32768 bytes (32 KiB)

Listing archive: p.zip.001

--
Path = p.zip.001
Type = Split
Physical Size = 32768
Volumes = 7
Total Physical Size = 208645
----
Path = p.zip
Size = 208645
--
Path = p.zip
Type = zip
Physical Size = 208645

----------
Path = big.bin
Folder = -
Size = 3145728
Packed Size = 208497
Encrypted = -
"""


def test_split_volume_reports_inner_format(tmp_path):
    """分卷包报的是**里面**的格式，不是 "Split" 这个容器名。

    7z 对分卷首卷会报两行 Type：外层 Split 容器在前，里面真实的 zip 在后。
    只取第一个匹配的话，「类型」列就显示成 "Split"——那是在说"这是分卷"，
    而用户问的是"这是什么包"。分卷与否已经由大小列的"（N 个分卷）"表达了，
    类型列再说一遍等于没说。
    """
    info = parse_slt(SPLIT_SLT, tmp_path / "p.zip.001")

    assert info.format_name == "zip", \
        f"类型列会显示 {info.format_name!r}，应为里层的 zip"
    assert info.format_name != "Split"


def test_split_volume_still_counts_whole_group(tmp_path):
    """顺带确认改格式没有碰坏体积：分卷的条目统计照常按整组走。

    （体积本身另有专门用例，这里只要求它别被这次改动带塌。）
    """
    info = parse_slt(SPLIT_SLT, tmp_path / "p.zip.001")

    assert info.file_count == 1
    assert info.total_uncompressed == 3145728
    assert info.flag_encrypted is False


def test_plain_archive_format_unchanged(tmp_path):
    """普通包只有一行 Type，取值不受影响。"""
    text = ("Listing archive: a.zip\n\n--\nPath = a.zip\nType = zip\n\n"
            "----------\nPath = f.txt\nFolder = -\nSize = 10\n")

    assert parse_slt(text, tmp_path / "a.zip").format_name == "zip"


def test_type_line_absent_stays_empty(tmp_path):
    """头部加密的包连 Type 都没有（空密码列不出目录）：留空给 magic 兜底，不编。

    这里的"空"是**有意**的——`_with_format` 靠它判断该不该读文件头。
    若哪天改成"没有就填 unknown"，兜底就永远不触发了。
    """
    text = "Listing archive: enc.7z\n\n--\nPath = enc.7z\nPhysical Size = 99\n"

    assert parse_slt(text, tmp_path / "enc.7z").format_name == ""


def test_container_name_alone_is_preserved(tmp_path):
    """7z 只报了容器名时原样带回，不在这里抹成空。

    抹空会让兜底逻辑以为"7z 什么都没说"；保留 `Split` 至少说明
    「这是个分卷容器」，兜底又认不出文件头时，显示它比显示 "—" 有信息量。
    """
    text = "Path = a.001\nType = Split\n"

    assert parse_slt(text, tmp_path / "a.001").format_name == "Split"


def test_magic_fallback_names_split_first_volume(tmp_path):
    """兜底仍在：只认得容器名时，读首卷文件头能认出底层格式。

    分卷首卷的文件头就是内层压缩包的头部（实测 PK\\x03\\x04），
    所以 7z 只报外层、或压根没报 Type 时，magic 这条路还接得住。
    """
    first = tmp_path / "p.zip.001"
    first.write_bytes(b"PK\x03\x04" + b"\0" * 200)

    info = _with_format(ArchiveInfo(path=first, format_name="Split"), first)

    assert info.format_name == "zip", "首卷是 PK 头，应认出 zip"


def test_magic_fallback_keeps_container_name_when_hopeless(tmp_path):
    """兜底也认不出来时保留容器名，不抹成空。"""
    first = tmp_path / "p.zip.001"
    first.write_bytes(b"\x00" * 200)   # 不是任何已知压缩包

    info = _with_format(ArchiveInfo(path=first, format_name="Split"), first)

    assert info.format_name == "Split"


def test_magic_fallback_does_not_touch_known_format(tmp_path):
    """7z 已经给出真实格式时，兜底不许插嘴。

    这条防的是"顺手覆盖"：把判断写成 `format_name = sniffed or format_name`，
    那些 magic 认不出的格式（wim 变体、7z 新变种…）就会被抹成空。
    """
    p = tmp_path / "a.zip"
    p.write_bytes(b"\x00" * 200)   # 文件头认不出是啥

    info = _with_format(ArchiveInfo(path=p, format_name="zip"), p)

    assert info.format_name == "zip"


def test_format_pick_ignores_case_of_container_name(tmp_path):
    """容器名大小写不敏感地跳过——7z 换个大写写法也不该退化成显示 "SPLIT"。"""
    text = "Type = SPLIT\n\n--------\nPath = f\nSize = 1\nType = zip\n"

    assert parse_slt(text, tmp_path / "a.zip.001").format_name == "zip"
