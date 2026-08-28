# Contributing

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
