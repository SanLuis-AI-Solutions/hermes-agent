"""An ``unknown`` admission is re-admitted only when its own managed worker is gone.

The owner writes ``session_admissions.status='unknown'`` when a managed worker child dies
(``gateway/session_worker_reservation.lose_admission_worker``) and handed that dead row back to
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

import pytest

import hermes_state_runtime as rt
from hermes_state import SessionDB

SESSION = 'canary'
PRINCIPAL = 'local:owner'
REQUEST = 'kanban:["board","t_1",7]'
TEXT = 'work kanban task t_1'
WORKER_KEY = 'gateway.admission_worker.v1.'
TOMBSTONE_KEY = 'gateway.terminal_admission.v1.'


def _digest():
    return rt.admission_fingerprint(canonical_target=SESSION,
                                    payload={'input': {'text': TEXT}, 'intent': 'queue'})


def _db(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session(SESSION, source='cli')
    return db, rt.begin_runtime_epoch(db, instance_id='owner')


def _seed_unknown(db, epoch, *, pid, birth):
    """The scar exactly as the loss path leaves it, plus the worker the owner reserved for it."""
    admission_id = 'adm-' + os.urandom(6).hex()

    def write(conn):
        conn.execute(
            "INSERT INTO session_admissions(admission_id,request_id,principal_id,target_session_id,"
            "lineage_json,payload_json,payload_digest,intent,status,owner_epoch,generation) "
            "VALUES(?,?,?,?,?,?,?,?,'unknown',?,?)",
            (admission_id, REQUEST, PRINCIPAL, SESSION, json.dumps([SESSION]), json.dumps({'text': TEXT}),
             _digest(), 'queue', epoch, 0))
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                     (WORKER_KEY + admission_id, json.dumps({'pid': pid, 'birth': birth})))
    db._execute_write(write)
    return admission_id


def _submit(db, epoch):
    return rt.admit_session_input(db, epoch=epoch, principal_id=PRINCIPAL, session_id=SESSION,
                                  request_id=REQUEST, payload={'text': TEXT})


def _counts(db):
    with db._read_ctx() as conn:
        return (conn.execute('SELECT COUNT(*) FROM session_admissions').fetchone()[0],
                conn.execute('SELECT COUNT(*) FROM state_meta WHERE key LIKE ?', (TOMBSTONE_KEY + '%',)).fetchone()[0])


def _dead_pid():
    """A pid this host has just run and reaped: psutil sees NoSuchProcess for it."""
    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait()
    return proc.pid


def test_dead_worker_admission_is_superseded_by_a_fresh_one(tmp_path):
    db, epoch = _db(tmp_path)
    try:
        from hermes_cli.process_identity import worker_birth
        import psutil
        old_id = _seed_unknown(db, epoch, pid=_dead_pid(), birth=worker_birth(psutil.Process(os.getpid())))
        assert rt.get_session_admission(db, admission_id=old_id)['status'] == 'unknown'

        row = _submit(db, epoch)
        assert row['admission_id'] != old_id, 'the dead admission was handed back instead of re-admitted'
        claimed = rt.claim_session_input(db, epoch=epoch, session_id=SESSION)
        assert claimed['admission_id'] == row['admission_id']
        assert claimed['status'] == 'started'
        assert claimed['owner_epoch'] == epoch
        # The dead row stays on the record, still unknown, but no longer blocks the FIFO.
        assert rt.get_session_admission(db, admission_id=old_id)['status'] == 'unknown'
        assert _counts(db) == (1, 1)
    finally:
        db.close()


def test_live_worker_admission_is_returned_unchanged(tmp_path):
    db, epoch = _db(tmp_path)
    try:
        from hermes_cli.process_identity import worker_birth
        import psutil
        old_id = _seed_unknown(db, epoch, pid=os.getpid(), birth=worker_birth(psutil.Process(os.getpid())))

        before = _counts(db)
        row = _submit(db, epoch)
        assert row['admission_id'] == old_id, 'a live worker\'s admission must not be superseded'
        assert row['status'] == 'unknown'
        assert _counts(db) == before, 'nothing may be written for a live worker'
        with pytest.raises(rt.RuntimeStoreError) as refusal:
            rt.claim_session_input(db, epoch=epoch, session_id=SESSION)
        assert refusal.value.reason == 'unknown_execution'
    finally:
        db.close()
