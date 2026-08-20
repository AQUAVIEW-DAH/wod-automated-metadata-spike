"""The project is usable from any shell-capable agent runtime, not just one.

That rests on two structural promises, and both rot quietly:

  1. The instructions live in exactly ONE place. Each runtime gets a thin pointer, never a
     copy -- four copies of a procedure drift, and a drifted copy is worse than no copy.
  2. The code is a normal root-level Python package, owned by no runtime's directory
     convention.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "docs" / "AGENT.md"

# Every file a runtime might look for. All must point at CANONICAL rather than restate it.
POINTERS = [
    ROOT / "CLAUDE.md",                                  # Claude Code project memory
    ROOT / "GEMINI.md",                                  # Gemini CLI
    ROOT / "AGENTS.md",                                  # Codex CLI, Cursor, Jules
    ROOT / ".claude/skills/wod-etl/SKILL.md",            # Claude Code skill entry
]

# A pointer that grows past this is turning into a second copy of the instructions.
MAX_POINTER_LINES = 40


class TestCanonicalInstructions:
    def test_canonical_doc_exists_and_is_substantial(self):
        assert CANONICAL.is_file(), "docs/AGENT.md is the canonical instruction file"
        assert len(CANONICAL.read_text().splitlines()) > 100

    @pytest.mark.parametrize("pointer", POINTERS, ids=lambda p: p.name)
    def test_pointer_exists(self, pointer):
        assert pointer.is_file(), f"{pointer.relative_to(ROOT)} is missing"

    @pytest.mark.parametrize("pointer", POINTERS, ids=lambda p: p.name)
    def test_pointer_references_the_canonical_doc(self, pointer):
        assert "AGENT.md" in pointer.read_text(), (
            f"{pointer.relative_to(ROOT)} must point at docs/AGENT.md, not restate it")

    @pytest.mark.parametrize("pointer", POINTERS, ids=lambda p: p.name)
    def test_pointer_stays_thin(self, pointer):
        n = len(pointer.read_text().splitlines())
        assert n <= MAX_POINTER_LINES, (
            f"{pointer.relative_to(ROOT)} is {n} lines: it is drifting into a second copy "
            f"of the instructions. Put the substance in docs/AGENT.md.")

    def test_skill_keeps_its_frontmatter(self):
        """Without name/description Claude Code stops discovering the skill."""
        text = (ROOT / ".claude/skills/wod-etl/SKILL.md").read_text()
        assert text.startswith("---\n")
        head = text.split("---\n", 2)[1]
        assert re.search(r"^name:\s*\S+", head, re.M)
        assert re.search(r"^description:\s*\S+", head, re.M)


class TestPortablePackage:
    def test_package_is_at_the_repo_root(self):
        assert (ROOT / "wod_etl" / "__init__.py").is_file()
        assert not (ROOT / ".claude/skills/wod-etl/wod_etl").exists(), (
            "the package must not live inside a runtime-specific directory")

    def test_package_imports_nothing_runtime_specific(self):
        """No agent SDK, no vendor client -- this must stay a plain library."""
        banned = re.compile(r"^\s*(?:import|from)\s+(anthropic|claude|openai|google\.gener)",
                            re.M)
        for py in (ROOT / "wod_etl").rglob("*.py"):
            assert not banned.search(py.read_text()), f"{py.name} imports a runtime SDK"

    def test_packaging_points_at_the_root(self):
        cfg = (ROOT / "pyproject.toml").read_text()
        assert ".claude" not in cfg, "pyproject must not reference a runtime directory"
        assert 'include = ["wod_etl*"]' in cfg


class TestEveryPackageDocumentsItself:
    """Each phase package carries its own README, and docs/AGENT.md routes to it.

    That per-package doc is how an agent works inside a phase without reading the source,
    so a package without one is invisible. Adding a package and forgetting its README is
    the easy mistake -- this catches it.
    """

    PACKAGES = sorted(p.name for p in (ROOT / "wod_etl").iterdir()
                      if p.is_dir() and (p / "__init__.py").exists())

    @pytest.mark.parametrize("package", PACKAGES)
    def test_package_has_a_readme(self, package):
        readme = ROOT / "wod_etl" / package / "README.md"
        assert readme.is_file(), f"wod_etl/{package}/ has no README.md"
        assert len(readme.read_text().splitlines()) > 10

    @pytest.mark.parametrize("package", PACKAGES)
    def test_canonical_doc_routes_to_it(self, package):
        assert f"wod_etl/{package}/README.md" in CANONICAL.read_text(), (
            f"docs/AGENT.md does not route to wod_etl/{package}/README.md")
