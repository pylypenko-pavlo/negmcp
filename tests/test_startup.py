import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from negmcp import _negpy, refs, startup
from negmcp.config import get_config, save_config_value
from negmcp.look import init_look

PIN_TEXT = "# pin comment\nNEGPY_COMMIT={commit}\nNEGPY_VERSION={version}\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def fake_negpy(tmp_path):
    """A git repo with release tags 0.1.0 / 0.2.0 and a pre-release 0.3.0-rc1; pin on 0.1.0."""
    repo = tmp_path / "NegPy"
    (repo / "negpy").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    for tag in ("0.1.0", "0.2.0", "0.3.0-rc1"):
        (repo / "VERSION").write_text(tag + "\n")
        (repo / "pyproject.toml").write_text('[project]\ndependencies = ["numpy==2.4.4"]\n')
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", tag)
        _git(repo, "tag", tag)
    _git(repo, "checkout", "-q", "0.1.0")
    pin = tmp_path / "NEGPY_PIN"
    pin.write_text(PIN_TEXT.format(commit=_git(repo, "rev-parse", "--short=7", "HEAD"), version="0.1.0"))
    return repo, pin


def test_latest_release_tag_ignores_prereleases(fake_negpy):
    repo, _ = fake_negpy
    assert startup.latest_release_tag(repo) == ("0.2.0", (0, 2, 0))


def test_update_moves_pin_when_smoke_passes(fake_negpy):
    repo, pin = fake_negpy
    res = startup.update_negpy(repo, pin, None, smoke=lambda arw: (True, "render ok"))
    assert res.ok and (res.old, res.new) == ("0.1.0", "0.2.0")
    assert _negpy.read_pin(pin) == _negpy.Pin(commit=_git(repo, "rev-parse", "--short=7", "HEAD"), version="0.2.0")
    assert (repo / "VERSION").read_text().strip() == "0.2.0"
    assert pin.read_text().startswith("# pin comment")
    assert "re-checked by the corridor" in res.line


def test_update_rolls_back_when_smoke_fails(fake_negpy):
    repo, pin = fake_negpy
    before_pin, before_head = pin.read_text(), _git(repo, "rev-parse", "HEAD")
    res = startup.update_negpy(repo, pin, None, smoke=lambda arw: (False, "ImportError: boom"))
    assert not res.ok and "FAILED smoke" in res.line and "boom" in res.line
    assert pin.read_text() == before_pin
    assert _git(repo, "rev-parse", "HEAD") == before_head


def test_no_update_keeps_pin_and_only_smokes(fake_negpy):
    repo, pin = fake_negpy
    before = pin.read_text()
    calls = []
    res = startup.update_negpy(repo, pin, None, fetch=False, smoke=lambda arw: (calls.append(arw), (True, "ok"))[1])
    assert res.ok and res.new == "0.1.0" and calls == [None]
    assert pin.read_text() == before and "--no-update" in res.line


def test_missing_module_is_installed_from_upstream_pin_and_retried(fake_negpy, monkeypatch):
    repo, _ = fake_negpy
    installed = []
    monkeypatch.setattr(startup, "_install_missing", lambda module, src: installed.append(module) or "numpy==2.4.4")
    answers = iter([(False, "ModuleNotFoundError: No module named 'numpy'"), (True, "render ok")])
    ok, msg, deps = startup.smoke_with_deps(None, repo, lambda arw: next(answers))
    assert ok and installed == ["numpy"] and deps == ["numpy==2.4.4"]


def _refs(tmp_path, n=3):
    refs = tmp_path / "refs"
    refs.mkdir(exist_ok=True)
    for i in range(n):
        Image.new("RGB", (32, 24), (40 + 60 * i, 120, 200 - 50 * i)).save(refs / f"r{i}.jpg")
    return refs


def test_refs_change_triggers_recalibration(tmp_path, monkeypatch):
    monkeypatch.setenv("NEGMCP_REFS_DIR", str(_refs(tmp_path)))
    get_config.cache_clear()
    assert refs.ensure().corridor_why == "missing"
    assert refs.ensure().corridor_why is None
    Image.new("RGB", (32, 24), (10, 10, 10)).save(tmp_path / "refs" / "new.jpg")
    st = refs.ensure()
    assert st.corridor_why == "refs changed" and st.corridor["n_refs"] == 4
    assert refs.ensure(force=True).corridor_why == "forced"


def test_start_no_update_on_temp_config(tmp_path, monkeypatch):
    """Real vendor checkout + pin (read-only on --no-update), everything else temporary."""
    for key, sub in (("NEG_ROOT", "neg"), ("REVIEW_ROOT", "review")):
        (tmp_path / sub).mkdir(exist_ok=True)
        monkeypatch.setenv(f"NEGMCP_{key}", str(tmp_path / sub))
    adv = tmp_path / "rolls"
    (adv / "closed").mkdir(parents=True)
    init_look(tmp_path / "look", adv)
    (adv / "good_recipe.json").write_text(
        json.dumps({"base": {"output_working_space": False}, "_graded_negpy_version": _negpy.read_pin().version})
    )
    (adv / "bad_recipe.json").write_text(json.dumps({"base": {"wb_yelow": 1}}))
    (adv / "closed" / "old_recipe.json").write_text("not json")
    save_config_value("refs_dir", str(_refs(tmp_path)))
    get_config.cache_clear()
    monkeypatch.setattr(startup.subprocess, "run", _fake_mcp_import(startup.subprocess.run))

    rep = startup.run_start(get_config(), update=False, smoke=lambda arw: (True, "render ok"))
    text = "\n".join(rep.lines)
    assert not rep.failed, text
    assert "refs_dir" in text and "[file]" in text and "[env]" in text
    assert "update skipped (--no-update)" in text
    assert "3 images" in text and "corridor re-derived (missing)" in text
    assert "house_look re-derived (not derived yet)" in text  # the init template has only taste
    good = next(line for line in rep.lines if line.strip().startswith("good"))
    assert f"valid, graded on {_negpy.read_pin().version}" in good and "⚠" not in good
    assert "bad                  WARN invalid" in text
    assert "old" not in text.split("recipes")[1].split("mcp")[0].replace("closed/", "")
    assert json.loads((tmp_path / "cache" / "state.json").read_text())["negpy_version"] == _negpy.read_pin().version
    assert len(rep.lines) <= 15


def test_start_reports_missing_paths(tmp_path, fake_negpy, monkeypatch):
    repo, pin = fake_negpy
    monkeypatch.setenv("NEGMCP_NEGPY_SRC", str(repo))
    monkeypatch.setenv("NEGMCP_REFS_DIR", str(tmp_path / "nope"))
    get_config.cache_clear()
    monkeypatch.setattr(startup.subprocess, "run", _fake_mcp_import(startup.subprocess.run))
    rep = startup.run_start(get_config(), update=False, pin_file=pin, smoke=lambda arw: (True, "ok"))
    lines = {line.split()[0]: line for line in rep.lines if line.startswith("  ") and line.split()}
    assert rep.failed
    assert "<-- MISSING" in lines["neg_root"] and "<-- MISSING" in lines["look_dir"]
    assert "not found" in lines["refs_dir"] and "MISSING" not in lines["refs_dir"]  # optional
    assert any("negmcp init" in line for line in rep.lines)


def _fake_mcp_import(real_run):
    def run(args, *a, **kw):
        if args[:2] == [startup.sys.executable, "-c"] and args[2] == "import negmcp.server":
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_run(args, *a, **kw)

    return run
