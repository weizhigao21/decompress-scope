"""密码库管理接口（D/U/筛选/统计）的单元测试。

覆盖：删除单条、批量删除（含空列表）、按来源删除、更新密码、更新来源、
更新撞 UNIQUE 触发合并、query 的 keyword/来源三态/四种排序、sources 聚合含空来源、
clear_all、count/get。
"""
from core.vault import PasswordVault, SourceStat, VaultEntry


def _seed(v: PasswordVault) -> None:
    """构造一批覆盖多来源/多命中/多时间的记录。"""
    v.record_success("alpha", "a.com")
    v.record_success("alpha", "a.com")      # alpha@a.com hit=2
    v.record_success("beta", "a.com")       # beta@a.com hit=1
    v.record_success("gamma", "b.com")      # gamma@b.com hit=1
    v.add_manual("delta")                    # delta@'' hit=0（无来源）
    v.add_manual("Epsilon", "b.com")        # Epsilon@b.com hit=0


# ---------- count / get ----------

def test_count_and_get(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    assert v.count() == 0
    _seed(v)
    assert v.count() == 5

    entries = v.query(order="password")
    first = entries[0]
    got = v.get(first.id)
    assert got == first
    assert isinstance(got, VaultEntry)

    assert v.get(99999) is None
    v.close()


# ---------- delete ----------

def test_delete_single(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    _seed(v)
    target = v.query(keyword="gamma")[0]
    assert v.delete(target.id) is True
    assert v.get(target.id) is None
    assert v.count() == 4
    # 再删同一 id → 不存在
    assert v.delete(target.id) is False
    v.close()


def test_delete_many(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    _seed(v)
    # 空列表：直接返回 0，不发 SQL
    assert v.delete_many([]) == 0
    ids = [e.id for e in v.query(source="a.com")]
    assert len(ids) == 2
    removed = v.delete_many(ids)
    assert removed == 2
    assert v.count() == 3
    # 混入不存在的 id：只删真实存在的那条
    live = v.query(keyword="delta")[0]
    assert v.delete_many([live.id, 888888]) == 1
    assert v.count() == 2
    v.close()


def test_delete_by_source(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    _seed(v)
    removed = v.delete_by_source("a.com")
    assert removed == 2
    assert v.count() == 3
    # 删无来源分组
    assert v.delete_by_source("") == 1
    assert v.count() == 2
    # 删不存在的来源 → 0
    assert v.delete_by_source("nope.com") == 0
    v.close()


# ---------- update ----------

def test_update_password(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    _seed(v)
    target = v.query(keyword="beta")[0]
    assert v.update(target.id, password="beta-new") is True
    got = v.get(target.id)
    assert got.password == "beta-new"
    assert got.source == "a.com"          # source 未传 None → 不变
    assert got.hit_count == target.hit_count
    v.close()


def test_update_source(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    _seed(v)
    target = v.query(keyword="gamma")[0]
    assert v.update(target.id, source="c.com") is True
    assert v.get(target.id).source == "c.com"
    v.close()


def test_update_source_to_empty(tmp_path):
    """显式传 "" = 改为无来源（区别于 None = 不改）。"""
    v = PasswordVault(tmp_path / "v.db")
    _seed(v)
    target = v.query(keyword="gamma")[0]
    assert v.update(target.id, source="") is True
    assert v.get(target.id).source == ""
    v.close()


def test_update_merge_on_unique_conflict(tmp_path):
    """改后撞 UNIQUE(password, source) → 合并语义：hit_count 累加、条数 -1、返回 True。"""
    v = PasswordVault(tmp_path / "v.db")
    v.record_success("shared", "a.com")     # shared@a.com hit=1
    v.record_success("shared", "a.com")     # hit=2
    v.add_manual("dup", "a.com")             # dup@a.com hit=0
    before = v.count()                       # 2
    dup_entry = v.query(keyword="dup")[0]
    shared_entry = v.query(keyword="shared")[0]

    assert v.update(dup_entry.id, password="shared") is True   # 合并到 shared@a.com
    assert v.count() == before - 1
    assert v.get(dup_entry.id) is None       # 被编辑行已删除
    merged = v.get(shared_entry.id)
    assert merged is not None
    assert merged.hit_count == 2 + 0         # 冲突行 hit 2 + 被编辑行 hit 0
    v.close()


def test_update_merge_accumulates_hits(tmp_path):
    """被编辑行有命中时，命中数必须被累加过去。"""
    v = PasswordVault(tmp_path / "v.db")
    v.record_success("keep", "s.com")        # hit=1
    v.record_success("move", "s.com")        # hit=1
    v.record_success("move", "s.com")        # hit=2
    move_entry = v.query(keyword="move")[0]
    keep_entry = v.query(keyword="keep")[0]
    assert move_entry.hit_count == 2
    assert v.update(move_entry.id, password="keep") is True
    merged = v.get(keep_entry.id)
    assert merged is not None
    assert merged.hit_count == 3             # 1 + 2
    assert v.count() == 1
    v.close()


def _set_last_hit(v: PasswordVault, entry_id: int, value: str | None) -> None:
    """直接改写 last_hit_at，构造合并语义的 4 种 NULL 组合（测试专用）。"""
    v.conn.execute("UPDATE passwords SET last_hit_at = ? WHERE id = ?", (value, entry_id))
    v.conn.commit()


# 合并语义：last_hit_at 取两行较晚者 —— 4 种 NULL 组合矩阵
# (保留行 last_hit_at, 被编辑行 last_hit_at) -> 期望合并后
_MERGE_LAST_HIT_CASES = [
    ("2024-01-01T00:00:00", "2024-06-01T00:00:00", "2024-06-01T00:00:00"),
    (None,                  "2024-06-01T00:00:00", "2024-06-01T00:00:00"),
    ("2024-01-01T00:00:00", None,                  "2024-01-01T00:00:00"),
    (None,                  None,                  None),
]


def test_update_merge_last_hit_takes_later(tmp_path):
    """合并语义回归：last_hit_at 取较晚者，且 NULL 不能被抹掉（4 种组合全覆盖）。

    这是原实现（只累加 hit_count，不动 last_hit_at）暴露的缺陷修复点。

    每个组合用**独立 db**（避免用例间互相污染），并用 try/finally 保证连接
    在任何断言失败时都被关闭——连接不泄漏，避免 WAL 附属文件/句柄在
    同进程内累积（Windows 上更易暴露），也避免失败时留下悬挂连接。
    """
    for idx, (kept_lh, edited_lh, expected) in enumerate(_MERGE_LAST_HIT_CASES):
        db = tmp_path / f"merge_case_{idx}.db"
        v = PasswordVault(db)
        try:
            # 保留行（hit=1），被编辑行（hit=1）
            v.record_success("keep", "s.com")
            v.record_success("move", "s.com")
            keep = v.query(keyword="keep")[0]
            move = v.query(keyword="move")[0]
            _set_last_hit(v, keep.id, kept_lh)
            _set_last_hit(v, move.id, edited_lh)

            assert v.update(move.id, password="keep") is True
            merged = v.get(keep.id)
            assert merged is not None, f"kept={kept_lh!r} edited={edited_lh!r}"
            assert merged.last_hit_at == expected, (
                f"kept={kept_lh!r} edited={edited_lh!r} → 期望 {expected!r}，实际 {merged.last_hit_at!r}")
            assert merged.hit_count == 2, "hit_count 应为两行之和 1+1"
            assert v.count() == 1
        finally:
            v.close()


def test_update_merge_last_hit_does_not_clobber_with_null(tmp_path):
    """定向防回归：SQLite 标量 MAX(NULL, x) 返回 NULL——绝不能用它抹掉有效值。

    保留行 NULL + 被编辑行有值 → 必须保留被编辑行的有效值。
    """
    v = PasswordVault(tmp_path / "v.db")
    v.record_success("keep", "s.com")
    v.record_success("move", "s.com")
    keep = v.query(keyword="keep")[0]
    move = v.query(keyword="move")[0]
    _set_last_hit(v, keep.id, None)
    _set_last_hit(v, move.id, "2024-06-01T00:00:00")

    assert v.update(move.id, password="keep") is True
    merged = v.get(keep.id)
    assert merged.last_hit_at == "2024-06-01T00:00:00", (
        "MAX(NULL, x) 返回 NULL 会把有效值抹掉；三层 COALESCE 兜底应保留 x")
    v.close()


def test_update_merge_keeps_created_at(tmp_path):
    """created_at 不参与合并，保留原值（被编辑行是即将消失的重复记录）。"""
    v = PasswordVault(tmp_path / "v.db")
    v.record_success("keep", "s.com")
    v.record_success("move", "s.com")
    keep = v.query(keyword="keep")[0]
    move = v.query(keyword="move")[0]
    v.conn.execute("UPDATE passwords SET created_at = ? WHERE id = ?",
                   ("2020-01-01T00:00:00", keep.id))
    v.conn.execute("UPDATE passwords SET created_at = ? WHERE id = ?",
                   ("2023-12-31T00:00:00", move.id))
    v.conn.commit()

    assert v.update(move.id, password="keep") is True
    merged = v.get(keep.id)
    assert merged.created_at == "2020-01-01T00:00:00"
    v.close()


def test_update_merge_preserves_candidate_ordering(tmp_path):
    """症状级回归：合并后 candidates_for() 的排序仍正确反映"最近命中"。

    比只断言字段值更贴近危害本身——锁的是"排序退化"这个用户可见症状，
    而非合并 SQL 的实现细节。架构师独立复核时提出该场景。

    关键：必须用 kept=NULL / edited=有值 的组合。因为旧实现（只累加 hit_count、
    不动 last_hit_at）恰恰只在"保留行为 NULL"时丢值；若换成保留行本就为 2025，
    旧实现也会因"不动保留行"而碰巧正确，测不出回归。

    场景：late(hit=1, last_hit=NULL, 将被保留) ← early(hit=1, last_hit=2025)
    合并后该条 last_hit 应变为 2025，候选排序中它应排在同来源"从未命中"的 other 之前。
    """
    v = PasswordVault(tmp_path / "v.db")
    v.record_success("late", "s.com")         # 保留行：从未命中
    v.record_success("early", "s.com")        # 被编辑行：最近命中年份较晚
    v.add_manual("other", "s.com")            # 同来源对照组，从未命中
    late = v.query(keyword="late")[0]
    early = v.query(keyword="early")[0]
    other = v.query(keyword="other")[0]
    _set_last_hit(v, late.id, None)
    _set_last_hit(v, early.id, "2025-01-01T00:00:00")
    _set_last_hit(v, other.id, None)

    # 把 early 并入 late（改名为 late）
    assert v.update(early.id, password="late") is True
    assert v.count() == 2

    merged = v.get(late.id)
    assert merged is not None
    assert merged.hit_count == 2
    assert merged.last_hit_at == "2025-01-01T00:00:00", (
        "合并后被编辑行的较晚命中时间必须归并进来，不能被 NULL 抹掉")

    # 症状验证：candidates_for 中，有较晚命中的 late 应排在从未命中的 other 之前
    cands = v.candidates_for("s.com")
    assert "late" in cands and "other" in cands
    assert cands.index("late") < cands.index("other"), (
        f"候选排序未体现较晚命中，出现排序退化：{cands}")
    v.close()


def test_update_missing_id_returns_false(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    _seed(v)
    assert v.update(424242, password="x") is False
    v.close()


def test_update_noop_returns_true(tmp_path):
    """不传任何变更参数 → 视为成功（幂等），不报错。"""
    v = PasswordVault(tmp_path / "v.db")
    _seed(v)
    target = v.query(keyword="beta")[0]
    assert v.update(target.id) is True
    assert v.get(target.id).password == "beta"
    v.close()


# ---------- query: keyword ----------

def test_query_keyword_matches_password_and_source(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    _seed(v)
    # 匹配密码名（大小写不敏感）
    hits = v.query(keyword="ALPHA")
    assert {e.password for e in hits} == {"alpha"}
    # 匹配来源
    by_src = v.query(keyword="A.COM")
    assert {e.source for e in by_src} == {"a.com"}
    assert len(by_src) == 2
    # 无匹配
    assert v.query(keyword="zzz-nope") == []
    v.close()


# ---------- query: 来源三态 ----------

def test_query_source_three_states(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    _seed(v)
    # None = 全部
    assert len(v.query(source=None)) == 5
    # "" = 仅无来源
    only_empty = v.query(source="")
    assert len(only_empty) == 1
    assert only_empty[0].password == "delta"
    # "a.com" = 指定来源
    a_only = v.query(source="a.com")
    assert {e.password for e in a_only} == {"alpha", "beta"}
    v.close()


# ---------- query: 排序 ----------

def test_query_order_source(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    _seed(v)
    asc = v.query(order="source")
    srcs = [e.source for e in asc]
    assert srcs == sorted(srcs)
    desc = v.query(order="source", descending=True)
    assert [e.source for e in desc] == sorted([e.source for e in desc], reverse=True)
    v.close()


def test_query_order_hits(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    _seed(v)
    asc = v.query(order="hits")
    hits = [e.hit_count for e in asc]
    assert hits == sorted(hits)
    # alpha@a.com hit=2 应排最前（降序）
    top = v.query(order="hits", descending=True)[0]
    assert top.password == "alpha"
    assert top.hit_count == 2
    v.close()


def test_query_order_password(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    _seed(v)
    asc = v.query(order="password")
    pwds = [e.password for e in asc]
    assert pwds == sorted(pwds)
    v.close()


def test_query_order_recent_nulls_last(tmp_path):
    """recent 排序：从未命中的（last_hit_at is None）恒沉底。"""
    v = PasswordVault(tmp_path / "v.db")
    _seed(v)
    asc = v.query(order="recent")
    # 尾部应全部是 None
    assert asc[-1].last_hit_at is None
    desc = v.query(order="recent", descending=True)
    assert desc[-1].last_hit_at is None
    # 有命中记录里的第一条在 asc / desc 中成镜像
    non_null_asc = [e.last_hit_at for e in asc if e.last_hit_at is not None]
    assert non_null_asc == sorted(non_null_asc)
    v.close()


def test_query_limit(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    _seed(v)
    assert len(v.query(limit=2)) == 2
    assert len(v.query(limit=0)) == 5
    v.close()


# ---------- sources ----------

def test_sources_aggregation_includes_empty(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    _seed(v)
    stats = v.sources()
    assert all(isinstance(s, SourceStat) for s in stats)
    by_src = {s.source: s for s in stats}
    assert set(by_src) == {"a.com", "b.com", ""}

    assert by_src["a.com"].count == 2
    assert by_src["a.com"].total_hits == 3     # alpha 2 + beta 1
    assert by_src["b.com"].count == 2
    assert by_src["b.com"].total_hits == 1     # gamma 1 + Epsilon 0
    assert by_src[""].count == 1
    assert by_src[""].total_hits == 0

    # 排序：count DESC, source ASC。a.com 与 b.com 都是 2，按 source 升序 a<b
    order = [s.source for s in stats]
    assert order.index("a.com") < order.index("b.com")
    assert order[-1] == ""                     # count=1 排最后
    v.close()


# ---------- clear_all ----------

def test_clear_all(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    _seed(v)
    removed = v.clear_all()
    assert removed == 5
    assert v.count() == 0
    assert v.query() == []
    assert v.sources() == []
    # 空库再清 → 0
    assert v.clear_all() == 0
    v.close()


# ---------- 向后兼容：冻结方法仍可用 ----------

def test_frozen_methods_still_work(tmp_path):
    v = PasswordVault(tmp_path / "v.db")
    v.record_success("pwA", "a.com")
    v.record_success("pwA", "a.com")
    v.add_manual("pwB", "")
    assert len(v.list_all()) == 2
    assert "pwA" in v.candidates_for("a.com")
    v.close()
