"""Dockerfile syntax checks that do not need a Docker daemon."""

import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DockerfileFromTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as f:
            self.from_lines = [ln.rstrip("\n") for ln in f if re.match(r"\s*FROM\b", ln, re.I)]

    def test_has_from_line(self):
        self.assertTrue(self.from_lines)

    def test_no_trailing_comment_on_from(self):
        # Docker only treats a comment as such at the start of a line; a trailing
        # "# ..." becomes extra FROM arguments and the build fails.
        for ln in self.from_lines:
            self.assertIsNone(re.search(r"\s#", ln), ln)

    def test_from_is_digest_pinned(self):
        for ln in self.from_lines:
            self.assertIn("@sha256:", ln)

    def test_from_has_one_image_argument(self):
        for ln in self.from_lines:
            args = [a for a in ln.split()[1:] if not a.startswith("--platform=")]
            self.assertIn(len(args), (1, 3), ln)
            if len(args) == 3:
                self.assertEqual(args[1].upper(), "AS", ln)


class NginxAccessLogTests(unittest.TestCase):
    CONFS = ("nginx.conf", "nginx-tls.conf", "nginx-plain.conf")

    def test_access_log_is_off_everywhere(self):
        for name in self.CONFS:
            with open(os.path.join(ROOT, "docker", name), encoding="utf-8") as f:
                text = f.read()
            self.assertNotRegex(text, r"(?m)^\s*access_log\s+/dev/stdout", name)
            for ln in re.findall(r"(?m)^\s*access_log\b.*$", text):
                self.assertRegex(ln, r"access_log\s+off\s*;", name)
        with open(os.path.join(ROOT, "docker", "nginx.conf"), encoding="utf-8") as f:
            text = f.read()
        self.assertRegex(text, r"(?m)^\s*access_log\s+off\s*;")
        self.assertRegex(text, r"(?m)^error_log /dev/stderr;")


class DockerfileStateDirTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as f:
            self.text = f.read()

    def test_state_dir_created_and_owned_before_user(self):
        mk = self.text.find("mkdir -p /var/lib/appneta-ia-bridge")
        own = re.search(r"chown appuser:appuser /var/lib/appneta-ia-bridge", self.text)
        user = self.text.find("\nUSER 10001")
        self.assertGreaterEqual(mk, 0)
        assert own is not None
        self.assertGreater(user, mk)
        self.assertGreater(user, own.start())

    def test_state_dir_is_a_volume_with_default_env(self):
        code = [ln for ln in self.text.splitlines() if not ln.lstrip().startswith("#")]
        self.assertTrue(
            any(re.match(r"\s*VOLUME\b.*/var/lib/appneta-ia-bridge", ln) for ln in code))
        self.assertIn("DEDUP_STATE_DIR=/var/lib/appneta-ia-bridge", "\n".join(code))


class InstallDocTests(unittest.TestCase):
    def test_install_docs_document_volume_log_rotation_and_tradeoffs(self):
        # The public copies sit at the root of the customer repo; here they are the overlays.
        text = ""
        for name in ("INSTALLATION.md", "REFERENCE.md"):
            for rel in (f"release/public/{name}", name):
                path = os.path.join(ROOT, rel)
                if os.path.exists(path):
                    break
            else:
                self.fail(f"no {name} found")
            with open(path, encoding="utf-8") as f:
                text += f.read()
        for needle in ("ia-bridge-state", "max-size=10m", "max-file=3", "docker rm", "10001",
                       "SPOOL_MAX_MB", "SPOOL_FREE_RESERVE_MB", "stop_grace_period"):
            self.assertIn(needle, text)


class InstallVerifyTests(unittest.TestCase):
    def _install_text(self):
        for rel in ("release/public/INSTALLATION.md", "INSTALLATION.md"):
            path = os.path.join(ROOT, rel)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    return f.read()
        self.fail("no INSTALLATION.md found")

    def test_step_7_payload_is_dedupable(self):
        # A verify payload without raisedTime is counted undedupable, so step 7 could never
        # show a repeat being suppressed.
        from alarm_bridge.dedup import dedup_key

        match = re.search(r"-d '(\{\"state\":\"RAISED\"[^']*\})'", self._install_text())
        if match is None:
            self.fail("step 7 curl payload not found")
        self.assertIsNotNone(dedup_key(json.loads(match.group(1))))

    def test_step_3_creates_the_trap_community_file(self):
        # Step 4 mounts /run/secrets/trap_community, so step 3 must say how to create it.
        text = self._install_text()
        step3 = text[text.index("## 3. Create the authentication token"):text.index("## 4. Run the container")]
        self.assertIn("secrets/trap_community", step3)

    def test_step_1_offers_download_and_copy_without_git(self):
        # Hosts that cannot reach the repository (VPN-only) need a no-git path.
        text = self._install_text()
        step1 = text[text.index("## 1. Get the code"):text.index("## 2. Install the TLS certificate")]
        for needle in ("git clone", "scp ", "unzip -q", "docker build"):
            self.assertIn(needle, step1)


if __name__ == "__main__":
    unittest.main()
