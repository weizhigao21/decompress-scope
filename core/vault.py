"""密码库：SQLite 存储，按来源分组记录成功密码与命中次数。

注意「分组存储」≠「分组检索」：来源只用于**排序**（同来源优先），
`candidates_for()` 会把全库都纳入候选，不隔离任何来源。

分层约定（见 docs/vault-window-design.md §8）：
- core 层零 Qt 依赖，方法返回 Python 原生类型 / frozen dataclass。
- 依赖方向单向：ui/* → core/*。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS passwords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    password TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    hit_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_hit_at TEXT,
    UNIQUE(password, source)
);
"""

# query() 的 order 参数 → SQL 片段映射（白名单，杜绝注入）
_ORDER_COLUMNS: dict[str, str] = {
    "source": "source",
    "hits": "hit_count",
    "recent": "last_hit_at",
    "password": "password",
}

# candidates_for() 的单次扫描上限。排序后前 N 条已远超任何实际用得到的配额
# （max_password_attempts 的上限是 200），但能挡住"把十万行字典灌进库"这种
# 让每个加密包都去拉全表的情形。去重与 limit 都在这批之内进行。
_SCAN_CAP = 2000


@dataclass(frozen=True)
class VaultEntry:
    """一条密码记录的只读视图。列表统一返回它，替代裸 tuple。"""

    id: int
    password: str
    source: str
    hit_count: int
    created_at: str          # ISO 8601（秒精度）
    last_hit_at: str | None  # ISO 8601 或 None


