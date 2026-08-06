"""翻译前静态 Token 预算测试。"""

from __future__ import annotations

import os
import tempfile
import unittest

from trans_novel.config import Config
from trans_novel.ingest.models import Chapter, Document, Segment
from trans_novel.pipeline.runstore import STATUS_DONE, RunStore, slugify
from trans_novel.pipeline.token_budget import estimate_text_tokens, estimate_translation_tokens


class TestTextTokenEstimate(unittest.TestCase):
    def test_handles_cjk_latin_and_empty_text(self):
        self.assertEqual(estimate_text_tokens(""), 0)
        self.assertEqual(estimate_text_tokens("中文测试"), 4)
        self.assertEqual(estimate_text_tokens("abcdefgh"), 2)
        self.assertGreater(estimate_text_tokens("Привет мир"), 0)


class TestTranslationTokenEstimate(unittest.TestCase):
    @staticmethod
    def _config(state_dir: str, **pipeline) -> Config:
        return Config.from_dict(
            {
                "language": {"source": "en", "target": "zh"},
                "llm": {"provider": "fake"},
                "paths": {"state_dir": state_dir},
                "segment": {"max_chars_per_batch": 30, "max_chars_per_segment": 100},
                "pipeline": {
                    "book_understanding": False,
                    "polish": False,
                    **pipeline,
                },
            }
        )

    def test_new_book_estimates_required_stages_without_writing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "novel.txt")
            state_dir = os.path.join(directory, "state")
            with open(source, "w", encoding="utf-8") as file:
                file.write("First paragraph with enough words.\n\nSecond paragraph follows here.\n")

            estimate = estimate_translation_tokens(source, self._config(state_dir))

            self.assertFalse(estimate.resumed)
            self.assertEqual(estimate.basis, "本地解析源文")
            self.assertGreater(estimate.pending_characters, 0)
            self.assertGreater(estimate.pending_batches, 0)
            self.assertGreater(estimate.prompt_tokens, 0)
            self.assertGreater(estimate.completion_tokens, 0)
            self.assertEqual(
                estimate.total_tokens,
                estimate.prompt_tokens + estimate.completion_tokens,
            )
            self.assertLess(estimate.lower_total_tokens, estimate.total_tokens)
            self.assertGreater(estimate.upper_total_tokens, estimate.total_tokens)
            self.assertIn("正文翻译", [stage.stage for stage in estimate.stages])
            self.assertIn("术语抽取", [stage.stage for stage in estimate.stages])
            self.assertFalse(os.path.exists(state_dir))

    def test_resume_estimates_only_unfinished_chapter(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "resume.txt")
            state_dir = os.path.join(directory, "state")
            with open(source, "w", encoding="utf-8") as file:
                file.write("source placeholder")
            config = self._config(state_dir)
            store = RunStore(os.path.join(state_dir, slugify("resume")))
            document = Document(
                title="resume",
                source_lang="en",
                target_lang="zh",
                fmt="text",
                source_path=source,
                chapters=[
                    Chapter(
                        index=0,
                        title="Done",
                        segments=[Segment(index=0, source="already done", target="已经完成")],
                    ),
                    Chapter(
                        index=1,
                        title="Pending",
                        segments=[Segment(index=0, source="still needs translation")],
                    ),
                ],
            )
            manifest = store.stage_document(document)
            manifest["chapters"][0]["status"] = STATUS_DONE
            manifest["chapters"][0]["title_translated"] = "完成"
            store.save_manifest(manifest)
            store.save_analysis({"genre": "novel"})

            estimate = estimate_translation_tokens(source, config)

            self.assertTrue(estimate.resumed)
            self.assertEqual(estimate.basis, "已有断点")
            self.assertEqual(estimate.pending_characters, len("still needs translation"))
            self.assertEqual(estimate.pending_batches, 1)

    def test_completed_resume_can_have_zero_future_model_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "done.txt")
            state_dir = os.path.join(directory, "state")
            with open(source, "w", encoding="utf-8") as file:
                file.write("done")
            config = self._config(state_dir)
            store = RunStore(os.path.join(state_dir, slugify("done")))
            document = Document(
                title="done",
                source_lang="en",
                target_lang="zh",
                fmt="text",
                source_path=source,
                chapters=[
                    Chapter(
                        index=0,
                        title="Done",
                        segments=[Segment(index=0, source="done", target="完成")],
                    )
                ],
            )
            manifest = store.stage_document(document)
            manifest["chapters"][0]["status"] = STATUS_DONE
            manifest["chapters"][0]["title_translated"] = "完成"
            store.save_manifest(manifest)
            store.save_analysis({"genre": "novel"})

            estimate = estimate_translation_tokens(source, config)

            self.assertEqual(estimate.total_tokens, 0)
            self.assertEqual(estimate.pending_characters, 0)
            self.assertEqual(estimate.pending_batches, 0)

    def test_review_is_included_but_conditional_rounds_are_only_noted(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "review.txt")
            state_dir = os.path.join(directory, "state")
            with open(source, "w", encoding="utf-8") as file:
                file.write("A paragraph to translate and review.\n")
            config = self._config(
                state_dir,
                review=True,
                review_agent_loop=True,
                review_fix_loop=True,
            )

            estimate = estimate_translation_tokens(
                source,
                config,
                include_review=True,
            )

            self.assertIn("全书审校 R1", [stage.stage for stage in estimate.stages])
            self.assertTrue(any("盲审" in note for note in estimate.conditional_notes))
            self.assertGreaterEqual(estimate.upper_total_tokens, ceil_int(estimate.total_tokens * 1.7))

    def test_invalid_chapter_is_rejected_before_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "chapter.txt")
            with open(source, "w", encoding="utf-8") as file:
                file.write("Only chapter.\n")

            with self.assertRaisesRegex(ValueError, "章节编号 9 不存在"):
                estimate_translation_tokens(
                    source,
                    self._config(os.path.join(directory, "state")),
                    only_chapter=9,
                )


def ceil_int(value: float) -> int:
    value_int = int(value)
    return value_int if value_int == value else value_int + 1


if __name__ == "__main__":
    unittest.main()
