"""Repository checks: package layout, READMEs, doc links, reference data, fingerprint paths."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def tracked(pattern):
    """Tracked files matching a git pathspec, or every matching file when git is unavailable."""
    try:
        out = subprocess.run(["git", "ls-files", pattern], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.split()
        return [ROOT / f for f in out]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob(pattern.lstrip("*")) if ".venv" not in p.parts]


class TestPortablePackage:
    def test_package_is_at_the_repo_root(self):
        assert (ROOT / "wod_etl" / "__init__.py").is_file()

    def test_package_imports_nothing_runtime_specific(self):
        """No vendor SDK or API client; this stays a plain library."""
        banned = re.compile(r"^\s*(?:import|from)\s+(anthropic|claude|openai|google\.gener)",
                            re.M)
        for py in (ROOT / "wod_etl").rglob("*.py"):
            assert not banned.search(py.read_text()), f"{py.name} imports a runtime SDK"

    def test_packaging_points_at_the_root(self):
        cfg = (ROOT / "pyproject.toml").read_text()
        assert 'include = ["wod_etl*"]' in cfg


class TestEveryPackageDocumentsItself:
    """Every package has a README."""

    PACKAGES = sorted(p.name for p in (ROOT / "wod_etl").iterdir()
                      if p.is_dir() and (p / "__init__.py").exists())

    @pytest.mark.parametrize("package", PACKAGES)
    def test_package_has_a_readme(self, package):
        readme = ROOT / "wod_etl" / package / "README.md"
        assert readme.is_file(), f"wod_etl/{package}/ has no README.md"
        assert len(readme.read_text().splitlines()) > 10


class TestDocLinksResolve:
    def test_every_relative_link_resolves(self):
        broken = []
        for md in tracked("*.md"):
            if ".claude" in md.relative_to(ROOT).parts:
                continue
            for target in LINK.findall(md.read_text()):
                if re.match(r"^(https?:|mailto:|#)", target):
                    continue
                if not (md.parent / target.split("#")[0]).exists():
                    broken.append(f"{md.relative_to(ROOT)} -> {target}")
        assert not broken, "broken links:\n" + "\n".join(broken)


class TestReferenceDataIsPresent:
    """Other tests skip without these files, so their absence must fail here."""

    def test_the_manual_is_present(self):
        assert (ROOT / "knowledge" / "wod_readme.pdf").is_file()

    def test_the_code_tables_are_present(self):
        tables = sorted((ROOT / "knowledge" / "wod_code_tables").glob("*.json"))
        assert len(tables) >= 12, [t.name for t in tables]

    def test_every_fingerprinted_code_path_exists(self):
        """`_code_hash` silently skips missing paths, so a renamed module would go unhashed."""
        from wod_etl.orchestration.pipeline import OUTPUT_CODE, TRIAGE_CODE

        missing = [p for p in set(TRIAGE_CODE + OUTPUT_CODE) if not (ROOT / p).exists()]
        assert not missing, missing


class TestNoAgentLayer:
    """The removed agent layer must not return. Patterns are split so this file does not match."""

    GONE = ["." + "claude/skills", "AGENTS" + ".md", "CLAUDE" + ".md", "GEMINI" + ".md"]
    TEXT = {".py", ".md", ".toml", ".txt", ".json", ".js", ".html", ".css", ".csv", ".lock",
            ".cfg", ".ini", ".yaml", ".yml", ""}
    BANNED = re.compile("|".join([
        re.escape("." + "claude/"), re.escape("SKILL" + ".md"), "AGENTS" + r"\.md",
        "CLAUDE" + r"\.md", "GEMINI" + r"\.md", "wod-" + "reference", "search" + "_manual",
        "query_wod" + "_knowledge", "index" + "_manual", "wod_readme" + "_db",
        r"orchestration[./]" + "web", r"orchestration[./]" + "cli", "web" + "_ui",
        "wod-" + "agent", "WOD-ETL-" + "Agent",
        r"(?<!user-)(?<!user_)(?<!user )\b" + "agent" + r"s?\b",
    ]), re.I)

    def test_the_agent_files_are_gone(self):
        assert [p for p in self.GONE if (ROOT / p).exists()] == []

    def test_nothing_refers_to_them(self):
        hits = []
        for path in tracked("*"):
            if path.suffix not in self.TEXT or path.name == Path(__file__).name \
                    or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if self.BANNED.search(line):
                    hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:100]}")
        assert not hits, "agent-layer references:\n" + "\n".join(hits)
