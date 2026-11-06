"""Research batches and immutable AI jobs, sharing the existing local library."""
import json
import sqlite3
import time
import uuid
import unicodedata
from pathlib import Path
from datetime import datetime, timedelta, timezone

from .library_store import LibraryStore

ROOT = Path(__file__).resolve().parents[2]
HOME = ROOT.parent / 'library'
PLATFORMS = {'xhs': '小红书', 'dy': '抖音', 'bili': 'B站'}
COUNTERS = ('hits', 'checked', 'new_posts', 'old_posts', 'new_comments', 'duplicate_comments', 'failures')


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def now():
    return int(time.time() * 1000)


def title_key(value):
    return ''.join(unicodedata.normalize('NFKC', value or '').casefold().split())


class ResearchStore:
    def __init__(self, home=HOME, data_dir=None):
        home = Path(home)
        home.mkdir(parents=True, exist_ok=True)
        path = home / 'library.sqlite3'
        if path.exists():
            with sqlite3.connect(path) as source:
                if not source.execute("SELECT 1 FROM sqlite_master WHERE name='research_runs'").fetchone():
                    backup = home / ('library.before-research-' + str(now()) + '.sqlite3')
                    with sqlite3.connect(backup) as target:
                        source.backup(target)
        self.library = LibraryStore(data_dir or ROOT / 'data', home, ROOT / 'database/sqlite_tables.db')
        self.connect = self.library.connect
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS research_runs(
                    id TEXT PRIMARY KEY,name TEXT,description TEXT,keywords TEXT,platforms TEXT,config TEXT,
                    status TEXT,created_at INTEGER,started_at INTEGER,ended_at INTEGER,error TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS research_platforms(
                    run_id TEXT,platform TEXT,status TEXT DEFAULT 'waiting',started_at INTEGER,ended_at INTEGER,
                    hits INTEGER DEFAULT 0,checked INTEGER DEFAULT 0,new_posts INTEGER DEFAULT 0,
                    old_posts INTEGER DEFAULT 0,new_comments INTEGER DEFAULT 0,duplicate_comments INTEGER DEFAULT 0,
                    failures INTEGER DEFAULT 0,reason TEXT DEFAULT '',PRIMARY KEY(run_id,platform));
                CREATE TABLE IF NOT EXISTS research_seen(
                    run_id TEXT,platform TEXT,post_id TEXT,PRIMARY KEY(run_id,platform,post_id));
                CREATE TABLE IF NOT EXISTS research_items(
                    run_id TEXT,platform TEXT,kind TEXT,item_id TEXT,post_id TEXT,is_new INTEGER,
                    PRIMARY KEY(run_id,platform,kind,item_id));
                CREATE INDEX IF NOT EXISTS research_item_post ON research_items(run_id,platform,post_id);
                CREATE TABLE IF NOT EXISTS research_titles(platform TEXT,post_id TEXT,title_key TEXT,PRIMARY KEY(platform,post_id));
                CREATE INDEX IF NOT EXISTS research_title_lookup ON research_titles(title_key);
                CREATE TABLE IF NOT EXISTS research_jobs(
                    id TEXT PRIMARY KEY,run_id TEXT,kind TEXT,status TEXT,payload TEXT,config_hash TEXT,
                    budget TEXT,state TEXT,result TEXT,error TEXT DEFAULT '',created_at INTEGER,updated_at INTEGER);
                CREATE INDEX IF NOT EXISTS research_job_run ON research_jobs(run_id,created_at DESC);
                CREATE TABLE IF NOT EXISTS research_demands(
                    job_id TEXT,demand_id TEXT,name TEXT,data TEXT,PRIMARY KEY(job_id,demand_id));
                CREATE TABLE IF NOT EXISTS research_evidence(
                    job_id TEXT,demand_id TEXT,evidence_id TEXT,data TEXT,
                    PRIMARY KEY(job_id,demand_id,evidence_id));
            ''')
            rows = db.execute('SELECT p.platform,p.post_id,p.title FROM posts p LEFT JOIN research_titles t ON t.platform=p.platform AND t.post_id=p.post_id WHERE t.post_id IS NULL')
            while batch := rows.fetchmany(500):
                db.executemany('INSERT OR IGNORE INTO research_titles VALUES(?,?,?)', [(r[0], r[1], title_key(r[2])) for r in batch])

    def recover(self):
        with self.connect() as db:
            stamp = now()
            db.execute("UPDATE research_runs SET status='interrupted',ended_at=?,error='服务重启，任务已中断' WHERE status IN ('running','waiting','stopping')", (stamp,))
            db.execute("UPDATE research_platforms SET status='interrupted',ended_at=?,reason='服务重启' WHERE status IN ('running','waiting')", (stamp,))
            db.execute("UPDATE research_jobs SET status='interrupted',error='服务重启；已保留阶段结果，重新预估后重试',updated_at=? WHERE status='running'", (stamp,))

    def create_run(self, body):
        stamp = now()
        base = datetime.fromtimestamp(stamp / 1000, timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rid, suffix = base, 1
            while db.execute('SELECT 1 FROM research_runs WHERE id=?', (rid,)).fetchone():
                suffix += 1
                rid = f'{base}-{suffix:02d}'
            db.execute('INSERT INTO research_runs(id,name,description,keywords,platforms,config,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
                       (rid, body['name'].strip() or rid, body['description'], encode(body['keywords']),
                        encode(body['platforms']), encode(body['config']), 'waiting', stamp))
            db.executemany('INSERT INTO research_platforms(run_id,platform) VALUES(?,?)', [(rid, p) for p in body['platforms']])
        return self.run(rid)

    def runs(self):
        with self.connect() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM research_runs ORDER BY created_at DESC,rowid DESC LIMIT 100')]
        return [self.run(rid, jobs=False) for rid in ids]

    def run(self, rid, jobs=True):
        with self.connect() as db:
            row = db.execute('SELECT * FROM research_runs WHERE id=?', (rid,)).fetchone()
            if not row:
                raise ValueError('采集批次不存在')
            result = dict(row)
            for key in ('keywords', 'platforms', 'config'):
                result[key] = json.loads(result[key])
            result['tasks'] = [dict(r) for r in db.execute('SELECT * FROM research_platforms WHERE run_id=?', (rid,))]
            result['counts'] = {key: sum(r[key] for r in result['tasks']) for key in COUNTERS}
            if jobs:
                result['jobs'] = []
                for row in db.execute('''SELECT id,run_id,kind,status,budget,error,created_at,updated_at,
                    json_extract(state,'$.usage') AS usage,json_extract(state,'$.chunk_index') AS chunk_index,
                    json_extract(state,'$.merge_index') AS merge_index,json_extract(state,'$.calls') AS calls
                    FROM research_jobs WHERE run_id=? ORDER BY created_at DESC''', (rid,)):
                    job = dict(row)
                    job['budget'] = json.loads(job['budget'])
                    job['usage'] = json.loads(job['usage'] or '{}')
                    job['progress'] = {k: job.pop(k) or 0 for k in ('chunk_index', 'merge_index', 'calls')}
                    job['result'] = {}
                    result['jobs'].append(job)
            return result

    def set_run(self, rid, status, error=''):
        with self.connect() as db:
            db.execute('UPDATE research_runs SET status=?,error=?,started_at=coalesce(started_at,?),ended_at=? WHERE id=?',
                       (status, error, now(), None if status in ('waiting', 'running', 'stopping') else now(), rid))

    def set_platform(self, rid, platform, status, reason=''):
        with self.connect() as db:
            db.execute('UPDATE research_platforms SET status=?,reason=?,started_at=coalesce(started_at,?),ended_at=? WHERE run_id=? AND platform=?',
                       (status, reason, now(), None if status == 'running' else now(), rid, platform))

    def failure(self, rid, platform):
        with self.connect() as db:
            db.execute('UPDATE research_platforms SET failures=failures+1 WHERE run_id=? AND platform=?', (rid, platform))

    def candidate(self, rid, platform, pid, keyword, limit):
        """Reserve a bounded unique candidate before requesting its detail/comments."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = db.execute('SELECT * FROM research_platforms WHERE run_id=? AND platform=?', (rid, platform)).fetchone()
            if not task or task['status'] != 'running':
                raise ValueError('批次已停止')
            db.execute('UPDATE research_platforms SET hits=hits+1 WHERE run_id=? AND platform=?', (rid, platform))
            if not pid:
                db.execute('UPDATE research_platforms SET failures=failures+1 WHERE run_id=? AND platform=?', (rid, platform))
                return False
            pid = str(pid)
            exists = db.execute('SELECT 1 FROM posts WHERE platform=? AND post_id=?', (platform, pid)).fetchone()
            seen = db.execute('SELECT 1 FROM research_seen WHERE run_id=? AND platform=? AND post_id=?', (rid, platform, pid)).fetchone()
            if seen:
                if exists:
                    db.execute('INSERT OR IGNORE INTO keywords VALUES(?,?,?)', (platform, pid, keyword))
                return False
            if task['checked'] >= limit:
                return False
            db.execute('INSERT INTO research_seen VALUES(?,?,?)', (rid, platform, pid))
            db.execute('UPDATE research_platforms SET checked=checked+1,old_posts=old_posts+? WHERE run_id=? AND platform=?', (int(bool(exists)), rid, platform))
            if exists:
                db.execute('INSERT OR IGNORE INTO research_items VALUES(?,?,?,?,?,0)', (rid, platform, 'post', pid, pid))
                db.execute('INSERT OR IGNORE INTO keywords VALUES(?,?,?)', (platform, pid, keyword))
            return not exists

    def save_item(self, rid, platform, row, comment=False):
        pid = str(next((row[k] for k in ('note_id', 'aweme_id', 'video_id') if row.get(k)), ''))
        iid = str(row.get('comment_id') or '') if comment else pid
        if not pid or not iid or pid.lower() in ('none', 'null', 'undefined') or iid.lower() in ('none', 'null', 'undefined'):
            self.failure(rid, platform)
            return False
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            active = db.execute("SELECT 1 FROM research_platforms WHERE run_id=? AND platform=? AND status='running'", (rid, platform)).fetchone()
            if not active:
                raise ValueError('批次已停止，不能继续写入')
            if comment and not db.execute("SELECT 1 FROM research_items WHERE run_id=? AND platform=? AND kind='post' AND item_id=? AND is_new=1", (rid, platform, pid)).fetchone():
                return False
            table, column = ('comments', 'comment_id') if comment else ('posts', 'post_id')
            exists = db.execute(f'SELECT 1 FROM {table} WHERE platform=? AND {column}=?', (platform, iid)).fetchone()
            if comment and not exists:
                maximum = json.loads(db.execute('SELECT config FROM research_runs WHERE id=?', (rid,)).fetchone()[0])['max_comments']
                saved = db.execute("SELECT count(*) FROM research_items WHERE run_id=? AND platform=? AND post_id=? AND kind='comment' AND is_new=1", (rid, platform, pid)).fetchone()[0]
                if saved >= maximum:
                    return False
            if not exists:
                self.library.upsert(db, platform, row, comment)
                if not comment:
                    db.execute('INSERT OR REPLACE INTO research_titles VALUES(?,?,?)', (platform, pid, title_key(row.get('title'))))
            inserted = db.execute('INSERT OR IGNORE INTO research_items VALUES(?,?,?,?,?,?)',
                                  (rid, platform, 'comment' if comment else 'post', iid, pid, int(not exists))).rowcount
            counter = 'new_comments' if comment else 'new_posts'
            if not exists and inserted:
                db.execute(f'UPDATE research_platforms SET {counter}={counter}+1 WHERE run_id=? AND platform=?', (rid, platform))
            elif comment:
                db.execute('UPDATE research_platforms SET duplicate_comments=duplicate_comments+1 WHERE run_id=? AND platform=?', (rid, platform))
            return not exists

    def documents(self, rid):
        self.run(rid)
        with self.connect() as db:
            rows = db.execute('''SELECT i.platform,i.kind,i.item_id,i.post_id,p.title,p.body,p.original_url
                FROM research_items i JOIN posts p ON p.platform=i.platform AND p.post_id=i.item_id
                WHERE i.run_id=? AND i.kind='post' AND i.is_new=1
                UNION ALL SELECT i.platform,i.kind,i.item_id,i.post_id,'',c.body,p.original_url
                FROM research_items i JOIN comments c ON c.platform=i.platform AND c.comment_id=i.item_id
                JOIN posts p ON p.platform=i.platform AND p.post_id=i.post_id
                WHERE i.run_id=? AND i.kind='comment' AND i.is_new=1
                ORDER BY 1,2,3''', (rid, rid))
            return [dict(r) for r in rows]

    @staticmethod
    def decode_job(row):
        result = dict(row)
        for key in ('payload', 'budget', 'state', 'result'):
            result[key] = json.loads(result[key])
        return result

    def job(self, jid):
        with self.connect() as db:
            row = db.execute('SELECT * FROM research_jobs WHERE id=?', (jid,)).fetchone()
            if not row:
                raise ValueError('AI任务不存在')
            return self.decode_job(row)

    def create_job(self, kind, payload, config_hash, budget, rid=None, state=None):
        jid = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO research_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (jid, rid, kind, 'estimated', encode(payload), config_hash, encode(budget), encode(state or {}), '{}', '', now(), now()))
        return self.job(jid)

    def update_job(self, jid, **values):
        allowed = {'status', 'budget', 'state', 'result', 'error'}
        if not values or not set(values) <= allowed:
            raise ValueError('无效任务更新')
        with self.connect() as db:
            assignments = ','.join(k + '=?' for k in values)
            args = [encode(v) if k in {'budget', 'state', 'result'} else v for k, v in values.items()]
            db.execute('UPDATE research_jobs SET ' + assignments + ',updated_at=? WHERE id=?', args + [now(), jid])

    def claim_job(self, jid, fingerprint):
        with self.connect() as db:
            changed = db.execute("UPDATE research_jobs SET status='running',updated_at=? WHERE id=? AND status='estimated' AND config_hash=?",
                                 (now(), jid, fingerprint)).rowcount
            if not changed:
                raise ValueError('预算已失效或任务已执行，请重新预估')

    def save_report(self, jid, demands, summary, usage):
        with self.connect() as db:
            for index, demand in enumerate(demands):
                did = str(index + 1)
                db.execute('INSERT INTO research_demands VALUES(?,?,?,?)', (jid, did, demand['name'], encode(demand)))
                for evidence in demand['evidence']:
                    db.execute('INSERT INTO research_evidence VALUES(?,?,?,?)', (jid, did, evidence['id'], encode(evidence)))
            result = {'demands': demands, 'summary': summary, 'usage': usage}
            db.execute("UPDATE research_jobs SET status='completed',result=?,updated_at=? WHERE id=?", (encode(result), now(), jid))
