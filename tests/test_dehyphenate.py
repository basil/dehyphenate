from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from dehyphenate import Dehyphenator, dehyphenate, dehyphenate_stream, iter_dehyphenate

ENGLISH_LOCALE = {name: "en_US.UTF-8" for name in ("LC_ALL", "LC_CTYPE", "LANG")}


def cli_command(*arguments: str) -> list[str]:
    """Run the CLI through the module installed for this interpreter."""

    return [sys.executable, "-m", "dehyphenate", *arguments]


def run_cli(*arguments: str, input: str = "") -> subprocess.CompletedProcess[str]:
    """Run the CLI to completion and require a clean exit."""

    return subprocess.run(
        cli_command(*arguments),
        input=input,
        text=True,
        capture_output=True,
        check=True,
    )


def pin_locale(test: unittest.TestCase, environment: dict[str, str]) -> None:
    """Fix the locale for one test, subprocesses included."""

    patcher = mock.patch.dict(os.environ, environment)
    patcher.start()
    test.addCleanup(patcher.stop)


class DehyphenationTests(unittest.TestCase):
    def setUp(self) -> None:
        # Every expectation below is English, so the default language must not
        # be whatever the machine running the suite happens to be set to.
        pin_locale(self, ENGLISH_LOCALE)

    def test_syllable_hyphens_are_removed(self) -> None:
        self.assertEqual(
            dehyphenate("quar-ter-ly re-port; rev-e-nue!"),
            "quarterly report; revenue!",
        )

    def test_real_compound_boundary_is_inferred(self) -> None:
        self.assertEqual(dehyphenate("cli-ent-fac-ing"), "client-facing")
        self.assertEqual(dehyphenate("mar-ket-read-y"), "market-ready")

    def test_attested_joined_spellings_override_component_words(self) -> None:
        self.assertEqual(
            dehyphenate("work-place dead-line fore-cast pay-roll sales-man"),
            "workplace deadline forecast payroll salesman",
        )

    def test_recognized_possessives_are_joined(self) -> None:
        self.assertEqual(dehyphenate("man-ag-er's"), "manager's")
        self.assertEqual(
            dehyphenate("man-ag-er\N{RIGHT SINGLE QUOTATION MARK}s"),
            "manager\N{RIGHT SINGLE QUOTATION MARK}s",
        )

    def test_plural_possessive_is_conservatively_retained(self) -> None:
        self.assertEqual(dehyphenate("cli-ents'"), "cli-ents'")

    def test_accented_words_are_dehyphenated_like_ascii_ones(self) -> None:
        self.assertEqual(dehyphenate("ré-su-mé pro-té-gé"), "résumé protégé")
        self.assertEqual(dehyphenate("café-man-ag-er"), "café-manager")

    def test_accents_compare_equal_however_they_are_composed(self) -> None:
        precomposed = "ré-su-mé"
        decomposed = unicodedata.normalize("NFD", precomposed)

        self.assertNotEqual(decomposed, precomposed)
        self.assertEqual(
            unicodedata.normalize("NFC", dehyphenate(decomposed)),
            "résumé",
        )

    def test_indic_combining_marks_stay_inside_token_runs(self) -> None:
        for language, text, expected in (
            ("hi", "न-म-स्ते", "नमस्ते"),
            ("bn", "বা-ং-লা", "বাংলা"),
            ("ta", "த-மி-ழ்", "தமிழ்"),
        ):
            with self.subTest(language=language):
                self.assertEqual(dehyphenate(text, language=language), expected)

    def test_unknown_non_ascii_brand_names_are_conservatively_retained(self) -> None:
        self.assertEqual(dehyphenate("Qor-väx LLC\n"), "Qor-väx LLC\n")
        self.assertEqual(dehyphenate("Mër-ger-tech"), "Mër-ger-tech")

    def test_examples_round_trip(self) -> None:
        text = (
            "The client-facing manager's market-ready workplace forecast met "
            "the quarterly revenue target.\n"
        )
        hyphenated = (
            "The cli-ent-fac-ing man-ag-er's mar-ket-read-y work-place fore-cast "
            "met the quar-ter-ly rev-e-nue tar-get.\n"
        )

        self.assertEqual(dehyphenate(hyphenated), text)

    def test_unknown_model_boundaries_are_conservatively_retained(self) -> None:
        engine = Dehyphenator(additional_words=())
        generated = "Rev-Ops"

        self.assertEqual(engine.dehyphenate(generated), generated)

    def test_weak_joined_spelling_does_not_override_component_evidence(self) -> None:
        self.assertEqual(dehyphenate("first-fruits"), "first-fruits")

    def test_wordfreq_must_recognize_the_exact_candidate(self) -> None:
        # wordfreq tokenizes this spelling as just "cup".  That score must not
        # be treated as evidence for a form containing another character.
        self.assertEqual(dehyphenate("½-cup"), "½-cup")

    def test_unknown_elided_word_is_conservatively_retained(self) -> None:
        self.assertEqual(dehyphenate("prof-’ta-ble"), "prof-’ta-ble")

    def test_unhyphenated_proper_names_from_wordfreq_are_known(self) -> None:
        self.assertEqual(
            dehyphenate("A-do-be Net-flix Tes-la’s"),
            "Adobe Netflix Tesla’s",
        )

    def test_additional_word_overrides_wordfreq(self) -> None:
        engine = Dehyphenator(
            minimum_zipf=8.0,
            additional_words={"revops"},
        )

        self.assertEqual(engine.dehyphenate("Rev-Ops"), "RevOps")

    def test_additional_words_accepts_accented_vocabulary(self) -> None:
        engine = Dehyphenator(minimum_zipf=8.0, additional_words={"résumé"})

        self.assertEqual(engine.dehyphenate("ré-su-mé"), "résumé")

    def test_malformed_additional_words_are_rejected(self) -> None:
        for word in ("Sales!", "B2B", "", "client success", "clients'"):
            with self.subTest(word=word):
                with self.assertRaises(ValueError) as caught:
                    Dehyphenator(additional_words={word})

                self.assertIn(repr(word), str(caught.exception))

    def test_minimum_zipf_must_be_non_negative_and_finite(self) -> None:
        for value in (-1.0, float("inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Dehyphenator(minimum_zipf=value)

    def test_chunk_iterator_is_independent_of_chunk_boundaries(self) -> None:
        text = "cli-ent-fac-ing and work-place\n"
        chunks = ["cli-", "ent-f", "ac-ing and wo", "rk-place\n"]

        self.assertEqual("".join(iter_dehyphenate(chunks)), dehyphenate(text))

    def test_every_stream_split_matches_whole_string_tokenization(self) -> None:
        text = "²-por-tion " + unicodedata.normalize("NFD", "ré-su-mé")
        expected = dehyphenate(text)

        for split in range(len(text) + 1):
            with self.subTest(split=split):
                self.assertEqual(
                    "".join(iter_dehyphenate((text[:split], text[split:]))),
                    expected,
                )

    def test_stream_api(self) -> None:
        source = StringIO("rev-e-nue\nwork-place\n")
        destination = StringIO()

        dehyphenate_stream(source, destination, chunk_size=2)

        self.assertEqual(destination.getvalue(), "revenue\nworkplace\n")

    def test_unhyphenated_text_is_passed_through_unchanged(self) -> None:
        text = "Quarterly revenue, forecasts — and clients’ targets.\n"

        self.assertEqual(dehyphenate(text), text)

    def test_long_unhyphenated_run_does_not_backtrack_quadratically(self) -> None:
        # The chain pattern once required a hyphenated tail, so a run without
        # one backtracked through every length at every start position: 1.5 s
        # for 8 kB, rising fourfold per doubling. Linear scanning is ~2 ms
        # here, so this bound fails only if that behavior comes back.
        run = "a" * 40000

        start = time.perf_counter()
        result = dehyphenate(run)
        elapsed = time.perf_counter() - start

        self.assertEqual(result, run)
        self.assertLess(elapsed, 5.0)

    def test_long_hyphen_chain_does_not_search_superlinearly(self) -> None:
        # The segmentation search once extended every candidate to the end of
        # the chain, so one 8 kB chain of known chunks cost 37 s and grew
        # faster than its length. Bounding the candidate length is ~0.4 s
        # here, so this bound fails only if that behavior comes back.
        chain = "-".join(["a"] * 4096)

        start = time.perf_counter()
        result = dehyphenate(chain)
        elapsed = time.perf_counter() - start

        self.assertEqual(result.replace("-", ""), "a" * 4096)
        self.assertLess(elapsed, 10.0)

    def test_cli_exits_cleanly_when_the_reader_closes_the_pipe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "input.txt"
            source_path.write_text("cli-ent-fac-ing rev-e-nue\n" * 20000)

            with source_path.open() as source:
                process = subprocess.Popen(
                    cli_command(),
                    stdin=source,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                stdout = process.stdout
                stderr_pipe = process.stderr
                assert stdout is not None
                assert stderr_pipe is not None
                with process:
                    self.assertEqual(stdout.readline(), "client-facing revenue\n")
                    stdout.close()
                    stderr = stderr_pipe.read()

        self.assertEqual(stderr, "")
        self.assertEqual(process.returncode, 0)

    def test_cli_is_a_lossless_filter(self) -> None:
        result = run_cli(input="mar-ket-read-y\n")

        self.assertEqual(result.stdout, "market-ready\n")
        self.assertEqual(result.stderr, "")

    def test_cli_round_trips_non_utf8_bytes(self) -> None:
        result = subprocess.run(
            cli_command(),
            input=b"\xff mar-ket-read-y\n",
            capture_output=True,
            check=True,
        )

        self.assertEqual(result.stdout, b"\xff market-ready\n")
        self.assertEqual(result.stderr, b"")

    def test_cli_reports_user_errors_without_a_traceback(self) -> None:
        result = subprocess.run(
            cli_command("--language", "xx"),
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("no word frequencies for language 'xx'", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class LanguageTests(unittest.TestCase):
    def test_language_changes_which_boundaries_survive(self) -> None:
        # "wunderschön" is one German word but has insufficient evidence in
        # an English wordlist, so English conservatively retains the input.
        self.assertEqual(dehyphenate("wun-der-schön", language="de"), "wunderschön")
        self.assertEqual(dehyphenate("wun-der-schön", language="en"), "wun-der-schön")

    def test_turkish_dotted_i_uses_the_language_lexicon(self) -> None:
        self.assertEqual(dehyphenate("İs-tan-bul", language="tr"), "İstanbul")

    def test_french_elisions_are_resolved(self) -> None:
        self.assertEqual(dehyphenate("au-jour-d'hui", language="fr"), "aujourd'hui")
        self.assertEqual(dehyphenate("mer-veil-leux", language="fr"), "merveilleux")

    def test_default_language_follows_the_locale(self) -> None:
        for locale, expected in (
            ("fr_FR.UTF-8", "fr"),
            ("de_DE@euro", "de"),
            ("pt_BR.UTF-8", "pt"),
            ("zh_TW.UTF-8", "zh"),
        ):
            with self.subTest(locale=locale):
                pin_locale(self, dict.fromkeys(ENGLISH_LOCALE, locale))

                self.assertEqual(Dehyphenator().language, expected)

    def test_locale_precedence_and_fallback(self) -> None:
        cases = [
            ({"LC_ALL": "de_DE.UTF-8", "LANG": "fr_FR.UTF-8"}, "de"),
            ({"LC_ALL": "", "LC_CTYPE": "", "LANG": "fr_FR.UTF-8"}, "fr"),
            # Neither "C" nor an unsupported language names a wordlist.
            ({"LC_ALL": "C", "LC_CTYPE": "C", "LANG": "C"}, "en"),
            ({"LC_ALL": "", "LC_CTYPE": "", "LANG": "xh_ZA.UTF-8"}, "en"),
        ]
        with mock.patch("dehyphenate.locale.getlocale", return_value=("C", "UTF-8")):
            for environment, expected in cases:
                with self.subTest(environment=environment):
                    pin_locale(self, dict.fromkeys(ENGLISH_LOCALE, "") | environment)

                    self.assertEqual(Dehyphenator().language, expected)

    def test_platform_locale_is_used_when_environment_has_no_language(self) -> None:
        pin_locale(self, dict.fromkeys(ENGLISH_LOCALE, ""))
        with mock.patch(
            "dehyphenate.locale.getlocale", return_value=("fr_FR", "UTF-8")
        ):
            self.assertEqual(Dehyphenator().language, "fr")

    def test_regional_tags_resolve_to_their_wordlist(self) -> None:
        for tag, expected in (("fr-CA", "fr"), ("pt-BR", "pt"), ("en_GB", "en")):
            with self.subTest(tag=tag):
                self.assertEqual(Dehyphenator(language=tag).language, expected)

    def test_unsupported_language_is_rejected_with_the_alternatives(self) -> None:
        for language in ("af", "cy", "xx"):
            with self.subTest(language=language):
                with self.assertRaises(ValueError) as caught:
                    Dehyphenator(language=language)

                self.assertIn(language, str(caught.exception))
                self.assertIn("fr", str(caught.exception))

    def test_advertised_cjk_languages_load_their_tokenizers(self) -> None:
        for language, text, expected in (
            ("ja", "日-本", "日本"),
            ("ko", "한-국", "한국"),
            ("zh", "中-国", "中国"),
        ):
            with self.subTest(language=language):
                self.assertEqual(dehyphenate(text, language=language), expected)

    def test_english_clitic_rule_does_not_apply_to_other_languages(self) -> None:
        english = Dehyphenator(language="en")
        german = Dehyphenator(language="de")

        self.assertGreater(english._word_strength("emmanuel's"), 0.0)
        self.assertEqual(german._word_strength("emmanuel's"), 0.0)

    def test_cli_language_flag_overrides_the_locale(self) -> None:
        pin_locale(self, ENGLISH_LOCALE)

        def run(*arguments: str) -> str:
            return run_cli(*arguments, input="wun-der-schön\n").stdout

        self.assertEqual(run(), "wun-der-schön\n")
        self.assertEqual(run("--language", "de"), "wunderschön\n")

    def test_cli_lists_available_languages(self) -> None:
        result = run_cli("--list-languages")

        self.assertIn("fr", result.stdout.split())
        self.assertIn("de", result.stdout.split())


if __name__ == "__main__":
    unittest.main()
