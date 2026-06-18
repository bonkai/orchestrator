"""Offline unit tests for the standalone Fusion provider scripts (F1.1/F1.2/F1.2b):
deepseek, xai, qwen, minimax — plus the already-shipped gemini, glm. Every script
is OpenAI-shaped and MUST honor the one normalized stdout contract the
orchestrator's _panel_answer parses:

    {"ok": true, "text": "...", "model": "...",
     "prompt_tokens": N, "completion_tokens": N, "error": ""}

NO NETWORK: urllib.request.urlopen is mocked, so this verifies request shape +
response parsing + the never-raise guarantees WITHOUT a single (paid) API call.
The live end-to-end (a real provider call) is deferred until a key is configured
(it costs money — see FUSION_PLAN.md F1.2 verify).

The scripts are STANDALONE (they import nothing from the orchestrator package and
run as their own subprocess), so we load each one by FILE PATH — exactly how the
panel fan-out execs them — rather than as a package import.

Usage:
    python -m unittest tests.test_fusion_providers -v
"""

import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
PROVIDERS_DIR = REPO / "orchestrator" / "providers"

# name → (host substring expected in the POST URL, key_env)
PROVIDERS = {
    "deepseek": ("api.deepseek.com", "DEEPSEEK_API_KEY"),
    "xai": ("api.x.ai", "XAI_API_KEY"),
    "qwen": ("dashscope-intl.aliyuncs.com", "DASHSCOPE_API_KEY"),
    "minimax": ("api.minimax.io", "MINIMAX_API_KEY"),
    "gemini": ("generativelanguage.googleapis.com", "GEMINI_API_KEY"),
    "glm": ("api.z.ai", "ZAI_API_KEY"),
}

# The four scripts written for F1.2 / F1.2b (gemini + glm pre-existed).
NEW_PROVIDERS = ["deepseek", "xai", "qwen", "minimax"]


def _load(name):
    """Load providers/<name>.py as an isolated module by file path (the way the
    panel runs it), so package import quirks never enter the picture."""
    path = PROVIDERS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_prov_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_resp(payload: dict):
    """A stand-in for urlopen's return: only .read() is used by the scripts."""
    class _R:
        def read(self_inner):
            return json.dumps(payload).encode()
    return _R()


class _BodyHTTPError(Exception):
    """Mimics urllib.error.HTTPError enough for the scripts' except block, which
    calls e.read() to pull the response body into the error string."""
    def __init__(self, detail):
        super().__init__("HTTP 401")
        self._detail = detail.encode()

    def read(self):
        return self._detail


OPENAI_OK = {
    "choices": [{"message": {"role": "assistant", "content": "WAL is fine."}}],
    "usage": {"prompt_tokens": 1200, "completion_tokens": 340, "total_tokens": 1540},
}


def _run_main(mod, req, urlopen=None, key="test-key"):
    """Drive a provider's main() offline: stub _read_req + _key, patch urlopen,
    capture stdout, and return the parsed normalized JSON. Never lets main() see
    the network or the real config.json. `urlopen` is an Exception instance to
    simulate a failure, or None for the canned OK response."""
    urlopen_kw = ({"side_effect": urlopen} if urlopen is not None
                  else {"return_value": _fake_resp(OPENAI_OK)})
    buf = io.StringIO()
    with mock.patch.object(mod, "_read_req", return_value=req), \
            mock.patch.object(mod, "_key", return_value=key), \
            mock.patch.object(mod.urllib.request, "urlopen", **urlopen_kw), \
            redirect_stdout(buf):
        mod.main()
    return json.loads(buf.getvalue().strip())


class TestProviderContract(unittest.TestCase):
    """Each script honors the normalized stdout contract on a happy response."""

    def test_happy_response_is_normalized(self):
        for name in PROVIDERS:
            with self.subTest(provider=name):
                mod = _load(name)
                out = _run_main(mod, {"prompt": "q", "model": "m-x", "timeout_s": 5})
                self.assertTrue(out["ok"], out)
                self.assertEqual(out["text"], "WAL is fine.")
                self.assertEqual(out["model"], "m-x")          # echoes the requested model
                self.assertEqual(out["prompt_tokens"], 1200)
                self.assertEqual(out["completion_tokens"], 340)
                self.assertEqual(out["error"], "")

    def test_uses_default_model_when_request_omits_it(self):
        for name in PROVIDERS:
            with self.subTest(provider=name):
                mod = _load(name)
                out = _run_main(mod, {"prompt": "q", "timeout_s": 5})
                self.assertTrue(out["ok"], out)
                self.assertTrue(out["model"])                  # a non-empty default kicked in


