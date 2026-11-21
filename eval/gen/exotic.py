"""Change kinds absent from the public sample (the final promises some).

``cases(rng)`` yields (changes, desc, kind, op).  Mostly mechanical over many
targets, plus a few hand-written ones.  Kinds:
  newfile, delete, rename, nonpy, export, syntax, importcycle, conftest
"""

from __future__ import annotations

import re

import mutators as M
from patchmaker import GEN, read


def _py(dirname):
    return sorted(str(p.relative_to(GEN)) for p in (GEN / dirname).glob("*.py") if p.name != "__init__.py")


def cases(rng):
    dialects = _py("sqlglot/dialects")
    parsers = _py("sqlglot/parsers")
    generators = _py("sqlglot/generators")
    typing_ = _py("sqlglot/typing")
    optim = _py("sqlglot/optimizer")
    core_misc = _py("sqlglot")
    fixtures = sorted(str(p.relative_to(GEN)) for p in (GEN / "tests/fixtures").rglob("*.sql"))
    tests = sorted(str(p.relative_to(GEN)) for p in (GEN / "tests").rglob("test_*.py"))

    # ---------------- new files
    yield [("write", "sqlglot/dialects/newdb.py",
            "from sqlglot.dialects.dialect import Dialect\n\n\nclass NewDB(Dialect):\n    pass\n")], \
        "new unregistered dialect module", "newfile", "new-dialect-module"
    yield [("write", "sqlglot/utils_extra.py", "def double(x):\n    return 2 * x\n")], \
        "new unused helper module", "newfile", "new-module"
    yield [("write", "sqlglot/optimizer/new_rule.py",
            "from sqlglot import exp\n\n\ndef new_rule(expression: exp.Expr) -> exp.Expr:\n    return expression\n")], \
        "new optimizer rule module (unused)", "newfile", "new-module"
    yield [("write", "tests/test_new_feature.py",
            "import unittest\n\n\nclass TestNew(unittest.TestCase):\n    def test_new(self):\n        self.assertEqual(1, 2)\n")], \
        "new failing test file (not in catalog)", "newfile", "new-test-file"
    yield [("write", "tests/conftest.py", "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef _boom():\n    raise RuntimeError('boom')\n")], \
        "new conftest with failing autouse fixture", "conftest", "conftest-autouse-error"
    yield [("write", "tests/conftest.py", "raise ImportError('broken conftest')\n")], \
        "new conftest that cannot import", "conftest", "conftest-import-error"
    yield [("write", "tests/conftest.py", "# shared pytest configuration\n")], \
        "new harmless conftest", "conftest", "conftest-harmless"
    yield [("write", "tests/dialects/conftest.py", "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef _boom():\n    raise RuntimeError('boom')\n")], \
        "conftest in tests/dialects with failing autouse fixture", "conftest", "conftest-autouse-error"
    yield [("write", "tests/fixtures/optimizer/new_rule.sql", "SELECT 1;\nSELECT 1;\n")], \
        "new unused fixture file", "newfile", "new-fixture"
    yield [("write", "docs/notes.md", "# Notes\n\nSome notes.\n")], "new docs file", "newfile", "new-doc"
    yield [("write", "sqlglot/py.typed.bak", "")], "new empty data file", "newfile", "new-data"

    # ---------------- deletions
    for p in rng.sample(dialects, 6):
        yield [("delete", p)], f"delete {p}", "delete", "delete-dialect"
    for p in rng.sample(parsers, 3) + rng.sample(generators, 3) + rng.sample(typing_, 3):
        yield [("delete", p)], f"delete {p}", "delete", "delete-module"
    for p in rng.sample(optim, 3) + rng.sample([c for c in core_misc if not c.endswith("__init__.py")], 3):
        yield [("delete", p)], f"delete {p}", "delete", "delete-module"
    for p in rng.sample(fixtures, 4):
        yield [("delete", p)], f"delete {p}", "delete", "delete-fixture"
    for p in rng.sample(tests, 3):
        yield [("delete", p)], f"delete {p}", "delete", "delete-test-file"
    yield [("delete", "sqlglot/py.typed")], "delete py.typed", "delete", "delete-data"
    yield [("delete", "tests/fixtures/jsonpath/cts.json")], "delete jsonpath cts fixture", "delete", "delete-fixture"
    yield [("delete", "CHANGELOG.md")], "delete changelog", "delete", "delete-doc"

    # ---------------- renames
    for p in rng.sample(dialects, 3):
        new = p.replace(".py", "_dialect.py")
        yield [("rename", p, new)], f"rename {p} -> {new}", "rename", "rename-dialect"
    for p in rng.sample(optim + parsers + generators, 4):
        new = re.sub(r"/(\w+)\.py$", r"/_\1.py", p)
        yield [("rename", p, new)], f"rename {p} -> {new}", "rename", "rename-module"
    for p in rng.sample(fixtures, 3):
        new = p.replace(".sql", "_v2.sql")
        yield [("rename", p, new)], f"rename {p} -> {new}", "rename", "rename-fixture"
    for p in rng.sample(tests, 2):
        new = p.replace("test_", "test_renamed_")
        yield [("rename", p, new)], f"rename {p} -> {new}", "rename", "rename-test-file"
    # rename with a compatibility shim left behind (harmless)
    p = rng.choice(optim)
    mod = p[:-3].replace("/", ".")
    new = re.sub(r"/(\w+)\.py$", r"/_\1_impl.py", p)
    newmod = new[:-3].replace("/", ".")
    yield [("rename", p, new), ("write", p, f"from {newmod} import *  # noqa: F401,F403\n")], \
        f"move {mod} to {newmod} with star-import shim", "rename", "rename-with-shim"

    # ---------------- non-Python files
    def append(path, extra, op, desc):
        return [("write", path, read(path) + extra)], desc, "nonpy", op

    yield append("README.md", "\n## Extra section\n\nMore docs.\n", "readme", "README edit")
    yield append("CHANGELOG.md", "\n- docs: mention the new release process\n", "changelog", "CHANGELOG edit")
    yield append("Makefile", "\nhello:\n\techo hello\n", "makefile", "Makefile target")
    yield append("setup.cfg", "\n[flake8]\nmax-line-length = 100\n", "setupcfg", "setup.cfg section")
    yield append("pyproject.toml", "\n[tool.mypy]\nignore_missing_imports = true\n", "pyproject", "pyproject mypy section")
    yield [("write", "pyproject.toml", read("pyproject.toml").replace("line-length = 100", "line-length = 120"))], \
        "pyproject ruff line length", "nonpy", "pyproject"
    yield append("pyproject.toml", '\n[tool.pytest.ini_options]\naddopts = "-p no:randomly"\n', "pyproject-pytest",
                 "pyproject pytest ini (harmless addopts)")
    yield append("pyproject.toml", '\n[tool.pytest.ini_options]\npython_classes = "Check*"\n', "pyproject-pytest",
                 "pyproject pytest ini changes collection (no Test* classes collected)")
    yield append(".gitignore", "\n*.swp\n", "gitignore", ".gitignore entry")
    yield append("MANIFEST.in", "\ninclude CHANGELOG.md\n", "manifest", "MANIFEST.in entry")
    yield append("sqlglotc/setup.py", "\n# build note\n", "sqlglotc", "sqlglotc/setup.py comment")
    sc = read("sqlglotc/setup.py")
    m = re.search(r"\bTrue\b|\bFalse\b|==|!=", sc)
    if m:
        new = sc[: m.start()] + {"True": "False", "False": "True", "==": "!=", "!=": "=="}[m.group()] + sc[m.end():]
        yield [("write", "sqlglotc/setup.py", new)], "sqlglotc/setup.py logic flip", "nonpy", "sqlglotc-code"
    yield append("setup.py", "\n# packaging note\n", "setup-py", "setup.py comment")
    yield append("sqlglot/py.typed", "partial\n", "pytyped", "py.typed content")
    yield append("tests/fixtures/jsonpath/LICENSE", "\nExtra line.\n", "fixture-license", "fixture LICENSE edit")
    for post in sorted((GEN / "posts").rglob("*.md"))[:1]:
        rel = str(post.relative_to(GEN))
        yield append(rel, "\nEdit.\n", "posts", "blog post edit")

    # ---------------- __init__ export changes
    init = read("sqlglot/__init__.py")
    for name in ["    union as union,\n", "    to_table as to_table,\n", "    maybe_parse as maybe_parse,\n",
                 "    case as case,\n"]:
        if name in init:
            yield [("write", "sqlglot/__init__.py", init.replace(name, "", 1))], \
                f"drop export {name.strip()} from sqlglot/__init__", "export", "drop-export"
    yield [("write", "sqlglot/__init__.py", init.replace("from sqlglot.diff import diff as diff\n", ""))], \
        "drop diff export", "export", "drop-export"
    yield [("write", "sqlglot/__init__.py", init + "\nfrom sqlglot.helper import seq_get as seq_get  # noqa\n")], \
        "add export", "export", "add-export"
    dinit = read("sqlglot/dialects/__init__.py")
    for d in ["Dune", "Doris", "Athena", "Spark2", "Tableau"]:
        yield [("write", "sqlglot/dialects/__init__.py", dinit.replace(f'    "{d}",\n', "", 1))], \
            f"drop {d} from DIALECTS", "export", "drop-dialect-registration"
    einit = read("sqlglot/expressions/__init__.py")
    for sub in ["json", "array", "temporal"]:
        line = f"from sqlglot.expressions.{sub} import *  # noqa: F401,F403\n"
        yield [("write", "sqlglot/expressions/__init__.py", einit.replace(line, "", 1))], \
            f"drop expressions.{sub} star export", "export", "drop-star-export"
    oinit = read("sqlglot/optimizer/__init__.py")
    yield [("write", "sqlglot/optimizer/__init__.py", oinit + "\n# re-exports\n")], \
        "comment in optimizer/__init__", "export", "init-comment"

    # ---------------- syntax errors
    for p in rng.sample(dialects + parsers + generators, 4) + rng.sample(optim, 2) + \
            ["sqlglot/helper.py", "sqlglot/tokens.py", "sqlglot/expressions/math.py"]:
        text = read(p)
        lines = text.splitlines(keepends=True)
        sites, _ = M.comment_sites(text)
        i = rng.choice(sites[len(sites) // 3:])
        indent = lines[i - 1][: len(lines[i - 1]) - len(lines[i - 1].lstrip())]
        lines.insert(i - 1, indent + rng.choice(["return return\n", "def (:\n", "x = = 1\n"]))
        yield [("write", p, "".join(lines))], f"syntax error in {p}:{i}", "syntax", "syntax-error"
    for p in rng.sample(dialects + generators, 2):
        text = read(p)
        lines = text.splitlines(keepends=True)
        code = [k for k, l in enumerate(lines, 1) if l.startswith("    ") and l.strip() and not l.strip().startswith("#")]
        i = rng.choice(code)
        lines[i - 1] = " " + lines[i - 1]
        yield [("write", p, "".join(lines))], f"indentation error in {p}:{i}", "syntax", "indent-error"

    # ---------------- import cycles / import-time errors
    yield [("write", "sqlglot/helper.py", "from sqlglot.optimizer.scope import Scope  # noqa\n" + read("sqlglot/helper.py"))], \
        "helper imports optimizer.scope at module top (cycle)", "importcycle", "import-cycle"
    yield [("write", "sqlglot/tokens.py", read("sqlglot/tokens.py").replace(
        "from __future__ import annotations\n", "from __future__ import annotations\n\nfrom sqlglot.parser import Parser  # noqa\n", 1))], \
        "tokens imports parser (cycle)", "importcycle", "import-cycle"
    for p in rng.sample(dialects, 2):
        text = read(p)
        yield [("write", p, text.replace("from __future__ import annotations\n",
                                          "from __future__ import annotations\n\nimport sqlglot.optimizer.qualify  # noqa\n", 1))], \
            f"{p} imports optimizer at top", "importcycle", "extra-import"
    for p in rng.sample(parsers, 2):
        yield [("write", p, read(p) + "\nraise ImportError('disabled')\n")], f"{p} raises at import", "importcycle", "import-raises"
