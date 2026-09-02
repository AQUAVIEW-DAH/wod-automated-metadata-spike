"""The instructions are Agent Skills, and the code is a plain Python package.

Both promises rot quietly, and neither shows up in a normal test run:

  1. Each SKILL.md *is* the instructions, not a pointer to them. Anthropic's Agent Skills
     format loads a skill's body when the skill triggers; a body that only says "read that
     other file" spends the trigger and delivers nothing. References stay one level deep
     for the same reason -- Claude may preview a file reached through another file with
     `head` instead of reading it whole, and silently act on half of it.
  2. The code is a normal root-level Python package, owned by no runtime's directory
     convention.

Frontmatter limits below come from the Agent Skills spec, not from this project:
https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview#skill-structure
"""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / ".claude" / "skills"
CANONICAL = SKILLS / "wod-etl" / "SKILL.md"

SKILL_NAMES = ["wod-etl", "wod-reference"]

# Spec limits.
MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 1024
MAX_BODY_LINES = 500        # "Keep SKILL.md body under 500 lines for optimal performance"
MIN_BODY_LINES = 100        # below this it has decayed back into a pointer

# The budgets that actually matter, from the progressive-disclosure model:
#   Level 1 (name + description, and Claude Code appends when_to_use) is in the system
#   prompt for EVERY conversation whether the skill fires or not -- the spec budgets it at
#   about 100 tokens per skill.
#   Level 2 (the SKILL.md body) loads when the skill triggers -- "under 5k tokens".
# Line count is only a proxy for the second: a 453-line body measured 6.4k tokens and passed
# MAX_BODY_LINES while being a third over the real budget. Detail belongs in a Level 3 file.
CHARS_PER_TOKEN = 4         # conservative for English prose; tables and code run denser
MAX_BODY_TOKENS = 5000
MAX_METADATA_TOKENS = 260   # ~100 is the guidance; this is the ceiling before it is bloat


def est_tokens(text):
    return len(text) / CHARS_PER_TOKEN

# Files a runtime that does not read .claude/skills/ looks for. These stay thin.
POINTERS = [
    ROOT / "CLAUDE.md",     # Claude Code project memory
    ROOT / "GEMINI.md",     # Gemini CLI
    ROOT / "AGENTS.md",     # Codex CLI, Cursor, Jules
]
MAX_POINTER_LINES = 40

# Indexes that were folded into the skills. Reintroducing either rebuilds the extra hop.
RETIRED = ["docs/AGENT.md", "knowledge/README.md"]

LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def skill_file(name):
    return SKILLS / name / "SKILL.md"


def split_frontmatter(path):
    """Return (frontmatter, body) for a SKILL.md, or fail loudly if it has none."""
    text = path.read_text()
    assert text.startswith("---\n"), f"{path.relative_to(ROOT)} has no YAML frontmatter"
    parts = text.split("---\n", 2)
    assert len(parts) == 3, f"{path.relative_to(ROOT)} frontmatter is not terminated"
    return parts[1], parts[2]


def field(frontmatter, key):
    m = re.search(rf"^{key}:\s*(.+?)(?=\n\S+:|\Z)", frontmatter, re.M | re.S)
    return m.group(1).strip() if m else None