@dataclass(frozen=True)
class SourceStat:
    """按来源聚合的统计。"""

    source: str        # '' 表示"无来源"
    count: int         # 该来源下的密码条数
    total_hits: int    # 该来源下命中次数合计


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class PasswordVault:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        # WAL + busy_timeout：解压工作线程与窗口并发读写时降低 SQLITE_BUSY。
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------- 冻结区（签名稳定；candidates_for 的检索范围于 2026-09-26 放宽，
    #            理由见其 docstring，勿按"只查同来源"的旧印象改回去） ----------

    def record_success(self, password: str, source: str = "") -> None:
        """解压成功后回写：命中次数 +1，无则新建。"""
        now = _now()
        self.conn.execute(
            """INSERT INTO passwords(password, source, hit_count, created_at, last_hit_at)
               VALUES(?, ?, 1, ?, ?)
               ON CONFLICT(password, source)
               DO UPDATE SET hit_count = hit_count + 1, last_hit_at = excluded.last_hit_at""",
            (password, source, now, now),
        )
        self.conn.commit()

    def add_manual(self, password: str, source: str = "") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO passwords(password, source, hit_count, created_at)"
            " VALUES(?, ?, 0, ?)",
            (password, source, _now()),
        )
        self.conn.commit()

    def candidates_for(self, source: str = "", limit: int = 0) -> list[str]:
        """候选密码：同来源 → 无来源 → 其他来源，段内按命中排序，去重保序。

        `limit` 沿用 `query()` 的约定：0 = 不限，正数 = 取前 N 条。
        截断发生在**排序与去重之后**，所以高优先级段不会被低优先级段挤掉。

        为什么是三段而不是简单按 hit_count 全局排：密码库是"我自己试通过什么"
        的历史，站点归属不是租户边界，但"属于另一个站点"与"没有站点归属"确实
        不是同一件事——后者多为手工录入、或来自文件名里读不出域名的包，属于
        通用密码，在无法判断时更值得先试。

        为什么不再只查 `source=?` 与 `source=''`：`record_success` 是按
        `extract_source(文件名)` 写入的，旧实现让每个来源各自成岛，跨来源的条目
        **永远不会被试**；文件名里带域名的包越多，盲区越大。而同一个发布者批量
        打包、或用户复用同一密码时，跨来源命中是常态。
        """
        cur = self.conn.execute(
            """SELECT password FROM passwords
                ORDER BY CASE WHEN source = ? THEN 0
                              WHEN source = '' THEN 1
                              ELSE 2 END ASC,
                         hit_count DESC,
                         last_hit_at DESC
                LIMIT ?""",
            (source, _SCAN_CAP),
        )
        seen: set[str] = set()
        ordered: list[str] = []
        for (pwd,) in cur.fetchall():
            key = pwd.lower()
            if key not in seen:
                seen.add(key)
                ordered.append(pwd)
        return ordered[:limit] if limit > 0 else ordered

    def list_all(self) -> list[tuple[str, str, int, str | None]]:
        cur = self.conn.execute(
            "SELECT password, source, hit_count, last_hit_at FROM passwords"
            " ORDER BY source, hit_count DESC"
        )
        return cur.fetchall()

    # ---------- 新增区：读 ----------

    def _row_to_entry(self, row: tuple) -> VaultEntry:
        """把 SELECT 出的 6 元组映射为 VaultEntry。"""
        return VaultEntry(
            id=int(row[0]),
            password=str(row[1]),
            source=str(row[2]),
            hit_count=int(row[3]),
            created_at=str(row[4]),
            last_hit_at=(str(row[5]) if row[5] is not None else None),
        )

    def query(
        self,
        keyword: str = "",
        source: str | None = None,
        order: str = "source",
        descending: bool = False,
        limit: int = 0,
    ) -> list[VaultEntry]:
        """统一查询入口。

        keyword 对 password 与 source 做大小写不敏感的包含匹配。
        source: None=全部 / ""=仅无来源 / "a.com"=指定来源。
        order: "source" | "hits" | "recent" | "password"。
        limit: 0 = 不限。
        """
        where: list[str] = []
        params: list[object] = []

        kw = keyword.strip().lower()
        if kw:
            # 转义 LIKE 通配符，使 keyword 按字面（包含）匹配：
            # 反斜杠须最先转义，否则会破坏后续的 \% / \_ 转义。
            esc = kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{esc}%"
            where.append(
                "(LOWER(password) LIKE ? ESCAPE '\\' OR LOWER(source) LIKE ? ESCAPE '\\')"
            )
            params.extend([like, like])

        if source is not None:
            where.append("source = ?")
            params.append(source)

        sql = "SELECT id, password, source, hit_count, created_at, last_hit_at FROM passwords"
        if where:
            sql += " WHERE " + " AND ".join(where)

        column = _ORDER_COLUMNS.get(order, _ORDER_COLUMNS["source"])
        direction = "DESC" if descending else "ASC"
        # 稳定的次级排序：同级按来源、再按 id 升序，避免结果抖动。
        if column == "source":
            sql += f" ORDER BY source {direction}, hit_count DESC, id ASC"
        elif column == "hit_count":
            sql += f" ORDER BY hit_count {direction}, source ASC, id ASC"
        elif column == "last_hit_at":
            # NULL 恒排在最后（无论升降序），让"从未命中"沉底。
            sql += f" ORDER BY (last_hit_at IS NULL) ASC, last_hit_at {direction}, id ASC"
        else:
            sql += f" ORDER BY password {direction}, id ASC"

        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))

        cur = self.conn.execute(sql, params)
        return [self._row_to_entry(r) for r in cur.fetchall()]

    def sources(self) -> list[SourceStat]:
        """所有来源及各自条数/命中合计，按 count DESC, source ASC。含 source=''。"""
        cur = self.conn.execute(
            "SELECT source, COUNT(*) AS c, COALESCE(SUM(hit_count), 0) AS h"
            " FROM passwords GROUP BY source ORDER BY c DESC, source ASC"
        )
        return [
            SourceStat(source=str(r[0]), count=int(r[1]), total_hits=int(r[2]))
            for r in cur.fetchall()
        ]

    def count(self) -> int:
        """总条数。主窗口计数标签专用，避免拉全表。"""
        cur = self.conn.execute("SELECT COUNT(*) FROM passwords")
        return int(cur.fetchone()[0])

    def get(self, entry_id: int) -> VaultEntry | None:
        """按主键取单条。不存在返回 None。"""
        cur = self.conn.execute(
            "SELECT id, password, source, hit_count, created_at, last_hit_at"
            " FROM passwords WHERE id = ?",
            (int(entry_id),),
        )
        row = cur.fetchone()
        return self._row_to_entry(row) if row is not None else None

    # ---------- 新增区：写 ----------

    def delete(self, entry_id: int) -> bool:
        """删除单条。返回是否真的删到了（False = id 不存在）。"""
        cur = self.conn.execute("DELETE FROM passwords WHERE id = ?", (int(entry_id),))
        self.conn.commit()
        return cur.rowcount > 0

    def delete_many(self, entry_ids: list[int]) -> int:
        """批量删除，返回实际删除条数。空列表直接返回 0，不发 SQL。"""
        if not entry_ids:
            return 0
        ids = [int(i) for i in entry_ids]
        placeholders = ",".join("?" for _ in ids)
        cur = self.conn.execute(
            f"DELETE FROM passwords WHERE id IN ({placeholders})", ids
        )
        self.conn.commit()
        return int(cur.rowcount)

    def delete_by_source(self, source: str) -> int:
        """删除某来源下全部密码，返回删除条数。source='' 表示无来源分组。"""
        cur = self.conn.execute("DELETE FROM passwords WHERE source = ?", (source,))
        self.conn.commit()
        return int(cur.rowcount)

    def update(
        self,
        entry_id: int,
        password: str | None = None,
        source: str | None = None,
    ) -> bool:
        """修改密码/来源。返回是否成功（False = id 不存在）。

        冲突处理：若改后与既有 (password, source) 撞 UNIQUE 约束，采用合并语义——
        把被编辑行的 hit_count 累加到冲突保留行、last_hit_at 取两者较晚者、
        删除被编辑行，返回 True。不向上抛 sqlite3.IntegrityError。
        """
        entry_id = int(entry_id)
        current = self.get(entry_id)
        if current is None:
            return False

        new_password = current.password if password is None else password
        new_source = current.source if source is None else source

        # 无实际变化时直接视为成功，不做多余写入。
        if new_password == current.password and new_source == current.source:
            return True

        # 查是否已存在目标 (password, source) 的另一条记录（排除自身）。
        conflict = self.conn.execute(
            "SELECT id FROM passwords WHERE password = ? AND source = ? AND id <> ?",
            (new_password, new_source, entry_id),
        ).fetchone()

        if conflict is not None:
            # 合并语义：把被编辑行的 hit_count 与 last_hit_at 归并到冲突保留行，删除被编辑行。
            #
            # 关于 last_hit_at：应取两行的"较晚者"，但不能直接写 MAX(a, b)——
            # SQLite 标量 MAX() 任一参数为 NULL 即返回 NULL，而 NULL 是
            # "手动添加、从未命中"的密码的合法 last_hit_at，会把有效值抹掉。
            # 故用三层 COALESCE 兜底（顺序不可调换）：
            #   第一层 MAX(...)  = 语义（取较晚者）
            #   第二层 last_hit_at           = 兜底（被编辑行为 NULL）
            #   第三层 (SELECT ...)          = 兜底（保留行为 NULL）
            # created_at 不参与合并，保留原值即正确（它是"记录何时进库"）。
            keep_id = int(conflict[0])
            self.conn.execute(
                """UPDATE passwords
                      SET hit_count   = hit_count + (SELECT hit_count FROM passwords WHERE id = ?),
                          last_hit_at = COALESCE(
                              MAX(last_hit_at, (SELECT last_hit_at FROM passwords WHERE id = ?)),
                              last_hit_at,
                              (SELECT last_hit_at FROM passwords WHERE id = ?)
                          )
                    WHERE id = ?""",
                (entry_id, entry_id, entry_id, keep_id),
            )
            self.conn.execute("DELETE FROM passwords WHERE id = ?", (entry_id,))
            self.conn.commit()
            return True

        self.conn.execute(
            "UPDATE passwords SET password = ?, source = ? WHERE id = ?",
            (new_password, new_source, entry_id),
        )
        self.conn.commit()
        return True

    def clear_all(self) -> int:
        """清空整个密码库，返回删除条数。危险操作，UI 必须二次确认。"""
        cur = self.conn.execute("DELETE FROM passwords")
        self.conn.commit()
        return int(cur.rowcount)
