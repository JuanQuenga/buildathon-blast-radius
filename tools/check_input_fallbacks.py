"""Run malformed-input and deadline controls inside the production image.

python tools/check_input_fallbacks.py IMAGE CATALOG
"""

import difflib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def cases():
    root = Path("/app/sqlglot")
    f = "sqlglot/executor/env.py"

    def patch(path, old, new):
        a = "a/" + path if old else "/dev/null"
        b = "b/" + path if new else "/dev/null"
        mode = (
            "new file mode 100644\n"
            if not old
            else "deleted file mode 100644\n"
            if not new
            else ""
        )
        header = f"diff --git a/{path} b/{path}\n{mode}"
        body = difflib.unified_diff(old.splitlines(True), new.splitlines(True), a, b)
        return (header + "".join(body)).encode()

    old = (root / f).read_text()
    config = (root / "pyproject.toml").read_text()
    source = patch(f, old, "# synthetic comment\n" + old)
    return {
        "mal1": b"truncated patch with no headers\n",
        "mal2": source[: source.index(b"@@")] + b"@@ -1,900 +1,900 @@\n+one\n",
        "huge": patch(f, old, ("# oversized diff input\n" * 200000) + old),
        "bin1": f"diff --git a/{f} b/{f}\nBinary files a/{f} and b/{f} differ\n".encode(),
        "ren1": f"diff --git a/{f} b/sqlglot/executor/renamed.py\nsimilarity index 100%\nrename from {f}\nrename to sqlglot/executor/renamed.py\n".encode(),
        "del1": patch(f, old, ""),
        "set1": patch("setup.py", "", 'raise RuntimeError("setup unavailable")\n'),
        "cnf1": patch(
            "tests/conftest.py", "", 'raise RuntimeError("collection unavailable")\n'
        ),
        "cfg1": patch("pyproject.toml", config, "[misformed\n" + config),
        "utf1": source.replace(b"# synthetic comment", b"\xff syntax"),
        "new1": patch(
            "tests/test_newfile.py", "", "def test_newfile():\n    assert True\n"
        ),
        "new2": patch("tests/test_newfile.py", "", "def test_newfile(:\n    pass\n"),
        "emp1": b"",
        "mode": f"diff --git a/{f} b/{f}\nold mode 100644\nnew mode 100755\n".encode(),
        "head": f"diff --git a/{f} b/{f}\n".encode(),
        "extr": source + b"+surplus line\n",
        "crlf": patch("pyproject.toml", config, "[misformed\n" + config).replace(
            b"\n", b"\r\n"
        ),
        "root": patch("sitecustomize.py", "", 'raise RuntimeError("unavailable")\n'),
        "long": patch(f, old, "#" + "x" * 300000 + "\n" + old),
        "many": (
            f"diff --git a/{f} b/{f}\n--- a/{f}\n+++ b/{f}\n"
            + "".join(f"@@ -{i + 1},0 +{i + 1} @@\n+# line\n" for i in range(2200))
        ).encode(),
        "nul1": source.replace(b"# synthetic comment", b"#\x00"),
    }


def check(root, ids, force_deadline=False):
    env = dict(os.environ, BLAST_DATA=str(root))
    command = [sys.executable, "-m", "selector.main"]
    if force_deadline:
        command = [
            sys.executable,
            "-c",
            "from selector import main; main.BATCH_WALL_LIMIT_S=0; raise SystemExit(main.main())",
        ]
    run = subprocess.run(command, env=env, capture_output=True, check=True, timeout=210)
    rows = [json.loads(line) for line in run.stdout.splitlines()]
    expected = {p.stem for p in (root / "commits").glob("*.patch")}
    assert len(rows) == len(expected) and {row["commit"] for row in rows} == expected
    for row in rows:
        workers = row["workers"]
        selected = [n for worker in workers for n in worker]
        assert 0 < len(workers) <= 8 and all(workers)
        assert selected and len(set(selected)) == len(selected) and set(selected) <= ids
        if force_deadline or row["commit"] not in {"ren1", "del1"}:
            assert set(selected) == ids, row["commit"]
    return len(rows)


def check_partial_deadline(root, catalog):
    from unittest.mock import patch
    from selector import main

    rows = json.loads(catalog.read_bytes())["tests"]
    durations = {t["number"]: main.duration_ms(t["seconds"]) for t in rows}
    patches = [main.Patch("crlf"), main.Patch("bin1")]
    elapsed = 0.0

    def finished_first(text, tests):
        nonlocal elapsed
        elapsed = 2.0
        return {"required": {rows[0]["number"]}, "plausible": set(), "full": False}

    with (
        patch.object(main, "DATA", root),
        patch.object(main.time, "monotonic", lambda: elapsed),
    ):
        main._impact_pass(
            patches, finished_first, rows, durations, set(durations), 0.0, deadline=1.0
        )
    assert patches[0].workers and not patches[0].full
    assert patches[1].full and patches[1].workers is None and not patches[1].text


def inside():
    catalog = Path("/data/tests.json")
    ids = {t["number"] for t in json.loads(catalog.read_bytes())["tests"]}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "tests.json").symlink_to(catalog)
        commits = root / "commits"
        commits.mkdir()
        for name, contents in cases().items():
            (commits / (name + ".patch")).write_bytes(contents)
        check_partial_deadline(root, catalog)
        count = check(root, ids)
        assert count == check(root, ids, force_deadline=True)
        print(f"{count} adversarial inputs and {count} deadline fallbacks passed")


if __name__ == "__main__":
    if sys.argv[1:] == ["--inside"]:
        sys.path.insert(0, "/app")
        inside()
    else:
        from measure_heavy import FLAGS

        image, catalog = sys.argv[1:]
        subprocess.run(
            [
                "docker",
                "run",
                *FLAGS,
                "-v",
                f"{Path(catalog).resolve()}:/data/tests.json:ro",
                "-v",
                f"{Path(__file__).resolve()}:/check.py:ro",
                image,
                "python",
                "/check.py",
                "--inside",
            ],
            check=True,
            timeout=250,
        )