class TestProviderRequestShape(unittest.TestCase):
    """The POST goes to <host>/chat/completions with Bearer auth and an
    OpenAI-style single-user-message body carrying the requested model."""

    def test_request_url_auth_and_body(self):
        for name, (host, key_env) in PROVIDERS.items():
            with self.subTest(provider=name):
                mod = _load(name)
                captured = {}

                def _spy(req, timeout=None, _c=captured):
                    _c["req"] = req
                    _c["timeout"] = timeout
                    return _fake_resp(OPENAI_OK)

                buf = io.StringIO()
                with mock.patch.object(mod, "_read_req",
                                       return_value={"prompt": "hello", "model": "m-x",
                                                     "timeout_s": 7}), \
                        mock.patch.object(mod, "_key", return_value="secret-key"), \
                        mock.patch.object(mod.urllib.request, "urlopen", _spy), \
                        redirect_stdout(buf):
                    mod.main()

                req = captured["req"]
                self.assertEqual(req.full_url, mod.BASE_URL + "/chat/completions")
                self.assertIn(host, req.full_url)
                self.assertEqual(req.get_method(), "POST")
                self.assertEqual(req.headers.get("Authorization"), "Bearer secret-key")
                self.assertEqual(req.headers.get("Content-type"), "application/json")
                body = json.loads(req.data.decode())
                self.assertEqual(body["model"], "m-x")
                self.assertEqual(body["messages"],
                                 [{"role": "user", "content": "hello"}])
                self.assertEqual(captured["timeout"], 7)       # timeout_s threaded through


class TestProviderNeverRaises(unittest.TestCase):
    """Every failure mode degrades to {"ok": false, ...} on stdout — never a
    traceback (the panel parses stdout and treats a missing/garbage result as a
    dropped seat)."""

    def test_missing_key_returns_ok_false(self):
        for name, (_host, key_env) in PROVIDERS.items():
            with self.subTest(provider=name):
                mod = _load(name)
                out = _run_main(mod, {"prompt": "q", "model": "m"}, key="")
                self.assertFalse(out["ok"])
                self.assertIn(key_env, out["error"])

    def test_network_error_returns_ok_false_no_raise(self):
        for name in PROVIDERS:
            with self.subTest(provider=name):
                mod = _load(name)
                out = _run_main(mod, {"prompt": "q", "model": "m"},
                                urlopen=ConnectionError("boom"))
                self.assertFalse(out["ok"])
                self.assertIn("boom", out["error"])

    def test_httperror_body_is_surfaced(self):
        for name in NEW_PROVIDERS:
            with self.subTest(provider=name):
                mod = _load(name)
                out = _run_main(mod, {"prompt": "q", "model": "m"},
                                urlopen=_BodyHTTPError('{"error":"bad model id"}'))
                self.assertFalse(out["ok"])
                self.assertIn("bad model id", out["error"])

    def test_garbage_response_returns_ok_false_no_raise(self):
        class _Garbage:
            def read(self):
                return b"<html>not json</html>"

        for name in NEW_PROVIDERS:
            with self.subTest(provider=name):
                mod = _load(name)
                buf = io.StringIO()
                with mock.patch.object(mod, "_read_req",
                                       return_value={"prompt": "q", "model": "m"}), \
                        mock.patch.object(mod, "_key", return_value="k"), \
                        mock.patch.object(mod.urllib.request, "urlopen",
                                          return_value=_Garbage()), \
                        redirect_stdout(buf):
                    mod.main()
                out = json.loads(buf.getvalue().strip())
                self.assertFalse(out["ok"])     # JSON decode failure → ok=false


class TestProviderKeyResolution(unittest.TestCase):
    """_key() precedence: env var → config.json api_key → '' (read inside the
    script, never passed via argv). Mirrors orchestrator.lib.config._resolve_key."""

    def test_env_var_takes_precedence(self):
        for name, (_host, key_env) in PROVIDERS.items():
            with self.subTest(provider=name):
                mod = _load(name)
                with mock.patch.dict("os.environ", {key_env: "env-secret"}, clear=False):
                    self.assertEqual(mod._key(), "env-secret")

    def test_no_key_anywhere_returns_empty(self):
        for name, (_host, key_env) in PROVIDERS.items():
            with self.subTest(provider=name):
                mod = _load(name)
                # No env var, and point config.json lookup at a missing file.
                with mock.patch.dict("os.environ", {}, clear=True), \
                        mock.patch.object(mod.os.path, "expanduser",
                                          return_value="/no/such/config.json"):
                    self.assertEqual(mod._key(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
