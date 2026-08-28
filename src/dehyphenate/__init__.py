#!/usr/bin/env python3
"""Reverse syllable hyphens while retaining lexical compound hyphens.

Dehyphenation is necessarily a lexical operation because the same ``-`` is
used for both a syllable boundary and a spelling boundary.  This module uses
word frequencies to resolve that ambiguity conservatively, in whichever of
``wordfreq``'s languages the caller names or the locale implies.
"""

from __future__ import annotations

import argparse
import locale
import math
import os
import sys
import unicodedata
from collections.abc import Iterable, Iterator
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from io import TextIOWrapper
from typing import NamedTuple, TextIO

import regex as re
from langcodes import closest_supported_match
from langcodes.tag_parser import LanguageTagError
from wordfreq import available_languages, zipf_frequency
from wordfreq.tokens import tokenize

__all__ = [
    "Dehyphenator",
    "dehyphenate",
    "dehyphenate_stream",
    "iter_dehyphenate",
]

try:
    # Named explicitly rather than taken from ``__name__``, which is not the
    # distribution name when this module is imported as ``__main__``.
    # pyproject.toml is the one place the number is written down.
    __version__ = version("dehyphenate")
except PackageNotFoundError:  # running from a source tree with no install
    __version__ = "0+unknown"

# What a word is made of, written once.  Every pattern and predicate below is
# derived from these pieces so they cannot drift apart, and they had: a
# separately hand-written predicate admitted the combining marks the token
# pattern rejects and rejected the numerals it accepts, which was enough to
# make a chunk boundary change the streaming API's output.
_LETTER = r"\p{L}"
_LETTER_OR_MARK = rf"(?:{_LETTER}|\p{{M}})"
_APOSTROPHES = "'\N{RIGHT SINGLE QUOTATION MARK}"
_TOKEN_CHARACTER = rf"(?:{_LETTER_OR_MARK}|[{_APOSTROPHES}])"
# Whole word-like runs, hyphenated or not.  Making the hyphenated tail optional
# looks wasteful but avoids a quadratic blowup: with the tail required, a long
# unhyphenated run backtracks through every length at every start position,
# which costs 1.5 s for an 8 kB run of letters and grows fourfold per doubling.
_TOKEN_RUN_RE = re.compile(rf"{_TOKEN_CHARACTER}+(?:-{_TOKEN_CHARACTER}+)*")
# A hyphen continues a run, so it too holds output back in the streaming path.
_TOKEN_CHARACTER_RE = re.compile(rf"{_TOKEN_CHARACTER}|-")
# Candidates reach the lexicon only through ``_normalize_word``, which has
# already folded every apostrophe to the ASCII one, so this spells just that.
_LEXICAL_FORM_RE = re.compile(
    rf"{_LETTER}{_LETTER_OR_MARK}*(?:'{_LETTER}{_LETTER_OR_MARK}*)*"
)
# Everything in _APOSTROPHES but the ASCII one, which is what they fold to.
_FOLDED_APOSTROPHES = _APOSTROPHES[1:]
_FALLBACK_LANGUAGE = "en"
_LOCALE_VARIABLES = ("LC_ALL", "LC_CTYPE", "LANG")
# Clitics worth trying as evidence for the word they attach to.  Only English
# is listed, so only English gets the benefit: Dutch ``'s``, the Scandinavian
# s-final genitive, and Turkish's productive apostrophe-separated suffixes are
# all missed, and want a rule rather than more literal suffixes.
_CLITIC_SUFFIXES = {"en": ("'s",)}
_DEFAULT_MINIMUM_ZIPF = 2.5
# Above the Zipf ceiling ("the" is 7.73), so a supplied word outranks any one
# attestation.  It does not outrank a *sum* of them, so a supplied word can
# still lose to a two-word split; fixing that needs a signal of its own rather
# than a bigger number.
_ADDITIONAL_WORD_STRENGTH = 9.0
# No wordlist entry comes close: the longest in any of wordfreq's languages is
# 80 characters.  Bounding the candidate length keeps the segmentation search
# linear in the number of chunks rather than cubic, which is the difference
# between 0.4 s and 37 s on a single 8 kB hyphen chain.
_MAXIMUM_WORD_LENGTH = 128
# Both per-engine caches are dropped wholesale once full, as ``wordfreq`` does
# with its own.  Text is locally repetitive, so a cold cache refills within a
# few kilobytes, whereas unbounded growth retained about 16 bytes per input
# character forever: 190 MB of caches to stream 12 MB past.
_MAXIMUM_CACHED_WORDS = 100_000
_MAXIMUM_CACHED_CHAINS = 50_000


