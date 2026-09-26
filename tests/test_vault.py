from core.vault import PasswordVault


def test_record_success_upsert(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    v.record_success("pwA", "a.com")
    v.record_success("pwA", "a.com")
    v.record_success("pwB", "a.com")
    v.record_success("pwC", "")
    by_key = {(p, s): h for p, s, h, _ in v.list_all()}
    assert by_key[("pwA", "a.com")] == 2
    assert by_key[("pwC", "")] == 1
    v.close()


def test_candidates_ordering(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    v.record_success("low", "a.com")
    v.record_success("high", "a.com")
    v.record_success("high", "a.com")
    v.record_success("global", "")
    v.record_success("foreign", "z.com")
    cands = v.candidates_for("a.com")
    assert cands.index("high") < cands.index("low")
    assert "global" in cands
    # 设计决策（2026-09-26 修订）: 来源只用来**排序**，不再用来**隔离**。
    # 旧实现只返回 source=? 与 source='' 两组，跨来源条目永远轮不到被试；
    # 而 record_success 是按 extract_source(文件名) 写入的，于是来源一分组
    # 就互不通气，文件名带域名的包越多盲区越大。
    other = v.candidates_for("other.com")
    assert "high" in other, "跨来源的密码被隔离了，永远不会被试"
    # 但档位必须分明：同来源(0) → 无来源(1) → 其他来源(2)
    assert other.index("global") < other.index("high"), \
        "无来源的通用密码应排在其他站点的密码之前"
    assert other.index("high") < other.index("foreign"), "档内仍应按命中次数排序"
    v.close()


def test_candidates_limit_truncates_after_priority(tmp_path):
    """limit 必须作用在「排序 + 去重之后」：低优先级段不能把高优先级段挤掉。

    旧实现的 limit 是**每段各取 limit 条**，再交给调用方 `[:cap]` 二次截断——
    于是「库里到底能进来几条」跟设置项对不上。这里钉住的是截断点的位置。
    """
    v = PasswordVault(tmp_path / "v.db")
    for i in range(30):
        v.record_success(f"stale{i:02d}", "b.com")  # 其他来源，量大
    v.record_success("mine", "a.com")
    v.record_success("common", "")

    cands = v.candidates_for("a.com", limit=3)
    assert len(cands) == 3, "limit 没生效"
    assert cands[0] == "mine", "同来源必须排第一位"
    assert cands[1] == "common", "无来源（通用）应排在跨来源之前"
    # limit=0 沿用 query() 的约定：不限
    assert len(v.candidates_for("a.com")) == 32
    v.close()


def test_add_manual_ignore_dup(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    v.add_manual("m1", "s.com")
    v.add_manual("m1", "s.com")
    assert len(v.list_all()) == 1
    v.close()
