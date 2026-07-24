"""Prompt contract tests — the JSON keys and placeholders each prompt MUST carry.

NO NETWORK: these read the prompt files and the provider constants only.

Two separate contracts break silently, so both are pinned here:

1. JSON KEYS. Every key a consumer reads via `.get("...")` has to be spelled in
   the prompt that is supposed to produce it. Drop or rename one in the prompt
   and nothing raises — the rewriter burns a retry and then dispatches the
   user's original task, while the summarizer/onboarding (which have NO retry)
   just lose the result. Green tests were previously compatible with any prompt
   wording at all; this file is what makes an edit to a prompt provable.

2. PLACEHOLDERS. `_fill_template` regex-substitutes a fixed name set, so a
   renamed or deleted `{bundle}`/`{user_task}`/... leaves an unfilled literal in
   a live prompt with no error. fusion._ANALYSIS_PROMPT is the odd one out: it
   uses real `str.format`, so its literal braces must stay DOUBLED or the fill
   raises — and enrich()'s blanket `except Exception` would swallow that into
   ok=False, silently dropping enrichment from every dispatch.

Usage:
    python -m unittest tests.test_prompt_contract -v
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator.lib import fusion, onboarding, rewriter, summarizer

# key set → the consumer that reads it. Kept literal (not derived) on purpose:
# a test that computes the keys from the same source as the code would pass
# even when both drift away from what the model is told to emit.
REWRITER_KEYS = ["rewritten_prompt", "rationale", "files_to_read",
                 "hazards_acknowledged", "proposed_edits",
                 "action", "path", "content"]
SUMMARIZER_KEYS = ["summary_md", "what_worked", "what_broke", "lessons", "tags"]
ONBOARDING_KEYS = ["project_summary", "strengths", "gaps", "recommendations",
                   "proposed_edits", "title", "rationale", "target_path",
                   "manual_content", "action", "path", "content"]

# edits.validate rejects anything outside this set, so the prompt must name them.
EDIT_ACTIONS = ["append_to_memory", "append_to_knowledge", "create_task_file"]


class PromptJSONKeyContract(unittest.TestCase):
    """Every consumer-read key is literally present in its prompt."""

    def _assert_keys(self, path: Path, keys: list):
        text = path.read_text()
        for key in keys:
            self.assertIn(f'"{key}"', text,
                          f'{path.name} no longer names the JSON key "{key}" that '
                          f'its consumer reads — the value would silently come back empty')

    def test_rewriter_keys(self):
        self._assert_keys(rewriter.PROMPT_PATH, REWRITER_KEYS)

    def test_summarizer_keys(self):
        self._assert_keys(summarizer.PROMPT_PATH, SUMMARIZER_KEYS)

    def test_onboarding_keys(self):
        self._assert_keys(onboarding.PROMPT_PATH, ONBOARDING_KEYS)

    def test_edit_actions_named_in_both_prompts(self):
        for path in (rewriter.PROMPT_PATH, onboarding.PROMPT_PATH):
            text = path.read_text()
            for action in EDIT_ACTIONS:
                self.assertIn(action, text,
                              f"{path.name} dropped the {action!r} action — a proposed "
                              f"edit using it would be rejected by edits.validate")

    def test_analysis_prompt_keys(self):
        """fusion's cross-lab analysis prompt names every ANALYSIS_KEYS entry."""
        for key in fusion.ANALYSIS_KEYS:
            self.assertIn(f'"{key}"', fusion._ANALYSIS_PROMPT,
                          f'_ANALYSIS_PROMPT dropped "{key}" — _coerce_list would '
                          f'return [] and render_block would omit the section')


class PromptPlaceholderContract(unittest.TestCase):
    """The `{name}` placeholders each template must expose to _fill_template."""

    def test_rewriter_placeholders(self):
        text = rewriter.PROMPT_PATH.read_text()
        for name in ("bundle", "user_task", "similar_tasks"):
            self.assertIn("{" + name + "}", text)

    def test_summarizer_placeholders(self):
        text = summarizer.PROMPT_PATH.read_text()
        for name in ("transcript", "user_task"):
            self.assertIn("{" + name + "}", text)

    def test_onboarding_placeholders(self):
        text = onboarding.PROMPT_PATH.read_text()
        for name in ("scan", "bundle", "prior_runs", "git_changes"):
            self.assertIn("{" + name + "}", text)

    def test_analysis_prompt_formats(self):
        """_ANALYSIS_PROMPT uses real str.format — every other brace stays doubled.

        This is the guard the blanket `except Exception` in enrich() would
        otherwise hide: a single stray `{` here raises at .format() time and
        enrichment silently vanishes from every dispatch.
        """
        out = fusion._ANALYSIS_PROMPT.format(task="TASK-SENTINEL")
        self.assertIn("TASK-SENTINEL", out)
        self.assertNotIn("{task}", out)
        # the literal first/last-character instruction must survive the fill
        # un-doubled, i.e. `{{`/`}}` render as single braces for the model.
        self.assertIn("`{`", out)
        self.assertIn("`}`", out)


class PromptEnvelopeContract(unittest.TestCase):
    """Each JSON-emitting prompt states the strict no-prose envelope.

    The rewriter's auto-retry addendum (rewriter.py) already carried the strict
    first/last-character wording while the primary prompts only said "no prose";
    these pin the hardened wording so the two paths cannot drift apart again.
    """

    def test_json_prompts_pin_first_and_last_character(self):
        for path in (rewriter.PROMPT_PATH, summarizer.PROMPT_PATH,
                     onboarding.PROMPT_PATH):
            text = path.read_text()
            self.assertIn("`{`", text, f"{path.name} lost the first-character rule")
            self.assertIn("`}`", text, f"{path.name} lost the last-character rule")
            self.assertIn("markdown fences", text,
                          f"{path.name} lost the no-fences rule")


if __name__ == "__main__":
    unittest.main()
