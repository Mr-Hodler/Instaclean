"""AUTOMATED TEST: security regression on our own source code.

Fails if the package starts using shell/eval/network/cookie APIs, or if
.gitignore stops covering sensitive paths.
"""

from __future__ import annotations

import ast
import subprocess  # test-only: asks git which paths are ignored
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "instaclean"

FORBIDDEN_IMPORTS = {"subprocess", "socket", "requests", "urllib.request", "http.client",
                     "httpx", "aiohttp", "ftplib", "smtplib", "telnetlib", "pickle", "marshal",
                     "importlib", "ctypes", "keyring", "shutil"}
FORBIDDEN_BUILTINS = {"eval", "exec", "compile", "__import__"}
FORBIDDEN_METHODS = {"system", "popen", "spawnv", "execv", "cookies", "add_cookies",
                     "storage_state", "add_init_script", "expose_function", "route_from_har"}


def _sources():
    return sorted(PKG.glob("*.py"))


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_forbidden_imports_or_calls(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name not in FORBIDDEN_IMPORTS, f"{path.name}: import {a.name}"
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "") not in FORBIDDEN_IMPORTS, f"{path.name}: from {node.module}"
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                assert f.id not in FORBIDDEN_BUILTINS, f"{path.name}:{node.lineno} {f.id}()"
            elif isinstance(f, ast.Attribute):
                assert f.attr not in FORBIDDEN_METHODS, f"{path.name}:{node.lineno} .{f.attr}()"


def test_no_direct_instagram_api_urls():
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for marker in ("/api/v1", "graphql", "i.instagram.com", "x-ig-app-id", "csrftoken"):
            assert marker not in text, f"{path.name} mentions {marker}"


@pytest.mark.parametrize("p", [".env", "prod.env", ".env.local", "logs/x.log", "checkpoints/s.json",
                               "screenshots/a.png", "debug/a.html", "browser-profile/Default",
                               "profiles/x", "sessions/x", "auth/x", ".venv/bin/python",
                               "storage_state.json", "trace.zip", "x.har"])
def test_gitignore_covers_sensitive_paths(p):
    r = subprocess.run(["git", "check-ignore", "-q", "--no-index", p], cwd=ROOT, check=False)
    assert r.returncode == 0, f"{p} is NOT ignored"


def test_env_example_is_tracked_and_has_no_secrets():
    r = subprocess.run(["git", "check-ignore", "-q", "--no-index", ".env.example"], cwd=ROOT,
                       check=False)
    assert r.returncode == 1
    text = (ROOT / ".env.example").read_text().lower()
    for word in ("password=", "cookie=", "token=", "sessionid", "username="):
        assert word not in text
