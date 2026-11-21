"""Register a locally built image for offline measurement of its frozen default command."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import tarfile

from paths import REPO, WORKBENCH


def command(*args):
    return subprocess.check_output(args, cwd=REPO)


def verify_expanded_bundle(sha, app, bundle):
    recipe = command("git", "show", f"{sha}:Dockerfile")
    prepare = command("git", "show", f"{sha}:selector/prepare_runtime.py")
    unpacked_by_module = (b"python -m selector.prepare_runtime" in recipe
                          and f'Path("/app/{bundle}")'.encode() in prepare
                          and b"archive.unlink()" in prepare)
    if f"rm /app/{bundle}".encode() not in recipe and not unpacked_by_module:
        raise ValueError("Tagged image build does not remove its input archive")
    blob = command("git", "show", f"{sha}:{bundle}")
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r|xz") as archive:
        for member in archive:
            if member.isdir():
                continue
            dest = (app / member.name).resolve()
            recompressed = unpacked_by_module and dest.name.endswith(".gz.raw")
            if recompressed:
                dest = dest.with_suffix("").resolve()
            if not member.isfile() or not dest.is_relative_to(app.resolve()) or not dest.is_file():
                raise ValueError(f"Missing or unexpected runtime bundle member: {member.name}")
            opener = gzip.open if recompressed else open
            with opener(dest, "rb") as image_file, archive.extractfile(member) as frozen:
                if hashlib.file_digest(image_file, "sha256").digest() != hashlib.file_digest(frozen, "sha256").digest():
                    raise ValueError(f"Extracted runtime file differs from Git: {member.name}")


def register(ref, image):
    sha = command("git", "rev-parse", "--verify", ref + "^{commit}").decode().strip()
    info, = json.loads(command("docker", "image", "inspect", image))
    image_id = info["Id"]
    config = info["Config"]
    if config["WorkingDir"] != "/app" or config["User"] != "65534:65534":
        raise ValueError("Unexpected image user or working directory")
    folder = WORKBENCH / "candidate-images"
    folder.mkdir(exist_ok=True)
    output = folder / sha
    if output.exists():
        if json.loads((output / "config.json").read_text())["image"] != image_id:
            raise ValueError("A different image is registered for this commit")
        return
    with tempfile.TemporaryDirectory(dir=folder) as td:
        stage = Path(td) / "image"
        root = stage / "root"
        root.mkdir(parents=True)
        cid = command("docker", "create", "--network", "none", image_id).decode().strip()
        try:
            with subprocess.Popen(["docker", "export", cid], stdout=subprocess.PIPE) as stream:
                subprocess.run(["tar", "-xf", "-", "--no-same-owner", "--exclude=dev/*",
                                "-C", str(root)], stdin=stream.stdout, check=True)
                if stream.wait():
                    raise RuntimeError("Docker image export failed")
        finally:
            command("docker", "rm", cid)
        expected = set(command("git", "ls-tree", "-r", "--name-only", sha, "selector").decode().splitlines())
        app = root / "app"
        actual = {str(path.relative_to(app)) for path in (app / "selector").rglob("*")
                  if path.is_file() and "__pycache__" not in path.parts}
        bundle = "selector/maps/pin-runtime.tar.xz"
        if expected - actual == {bundle}:
            verify_expanded_bundle(sha, app, bundle)
            expected.remove(bundle)
        if actual != expected or any((app / path).read_bytes() != command("git", "show", f"{sha}:{path}")
                                     for path in sorted(expected)):
            raise ValueError("Image selector files differ from the frozen commit")
        (root / "data" / "commits").mkdir(parents=True, exist_ok=True)
        (root / "data" / "tests.json").touch()
        (stage / "config.json").write_text(json.dumps({"image": image_id, "env": config["Env"],
            "cmd": (config.get("Entrypoint") or []) + config["Cmd"]}))
        stage.rename(output)
    print(f"Verified and registered {sha[:12]} at {image_id[:19]}", flush=True)


CPU_MARKER = "BR_EVAL_CHILD_CPU_US="
# A namespace launcher can auto-reap intermediaries. Measure the image's
# actual default command directly, including children it waits for.
WAIT = """import os, sys
pid = os.posix_spawnp(sys.argv[1], sys.argv[1:], os.environ)
_, status, usage = os.wait4(pid, 0)
print('BR_EVAL_CHILD_CPU_US=' + str(round((usage.ru_utime + usage.ru_stime) * 1e6)), file=sys.stderr)
code = os.waitstatus_to_exitcode(status)
sys.exit(code if code >= 0 else 128-code)
"""


def runtime_command(image, data):
    conf = json.loads((image / "config.json").read_text())
    cmd = ["bwrap", "--die-with-parent", "--unshare-all", "--uid", "65534", "--gid", "65534",
           "--ro-bind", str(image / "root"), "/", "--dev", "/dev", "--proc", "/proc",
           "--size", str(64 * 1024**2), "--tmpfs", "/tmp", "--chdir", "/app", "--clearenv",
           "--ro-bind", str(data / "commits"), "/data/commits",
           "--ro-bind", str(data / "tests.json"), "/data/tests.json"]
    for spec in conf["env"]:
        key, value = spec.split("=", 1)
        cmd.extend(["--setenv", key, value])
    return cmd + ["--setenv", "HOME", "/tmp", "--", "python", "-c", WAIT, *conf["cmd"]]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ref")
    ap.add_argument("image")
    args = ap.parse_args()
    register(args.ref, args.image)
