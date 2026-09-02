"""Shared pytest configuration.

Defaults the batch grader to SERIAL (BATCH_SHEET_CONCURRENCY=1) for every test. The existing batch
tests stub full_evaluate / resume_after_orientation IN-PROCESS, so they must not accidentally take the
subprocess-based concurrent path just because .env turns concurrency on in production. `_sheet_concurrency`
reads os.environ first, so this pin wins over the .env default; tests that exercise concurrency override
it with their own monkeypatch.setenv (same monkeypatch instance -> last write wins).

Also sweeps the empty directories the batch tests leave in the REAL output/ tree (see
_clean_empty_output_dirs)."""
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")


@pytest.fixture(autouse=True)
def _default_serial_batch(monkeypatch):
    monkeypatch.setenv("BATCH_SHEET_CONCURRENCY", "1")


@pytest.fixture(scope="session", autouse=True)
def _clean_empty_output_dirs():
    """Remove the empty dirs the batch tests leave behind in the real `output/` tree.

    `batch_evaluate`/`batch_prepare_orientation` build their paths from `batch_evaluator.PROJECT_ROOT`
    and `os.makedirs` them before any stubbing takes effect, so tests using ids like "batch_test" /
    "batch_x" / "b" mkdir straight into production `output/`. Patching PROJECT_ROOT session-wide is not
    an option -- `_dotenv_raw` resolves the real `.env` through the same constant.

    Deliberately removes ONLY empty directories, via rmdir, which refuses a non-empty target. Real
    evaluation runs always contain files, so this physically cannot delete graded work even if a test
    id ever collided with a real run_id.
    """
    yield
    if not os.path.isdir(OUTPUT_DIR):
        return
    # Bottom-up: an inner dir must go before its now-empty parent can.
    for root, dirs, _files in os.walk(OUTPUT_DIR, topdown=False):
        if root == OUTPUT_DIR:
            continue
        try:
            os.rmdir(root)          # no-op (OSError) unless genuinely empty
        except OSError:
            pass
