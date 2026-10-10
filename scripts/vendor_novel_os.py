"""Fetch the requested MIT-licensed review prompts at a pinned upstream revision."""
import hashlib
import json
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

REPO = "mrigankad/Novel-OS"
DEST = Path(__file__).resolve().parents[1] / "src/pov_novel/vendor/novel_os"
FILES = {
    "agents/editor/prompt.md": "editor.md",
    "agents/style_curator/prompt.md": "style_curator.md",
    "agents/continuity_guardian/prompt.md": "continuity_guardian.md",
    "LICENSE": "LICENSE",
}


def fetch(url):
    request = Request(url, headers={"User-Agent": "pov-novel-agent"})
    with urlopen(request, timeout=60) as response:
        return response.read()


def main():
    # git's public refs endpoint avoids the unauthenticated GitHub API quota.
    refs = subprocess.run(["git", "ls-remote", f"https://github.com/{REPO}.git",
                           "refs/heads/dev"], check=True, capture_output=True,
                          text=True, timeout=60).stdout
    commit = refs.split()[0]
    # Download all files before replacing a previously installed snapshot.
    blobs = {target: fetch(f"https://raw.githubusercontent.com/{REPO}/{commit}/{source}")
             for source, target in FILES.items()}
    DEST.mkdir(parents=True, exist_ok=True)
    for target, blob in blobs.items():
        (DEST / target).write_bytes(blob)
    manifest = {"repository": f"https://github.com/{REPO}", "commit": commit,
                "license": "MIT", "files": [
                    {"source": source, "local": target,
                     "sha256": hashlib.sha256(blobs[target]).hexdigest()}
                    for source, target in FILES.items()]}
    (DEST / "provenance.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"commit": commit, "files": list(blobs)}, indent=2))


if __name__ == "__main__":
    main()
