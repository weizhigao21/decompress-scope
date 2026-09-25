"""应用配置持久化测试：字段校验、损坏容错、原子落盘、历史记录。"""
import json
from pathlib import Path

from core.appconfig import (
    AUTORUN_CONFIRM,
    AUTORUN_DIRECT,
    AUTORUN_MODES,
    AUTORUN_OFF,
    OPEN_ITEMS,
    OPEN_PATHS,
    OUTPUT_MODES,
    OUTPUT_SAMEDIR,
    OUTPUT_WORKDIR,
    AppConfig,
)


def test_defaults_are_sane(tmp_path):
    """默认值必须是"开箱可用"的：解到源目录旁、拖入即开始、完成后开目录。"""
    cfg = AppConfig()
    assert cfg.output_mode == OUTPUT_SAMEDIR
    assert cfg.output_mode in OUTPUT_MODES
    assert cfg.autorun_mode == AUTORUN_DIRECT
    assert cfg.open_after == OPEN_PATHS
    assert cfg.open_after in OPEN_ITEMS
    assert cfg.recent_inputs == []
    assert cfg.subdir_name == "_解压开镜"


def test_roundtrip(tmp_path):
    """存盘再读回，所有字段值不变。"""
    path = tmp_path / "cfg.json"
    cfg = AppConfig(
        output_mode=OUTPUT_WORKDIR,
        autorun_mode=AUTORUN_CONFIRM,
        open_after="workdir",
        max_depth=7,
        max_total_gb=120,
        max_ratio=250.5,
        sniff_archives=False,
        keep_original=False,
        recent_inputs=["D:/a.zip", "D:/b.rar"],
        recent_inputs_limit=5,
    ).normalize()
    cfg.save(path)

    back = AppConfig.load(path)
    assert back.to_dict() == cfg.to_dict()
    assert back.recent_inputs == ["D:/a.zip", "D:/b.rar"]


def test_load_missing_file_returns_defaults(tmp_path):
    """文件不存在 → 默认配置，不抛异常。"""
    cfg = AppConfig.load(tmp_path / "nope" / "cfg.json")
    assert cfg.to_dict() == AppConfig().to_dict()


def test_load_corrupt_json_returns_defaults(tmp_path):
    """半截 JSON / 非 JSON → 默认配置，不把损坏内容抛给 UI。"""
    path = tmp_path / "cfg.json"
    path.write_text('{"max_depth": 3, "unterminated', encoding="utf-8")
    assert AppConfig.load(path).to_dict() == AppConfig().to_dict()

    path.write_text("这不是 JSON", encoding="utf-8")
    assert AppConfig.load(path).to_dict() == AppConfig().to_dict()

    path.write_text("", encoding="utf-8")
    assert AppConfig.load(path).to_dict() == AppConfig().to_dict()


def test_load_tolerates_bad_types_and_unknown_keys(tmp_path):
    """手改配置文件写错类型/写错键名，不能让程序崩。"""
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({
        "max_depth": "不是数字",
        "max_total_gb": None,
        "sniff_archives": "yes",
        "unknown_key": 123,
        "output_mode": "不存在的模式",
        "recent_inputs": "不是列表",
    }, ensure_ascii=False), encoding="utf-8")

    cfg = AppConfig.load(path)
    assert cfg.max_depth == AppConfig().max_depth      # 类型不符 → 默认值
    assert cfg.max_total_gb == AppConfig().max_total_gb
    assert cfg.sniff_archives is True                  # "yes" 能识别为真
    assert cfg.output_mode == OUTPUT_SAMEDIR           # 越界枚举 → 纠正
    assert cfg.recent_inputs == []                     # 非列表 → 空


def test_load_coerces_numeric_strings(tmp_path):
    """JSON 里写成字符串的数字应被接受（用户手改时常见）。"""
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"max_depth": "8", "max_ratio": "500.5"}),
                    encoding="utf-8")
    cfg = AppConfig.load(path)
    assert cfg.max_depth == 8
    assert cfg.max_ratio == 500.5


