import os
import tempfile
import unittest

from selector.patch import PatchError, parse
from selector.verify import apply_patch, build_overlay, check_verifiable

PATCH = """diff --git c/pkg/a.py i/pkg/a.py
--- c/pkg/a.py
+++ i/pkg/a.py
@@ -1,3 +1,3 @@
 x = 1
-y = 2
+y = 3
 z = 4
diff --git a/pkg/new.py b/pkg/new.py
new file mode 100644
--- /dev/null
+++ b/pkg/new.py
@@ -0,0 +1 @@
+hello = 1
\\ No newline at end of file
diff --git a/pkg/gone.py b/pkg/gone.py
deleted file mode 100644
--- a/pkg/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-bye
"""


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "pkg"))
        for name, text in (("a.py", "x = 1\ny = 2\nz = 4\n"), ("gone.py", "bye\n"), ("keep.py", "k\n")):
            with open(os.path.join(self.d, "pkg", name), "w") as f:
                f.write(text)

    def test_parse_prefixes(self):
        self.assertEqual([f.path for f in parse(PATCH)], ["pkg/a.py", "pkg/new.py", "pkg/gone.py"])

    def test_apply_and_overlay(self):
        ch = apply_patch(PATCH, self.d)
        self.assertEqual(ch, {"pkg/a.py": "x = 1\ny = 3\nz = 4\n", "pkg/new.py": "hello = 1",
                              "pkg/gone.py": None})
        out = tempfile.mkdtemp()
        build_overlay(os.path.join(out, "ov"), ch, self.d)
        ov = os.path.join(out, "ov", "pkg")
        self.assertEqual(sorted(os.listdir(ov)), ["a.py", "keep.py", "new.py"])
        self.assertTrue(os.path.islink(os.path.join(ov, "keep.py")))
        self.assertEqual(open(os.path.join(ov, "a.py")).read(), "x = 1\ny = 3\nz = 4\n")

    def test_context_mismatch(self):
        with open(os.path.join(self.d, "pkg", "a.py"), "w") as f:
            f.write("x = 1\ny = 5\nz = 4\n")
        with self.assertRaises(PatchError):
            apply_patch(PATCH, self.d)

    def test_refuse_pytest_level(self):
        with self.assertRaises(PatchError):
            check_verifiable({"tests/conftest.py": "x"})
        with self.assertRaises(PatchError):
            check_verifiable({"README.md": "x"})
        check_verifiable({"sqlglot/x.py": "x", "tests/fixtures/a.sql": None})


if __name__ == "__main__":
    unittest.main()