def _normalize_word(text: str, language: str | None = None) -> str:
    """Fold a word to the spelling used as a lexicon and cache key.

    Composing both before and after case folding means an accented letter
    compares equal however it was typed.
    """

    folded = unicodedata.normalize("NFC", text)
    for apostrophe in _FOLDED_APOSTROPHES:
        folded = folded.replace(apostrophe, "'")
    # Python's language-neutral casefold turns Turkish dotted I into ``i``
    # plus a combining dot, which is not a spelling in wordfreq's Turkish
    # lexicon.  Apply Turkish casing before the otherwise language-neutral
    # fold, then compose marks introduced by folding as well.
    if language == "tr":
        folded = folded.replace("I", "ı").replace("İ", "i")
    return unicodedata.normalize("NFC", folded.casefold())


@lru_cache(maxsize=1)
def _supported_languages() -> tuple[str, ...]:
    return tuple(sorted(available_languages()))


# Cached because it is a pure function of the tag, and because anything but a
# bare language costs langcodes about 66 us -- which, since POSIX locales are
# always regional, otherwise dominated every call to the functions below.
@lru_cache
def _match_language(tag: str) -> str | None:
    """Map a language tag or POSIX locale onto a wordlist, or ``None``."""

    # "fr_FR.UTF-8" and "de_DE@euro" are locales rather than language tags.
    base = tag.split(".")[0].split("@")[0].replace("_", "-")
    supported = _supported_languages()
    # A regional tag may be too far from any wordlist to match while its bare
    # language is exact, as for "zh-TW" against "zh".
    for candidate in (base, base.partition("-")[0]):
        try:
            match = closest_supported_match(candidate, supported, max_distance=0)
        except LanguageTagError:
            return None
        if match is not None:
            return match
    return None


def _resolve_language(tag: str) -> str:
    """Map a caller-supplied language onto a wordlist, or explain why not."""

    match = _match_language(tag)
    if match is None:
        raise ValueError(
            f"no word frequencies for language {tag!r}; available: "
            + ", ".join(_supported_languages())
        )
    return match


def _default_language() -> str:
    """The wordlist for the caller's environment or platform locale.

    Deliberately not cached: a process may change its environment, and the
    per-tag lookup underneath is memoized already.
    """

    for name in _LOCALE_VARIABLES:
        value = os.environ.get(name)
        # "C" and "POSIX" name no human language, and neither does "".
        if value and (match := _match_language(value)) is not None:
            return match

    # POSIX locale variables are commonly absent on Windows.  LC_CTYPE is
    # initialized by Python at startup and can therefore supply the platform
    # locale without the process-global mutation that setlocale() would cause.
    platform_language, _encoding = locale.getlocale(locale.LC_CTYPE)
    if platform_language and (match := _match_language(platform_language)) is not None:
        return match
    return _FALLBACK_LANGUAGE


def _resolved_language(language: str | None) -> str:
    """The wordlist ``language`` names, or the one the environment implies."""

    return _default_language() if language is None else _resolve_language(language)


def _is_token_character(character: str) -> bool:
    return _TOKEN_CHARACTER_RE.fullmatch(character) is not None


class _Segmentation(NamedTuple):
    groups: tuple[str, ...]
    strength: float
    case_boundaries: int

    @property
    def score(self) -> tuple[int, float, int]:
        # Fewest groups first, then strongest lexical evidence, then the most
        # capitalized boundaries.  An exact tie keeps whichever came first.
        return (len(self.groups), -self.strength, -self.case_boundaries)


