from core.password_finder import (
    build_candidates,
    derived_from_source,
    extract_passwords_from_name,
    extract_source,
)


def test_bracket_halfwidth():
    assert extract_passwords_from_name("[group]合集[密码:wnacg.com].zip") == ["wnacg.com"]


def test_bracket_fullwidth():
    assert extract_passwords_from_name("合集（密码：88888888）.rar") == ["88888888"]


def test_plain_marker():
    assert extract_passwords_from_name("somewhere 密码:abc123 inside.7z") == ["abc123"]


def test_no_password():
    assert extract_passwords_from_name("clean_archive.zip") == []


def test_source_in_brackets():
    assert extract_source("[www.example.com]pack.zip") == "www.example.com"


def test_source_loose():
    assert extract_source("comic_pack_from_moe123.net.zip") == "moe123.net"


def test_derived_from_source():
    assert derived_from_source("abc.com") == ["abc.com", "www.abc.com"]
    assert derived_from_source("") == []


def test_candidates_order_and_dedupe():
    cands = build_candidates("a[密码:zz].zip", "abc.com", ["abc.com", "zz", "xx"])
    assert cands[0] == "zz"
    assert cands.index("abc.com") < cands.index("xx")
    lowers = [c.lower() for c in cands]
    assert len(lowers) == len(set(lowers))


def test_source_derived_survives_truncation():
    """P0-2 回归：库候选占满配额时，来源派生密码仍必须进入候选。

    旧实现把 derived_from_source 排在 vault 之后，库满 20 条时
    来源派生（命中率最高）被 [:max_password_attempts] 整体切掉。
    断言用的是"截断后"的列表——这才是 pipeline 真正消费的东西。
    """
    vault_full = [f"stale{i:02d}" for i in range(20)]
    cands = build_candidates("pack.zip", "wnacg.com", vault_full)[:20]
    assert "wnacg.com" in cands, "来源域名被库候选挤出了候选列表"
    assert "www.wnacg.com" in cands, "来源域名变体被库候选挤出了候选列表"