class TestSkillsCarryTheInstructions:
    """A skill body must be the procedure itself, within the spec's size guidance."""

    @pytest.mark.parametrize("name", SKILL_NAMES)
    def test_skill_exists(self, name):
        assert skill_file(name).is_file(), f".claude/skills/{name}/SKILL.md is missing"

    @pytest.mark.parametrize("name", SKILL_NAMES)
    def test_body_is_substantial_not_a_pointer(self, name):
        _, body = split_frontmatter(skill_file(name))
        n = len(body.splitlines())
        assert n >= MIN_BODY_LINES, (
            f"{name}/SKILL.md is {n} lines: it has decayed into a pointer. The skill body "
            f"is what loads when the skill triggers -- put the substance here.")

    @pytest.mark.parametrize("name", SKILL_NAMES)
    def test_body_is_within_the_size_guidance(self, name):
        _, body = split_frontmatter(skill_file(name))
        n = len(body.splitlines())
        assert n <= MAX_BODY_LINES, (
            f"{name}/SKILL.md is {n} lines, over the {MAX_BODY_LINES}-line guidance. Split "
            f"detail into a file linked from the body, one level deep.")

    @pytest.mark.parametrize("name", SKILL_NAMES)
    def test_body_is_within_the_level_2_token_budget(self, name):
        """The real Level 2 limit. Lines are a proxy and a loose one."""
        _, body = split_frontmatter(skill_file(name))
        n = est_tokens(body)
        assert n <= MAX_BODY_TOKENS, (
            f"{name}/SKILL.md body is ~{n:.0f} tokens, over the {MAX_BODY_TOKENS} Level 2 "
            f"budget. Move reference detail -- schemas, enumerations, full field tables -- "
            f"into a Level 3 file linked from the body, and keep the decisions here.")


class TestFrontmatterMatchesTheSpec:
    @pytest.mark.parametrize("name", SKILL_NAMES)
    def test_name_is_valid_and_matches_its_directory(self, name):
        front, _ = split_frontmatter(skill_file(name))
        value = field(front, "name")
        assert value == name, f"name: {value!r} does not match directory {name!r}"
        assert len(value) <= MAX_NAME_CHARS
        assert re.fullmatch(r"[a-z0-9-]+", value), "name must be lowercase, digits, hyphens"
        # The spec reserves these; a skill named with either is rejected on upload.
        assert "anthropic" not in value and "claude" not in value

    @pytest.mark.parametrize("name", SKILL_NAMES)
    def test_description_is_present_and_within_the_cap(self, name):
        front, _ = split_frontmatter(skill_file(name))
        value = field(front, "description")
        assert value, f"{name} has no description -- Claude cannot discover it"
        assert len(value) <= MAX_DESCRIPTION_CHARS, (
            f"{name} description is {len(value)} chars, over the {MAX_DESCRIPTION_CHARS} cap")

    @pytest.mark.parametrize("name", SKILL_NAMES)
    def test_frontmatter_is_valid_yaml(self, name):
        """A plain scalar cannot contain ': ' or start with a quote.

        Both mistakes are silent: the frontmatter fails to parse and the skill falls back to
        whatever the runtime uses instead -- for Claude Code, the body's H1 -- so the skill
        keeps its name and loses the description that makes it discoverable at all.
        """
        front, _ = split_frontmatter(skill_file(name))
        try:
            parsed = yaml.safe_load(front)
        except yaml.YAMLError as e:
            pytest.fail(f"{name}/SKILL.md frontmatter is not valid YAML: {e}")
        assert isinstance(parsed, dict), f"{name} frontmatter is not a mapping"
        assert parsed.get("name") == name
        assert parsed.get("description"), f"{name} description did not survive parsing"

    @pytest.mark.parametrize("name", SKILL_NAMES)
    def test_metadata_stays_within_its_always_loaded_budget(self, name):
        """Level 1 sits in the system prompt whether the skill fires or not."""
        front, _ = split_frontmatter(skill_file(name))
        parsed = yaml.safe_load(front)
        loaded = f"{parsed['description']} {parsed.get('when_to_use', '')}"
        n = est_tokens(loaded)
        assert n <= MAX_METADATA_TOKENS, (
            f"{name} description + when_to_use is ~{n:.0f} tokens, over {MAX_METADATA_TOKENS}. "
            f"They are concatenated into the system prompt for every conversation, so they "
            f"must be complementary: description says what and when, when_to_use carries the "
            f"trigger phrases. Neither should restate the other.")

    @pytest.mark.parametrize("name", SKILL_NAMES)
    def test_no_xml_tags_in_metadata(self, name):
        """The spec forbids them: metadata is injected into the system prompt."""
        front, _ = split_frontmatter(skill_file(name))
        for key in ("name", "description"):
            assert not re.search(r"<[^>]+>", field(front, key) or "")


