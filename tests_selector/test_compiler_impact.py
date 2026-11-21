import difflib
import unittest

from selector.impact import _sources, select


class TestCompilerImpact(unittest.TestCase):
    def test_changes_after_return_affect_importers(self):
        path = "sqlglot/__init__.py"
        source = _sources()["src"][path]
        ret = "    return Dialect.get_or_raise(read or dialect).tokenize(sql)"
        self.assertEqual(source.count(ret), 1)
        for statement in ("break", "yield None", "Dialect = None"):
            with self.subTest(statement=statement):
                modified = source.replace(ret, f"{ret}\n    {statement}")
                patch = difflib.unified_diff(source.splitlines(True), modified.splitlines(True),
                                             fromfile=f"a/{path}", tofile=f"b/{path}")
                result = select("".join(patch), [])
                self.assertTrue(result["full"], result["reasons"])

    def test_new_collection_files(self):
        for path, source, full in (
            ("tests/conftest.py", "# harmless comment", False),
            ("tests/conftest.py", "raise RuntimeError()", True),
            ("tests/test_added.py", "raise RuntimeError()", True),
            ("sqlglot/parser/__init__.py", "# shadows sqlglot/parser.py", True),
        ):
            with self.subTest(path=path, source=source):
                patch = f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+{source}\n"
                self.assertEqual(select(patch, [])["full"], full)