class Dehyphenator:
    """Remove syllable hyphens and infer which lexical hyphens must remain."""

    def __init__(
        self,
        *,
        language: str | None = None,
        minimum_zipf: float = _DEFAULT_MINIMUM_ZIPF,
        additional_words: Iterable[str] = (),
    ) -> None:
        self.language = _resolved_language(language)
        if not math.isfinite(minimum_zipf) or minimum_zipf < 0:
            raise ValueError("minimum_zipf must be a finite non-negative number")
        self.minimum_zipf = float(minimum_zipf)

        accepted: set[str] = set()
        rejected: list[str] = []
        for word in additional_words:
            normalized = _normalize_word(word, self.language)
            if _LEXICAL_FORM_RE.fullmatch(normalized):
                accepted.add(normalized)
            else:
                rejected.append(word)
        if rejected:
            raise ValueError(
                "additional_words entries must be letters, optionally "
                "separated by apostrophes: "
                + ", ".join(repr(word) for word in sorted(rejected))
            )
        self.additional_words = frozenset(accepted)
        # A supplied word may be longer than anything in a wordlist, so the
        # search bound has to stretch to cover it.
        self._longest_candidate = max(
            [_MAXIMUM_WORD_LENGTH, *(len(word) for word in self.additional_words)]
        )
        self._strength_cache: dict[str, float] = {}
        self._chain_cache: dict[str, str] = {}

    def _word_strength(self, candidate: str) -> float:
        normalized = _normalize_word(candidate, self.language)
        cached = self._strength_cache.get(normalized)
        if cached is not None:
            return cached

        strength = 0.0
        if _LEXICAL_FORM_RE.fullmatch(normalized):
            # A clitic is evidence for the word it attaches to.  A plural
            # possessive never reaches here: the pattern above requires a
            # letter after every apostrophe, so ``s'`` is not a lexical form.
            forms = [normalized]
            for suffix in _CLITIC_SUFFIXES.get(self.language, ()):
                if normalized.endswith(suffix):
                    forms.append(normalized[: -len(suffix)])
            for form in forms:
                if form in self.additional_words:
                    strength = max(strength, _ADDITIONAL_WORD_STRENGTH)
                # zipf_frequency tokenizes its input and otherwise reports the
                # score of whatever survives.  Only an unchanged, single token
                # is evidence for this exact candidate spelling.
                if tokenize(form, self.language) == [form]:
                    frequency = zipf_frequency(form, self.language, wordlist="best")
                    if frequency >= self.minimum_zipf:
                        strength = max(strength, frequency)

        if len(self._strength_cache) >= _MAXIMUM_CACHED_WORDS:
            self._strength_cache.clear()
        self._strength_cache[normalized] = strength
        return strength

    def _segment_known_words(self, chunks: list[str]) -> _Segmentation | None:
        """Find the smallest all-known lexical segmentation of a hyphen chain."""

        # Extending a prefix adds the same group, strength, and boundary to
        # every candidate, so ``score`` order is preserved and keeping only the
        # best segmentation reaching each index is sufficient.
        best: list[_Segmentation | None] = [None] * (len(chunks) + 1)
        best[0] = _Segmentation((), 0.0, 0)

        for start in range(len(chunks)):
            previous = best[start]
            if previous is None:
                continue
            case_boundary = int(start > 0 and chunks[start][0].isupper())
            candidate = ""
            for finish in range(start + 1, len(chunks) + 1):
                candidate += chunks[finish - 1]
                # Candidates only grow, so nothing further can be a word.
                if len(candidate) > self._longest_candidate:
                    break
                strength = self._word_strength(candidate)
                if not strength:
                    continue
                current = _Segmentation(
                    groups=previous.groups + (candidate,),
                    strength=previous.strength + strength,
                    case_boundaries=previous.case_boundaries + case_boundary,
                )
                existing = best[finish]
                if existing is None or current.score < existing.score:
                    best[finish] = current

        return best[-1]

    def _dehyphenate_chain(self, chain: str) -> str:
        chunks = chain.split("-")
        joined = "".join(chunks)

        # A fast path, not a policy: "fewest groups first" means the search
        # below reaches the same one-group answer, four times more slowly.
        # Resolving ``first-fruits`` to ``firstfruits`` is that rule's doing.
        if self._word_strength(joined):
            return joined

        segmentation = self._segment_known_words(chunks)
        if segmentation is not None:
            return "-".join(segmentation.groups)

        # With no lexical evidence, retaining every boundary is the
        # conservative choice: false syllable boundaries remain visible, but
        # a genuine spelling boundary is never silently discarded.
        return chain

    def _replace_token_run(self, match: re.Match[str]) -> str:
        run = match.group()
        if "-" not in run:
            return run
        # Text repeats its vocabulary, and a chain needing the full
        # segmentation search costs tens of microseconds against well under one
        # for a cache hit.
        resolved = self._chain_cache.get(run)
        if resolved is None:
            resolved = self._dehyphenate_chain(run)
            if len(self._chain_cache) >= _MAXIMUM_CACHED_CHAINS:
                self._chain_cache.clear()
            self._chain_cache[run] = resolved
        return resolved

    def dehyphenate(self, text: str) -> str:
        """Dehyphenate a fixed string while preserving all other text."""

        return _TOKEN_RUN_RE.sub(self._replace_token_run, text)


