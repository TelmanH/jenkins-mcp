# Contributing

Thanks for helping! Setup, tests and the live Jenkins are described in [tests/README.md](tests/README.md).

1. Open an issue first for larger changes, so we can agree on the approach.
2. Keep the server **read-only**: no tool may change Jenkins. New Jenkins tools use
   `@mcp.tool(annotations=READ_ONLY)` and only `Instance.get`/`Instance.json`.
3. Add tests: offline (`tests/test_*.py`, fake Jenkins in `tests/fake_jenkins.py`); for behaviour that
   depends on real Jenkins also a check in `tests/live/live_test.py`.
4. Before opening a pull request: `pytest -q` and `mypy src` pass.
5. Never commit real Jenkins URLs, logs, tokens or knowledge-base entries of your company. Test data must be
   synthetic (`example.com`, made-up names).

Security problems: see [SECURITY.md](SECURITY.md).