class TestProgressiveDisclosure:
    """Level 3 files must be reachable from the body, and reachable in one hop."""

    @pytest.mark.parametrize("name", SKILL_NAMES)
    def test_every_bundled_file_is_linked_from_the_body(self, name):
        """A bundled file the body never mentions is dead weight: nothing will open it."""
        skill_dir = SKILLS / name
        body = skill_file(name).read_text()
        linked = {(skill_dir / t.split("#")[0]).resolve() for t in LINK.findall(body)
                  if not t.startswith(("http://", "https://", "#", "mailto:"))}
        orphans = [str(p.relative_to(skill_dir)) for p in skill_dir.rglob("*.md")
                   if p.is_file() and p.name != "SKILL.md"
                   and "evals" not in p.parts and p.resolve() not in linked]
        assert not orphans, (
            f"{name} bundles {orphans} but SKILL.md links to none of them. Either link it or "
            f"delete it -- Claude only reads what the body points at.")


class TestReferencesStayOneLevelDeep:
    @pytest.mark.parametrize("name", SKILL_NAMES)
    def test_every_relative_link_resolves(self, name):
        path = skill_file(name)
        broken = []
        for target in LINK.findall(path.read_text()):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            if not (path.parent / target.split("#")[0]).resolve().exists():
                broken.append(target)
        assert not broken, f"{name}/SKILL.md links to missing files: {broken}"

    def test_no_retired_index_is_referenced_anywhere(self):
        """docs/AGENT.md and knowledge/README.md were the middle hop. Keep them gone.

        This file names both on purpose, so it excludes itself from the scan.
        """
        skip_dirs = {".git", ".venv", "__pycache__", ".pytest_cache", "data", "node_modules"}
        offenders = []
        for f in ROOT.rglob("*"):
            if not f.is_file() or f.suffix not in {".md", ".py", ".toml", ".json"}:
                continue
            if set(f.relative_to(ROOT).parts) & skip_dirs or f == Path(__file__):
                continue
            text = f.read_text(errors="ignore")
            for retired in RETIRED:
                if retired in text:
                    offenders.append(f"{f.relative_to(ROOT)} -> {retired}")
        assert not offenders, (
            "these files reference an index that was folded into a skill; point them at "
            f"the file that actually answers the question instead: {offenders}")


class TestPointersStayThin:
    @pytest.mark.parametrize("pointer", POINTERS, ids=lambda p: p.name)
    def test_pointer_exists(self, pointer):
        assert pointer.is_file(), f"{pointer.relative_to(ROOT)} is missing"

    @pytest.mark.parametrize("pointer", POINTERS, ids=lambda p: p.name)
    def test_pointer_references_the_skill(self, pointer):
        assert ".claude/skills/wod-etl/SKILL.md" in pointer.read_text(), (
            f"{pointer.relative_to(ROOT)} must point at the wod-etl skill, not restate it")

    @pytest.mark.parametrize("pointer", POINTERS, ids=lambda p: p.name)
    def test_pointer_stays_thin(self, pointer):
        n = len(pointer.read_text().splitlines())
        assert n <= MAX_POINTER_LINES, (
            f"{pointer.relative_to(ROOT)} is {n} lines: it is drifting into a second copy "
            f"of the instructions. Put the substance in the skill.")


class TestPortablePackage:
    def test_package_is_at_the_repo_root(self):
        assert (ROOT / "wod_etl" / "__init__.py").is_file()
        assert not (SKILLS / "wod-etl" / "wod_etl").exists(), (
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
    """Each phase package carries its own README, and the skill routes to it.

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
    def test_the_skill_routes_to_it(self, package):
        assert f"wod_etl/{package}/README.md" in CANONICAL.read_text(), (
            f"the wod-etl skill does not route to wod_etl/{package}/README.md")
