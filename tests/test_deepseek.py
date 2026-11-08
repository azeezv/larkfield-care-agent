"""DeepSeek provider tests with a fake OpenAI client: no network."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import extract, pipeline
from src.extract import Extracted

ROOT = Path(__file__).resolve().parents[1]
TICKETS = {t["ticket_id"]: t for t in json.loads(
    (ROOT / "dataset" / "tickets" / "support_inbox_export.json").read_text())}

GOOD = Extracted(
    order_ref="LF-100200", ticket_type="REFUND",
    affected_lines=[]).model_dump_json()


class FakeCompletions:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=self.contents.pop(0)))],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
            model=kwargs["model"])


class FakeClient:
    def __init__(self, contents):
        self.chat = SimpleNamespace(completions=FakeCompletions(contents))


class DeepSeekTest(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {
            "PROVIDER": "deepseek", "MODEL_NAME": "deepseek-flash",
            "DEEPSEEK_API_KEY": "test-key"}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def test_structured_request_and_reply(self):
        client = FakeClient([GOOD])
        reply = extract.generate("Extract this email", Extracted, client=client)

        self.assertEqual(reply.data.order_ref, "LF-100200")
        self.assertEqual(reply.model, "deepseek-flash")
        self.assertEqual((reply.input_tokens, reply.output_tokens), (11, 7))

        call = client.chat.completions.calls[0]
        self.assertEqual(call["model"], "deepseek-flash")
        self.assertEqual(call["messages"][1]["content"], "Extract this email")
        self.assertIn("Return only a JSON object", call["messages"][0]["content"])
        self.assertIn("order_ref", call["messages"][0]["content"])
        self.assertEqual(call["response_format"], {"type": "json_object"})
        self.assertEqual(call["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(call["temperature"], 0)

    def test_invalid_reply_retries_once_then_succeeds(self):
        client = FakeClient(['{"order_ref": "LF-100200"}', GOOD])
        reply = extract.generate("Extract this email", Extracted, client=client)

        self.assertEqual(reply.data.order_ref, "LF-100200")
        self.assertEqual(len(client.chat.completions.calls), 2)

    def test_invalid_reply_twice_raises(self):
        client = FakeClient(['{"order_ref": "LF-100200"}', "not json"])
        with self.assertRaises(ValueError):
            extract.generate("Extract this email", Extracted, client=client)
        self.assertEqual(len(client.chat.completions.calls), 2)

    def test_invalid_reply_falls_back_to_unable_to_verify(self):
        client = FakeClient(["not json", "{}"])
        with tempfile.TemporaryDirectory() as tmp:
            response = pipeline.run_ticket(
                TICKETS["T002"], checkpoint_path=str(Path(tmp) / "runs.db"),
                extract_call=_deepseek_call(client))
        self.assertEqual(response.outcome, "UNABLE_TO_VERIFY")
        self.assertEqual(response.checks[0].check_id, "MODEL_UNAVAILABLE")
        self.assertEqual(len(client.chat.completions.calls), 2)


class _deepseek_call:
    """Call the pipeline with the DeepSeek provider and a fake client."""

    def __init__(self, client):
        self.client = client

    def __call__(self, prompt):
        return extract.generate(prompt, Extracted, client=self.client)


if __name__ == "__main__":
    unittest.main()
