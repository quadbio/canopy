from __future__ import annotations

from pathlib import Path

from dbx.config import load_config


def test_relative_paths_resolve_against_config_file(tmp_path: Path) -> None:
    (tmp_path / "conf").mkdir()
    f = tmp_path / "conf" / "config.toml"
    f.write_text('cache_dir = "c"\n[[data_roots]]\nname = "a"\npath = "../data"\n')
    cfg = load_config(f)
    assert cfg.cache_dir == tmp_path / "conf" / "c"
    assert cfg.data_roots[0].path == (tmp_path / "data").resolve()


def test_defaults_without_file() -> None:
    cfg = load_config(None)
    assert cfg.server.port == 8765
    assert cfg.data_roots == []
