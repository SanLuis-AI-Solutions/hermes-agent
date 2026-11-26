"""An ``unknown`` admission is re-admitted only when its own managed worker is gone.

The owner writes ``session_admissions.status='unknown'`` when a managed worker child dies
(``persistence.sqlite.workers._gateway.lose_admission_worker``) and handed that dead row back to
every later submission of the same request, so the worker read a dead receipt and exited. These
tests pin both halves of the repair: a dead worker's row is superseded by a fresh admission for
the same request, and a live worker's row is returned exactly as before.

The worker record the owner writes at reservation is a durable fact
(``state_meta['gateway.admission_worker.v1.<admission_id>'] = {"pid", "birth"}``); this test seeds
it directly with a real dead pid and a real live pid, so no psutil stub can decide the verdict.
"""
import json
import os
import subprocess
import sys

import psutil
import pytest

from hermes_cli.process_identity import worker_birth
from persistence.conversation.sessions import NewSession
from persistence.core.errors import AuthorityRefusal
from persistence.sqlite.authority._admission_store import (
    admit_session_input, claim_session_input, get_session_admission,
)
from persistence.sqlite.authority._runtime import ADMISSION_WORKER_PREFIX
from persistence.sqlite.authority._runtime_ends import begin_runtime_epoch
from persistence.sqlite.conversation._store.terminal import ADMISSION_PREFIX

SESSION = 's'
PRINCIPAL = 'local:owner'
REQUEST = 'kanban:["board","t_1",7]'
TEXT = 'work kanban task t_1'
WORKER_KEY = ADMISSION_WORKER_PREFIX
TOMBSTONE_KEY = ADMISSION_PREFIX


def _holder(store):
    store.sessions.create(NewSession(session_id=SESSION, source='test'))
    return store.holder()


def _seed_unknown(holder, epoch, *, pid, birth):
    """The scar exactly as the loss path leaves it: an admitted attempt whose worker died."""
    row, created = admit_session_input(holder, epoch=epoch, principal_id=PRINCIPAL, session_id=SESSION,
                                       request_id=REQUEST, payload={'text': TEXT})
    assert created is True

    def write(conn):
        conn.execute('INSERT OR REPLACE INTO state_meta(key,value) VALUES(?,?)',
                     (WORKER_KEY + row['admission_id'], json.dumps({'pid': pid, 'birth': birth})))
        conn.execute("UPDATE session_admissions SET status='unknown',generation=0 WHERE admission_id=?",
                     (row['admission_id'],))
    holder._execute_write(write)
    return row['admission_id'], row['seq']


def _submit(holder, epoch):
    return admit_session_input(holder, epoch=epoch, principal_id=PRINCIPAL, session_id=SESSION,
                               request_id=REQUEST, payload={'text': TEXT})


def _counts(holder):
    with holder._read_ctx() as conn:
        return (conn.execute('SELECT COUNT(*) FROM session_admissions').fetchone()[0],
                conn.execute('SELECT COUNT(*) FROM state_meta WHERE key LIKE ?',
                             (TOMBSTONE_KEY + '%',)).fetchone()[0],
                conn.execute('SELECT COUNT(*) FROM state_meta WHERE key LIKE ?',
                             (WORKER_KEY + '%',)).fetchone()[0])


def _dead_pid():
    """A pid this host has just run and reaped: psutil sees NoSuchProcess for it."""
    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait()
    return proc.pid


def test_dead_worker_admission_is_superseded_by_a_fresh_one(store):
    holder = _holder(store)
    epoch = begin_runtime_epoch(holder, instance_id='boot')
    old_id, old_seq = _seed_unknown(holder, epoch, pid=_dead_pid(),
                                    birth=worker_birth(psutil.Process(os.getpid())))
    assert get_session_admission(holder, admission_id=old_id)['status'] == 'unknown'

    row, created = _submit(holder, epoch)
    assert created is True, 'the dead admission was handed back instead of re-admitted'
    assert row['admission_id'] != old_id
    assert row['status'] == 'queued'
    assert row['seq'] == old_seq, 'the successor must keep the dead attempt FIFO place'
    claimed = claim_session_input(holder, epoch=epoch, session_id=SESSION)
    assert claimed['admission_id'] == row['admission_id']
    assert claimed['status'] == 'started'
    assert claimed['owner_epoch'] == epoch
    # The dead row stays on the record, still unknown, but no longer blocks the FIFO.
    assert get_session_admission(holder, admission_id=old_id)['status'] == 'unknown'
    assert _counts(holder) == (1, 1, 0), 'tombstone kept, stale worker witness dropped'


def test_live_worker_admission_is_returned_unchanged(store):
    holder = _holder(store)
    epoch = begin_runtime_epoch(holder, instance_id='boot')
    old_id, _ = _seed_unknown(holder, epoch, pid=os.getpid(),
                              birth=worker_birth(psutil.Process(os.getpid())))

    before = _counts(holder)
    row, created = _submit(holder, epoch)
    assert created is False
    assert row['admission_id'] == old_id, "a live worker's admission must not be superseded"
    assert row['status'] == 'unknown'
    assert _counts(holder) == before, 'nothing may be written for a live worker'
    with pytest.raises(AuthorityRefusal) as refusal:
        claim_session_input(holder, epoch=epoch, session_id=SESSION)
    assert refusal.value.reason == 'unknown_execution'
