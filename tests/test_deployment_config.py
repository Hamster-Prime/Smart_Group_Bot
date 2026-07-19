from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class DeploymentConfigTests(unittest.TestCase):
    def test_compose_has_safe_network_identity_and_shutdown_defaults(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('user: "${APP_UID:-1000}:${APP_GID:-1000}"', compose)
        self.assertIn("init: true", compose)
        self.assertIn("${MINIAPP_BIND_ADDRESS:-127.0.0.1}:8480:8480", compose)
        self.assertRegex(compose, r"(?m)^\s*stop_grace_period:\s*8m\s*$")

    def test_image_runs_non_root_and_uses_locked_dependencies(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(
            dockerfile,
            r"(?m)^FROM python:3\.12\.\d+-slim-(?:bookworm|trixie)"
            r"@sha256:[0-9a-f]{64}\s*$",
        )
        self.assertIn("COPY requirements.lock", dockerfile)
        self.assertIn("--requirement requirements.lock", dockerfile)
        self.assertRegex(dockerfile, r"(?m)^USER\s+app:app\s*$")
        self.assertNotRegex(dockerfile, r"(?m)^COPY\s+\.\s+\.\s*$")
        self.assertNotIn("COPY --chown=app:app config.toml", dockerfile)

    def test_lockfiles_are_present_and_not_ignored(self) -> None:
        self.assertTrue((ROOT / "uv.lock").is_file())
        requirements = (ROOT / "requirements.lock").read_text(encoding="utf-8")
        pins = [
            line.strip()
            for line in requirements.splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "--"))
        ]
        self.assertGreater(len(pins), 20)
        self.assertTrue(all("==" in line or line.startswith(("-e ", ".")) for line in pins))
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertNotRegex(gitignore, r"(?m)^uv\.lock\s*$")

    def test_docker_context_excludes_secrets_and_runtime_state(self) -> None:
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for required in (
            ".env",
            ".env.*",
            "config.toml",
            "data/",
            "*.db",
            "*.log*",
            "tests/",
        ):
            self.assertIn(required, dockerignore)


if __name__ == "__main__":
    unittest.main()
