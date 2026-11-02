"""Regression coverage for Claude-only campaign setup."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import factory


class ClaudeConfigTests(unittest.TestCase):
    def test_defaults_use_claude_for_both_roles(self):
        cfg = factory.default_config()
        factory.check_config(cfg)
        for role in ('researcher', 'critic'):
            self.assertEqual(cfg[role], {'backend': 'claude', 'model': 'opus'})

    def test_doctor_does_not_require_unused_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(factory.shutil, 'which', side_effect=lambda name: None if name == 'bwrap' else '/bin/' + name):
                report = factory.doctor(Path(tmp), factory.default_config())
        self.assertTrue(report['checks']['research-claude'])
        self.assertNotIn('research-opencode', report['checks'])
