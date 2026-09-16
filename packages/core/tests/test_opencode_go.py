"""OpenCode Go provider identity and session header tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from wenyi_core.config import Config
from wenyi_core.llm.configuration import ProviderConfig
from wenyi_core.llm.providers.opencode_go import USER_AGENT, OpenCodeGoClient
from wenyi_core.llm.registry import provider_spec


class TestOpenCodeGoProvider(unittest.TestCase):
    def test_provider_is_registered(self):
        self.assertEqual(provider_spec("opencode-go").kind, "opencode-go")
        self.assertEqual(provider_spec("opencode-go").client_class, "OpenCodeGoClient")

    def test_opencode_go_has_no_builtin_preset(self):
        with self.assertRaisesRegex(ValueError, "has no preset"):
            provider_spec("opencode-go").preset()

    def test_config_accepts_opencode_go_connection(self):
        cfg = Config.from_dict(
            {
                "llm": {
                    "providers": {"go": {"kind": "opencode-go"}},
                    "models": {
                        "writer": {
                            "provider": "go",
                            "model": "deepseek-v4-flash",
                        }
                    },
                    "tiers": {"strong": "writer", "cheap": "writer", "fast": "writer"},
                }
            }
        )
        self.assertEqual(cfg.llm.providers["go"].kind, "opencode-go")

    def test_ensure_client_passes_opencode_go_headers_only(self):
        client = OpenCodeGoClient(ProviderConfig(kind="opencode-go"))
        with (
            patch.dict("os.environ", {"OPENCODE_API_KEY": "test-key"}),
            patch("openai.OpenAI") as openai_ctor,
        ):
            client._ensure_client()
            client._ensure_client()  # reuse the same session id

        self.assertEqual(openai_ctor.call_count, 1)
        kwargs = openai_ctor.call_args.kwargs
        self.assertEqual(kwargs["base_url"], "https://opencode.ai/zen/go/v1")
        self.assertEqual(kwargs["api_key"], "test-key")
        headers = kwargs["default_headers"]
        self.assertEqual(headers["User-Agent"], USER_AGENT)
        self.assertEqual(headers["User-Agent"], "wenyi")
        self.assertRegex(
            headers["x-opencode-session"],
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )
        self.assertEqual(headers["x-opencode-session"], client._session_id)


if __name__ == "__main__":
    unittest.main()
