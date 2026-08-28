# Dehyphenate

`dehyphenate` removes syllable hyphens while retaining compound-word
boundaries when there is lexical evidence for them. Unknown forms are left
unchanged rather than having potentially meaningful hyphens removed. Any of
the 42 languages `wordfreq` covers will do; the default follows your locale.

Use it as a standard-input to standard-output filter:

```console
$ printf 'Quar-ter-ly re-port, cli-ent-fac-ing!\n' | dehyphenate
Quarterly report, client-facing!
```

Or call the library API:

```python
from dehyphenate import dehyphenate

assert dehyphenate("rev-e-nue and work-place") == "revenue and workplace"
```

Use an editable installation while developing. Because the importable package
lives under `src/`, the tests exercise the installed package instead of an
in-tree copy that could hide packaging mistakes:

```console
python -m venv .venv
. .venv/bin/activate
python -m pip install --group dev -e .
python -m black --check .
python -m isort --check-only .
python -m mypy
python -m unittest discover -v
```

Black, isort, and mypy are configured in `pyproject.toml`. To apply the two
automatic formatters before repeating the checks, run `python -m black .` and
`python -m isort .`.

Use a regular installation for a final packaging check:

```console
python -m pip install .
python -m unittest discover -v
```

Process a stream without holding it in memory, or transform an arbitrary
iterable of chunks. Hyphen chains split across chunk boundaries are still
resolved as a whole:

```python
from dehyphenate import dehyphenate_stream, iter_dehyphenate

dehyphenate_stream(sys.stdin, sys.stdout)
assert "".join(iter_dehyphenate(["mar-", "ket-read", "-y"])) == "market-ready"
```

Which language the text is in decides which boundaries survive, so it is worth
being explicit when the input is not in your own locale's language. `wun-der-schön`
is one German word but lacks enough evidence in an English wordlist:

```console
$ echo 'wun-der-schön' | dehyphenate --language de
wunderschön
$ echo 'wun-der-schön' | dehyphenate --language en
wun-der-schön
$ dehyphenate --list-languages
ar bg bn ca cs da de el en es fa fi fil fr he hi hu id is it ja ko lt lv mk ms nb nl pl pt ro ru sh sk sl sv ta tr uk ur vi zh
```

With no language given, `LC_ALL`, `LC_CTYPE`, and `LANG` are consulted in that
order, followed by the platform's current `LC_CTYPE` locale. English is used if
none of them names a supported language. Every entry point takes the same
`language` argument, and a regional tag resolves to its wordlist, so `fr-CA`
and `pt-BR` work as well as `fr` and `pt`:

```python
from dehyphenate import dehyphenate

assert dehyphenate("au-jour-d'hui", language="fr") == "aujourd'hui"
assert dehyphenate("wun-der-schön", language="de") == "wunderschön"
```

An unsupported language raises `ValueError` listing the ones that are
available, rather than quietly falling back to a nearest match the way
`wordfreq` does on its own.

Vocabulary the frequency list does not cover is retained rather than guessed
at. By default a spelling needs a Zipf frequency of at least 2.5. Supply your
own vocabulary to resolve rarer words, or raise `minimum_zipf` to demand
stronger evidence before a spelling counts as attested:

```python
from dehyphenate import Dehyphenator

engine = Dehyphenator(additional_words={"revops"})
assert engine.dehyphenate("Rev-Ops") == "RevOps"
```

Accented vocabulary is treated no differently from ASCII — `ré-su-mé` becomes
`résumé` and `café-man-ag-er` becomes `café-manager` — and an accent
compares equal however it was composed. An `additional_words` entry must be
letters, optionally separated by apostrophes; anything else raises
`ValueError` rather than being dropped unnoticed.