def test_clamp_out_of_range(tmp_path):
    """超范围数值收敛到边界，而不是拒绝整份配置。"""
    cfg = AppConfig(max_depth=999, max_total_gb=-5, max_ratio=0.0001,
                    max_password_attempts=100000, autorun_delay_ms=-100)
    cfg.normalize()
    assert cfg.max_depth == 10
    assert cfg.max_total_gb == 1
    assert cfg.max_ratio == 1.0
    assert cfg.max_password_attempts == 200
    assert cfg.autorun_delay_ms == 0


def test_normalize_subdir_name_blocks_escape():
    """子目录名不得含分隔符，否则会解压到源目录之外。"""
    cfg = AppConfig(subdir_name="../../etc").normalize()
    assert "/" not in cfg.subdir_name and "\\" not in cfg.subdir_name
    assert not cfg.subdir_name.startswith(".")

    cfg2 = AppConfig(subdir_name="   ").normalize()
    assert cfg2.subdir_name == "_解压开镜"


def test_normalize_subdir_name_blocks_drive_letter():
    """盘符冒号必须被中和——Path("C:foo") 会被 pathlib 当成盘符相对路径。"""
    cfg = AppConfig(subdir_name="C:\\Windows\\sys").normalize()
    assert ":" not in cfg.subdir_name
    assert not cfg.subdir_name.startswith(".")

    cfg2 = AppConfig(subdir_name="D:/payload").normalize()
    assert ":" not in cfg2.subdir_name
    assert cfg2.subdir_name == "D__payload"


def test_from_dict_none_str_field_keeps_default():
    """JSON 里的 null 不能变成字面字符串 "None"（会在源目录建出叫 None 的目录）。"""
    assert AppConfig.from_dict({"subdir_name": None}).subdir_name == "_解压开镜"
    assert AppConfig.from_dict({"workdir": None}).workdir == ""
    assert AppConfig.from_dict({"workdir": 12345}).workdir == ""
    assert AppConfig.from_dict({"output_mode": None}).output_mode == OUTPUT_SAMEDIR


def test_normalize_recent_inputs_dedup_and_limit():
    """历史输入去重保序、按上限截断。"""
    cfg = AppConfig(recent_inputs_limit=3,
                    recent_inputs=["a", "b", "a", "", "  ", "c", "d"])
    cfg.normalize()
    assert cfg.recent_inputs == ["a", "b", "c"]


def test_note_inputs_prepends_and_dedupes():
    """新输入排在最前，重复路径上移而不是重复出现。"""
    cfg = AppConfig(recent_inputs=["old1", "old2"])
    cfg.note_inputs(["new.zip", "old1"])
    assert cfg.recent_inputs == ["new.zip", "old1", "old2"]


def test_note_inputs_respects_remember_switch():
    """关掉 remember_inputs 后不再记录。"""
    cfg = AppConfig(remember_inputs=False, recent_inputs=["keep"])
    cfg.note_inputs(["should_not_appear"])
    assert cfg.recent_inputs == ["keep"]


def test_delete_intermediate_kept_independent():
    """保留中间层与删除原件是两个独立开关，normalize 不得偷偷覆盖用户意图。

    （早先版本在 keep_original=False 时强改 delete_intermediate=True，会与
    用户的显式选择打架；改为各自独立，误删风险由 UI 的二次确认承担。）
    """
    cfg = AppConfig(keep_original=False, delete_intermediate=False).normalize()
    assert cfg.delete_intermediate is False
    assert cfg.keep_original is False


def test_resolve_workdir(tmp_path):
    """空 workdir 落到项目 .workspace；指定值则展开为绝对路径。"""
    root = tmp_path / "proj"
    assert AppConfig().resolve_workdir(root) == (root / ".workspace").resolve()
    custom = tmp_path / "out"
    assert AppConfig(workdir=str(custom)).resolve_workdir(root) == custom.resolve()


