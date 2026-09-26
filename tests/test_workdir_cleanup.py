"""工作目录残留盘点与清理的守卫。

背景：工作目录此前**没有任何清理机制**，实测 `task_83` 一次留下 5.38 GB——
而库里那条记录是 DONE、界面显示「完成」，用户既没有线索也没有入口。
这组测试钉住两件事：

1. **定性必须保守**——只有"产物已确认交付到工作目录之外"才算可清理；
   产物还在这儿、任务没结束、库里查不到的，一律标保留（那可能是唯一副本）。
2. **删除必须收紧**——凡是超出工作目录范围的目标（越界路径、非 task_<id> 名、
   符号链接）一律拒删，绝不因为是"我们自己建的目录"就放手。

测试构造顺序固定为：建库拿真实 id → 按 id 建 `task_<id>` 目录 → 盘点/清理。
绝不假设自增 id 等于想要的数字（自增值由插入顺序决定，不是我们想要什么就是什么）。
"""
import os
import shutil

import pytest

from core.models import Task, TaskStatus
from core.store import TaskStore
from core.workdir_cleanup import (
    CLEANABLE,
    NOT_DELIVERED,
    ORPHAN,
    UNFINISHED,
    Leftover,
    iter_task_dirs,
    prune,
    scan_workdir,
)


