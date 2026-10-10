from __future__ import annotations

from pathlib import Path

import pytest

from dbx.config import ConfigError, load_config


def test_relative_paths_resolve_against_config_file(tmp_path: Path) -> None:
    (tmp_path / "conf").mkdir()
    (tmp_path / "data").mkdir()
    f = tmp_path / "conf" / "config.toml"
    f.write_text('cache_dir = "c"\n[[data_roots]]\nname = "a"\npath = "../data"\n')
    cfg = load_config(f)
    assert cfg.cache_dir == tmp_path / "conf" / "c"
    assert cfg.data_roots[0].path == (tmp_path / "data").resolve()


def test_defaults_without_file() -> None:
    cfg = load_config(None)
    assert cfg.server.port == 8765
    assert cfg.data_roots == []


def _write(tmp_path: Path, text: str) -> Path:
    (tmp_path / "data").mkdir(exist_ok=True)
    f = tmp_path / "config.toml"
    f.write_text(text)
    return f


ROOT = '[[data_roots]]\nname = "a"\npath = "data"\n'


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[memmory]\nbudget_gb = 800\n", "memmory"),
        ("[logging]\nprivacy_level = 'strict'\n", "privacy_level"),
        ("[logging]\nprivacy = 'Strict'\n", "privacy"),
        ("[memory]\nbudget_gb = 8\ncache_gb = 20\n", "exceeds budget_gb"),
        ('[[data_roots]]\nname = "x"\npath = "nowhere"\n', "not an existing folder"),
        (ROOT + 'cache_dir = "data/cache"\n', "inside data root"),
        (ROOT + ROOT, "duplicate data root names"),
    ],
)
def test_invalid_configs_are_rejected(tmp_path: Path, text: str, message: str) -> None:
    # cache_dir must be a top-level key, so it goes first when combined with a table.
    if text.startswith(ROOT) and "cache_dir" in text:
        text = 'cache_dir = "data/cache"\n' + ROOT
    with pytest.raises(ConfigError, match=message):
        load_config(_write(tmp_path, text))


def test_example_config_is_valid() -> None:
    example = Path(__file__).resolve().parents[2] / "config.example.toml"
    text = example.read_text()
    import tomllib

    from dbx.config import Config

    Config.model_validate(tomllib.loads(text))  # schema only; roots differ per machine
