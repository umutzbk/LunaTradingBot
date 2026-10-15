"""V7 test configuration: every guarded research entry point refuses to run without a K9 refusal log
(``luna.v7.k9.require``, Task 25 review I-3), so the whole V7 test session runs with one configured in a
pytest tmp directory (never the repository, the user's home or var/). Session scope, so module-scoped fixtures
that build providers / run books are covered too; tests that need their own log use ``k9.using``."""

from __future__ import annotations

import pytest

from luna.v7 import k9


@pytest.fixture(autouse=True, scope="session")
def _k9_session_log(tmp_path_factory):
    with k9.using(k9.K9Log(tmp_path_factory.mktemp("k9_session"))) as log:
        yield log
