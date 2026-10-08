#!/usr/bin/env python3
"""Rehearse copy-keepers with a real, cooperatively quiesced synthetic writer."""
import argparse
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='postgres:17')
    args = parser.parse_args()
    name = 'retention-concurrency-' + uuid.uuid4().hex[:12]
    dest = ROOT / '.lab' / name
    dest.mkdir(parents=True)
    receipt = {'passed': False, 'image': args.image, 'checks': []}
    stop = threading.Event()
    writer = None
    failures = []
    committed = []
    created = False

    def sql(statement):
        r = subprocess.run(['docker', 'exec', '-i', name, 'psql', '-XAt',
                            '-U', 'postgres', '-v', 'ON_ERROR_STOP=1'],
                           input=statement, capture_output=True, text=True, timeout=30)
        if r.returncode:
            raise RuntimeError(r.stderr)
        return r.stdout.strip()

    def check(label, statement):
        if sql(statement) != 't':
            raise RuntimeError('FAIL: ' + label)
        receipt['checks'].append(label)

    def write(phase):
        try:
            while not stop.is_set():
                # Each operation has its own connection and one transaction.
                row = sql(f'''WITH e AS (
                    INSERT INTO event(keep,body) VALUES(true,'writer {phase}') RETURNING id),
                  q AS (INSERT INTO queue SELECT id,id FROM e),
                  p AS (INSERT INTO push SELECT id,id FROM e),
                  h AS (INSERT INTO history SELECT id,id FROM e)
                  INSERT INTO ledger SELECT id,'{phase}' FROM e RETURNING id;''')
                committed.append(row.splitlines()[0])
                time.sleep(0.02)
        except Exception as exc:
            failures.append(str(exc))

    def start_writer(phase):
        nonlocal writer
        stop.clear()
        writer = threading.Thread(target=write, args=(phase,), daemon=True)
        writer.start()

    def wait_for(count):
        deadline = time.monotonic()+30
        while len(committed) < count and not failures:
            if time.monotonic() > deadline:
                raise RuntimeError('Writer made insufficient progress')
            time.sleep(0.05)
        if failures:
            raise RuntimeError(failures[0])

    def quiesce():
        stop.set()
        if writer:
            writer.join(timeout=35)
            if writer.is_alive():
                raise RuntimeError('Writer did not acknowledge shutdown; cutover forbidden')
        if failures:
            raise RuntimeError(failures[0])

    try:
        subprocess.run(['docker','run','-d','--name',name,'--network','none',
                        '--label','project=aurora-retention-lab','--memory','1g','--cpus','2',
                        '--tmpfs','/var/lib/postgresql/data:rw,size=512m',
                        '-e','POSTGRES_HOST_AUTH_METHOD=trust',args.image],
                       check=True,capture_output=True)
        created = True
        for _ in range(60):
            if subprocess.run(['docker','exec',name,'pg_isready','-U','postgres'],capture_output=True).returncode==0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('Database startup timeout')
        receipt['engine'] = sql('SELECT version()')
        sql('''CREATE TABLE event(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, keep boolean NOT NULL, body text);
          CREATE TABLE queue(id bigint PRIMARY KEY,event_id bigint UNIQUE NOT NULL REFERENCES event(id));
          CREATE TABLE push(id bigint PRIMARY KEY,event_id bigint NOT NULL REFERENCES event(id));
          CREATE TABLE history(id bigint PRIMARY KEY,queue_id bigint NOT NULL);
          CREATE TABLE ledger(id bigint PRIMARY KEY,phase text);
          INSERT INTO event(keep,body) SELECT n>100,repeat(md5(n::text),32) FROM generate_series(1,200) n;
          INSERT INTO queue SELECT id,id FROM event;
          INSERT INTO push SELECT id,id FROM event;
          INSERT INTO history SELECT id,id FROM event;''')
        start_writer('before')
        wait_for(3)
        sql('CREATE TABLE naive_copy AS SELECT * FROM event WHERE keep')
        checkpoint = len(committed)
        wait_for(checkpoint+3)
        check('uncoordinated copy misses committed writes',
              'SELECT EXISTS(SELECT FROM ledger l LEFT JOIN naive_copy n USING(id) WHERE n.id IS NULL)')
        frozen_at = time.monotonic()
        quiesce()  # Finish pending transactions and close every modeled writer connection.
        before_count = len(committed)
        receipt['writes_before_cutover'] = before_count
        sql('''BEGIN;
          SET LOCAL lock_timeout='2s';
          LOCK TABLE event,queue,push,history IN ACCESS EXCLUSIVE MODE;
          CREATE TABLE next_event(LIKE event INCLUDING ALL);
          CREATE TABLE next_queue(LIKE queue INCLUDING ALL);
          CREATE TABLE next_push(LIKE push INCLUDING ALL);
          CREATE TABLE next_history(LIKE history INCLUDING ALL);
          INSERT INTO next_event OVERRIDING SYSTEM VALUE SELECT * FROM event WHERE keep;
          INSERT INTO next_queue SELECT q.* FROM queue q JOIN next_event e ON e.id=q.event_id;
          INSERT INTO next_push SELECT p.* FROM push p JOIN next_event e ON e.id=p.event_id;
          INSERT INTO next_history SELECT h.* FROM history h JOIN next_queue q ON q.id=h.queue_id;
          ALTER TABLE next_queue ADD FOREIGN KEY(event_id) REFERENCES next_event(id);
          ALTER TABLE next_push ADD FOREIGN KEY(event_id) REFERENCES next_event(id);
          SELECT setval(pg_get_serial_sequence('next_event','id'),(SELECT max(id) FROM event),true);
          ALTER TABLE event RENAME TO old_event; ALTER TABLE next_event RENAME TO event;
          ALTER TABLE queue RENAME TO old_queue; ALTER TABLE next_queue RENAME TO queue;
          ALTER TABLE push RENAME TO old_push; ALTER TABLE next_push RENAME TO push;
          ALTER TABLE history RENAME TO old_history; ALTER TABLE next_history RENAME TO history;
          COMMIT;''')
        receipt['quiesce_and_copy_seconds'] = round(time.monotonic()-frozen_at,4)
        start_writer('after')
        wait_for(before_count+3)
        quiesce()
        receipt['writes_after_cutover'] = len(committed)-before_count
        check('client acknowledgements match committed ledger',
              f'SELECT count(*)={len(committed)} FROM ledger')
        check('all acknowledged writes preserved across cutover', '''SELECT NOT EXISTS(
          SELECT FROM ledger l LEFT JOIN event e USING(id) LEFT JOIN queue q ON q.event_id=e.id
          LEFT JOIN push p ON p.event_id=e.id LEFT JOIN history h ON h.queue_id=q.id
          WHERE e.id IS NULL OR q.id IS NULL OR p.id IS NULL OR h.id IS NULL
             OR e.body IS DISTINCT FROM ('writer ' || l.phase))''')
        check('initial keepers preserved exactly', '''SELECT NOT EXISTS(
          (SELECT * FROM old_event WHERE id<=200 AND keep EXCEPT ALL SELECT * FROM event WHERE id<=200)
          UNION ALL (SELECT * FROM event WHERE id<=200 EXCEPT ALL SELECT * FROM old_event WHERE id<=200 AND keep))''')
        check('expired rows excluded from active tables', '''SELECT
          NOT EXISTS(SELECT FROM event WHERE NOT keep) AND
          NOT EXISTS(SELECT FROM queue WHERE event_id<=100) AND
          NOT EXISTS(SELECT FROM push WHERE event_id<=100) AND
          NOT EXISTS(SELECT FROM history WHERE queue_id<=100)''')
        check('new queue FK targets active parent', "SELECT EXISTS(SELECT FROM pg_constraint WHERE conrelid='queue'::regclass AND confrelid='event'::regclass)")
        check('old copy cannot support lossless rollback after resume', '''SELECT EXISTS(
          SELECT FROM ledger l LEFT JOIN old_event o USING(id) WHERE l.phase='after' AND o.id IS NULL)''')
        receipt['passed'] = True
    finally:
        stop.set()
        if writer:
            writer.join(timeout=35)
        if created:
            subprocess.run(['docker','rm','-f','-v',name],check=True,capture_output=True)
        (dest/'evidence.json').write_text(json.dumps(receipt,indent=2)+'\n')
        print(json.dumps(receipt,indent=2))
        print('Evidence:',dest/'evidence.json')


if __name__ == '__main__':
    main()
