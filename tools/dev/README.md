# Developer helpers

Small scripts that print summaries instead of full output, so agents and reviewers keep their context small.

| Script | What it prints |
|---|---|
| `test_summary.py [pytest args]` | The totals line and one line per failing test. Exit code is pytest's. |

```bash
python tools/dev/test_summary.py                               # whole suite
python tools/dev/test_summary.py tests/test_signal_integrity.py
python tools/dev/test_summary.py -k phone
```
