"""Real subprocess fixture tests: no network or live model execution."""

import importlib.util
import json
import os
from pathlib import Path
import stat
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "headless.py"
sys.path.insert(0, str(MODULE.parent))


class HeadlessTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.exists(), "headless helper has not been implemented")
        spec = importlib.util.spec_from_file_location("headless", MODULE)
        self.h = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.h)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.brief = self.root / "brief.md"
        self.brief.write_text("Inspect the code; do not change files. $HOME `touch bad`")
        self.exe = self.root / "claude-fixture"
        self.exe.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
if '--version' in sys.argv:
 print(os.environ.get('FIX_VERSION','2.1.261') + ' (Claude Code)'); sys.exit(0)
if 'auth' in sys.argv:
 print(json.dumps({'loggedIn': True, 'authMethod': os.environ.get('FIX_AUTH','claude.ai'), 'apiProvider': os.environ.get('FIX_PROVIDER','firstParty'), 'subscriptionType': os.environ.get('FIX_SUB','max'), 'email':'do-not-record@example.test'})); sys.exit(0)
Path(os.environ['FIX_CALLED']).write_text(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}))
mode=os.environ.get('FIX_MODE','success')
print(json.dumps({'type':'system','subtype':'init','session_id':'11111111-1111-4111-8111-111111111111','model':os.environ.get('FIX_MODEL','claude-fable-5-1')}))
if mode == 'truncated': sys.exit(0)
if mode == 'malformed': print('not json')
print(json.dumps({'type':'result','subtype':'success' if mode != 'error' else 'error_during_execution','is_error':mode == 'error','session_id':'11111111-1111-4111-8111-111111111111','result':'done'}))
sys.exit(9 if mode == 'exit_error' else 0)
""")
        self.exe.chmod(0o700)
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": os.environ["HOME"],
            "FIX_CALLED": str(self.root / "called"),
        }
        self.kw = dict(
            cwd=self.root,
            brief_path=self.brief,
            state_dir=self.root / "state",
            executable=str(self.exe),
            env=self.env,
            billing_policy="native-metering",
        )

    def test_explicit_native_metering_runs_without_claiming_billing_enforcement(self):
        result = self.h.run(**self.kw)
        self.assertEqual(result["status"], "completed")
        self.assertTrue((self.root / "called").exists())
        self.assertFalse(result["subscription_only_enforced"])

    def test_default_subscription_only_blocks_model_and_state_writes(self):
        self.kw.pop("billing_policy")
        plan = self.h.preflight(**self.kw)
        self.assertFalse(plan["launch_allowed"])
        self.assertIn("subscription-only", plan["block_reason"])
        result = self.h.run(**self.kw)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse((self.root / "called").exists())
        self.assertFalse((self.root / "state").exists())

    def test_unknown_version_blocks_even_explicit_native_metering(self):
        self.env["FIX_VERSION"] = "2.1.262"
        result = self.h.run(**self.kw)
        self.assertEqual(result["compatibility_status"], "untested")
        self.assertEqual(result["status"], "blocked")
        self.assertFalse((self.root / "called").exists())

    def test_unknown_resume_state_never_launches(self):
        with mock.patch.object(self.h, "session_lifecycle", return_value="unknown"):
            with self.assertRaises(ValueError):
                self.h.run(**self.kw, resume="22222222-2222-4222-8222-222222222222")
        self.assertFalse((self.root / "called").exists())

    def test_resume_rechecked_immediately_before_launch(self):
        with mock.patch.object(self.h, "session_lifecycle", side_effect=["stopped", "active"]):
            result = self.h.run(**self.kw, resume="22222222-2222-4222-8222-222222222222")
        self.assertEqual(result["status"], "failed")
        self.assertFalse((self.root / "called").exists())

    def test_preflight_excludes_auth_identity_and_never_launches_model(self):
        result = self.h.preflight(**self.kw)
        self.assertEqual(result["authentication"]["subscriptionType"], "max")
        self.assertNotIn("do-not-record", json.dumps(result))
        self.assertFalse((self.root / "called").exists())

    def test_unsafe_auth_or_provider_fails_before_model_launch(self):
        self.env["FIX_AUTH"] = "api_key"
        with self.assertRaises(ValueError):
            self.h.run(**self.kw)
        self.assertFalse((self.root / "called").exists())
        self.env.pop("FIX_AUTH")
        for key in [
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_EFFORT_LEVEL",
        ]:
            with self.subTest(key=key):
                self.env[key] = "private-secret"
                with self.assertRaises(ValueError) as caught:
                    self.h.preflight(**self.kw)
                self.assertNotIn("private-secret", str(caught.exception))
                self.env.pop(key)

    def test_non_max_or_third_party_auth_refused(self):
        for key, value in [("FIX_SUB", "pro"), ("FIX_PROVIDER", "bedrock")]:
            with self.subTest(key=key):
                self.env[key] = value
                with self.assertRaises(ValueError):
                    self.h.run(**self.kw)
                self.assertFalse((self.root / "called").exists())
                self.env.pop(key)

    def test_shared_state_directory_refused_before_model_call(self):
        state = self.root / "state"
        state.mkdir()
        state.chmod(0o755)
        with self.assertRaises(ValueError):
            self.h.run(**self.kw)
        self.assertFalse((self.root / "called").exists())

    def test_state_symlink_ancestor_refused_before_writing_or_launch(self):
        target = self.root / "target"
        target.mkdir(mode=0o700)
        link = self.root / "linked"
        link.symlink_to(target, target_is_directory=True)
        self.kw["state_dir"] = link / "new-state"
        with self.assertRaises(ValueError):
            self.h.run(**self.kw)
        self.assertFalse((target / "new-state").exists())
        self.assertFalse((self.root / "called").exists())

    def test_active_session_resume_refused_in_selected_profile(self):
        sid = "22222222-2222-4222-8222-222222222222"
        profile = self.root / "profile"
        sessions = profile / "sessions"
        sessions.mkdir(parents=True, mode=0o700)
        endpoint = self.root / "live.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(endpoint))
        endpoint.chmod(0o600)
        start = subprocess.check_output(
            ["ps", "-p", str(os.getpid()), "-o", "lstart="],
            text=True,
            env=dict(os.environ, LC_ALL="C", TZ="UTC"),
        ).strip()
        (sessions / (str(os.getpid()) + ".json")).write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "sessionId": sid,
                    "cwd": str(self.root),
                    "version": "2.1.261",
                    "peerProtocol": 1,
                    "messagingSocketPath": str(endpoint),
                    "procStart": start,
                    "pidDomain": sys.platform,
                }
            )
        )
        self.env["CLAUDE_CONFIG_DIR"] = str(profile)
        with self.assertRaises(ValueError):
            self.h.run(**self.kw, resume=sid)
        self.assertFalse((self.root / "called").exists())

    def test_read_only_run_has_exact_tools_and_private_receipt(self):
        result = self.h.run(**self.kw)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["observed_models"], ["claude-fable-5-1"])
        self.assertEqual(result.get("observed_model"), "claude-fable-5-1")
        self.assertEqual(result["session_id"], "11111111-1111-4111-8111-111111111111")
        call = json.loads((self.root / "called").read_text())
        args = call["argv"]
        self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(args[args.index("--permission-prompts") + 1], "none")
        self.assertIn("--strict-mcp-config", args)
        self.assertIn("--safe-mode", args)
        prompt = args[args.index("--append-system-prompt") + 1]
        self.assertIn(str((MODULE.parent.parent / "references/models.json").resolve()), prompt)
        self.assertIn("exact model IDs", prompt)
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertEqual(call["stdin"], self.brief.read_text())
        for key in ["receipt_path", "events_path", "stderr_path"]:
            path = Path(result[key])
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn("do-not-record", Path(result["receipt_path"]).read_text())

    def test_write_and_resume_preserve_exact_explicit_session(self):
        sid = "22222222-2222-4222-8222-222222222222"
        with mock.patch.object(self.h, "session_lifecycle", return_value="stopped"):
            result = self.h.run(**self.kw, mode="workspace-write", resume=sid)
        args = json.loads((self.root / "called").read_text())["argv"]
        self.assertEqual(args[args.index("--resume") + 1], sid)
        self.assertEqual(args[args.index("--permission-mode") + 1], "auto")
        self.assertIn("Agent", args[args.index("--tools") + 1].split(","))
        self.assertNotEqual(result["status"], "completed")  # fixture reports different session

    def test_bad_output_nonzero_and_model_substitution_never_complete(self):
        for mode in ["truncated", "malformed", "exit_error", "error"]:
            with self.subTest(mode=mode):
                self.env["FIX_MODE"] = mode
                result = self.h.run(**self.kw)
                self.assertNotEqual(result["status"], "completed")
        self.env.pop("FIX_MODE")
        self.env["FIX_MODEL"] = "claude-opus-5"
        result = self.h.run(**self.kw)
        self.assertEqual(result["status"], "model_mismatch")

    def test_invalid_model_effort_resume_and_mode_fail_before_process(self):
        for args in [
            dict(model="fable"),
            dict(effort="ultracode"),
            dict(resume="--last"),
            dict(mode="bypassPermissions"),
        ]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.h.preflight(**self.kw, **args)
        self.assertFalse((self.root / "called").exists())


if __name__ == "__main__":
    unittest.main()