def test_as_overrides_maps_units(tmp_path):
    """as_overrides 把 GB 换算成字节，并透传解压开关。"""
    cfg = AppConfig(max_depth=4, max_total_gb=2, max_ratio=88.0,
                    max_password_attempts=7, sniff_archives=False,
                    delete_intermediate=True, keep_original=True)
    ov = cfg.as_overrides(tmp_path)
    assert ov["max_depth"] == 4
    assert ov["max_total_uncompressed"] == 2 * (1024 ** 3)
    assert ov["max_compression_ratio"] == 88.0
    assert ov["max_password_attempts"] == 7
    assert ov["sniff_archives"] is False
    assert ov["workdir"] == (tmp_path / ".workspace").resolve()


def test_as_overrides_workdir_override(tmp_path):
    """显式覆盖工作目录（CLI --workdir 用）。"""
    target = tmp_path / "explicit"
    ov = AppConfig().as_overrides(tmp_path, workdir_override=target)
    assert ov["workdir"] == target


def test_save_is_atomic_no_tmp_left(tmp_path):
    """原子写入：目录里只应留下最终文件，不留临时文件。"""
    path = tmp_path / "cfg.json"
    AppConfig(max_depth=6).save(path)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["cfg.json"]

    # 覆盖写一次同样不留残渣
    AppConfig(max_depth=7).save(path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cfg.json"]
    assert AppConfig.load(path).max_depth == 7


def test_save_creates_parent_dirs(tmp_path):
    """目标父目录不存在时自动创建。"""
    path = tmp_path / "deep" / "nested" / "cfg.json"
    AppConfig().save(path)
    assert path.is_file()


def test_save_survives_concurrent_writers(tmp_path):
    """并发保存不应抛异常、不留 .tmp、文件始终是合法 JSON。

    Windows 的 os.replace 非幂等抗并发（目标被短暂占用会抛 WinError 5），
    故 save() 内加了短重试。这条测试守住那个重试逻辑。
    """
    import json
    import threading

    path = tmp_path / "cfg.json"
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(30):
                AppConfig(max_depth=(n + i) % 10 + 1).save(path)
        except BaseException as exc:  # noqa: BLE001 - 就是要抓全部
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发保存抛出异常：{errors[:3]}"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cfg.json"]
    json.loads(path.read_text(encoding="utf-8"))  # 必须是完整合法 JSON


def test_ensure_writes_default_on_first_run(tmp_path):
    """首次运行把默认配置落盘，让用户有文件可改。"""
    path = tmp_path / "cfg.json"
    cfg = AppConfig.ensure(path)
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["max_depth"] == AppConfig().max_depth
    assert cfg.max_depth == AppConfig().max_depth


def test_ensure_does_not_overwrite_existing(tmp_path):
    """已有配置不能被 ensure 覆盖回去。"""
    path = tmp_path / "cfg.json"
    AppConfig(max_depth=9).save(path)
    cfg = AppConfig.ensure(path)
    assert cfg.max_depth == 9


def test_with_changes_returns_new_instance():
    """with_changes 不改原对象，且结果已 normalize。"""
    base = AppConfig(max_depth=5)
    changed = base.with_changes(max_depth=99, output_mode=OUTPUT_WORKDIR)
    assert base.max_depth == 5
    assert changed.max_depth == 10          # 越界被收敛
    assert changed.output_mode == OUTPUT_WORKDIR


def test_enum_tuples_cover_labels():
    """枚举常量与全部合法值一致（防止漏配导致 normalize 反复改写）。"""
    assert set(OUTPUT_MODES) == {OUTPUT_WORKDIR, OUTPUT_SAMEDIR}
    assert set(AUTORUN_MODES) == {AUTORUN_OFF, AUTORUN_DIRECT, AUTORUN_CONFIRM}
    assert set(OPEN_ITEMS) == {"none", "workdir", "paths"}
