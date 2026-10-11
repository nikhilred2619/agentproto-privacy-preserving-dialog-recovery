from __future__ import annotations

import pytest

from agentproto_recovery.evaluation import MODES, Deployment

ALL_MODES = list(MODES)
# Modes in which a replacement gateway has some way to find the dialog.
RECOVERABLE = ["global-store", "global-endpoint", "scoped-store", "scoped-endpoint-ticket",
               "scoped-endpoint-medticket"]
STORE_MODES = ["global-store", "scoped-store"]


@pytest.fixture
def make(tmp_path):
    made: list[Deployment] = []

    def _make(mode: str, **kw) -> Deployment:
        d = Deployment(mode, tmp_path / f"{mode}-{len(made)}", **kw)
        made.append(d)
        return d

    yield _make
    for d in made:
        d.close()
