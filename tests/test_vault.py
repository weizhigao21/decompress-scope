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
    cands = v.candidates_for("a.com")
    assert cands.index("high") < cands.index("low")
    assert "global" in cands
    # 设计决策: 不同来源的密码互不污染, 仅同来源 + 全局(source='')参与候选
    assert "high" not in v.candidates_for("other.com")
    v.close()


def test_add_manual_ignore_dup(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    v.add_manual("m1", "s.com")
    v.add_manual("m1", "s.com")
    assert len(v.list_all()) == 1
    v.close()