@lru_cache(maxsize=None)
def _shared_dehyphenator(language: str) -> Dehyphenator:
    """The process-wide engine for one resolved language, built at most once.

    Callers resolve the language before looking one up, so that every spelling
    of a language shares a single engine, and with it a single warmed cache.
    """

    return Dehyphenator(language=language)


def dehyphenate(text: str, *, language: str | None = None) -> str:
    """Dehyphenate a fixed string using word frequencies."""

    return _shared_dehyphenator(_resolved_language(language)).dehyphenate(text)


def iter_dehyphenate(
    chunks: Iterable[str], *, language: str | None = None
) -> Iterator[str]:
    """Yield dehyphenated output incrementally from arbitrary text chunks.

    Output is held back at the last token boundary so that a hyphen chain
    straddling two chunks is still seen whole.
    """

    engine = _shared_dehyphenator(_resolved_language(language))
    pending = ""
    for chunk in chunks:
        # Whatever is pending is all token characters by construction, so only
        # the new chunk has to be scanned.  Re-walking the whole buffer every
        # time made a token spanning many chunks quadratic.
        boundary = len(chunk)
        while boundary and _is_token_character(chunk[boundary - 1]):
            boundary -= 1
        if boundary:
            yield engine.dehyphenate(pending + chunk[:boundary])
            pending = chunk[boundary:]
        else:
            pending += chunk
    if pending:
        yield engine.dehyphenate(pending)


def dehyphenate_stream(
    source: TextIO,
    destination: TextIO,
    *,
    chunk_size: int = 8192,
    language: str | None = None,
) -> None:
    """Read, reverse, and write a text stream without buffering it in full."""

    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    chunks = iter(lambda: source.read(chunk_size), "")
    for output in iter_dehyphenate(chunks, language=language):
        destination.write(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Remove syllable hyphens as a stdin-to-stdout filter."
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--language",
        metavar="TAG",
        help="language of the input, as a tag such as fr or pt-BR "
        "(default: from the locale, otherwise English)",
    )
    parser.add_argument(
        "--list-languages",
        action="store_true",
        help="print the languages word frequencies are available for and exit",
    )
    arguments = parser.parse_args(argv)

    if arguments.list_languages:
        print(" ".join(_supported_languages()))
        return 0

    try:
        if isinstance(sys.stdin, TextIOWrapper):
            sys.stdin.reconfigure(errors="surrogateescape")
        if isinstance(sys.stdout, TextIOWrapper):
            sys.stdout.reconfigure(errors="surrogateescape")
        dehyphenate_stream(sys.stdin, sys.stdout, language=arguments.language)
        # Flush here so that a closed downstream reader is reported inside this
        # block.  Buffered output otherwise fails first in the interpreter's
        # own shutdown flush, which no caller is in a position to catch.
        sys.stdout.flush()
    except BrokenPipeError:
        # That shutdown flush still runs and would re-raise on the dead pipe,
        # turning an ordinary downstream ``head`` into exit status 120 and a
        # spurious traceback.  Give the buffer somewhere harmless to drain.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (UnicodeDecodeError, ValueError) as error:
        parser.error(str(error))
    return 0
