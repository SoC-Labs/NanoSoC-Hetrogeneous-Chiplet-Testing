# =============================================================================
# L0 — config loading, the TOML subset parser, and the example file.
#
# The example file is parsed as part of the suite: it is the file every operator
# copies, and a typo in it lands as a wrong address on a real board.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
import os

import pytest

from hetsoc import _toml
from hetsoc.config import Config, config_search_path, load
from hetsoc.safety import ConfigError

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
EXAMPLE = os.path.join(REPO_ROOT, "hetsoc.toml.example")

MINIMAL = """
[pair.default]
a = "eth"
b = "eth_b"

[board.eth]
host = "ubuntu@10.22.24.159"
fpgahub = "kr260_01"
target = "kr260-eth-chiplet"
role = "die_a"

[board.eth_b]
host = "ubuntu@10.22.24.153"
fpgahub = "kr260_02"
target = "kr260-eth-chiplet"
role = "die_b"
"""


def _write(tmp_path, text, name="hetsoc.toml"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


# =============================================================================
class TestTomlSubsetParser:
    """The fallback parser used when neither tomllib nor tomli is available."""

    def test_backend_is_reported(self):
        assert _toml.backend() in ("tomllib", "tomli", "builtin-subset")

    def test_sections_keys_and_types(self):
        parsed = _toml._loads_subset(
            '[a.b]\nname = "x"\nnum = 42\nhexnum = 0x2E030000\n'
            'flag = true\nlist = ["p", "q"]\n')
        assert parsed == {"a": {"b": {"name": "x", "num": 42,
                                      "hexnum": 0x2E030000, "flag": True,
                                      "list": ["p", "q"]}}}

    def test_hex_with_underscores(self):
        parsed = _toml._loads_subset("[t]\nwindow_base = 0x4_0000_0000\n")
        assert parsed["t"]["window_base"] == 0x4_0000_0000

    def test_comments_are_stripped_but_not_inside_strings(self):
        parsed = _toml._loads_subset(
            '[t]\n# whole line\nk = "a#b"   # trailing\n')
        assert parsed["t"]["k"] == "a#b"

    def test_dashed_table_names(self):
        parsed = _toml._loads_subset("[target.kr260-compute-chiplet]\nx = 1\n")
        assert parsed["target"]["kr260-compute-chiplet"]["x"] == 1

    def test_unsupported_constructs_raise_rather_than_guess(self):
        # A mis-parsed window_base is a wedged board; guessing is not an option.
        for bad in ("[t]\nk = {a = 1}\n", "[t]\nk = [\n  1,\n]\n",
                    "[t]\nnot a pair\n"):
            with pytest.raises(ValueError):
                _toml._loads_subset(bad)

    def test_the_example_file_parses_with_the_fallback_parser(self):
        # Even on a Python with no tomllib and no tomli.
        with open(EXAMPLE) as handle:
            parsed = _toml._loads_subset(handle.read())
        assert "pair" in parsed and "board" in parsed


# =============================================================================
class TestExampleFile:
    def test_it_exists_and_loads(self, tmp_path):
        assert os.path.isfile(EXAMPLE)
        cfg = Config(_toml.load(EXAMPLE), EXAMPLE, register_targets=False)
        assert "eth" in cfg.boards and "compute" in cfg.boards
        assert "default" in cfg.pairs

    def test_the_default_pair_is_the_heterogeneous_one(self):
        cfg = Config(_toml.load(EXAMPLE), EXAMPLE, register_targets=False)
        assert cfg.pairs["default"]["a"] == "eth"
        assert cfg.pairs["default"]["b"] == "compute"

    def test_the_two_dies_carry_different_roles(self):
        cfg = Config(_toml.load(EXAMPLE), EXAMPLE, register_targets=False)
        pair = cfg.pairs["default"]
        assert cfg.boards[pair["a"]]["role"] != cfg.boards[pair["b"]]["role"]

    def test_the_compute_target_override_is_commented_out(self):
        # Uncommenting it with a guessed window base would wedge a board; the
        # example must ship it inert.
        with open(EXAMPLE) as handle:
            text = handle.read()
        assert "# [target.kr260-compute-chiplet]" in text
        cfg = Config(_toml.load(EXAMPLE), EXAMPLE, register_targets=False)
        assert cfg.targets == {}


# =============================================================================
class TestLoad:
    def test_search_order(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HETSOC_CONFIG", "/explicit/path.toml")
        paths = config_search_path()
        assert paths[0] == "/explicit/path.toml"
        assert paths[1].endswith("hetsoc.toml")
        assert paths[2].endswith(os.path.join(".config", "hetsoc.toml"))

    def test_env_override_wins(self, monkeypatch, tmp_path):
        path = _write(tmp_path, MINIMAL, "elsewhere.toml")
        monkeypatch.setenv("HETSOC_CONFIG", path)
        cfg = load(register_targets=False)
        assert cfg.path == path

    def test_missing_config_is_actionable(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HETSOC_CONFIG", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        with pytest.raises(ConfigError) as info:
            load(register_targets=False)
        assert "hetsoc.toml.example" in str(info.value)

    def test_explicit_path_wins(self, tmp_path):
        path = _write(tmp_path, MINIMAL)
        assert load(path, register_targets=False).path == path


# =============================================================================
class TestValidation:
    def test_a_board_missing_its_role_is_refused(self, tmp_path):
        text = MINIMAL.replace('role = "die_a"\n', "")
        with pytest.raises(ConfigError) as info:
            load(_write(tmp_path, text), register_targets=False)
        assert "ribbon lane" in str(info.value)

    def test_an_unknown_target_name_is_refused(self, tmp_path):
        text = MINIMAL.replace("kr260-eth-chiplet", "kr260-guess", 1)
        with pytest.raises(ConfigError):
            load(_write(tmp_path, text), register_targets=False)

    def test_a_pair_naming_an_unknown_board_is_refused(self, tmp_path):
        text = MINIMAL.replace('b = "eth_b"', 'b = "typo"', 1)
        with pytest.raises(ConfigError) as info:
            load(_write(tmp_path, text), register_targets=False)
        assert "typo" in str(info.value)

    def test_unknown_board_keys_are_refused(self, tmp_path):
        text = MINIMAL + '\n[board.x]\nhost = "h"\ntarget = "kr260-eth-chiplet"\n' \
                         'role = "die_a"\nwindowbase = 1\n'
        with pytest.raises(ConfigError) as info:
            load(_write(tmp_path, text), register_targets=False)
        assert "unknown key" in str(info.value)

    def test_unknown_top_level_sections_are_refused(self, tmp_path):
        with pytest.raises(ConfigError):
            load(_write(tmp_path, MINIMAL + "\n[bench]\nx = 1\n"),
                 register_targets=False)


# =============================================================================
class TestFactories:
    def test_board_is_built_from_the_table(self, tmp_path):
        cfg = load(_write(tmp_path, MINIMAL), register_targets=False)
        board = cfg.board("eth")
        assert board.host == "ubuntu@10.22.24.159"
        assert board.role == "die_a"
        assert board.fpgahub_name == "kr260_01"
        assert board.target.name == "kr260-eth-chiplet"

    def test_pair_is_built_from_the_table(self, tmp_path):
        cfg = load(_write(tmp_path, MINIMAL), register_targets=False)
        pair = cfg.pair("default")
        assert pair.a.role == "die_a" and pair.b.role == "die_b"
        assert pair.heterogeneous is False

    def test_defaults_are_applied(self, tmp_path):
        text = MINIMAL + '\n[defaults]\ntimeout_s = 7.5\n'
        cfg = load(_write(tmp_path, text), register_targets=False)
        assert cfg.board("eth").timeout_s == 7.5

    def test_an_unknown_board_name_is_actionable(self, tmp_path):
        cfg = load(_write(tmp_path, MINIMAL), register_targets=False)
        with pytest.raises(ConfigError) as info:
            cfg.board("nope")
        assert "Known boards" in str(info.value)


# =============================================================================
class TestTargetOverrideSection:
    def test_an_override_reaches_the_registry_when_asked(self, tmp_path):
        text = MINIMAL + (
            '\n[target.kr260-eth-chiplet-v2]\n'
            'inherit = "kr260-eth-chiplet"\n'
            'window_base = 0x500000000\n'
            'source = "v2 tidelink.hwh:4112"\n')
        cfg = load(_write(tmp_path, text), register_targets=True)
        from hetsoc.targets import get_target
        assert get_target("kr260-eth-chiplet-v2").window_base == 0x5_0000_0000
        assert cfg.targets["kr260-eth-chiplet-v2"].peer_aperture == 0x2F

    def test_register_targets_false_leaves_the_registry_alone(self, tmp_path):
        text = MINIMAL + (
            '\n[target.kr260-scratch]\n'
            'inherit = "kr260-eth-chiplet"\n')
        load(_write(tmp_path, text), register_targets=False)
        from hetsoc.targets import TARGETS
        assert "kr260-scratch" not in TARGETS
