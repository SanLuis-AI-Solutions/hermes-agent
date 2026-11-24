"""An ``unknown`` admission receipt costs the worker one re-submission, not the attempt.

A dead managed-worker child leaves the attempt's admission row at ``status='unknown'``
(``gateway/session_worker_reservation.py::lose_admission_worker``). The worker client used to read
that scar on its next receipt and raise from ``_exit_code``, so every attempt that touched the
admission died with ``Kanban execution unknown; retained by owner``. These tests pin the client's
behaviour against a fake owner: one redial and a normal finish when the owner answers ``unknown``
once, and exactly one redial — never a loop — when it answers ``unknown`` twice.
"""
import asyncio
import contextlib

import pytest

from hermes_cli import gateway_client, gateway_owner_restart
from hermes_cli import kanban_worker_client

_PARAMS = {'board': 'test-board', 'task_id': 't_00000000', 'run_id': 1, 'claim_lock': 'host:1'}
_BOARD_DB = 'C:/fake/board.db'


class _FakeOwner:
    """The owner as the client sees it: one ``kanban.run`` answer per submission, then receipts."""

    def __init__(self, submissions, receipts=None):
        self._submissions = list(submissions)  # (admission_id, status) per ``kanban.run``
        self._receipts = {k: list(v) for k, v in (receipts or {}).items()}
        self.events = asyncio.Queue()
        self.runs = []  # admission ids handed back by ``kanban.run``, in order
        self.receipts_read = []  # (admission_id, session_id) per ``prompt.receipt``

    async def rpc(self, method, **params):
        if method == 'kanban.run':
            admission_id, status = self._submissions[len(self.runs)]
            self.runs.append(admission_id)
            return {'session_id': f'session-{admission_id}',
                    'receipt': {'admission_id': admission_id, 'status': status}}
        assert method == 'prompt.receipt', method
        self.receipts_read.append((params['admission_id'], params['session_id']))
        statuses = self._receipts[params['admission_id']]
        return {'admission_id': params['admission_id'], 'status': statuses.pop(0)}


@pytest.fixture
def fake_owner(monkeypatch):
    def install(owner):
        @contextlib.asynccontextmanager
        async def connect_gateway():
            yield owner
        monkeypatch.setattr(gateway_client, 'connect_gateway', connect_gateway)
        monkeypatch.setattr(gateway_client, 'outside_restart_wait', contextlib.nullcontext)
        monkeypatch.setattr(gateway_owner_restart, 'restartable', lambda exc: False)
        monkeypatch.delenv('HERMES_TUI_GATEWAY_URL', raising=False)
        return owner
    return install


def test_unknown_receipt_redials_once_and_the_attempt_continues(fake_owner, monkeypatch):
    owner = fake_owner(_FakeOwner([('adm-unknown', 'unknown'), ('adm-good', 'started')],
                                  {'adm-good': ['started', 'terminal']}))
    monkeypatch.setattr('gateway.session_kanban.worker_result',
                        lambda db, params: {'exit_code': 0})

    assert asyncio.run(kanban_worker_client.run(_PARAMS, _BOARD_DB)) == 0

    # The redial re-submitted (a second ``kanban.run``), and the unknown admission was never
    # re-read as if it could answer differently.
    assert owner.runs == ['adm-unknown', 'adm-good']
    assert {admission for admission, _ in owner.receipts_read} == {'adm-good'}


def test_two_unknown_receipts_still_fail_the_worker(fake_owner):
    owner = fake_owner(_FakeOwner([('adm-unknown', 'unknown'), ('adm-unknown', 'unknown')]))

    with pytest.raises(gateway_client.GatewayClientError) as caught:
        asyncio.run(kanban_worker_client.run(_PARAMS, _BOARD_DB))

    assert 'execution unknown' in str(caught.value)
    # Exactly one redial: a third dial would be a retry loop, not a second chance.
    assert owner.runs == ['adm-unknown', 'adm-unknown']
    assert owner.receipts_read == []