def _mk_workdir(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    return wd


def _mk_task_dir(workdir, task_id: int, payload: bytes = b"x" * 100):
    d = workdir / f"task_{task_id}" / "out" / f"pack{task_id}"
    d.mkdir(parents=True)
    (d / "payload.bin").write_bytes(payload)
    return d


def _store_with(tmp_path, specs: list[dict]):
    """建库并按顺序插入任务，返回 (store, 真实 id 列表)。

    注意：`store.create` 只写 archive_path/status 等基础列，extracted_dir 与
    delivery_error 要靠 update 落库——这也是真实链路的顺序（先 create 建行，
    解压完再回写落点）。
    """
    store = TaskStore(tmp_path / "t.db")
    ids: list[int] = []
    for i, spec in enumerate(specs):
        t = Task(
            archive_path=spec.get("archive_path", str(tmp_path / f"a{i}.zip")),
            depth=spec.get("depth", 0),
            status=spec.get("status", TaskStatus.DONE),
        )
        t.id = store.create(t)
        t.extracted_dir = spec.get("extracted_dir", "")
        t.delivery_error = spec.get("delivery_error", "")
        store.update(t)
        ids.append(t.id)
    return store, ids


# ---------- 盘点：只认自己的目录 ----------


def test_iter_task_dirs_only_claims_own_dirs(tmp_path):
    """只把 `task_<数字>` 认作自己的残留；用户放进来的其它目录/文件一概不算。

    工作目录理论上是我们独占的，但用户完全可能把压缩包直接拖到这儿、
    或者在这里放别的目录。用白名单认领比"假设这层都是我的"安全得多。
    """
    wd = _mk_workdir(tmp_path)
    (wd / "task_7").mkdir()
    (wd / "task_12").mkdir()
    (wd / "用户自己的目录").mkdir()
    (wd / "downloads").mkdir()
    (wd / "task_abc").mkdir()          # 名字形似但不是 id
    (wd / "task_7.txt").write_text("file", encoding="utf-8")

    names = [p.name for p in iter_task_dirs(wd)]

    # 按任务号数值排序，不是字符串序（字符串序会把 task_12 排在 task_7 前面）
    assert names == ["task_7", "task_12"]


def test_iter_task_dirs_missing_workdir_is_empty(tmp_path):
    """工作目录还不存在时不能抛异常——主界面启动就会调它。"""
    assert iter_task_dirs(tmp_path / "nope") == []


# ---------- 定性 ----------


def test_delivered_copy_is_cleanable(tmp_path):
    """产物已交付到工作目录之外：工作目录里这份是副本，可清理。"""
    wd = _mk_workdir(tmp_path)
    final = tmp_path / "downloads" / "_解压开镜" / "pack"
    final.mkdir(parents=True)
    store, (tid,) = _store_with(tmp_path, [{"extracted_dir": str(final)}])
    _mk_task_dir(wd, tid)

    rows = scan_workdir(wd, store)
    store.close()

    assert len(rows) == 1
    assert rows[0].kind == CLEANABLE and rows[0].safe is True
    assert str(final) in rows[0].reason, "理由里要写明产物现在在哪，用户才敢删"


def test_product_still_inside_workdir_is_kept(tmp_path):
    """库里的产物位置仍指向工作目录内部 = 没搬出去，必须保留。

    这是最常见的形态：跨卷 copytree 之后源目录留在工作目录里当安全网，
    此时 extracted_dir 若没同步到交付位置，就只能保守地判"没交付"。
    """
    wd = _mk_workdir(tmp_path)
    store, (tid,) = _store_with(tmp_path, [{}])
    inside = _mk_task_dir(wd, tid)
    t = store.get(tid)
    t.extracted_dir = str(inside)
    store.update(t)

    rows = scan_workdir(wd, store)
    store.close()

    assert rows[0].kind == NOT_DELIVERED
    assert rows[0].safe is False, "唯一副本绝不能被默认勾选清理"


def test_delivery_error_forces_keep(tmp_path):
    """交付失败必须标保留，理由里带上失败原因。"""
    wd = _mk_workdir(tmp_path)
    outside = tmp_path / "downloads" / "pack"
    outside.mkdir(parents=True)
    store, (tid,) = _store_with(tmp_path, [{
        "extracted_dir": str(outside),
        "delivery_error": "交付到源目录失败（产物仍在工作目录）：磁盘空间不足",
    }])
    _mk_task_dir(wd, tid)

    rows = scan_workdir(wd, store)
    store.close()

    assert rows[0].kind == NOT_DELIVERED
    assert "磁盘空间不足" in rows[0].reason


def test_unfinished_task_is_kept(tmp_path):
    """待密码/失败等非 DONE 的任务：产物去向未知，保留。"""
    wd = _mk_workdir(tmp_path)
    store, (tid,) = _store_with(tmp_path, [{"status": TaskStatus.NEEDS_PASSWORD}])
    _mk_task_dir(wd, tid)

    rows = scan_workdir(wd, store)
    store.close()

    assert rows[0].kind == UNFINISHED
    assert "needs_password" in rows[0].reason


def test_orphan_dir_requires_confirmation(tmp_path):
    """库里查不到的目录：待确认，不默认勾选。"""
    wd = _mk_workdir(tmp_path)
    store, (tid,) = _store_with(tmp_path, [{}])
    _mk_task_dir(wd, tid + 500)   # 库里没有这个 id

    rows = scan_workdir(wd, store)
    store.close()

    assert rows[0].kind == ORPHAN
    assert rows[0].safe is False


def test_scan_reports_real_size_and_file_count(tmp_path):
    """大小/文件数必须真统计——用户就是冲着"占了多少空间"来的。"""
    wd = _mk_workdir(tmp_path)
    store, (tid,) = _store_with(tmp_path, [{}])
    d = wd / f"task_{tid}" / "out" / "pack"
    d.mkdir(parents=True)
    (d / "a.bin").write_bytes(b"a" * 1000)
    (d / "sub").mkdir()
    (d / "sub" / "b.bin").write_bytes(b"b" * 500)

    rows = scan_workdir(wd, store)
    store.close()

    assert rows[0].size == 1500
    assert rows[0].files == 2


# ---------- 清理：默认只删安全的 ----------


def test_prune_removes_only_safe_by_default(tmp_path):
    """默认只删「可清理」项；「请保留」的必须原样留着，并在 errors 里说明原因。"""
    wd = _mk_workdir(tmp_path)
    outside = tmp_path / "downloads" / "pack"
    outside.mkdir(parents=True)
    store, (safe_id, keep_id) = _store_with(tmp_path, [
        {"extracted_dir": str(outside)},
        {"status": TaskStatus.NEEDS_PASSWORD},
    ])
    _mk_task_dir(wd, safe_id)
    _mk_task_dir(wd, keep_id)
    rows = scan_workdir(wd, store)
    store.close()

    freed, errors = prune(rows, wd)

    assert freed > 0
    assert not (wd / f"task_{safe_id}").exists(), "已交付的副本没被清掉"
    assert (wd / f"task_{keep_id}").exists(), "未完成任务的产物被误删"
    assert any("跳过" in e for e in errors), "被跳过的项必须给出理由"


def test_prune_allow_unsafe_removes_kept_items(tmp_path):
    """显式 allow_unsafe 才动「请保留」项（CLI --all / 界面二次确认的路径）。"""
    wd = _mk_workdir(tmp_path)
    store, (tid,) = _store_with(tmp_path, [{"status": TaskStatus.NEEDS_PASSWORD}])
    _mk_task_dir(wd, tid)
    rows = scan_workdir(wd, store)
    store.close()

    freed, errors = prune(rows, wd, allow_unsafe=True)

    assert errors == []
    assert freed > 0
    assert not (wd / f"task_{tid}").exists()


def test_prune_leaves_siblings_alone(tmp_path):
    """只删清单里的项，同批没列进去的目录必须原封不动。"""
    wd = _mk_workdir(tmp_path)
    store, (one, two) = _store_with(tmp_path, [{}, {}])
    _mk_task_dir(wd, one)
    _mk_task_dir(wd, two)
    rows = {r.path.name: r for r in scan_workdir(wd, store)}
    store.close()

    prune([rows[f"task_{one}"]], wd, allow_unsafe=True)

    assert not (wd / f"task_{one}").exists()
    assert (wd / f"task_{two}").is_dir(), "没列进清单的目录被连带删了"


def test_prune_already_gone_is_not_an_error(tmp_path):
    """清单里的目录在删除前已消失（另一个进程清过）：报提示但不崩。"""
    wd = _mk_workdir(tmp_path)
    store, (tid,) = _store_with(tmp_path, [{}])
    d = _mk_task_dir(wd, tid)
    rows = scan_workdir(wd, store)
    store.close()
    shutil.rmtree(d.parent.parent)   # 模拟被别的进程清掉

    freed, errors = prune(rows, wd, allow_unsafe=True)

    assert freed == 0
    assert any("已不存在" in e for e in errors), errors


# ---------- 清理：越界三道闸 ----------


def test_prune_refuses_target_outside_workdir(tmp_path):
    """越界拒删：指向工作目录之外的目标一律不动，哪怕它名字长得像 task_1。"""
    wd = _mk_workdir(tmp_path)
    victim = tmp_path / "外面" / "task_1"
    victim.mkdir(parents=True)
    (victim / "important.txt").write_text("用户数据", encoding="utf-8")

    # 直接伪造一个 Leftover：模拟调用方拿到被篡改/拼接出来的路径
    fake = Leftover(path=victim, task_id=1, kind=CLEANABLE, reason="伪造",
                    size=10, files=1, mtime=0.0)
    freed, errors = prune([fake], wd)

    assert freed == 0
    assert (victim / "important.txt").is_file(), "工作目录之外的目录被删了"
    assert any("直属子目录" in e for e in errors), errors


def test_prune_refuses_non_task_dir_name(tmp_path):
    """名字不是 task_<数字> 的一律不删——用户自己放进工作目录的东西不在范围内。"""
    wd = _mk_workdir(tmp_path)
    (wd / "downloads").mkdir()
    (wd / "downloads" / "keep.txt").write_text("keep", encoding="utf-8")

    fake = Leftover(path=wd / "downloads", task_id=None, kind=CLEANABLE, reason="伪造",
                    size=4, files=1, mtime=0.0)
    freed, errors = prune([fake], wd)

    assert freed == 0
    assert (wd / "downloads" / "keep.txt").is_file()
    assert any("task_<id>" in e for e in errors), errors


def test_prune_refuses_symlink(tmp_path):
    """符号链接拒删：rmtree 会顺着链接删到目标目录去，是彻底的越界删除。"""
    wd = _mk_workdir(tmp_path)
    real = tmp_path / "真目录"
    real.mkdir()
    (real / "data.txt").write_text("outside", encoding="utf-8")
    link = wd / "task_1"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接（需要开发者模式/管理员）")

    fake = Leftover(path=link, task_id=1, kind=CLEANABLE, reason="伪造",
                    size=1, files=1, mtime=0.0)
    freed, errors = prune([fake], wd)

    assert freed == 0
    assert (real / "data.txt").is_file(), "顺着符号链接删到了工作目录外面"
    assert any("链接" in e for e in errors), errors


def test_prune_refuses_junction(tmp_path):
    """junction 同样拒删，且**不能**只靠 os.path.islink 判。

    Windows 上 `os.path.islink(junction)` 返回 False（实测），但 junction 也
    指向另一个目录，rmtree 的后果与符号链接一样是越界删除。这条测试钉住
    "必须查 reparse 标志位"这一实现细节——只写 islink 会让这个洞静默存在。
    """
    import subprocess

    wd = _mk_workdir(tmp_path)
    real = tmp_path / "真目录"
    real.mkdir()
    (real / "data.txt").write_text("outside", encoding="utf-8")
    link = wd / "task_1"
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)],
                          capture_output=True, errors="replace")
    if proc.returncode != 0 or not link.exists():
        pytest.skip(f"当前环境无法创建 junction：{proc.stdout or proc.stderr}")

    # 先确认这条测试的前提成立：islink 认不出 junction
    assert os.path.islink(link) is False, \
        "本环境 islink 能认出 junction，这条测试的前提不成立"

    fake = Leftover(path=link, task_id=1, kind=CLEANABLE, reason="伪造",
                    size=1, files=1, mtime=0.0)
    freed, errors = prune([fake], wd)

    assert freed == 0
    assert (real / "data.txt").is_file(), "顺着 junction 删到了工作目录外面"
    assert any("链接" in e for e in errors), errors
