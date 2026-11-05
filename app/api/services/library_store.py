"""SQLite read model for local crawl output. Source files remain untouched."""
import base64
import csv
from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time


def number(value):
    text = str(value or '0').strip().lower().replace(',', '').replace('+', '')
    match = re.match(r'^(\d+(?:\.\d+)?)\s*([万亿千wkm]?)', text)
    if not match:
        return 0
    return int(float(match[1]) * {'万':10000, '亿':100000000, '千':1000, 'w':10000, 'k':1000, 'm':1000000, '':1}[match[2]])


def timestamp(value):
    if not value:
        return 0
    try:
        n = float(value)
        return int(n * 1000 if n < 100000000000 else n)
    except (ValueError, TypeError, OverflowError):
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return int(dt.replace(tzinfo=dt.tzinfo or timezone(timedelta(hours=8))).timestamp() * 1000)
        except ValueError:
            return 0


def json_array(path):
    """Stream a JSON array using bounded buffers rather than json.load()."""
    decoder = json.JSONDecoder()
    with path.open(encoding='utf-8-sig') as stream:
        buffer, eof = '', False

        def fill():
            nonlocal buffer, eof
            chunk = stream.read(65536)
            eof = not chunk
            buffer += chunk

        fill()
        while not buffer.strip() and not eof:
            fill()
        buffer = buffer.lstrip()
        array = buffer.startswith('[')
        if array:
            buffer = buffer[1:]
        expect_value = True
        while True:
            buffer = buffer.lstrip()
            if not buffer and not eof:
                fill()
                continue
            if array and buffer.startswith(']'):
                return
            if not expect_value and array:
                if not buffer.startswith(','):
                    raise ValueError('Incomplete JSON array')
                buffer = buffer[1:]
                expect_value = True
                continue
            try:
                record, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                if eof or len(buffer) > 16 * 1024 * 1024:
                    raise ValueError('Incomplete or oversized JSON record')
                fill()
                continue
            if isinstance(record, dict):
                yield record, 0
            buffer = buffer[end:]
            if not array:
                return
            expect_value = False


def records(path, offset=0):
    if path.suffix == '.json':
        yield from json_array(path)
    elif path.suffix == '.jsonl':
        with path.open('rb') as stream:
            stream.seek(offset)
            for line in stream:
                if not line.endswith(b'\n'):
                    return  # A crawler may still be writing the last line.
                if line.strip():
                    yield json.loads(line.decode('utf-8-sig')), stream.tell()
    elif path.suffix == '.csv':
        with path.open(encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream):
                yield row, 0
    elif path.suffix == '.xlsx':
        from openpyxl import load_workbook
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            rows = book.active.iter_rows(values_only=True)
            headers = [str(x or '') for x in next(rows)]
            for values in rows:
                yield dict(zip(headers, values)), 0
        finally:
            book.close()


class LibraryStore:
    def __init__(self, data_dir, home, sqlite_source=None):
        self.data_dir, self.home = Path(data_dir).resolve(), Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = self.home / 'library.sqlite3'
        self.sqlite_source = Path(sqlite_source) if sqlite_source else None
        self.sync_lock = threading.Lock()
        self._stats_cache = None
        self._stats_at = 0
        self._media_at = 0
        self.state = {'running':False, 'last_sync':None, 'processed':0, 'errors':[]}
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS posts(
                    platform TEXT, post_id TEXT, title TEXT, body TEXT, kind TEXT, author TEXT,
                    published_at INTEGER, likes INTEGER, collects INTEGER, comments INTEGER,
                    original_url TEXT, tag_text TEXT, modified_at INTEGER,
                    PRIMARY KEY(platform,post_id));
                CREATE INDEX IF NOT EXISTS posts_time ON posts(published_at DESC);
                CREATE INDEX IF NOT EXISTS posts_likes ON posts(likes DESC);
                CREATE INDEX IF NOT EXISTS posts_collects ON posts(collects DESC);
                CREATE INDEX IF NOT EXISTS posts_comments ON posts(comments DESC);
                CREATE INDEX IF NOT EXISTS posts_platform_time ON posts(platform,published_at DESC);
                CREATE TABLE IF NOT EXISTS comments(
                    platform TEXT, comment_id TEXT, post_id TEXT, body TEXT, author TEXT,
                    published_at INTEGER, likes INTEGER, parent_id TEXT,
                    PRIMARY KEY(platform,comment_id));
                CREATE INDEX IF NOT EXISTS comment_post ON comments(platform,post_id,published_at);
                CREATE TABLE IF NOT EXISTS keywords(platform TEXT,post_id TEXT,keyword TEXT,
                    PRIMARY KEY(platform,post_id,keyword));
                CREATE INDEX IF NOT EXISTS keyword_lookup ON keywords(keyword,platform,post_id);
                CREATE TABLE IF NOT EXISTS annotations(platform TEXT,post_id TEXT,saved INTEGER DEFAULT 0,
                    hidden INTEGER DEFAULT 0,tags TEXT DEFAULT '',note TEXT DEFAULT '',PRIMARY KEY(platform,post_id));
                CREATE INDEX IF NOT EXISTS annotation_saved ON annotations(saved,platform,post_id);
                CREATE TABLE IF NOT EXISTS media(platform TEXT,post_id TEXT,name TEXT,kind TEXT,
                    PRIMARY KEY(platform,post_id,name));
                CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY,mtime INTEGER,size INTEGER,offset INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS folders(path TEXT PRIMARY KEY,mtime INTEGER);
                CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(title,body,tag_text,content='posts',content_rowid='rowid',tokenize='trigram');
                CREATE VIRTUAL TABLE IF NOT EXISTS posts_pairs USING fts5(pairs);
                CREATE TRIGGER IF NOT EXISTS post_insert AFTER INSERT ON posts BEGIN
                    INSERT INTO posts_fts(rowid,title,body,tag_text) VALUES(new.rowid,new.title,new.body,new.tag_text);
                    INSERT INTO posts_pairs(rowid,pairs) VALUES(new.rowid,bigrams(new.title||' '||new.body||' '||new.tag_text)); END;
                CREATE TRIGGER IF NOT EXISTS post_delete AFTER DELETE ON posts BEGIN
                    INSERT INTO posts_fts(posts_fts,rowid,title,body,tag_text) VALUES('delete',old.rowid,old.title,old.body,old.tag_text);
                    DELETE FROM posts_pairs WHERE rowid=old.rowid; END;
                CREATE TRIGGER IF NOT EXISTS post_update AFTER UPDATE ON posts BEGIN
                    INSERT INTO posts_fts(posts_fts,rowid,title,body,tag_text) VALUES('delete',old.rowid,old.title,old.body,old.tag_text);
                    INSERT INTO posts_fts(rowid,title,body,tag_text) VALUES(new.rowid,new.title,new.body,new.tag_text);
                    DELETE FROM posts_pairs WHERE rowid=old.rowid;
                    INSERT INTO posts_pairs(rowid,pairs) VALUES(new.rowid,bigrams(new.title||' '||new.body||' '||new.tag_text)); END;
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.create_function('bigrams',1,lambda text:' '.join(set(text[i:i+2].lower() for i in range(len(text)-1) if text[i:i+2].isalnum())))
        try:
            with db:
                yield db
        finally:
            db.close()

    def upsert(self, db, platform, row, is_comment=False):
        if not isinstance(row, dict):
            return
        pid = str(next((row[k] for k in ('note_id','aweme_id','video_id','mblogid','content_id','id') if row.get(k)), ''))
        if not pid:
            return
        if is_comment:
            cid = str(row.get('comment_id') or row.get('cid') or '')
            if not cid:
                return
            db.execute('INSERT INTO comments VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(platform,comment_id) DO UPDATE SET body=excluded.body,likes=excluded.likes',
                       (platform,cid,pid,str(row.get('content') or row.get('comment_text') or ''),str(row.get('nickname') or row.get('user_nickname') or ''),
                        timestamp(row.get('create_time') or row.get('publish_time')),number(row.get('like_count')),str(row.get('parent_comment_id') or '')))
            return
        tags = row.get('tag_list') or row.get('tags') or ''
        if not isinstance(tags,str):
            tags = json.dumps(tags,ensure_ascii=False)
        title, body = str(row.get('title') or ''), str(row.get('desc') or row.get('content') or row.get('content_text') or '')
        url = str(row.get('note_url') or row.get('video_url') or row.get('content_url') or '')
        if not url.startswith(('https://','http://')):
            url = ''
        values = (platform,pid,title,body,str(row.get('type') or row.get('content_type') or 'note'),str(row.get('nickname') or row.get('user_nickname') or ''),
                  timestamp(row.get('time') or row.get('create_time') or row.get('publish_time') or row.get('created_time')),
                  number(row.get('liked_count') or row.get('digg_count') or row.get('voteup_count')),
                  number(row.get('collected_count') or row.get('collect_count')),
                  number(row.get('comment_count') or row.get('comments_count') or row.get('total_replay_num')),
                  url,tags,timestamp(row.get('last_modify_ts') or row.get('last_update_time')))
        db.execute('''INSERT INTO posts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(platform,post_id) DO UPDATE SET
            title=excluded.title,body=excluded.body,kind=excluded.kind,author=excluded.author,published_at=excluded.published_at,
            likes=excluded.likes,collects=excluded.collects,comments=excluded.comments,original_url=excluded.original_url,
            tag_text=excluded.tag_text,modified_at=excluded.modified_at WHERE excluded.modified_at>=posts.modified_at''', values)
        keyword = str(row.get('source_keyword') or '').strip()
        if keyword:
            db.execute('INSERT OR IGNORE INTO keywords VALUES(?,?,?)',(platform,pid,keyword))

    def sync_file(self, db, path):
        stat = path.stat()
        old = db.execute('SELECT * FROM sources WHERE path=?',(str(path),)).fetchone()
        if old and old['mtime'] == stat.st_mtime_ns and old['size'] == stat.st_size:
            return
        offset = old['offset'] if old and path.suffix == '.jsonl' and stat.st_size >= old['size'] else 0
        platform = path.relative_to(self.data_dir).parts[0]
        final_offset = offset
        for count, (row, position) in enumerate(records(path,offset),1):
            self.upsert(db,platform,row,'comment' in path.stem)
            final_offset = position
            self.state['processed'] += 1
            if count % 500 == 0:
                db.commit()
        current = path.stat()
        # Never acknowledge a source snapshot that changed while being read.
        if (current.st_mtime_ns,current.st_size) == (stat.st_mtime_ns,stat.st_size):
            db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?)',(str(path),stat.st_mtime_ns,stat.st_size,final_offset))
        db.commit()

    def sync_media(self, db):
        for platform in self.data_dir.iterdir() if self.data_dir.exists() else []:
            parent = platform / 'media'
            if not parent.is_dir():
                continue
            for folder in parent.iterdir():
                if not folder.is_dir():
                    continue
                stamp = folder.stat().st_mtime_ns
                old = db.execute('SELECT mtime FROM folders WHERE path=?',(str(folder),)).fetchone()
                if old and old[0] == stamp:
                    continue
                db.execute('DELETE FROM media WHERE platform=? AND post_id=?',(platform.name,folder.name))
                for file in folder.iterdir():
                    kind = 'image' if file.suffix.lower() in {'.jpg','.jpeg','.webp','.png','.gif'} else 'video' if file.suffix.lower() in {'.mp4','.webm','.mov'} else None
                    if kind and file.is_file():
                        db.execute('INSERT OR IGNORE INTO media VALUES(?,?,?,?)',(platform.name,folder.name,file.name,kind))
                db.execute('INSERT OR REPLACE INTO folders VALUES(?,?)',(str(folder),stamp))
            db.commit()

    def sync_sqlite(self, db):
        path = self.sqlite_source
        if not path or not path.is_file():
            return
        # Crawl DB is read-only here. Revisit its update watermark, not each HTTP query.
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5)) as source:
            source.row_factory = sqlite3.Row
            tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            names = {'xhs':'xhs','bilibili':'bili','douyin':'dy','kuaishou':'ks','weibo':'wb','tieba':'tieba','zhihu':'zhihu'}
            for table in sorted(tables):
                if not re.fullmatch('[a-z0-9_]+',table):
                    continue
                parts = table.split('_')
                if parts[0] not in names or not any(x in table for x in ('note','comment','content','video','aweme')):
                    continue
                cols = {r[1] for r in source.execute(f'PRAGMA table_info("{table}")')}
                marker = str(path)+'::'+table
                old = db.execute('SELECT mtime FROM sources WHERE path=?',(marker,)).fetchone()
                watermark = old[0] if old else 0
                if 'last_modify_ts' in cols:
                    query, args = f'SELECT * FROM "{table}" WHERE last_modify_ts>=?',(watermark,)
                else:
                    if old and old[0] == path.stat().st_mtime_ns:
                        continue
                    query, args = f'SELECT * FROM "{table}"',()
                latest = watermark
                for i,row in enumerate(source.execute(query,args),1):
                    item = dict(row)
                    self.upsert(db,names[parts[0]],item,'comment' in table)
                    latest = max(latest,int(item.get('last_modify_ts') or 0))
                    self.state['processed'] += 1
                    if i % 500 == 0:
                        db.commit()
                db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,0)',(marker,latest if 'last_modify_ts' in cols else path.stat().st_mtime_ns,0))
                db.commit()

    def sync(self, force_media=False):
        if not self.sync_lock.acquire(blocking=False):
            return
        self.state.update(running=True, processed=0, errors=[])
        try:
            with self.connect() as db:
                paths = []
                for folder, dirs, files in os.walk(self.data_dir):
                    dirs[:] = [d for d in dirs if d not in {'media','browser_data','__pycache__'}]
                    paths.extend(Path(folder)/name for name in files)
                for path in sorted(paths):
                    if path.suffix not in {'.json','.jsonl','.csv','.xlsx'} or not any(x in path.stem for x in ('contents','comments')) or 'media' in path.relative_to(self.data_dir).parts:
                        continue
                    try:
                        self.sync_file(db,path)
                    except (ValueError,OSError,sqlite3.Error) as error:
                        logging.getLogger(__name__).warning('Source %s deferred: %s',path.name,error)
                        db.rollback()
                        self.state['errors'].append({'file':path.name,'message':'文件正在写入或格式暂不可读，下次同步重试'})
                self.sync_sqlite(db)
                if force_media or time.monotonic()-self._media_at>300:
                    self.sync_media(db)
                    self._media_at = time.monotonic()
        except Exception:
            logging.getLogger(__name__).exception('Library synchronization did not complete')
            self.state['errors'].append({'file':'index','message':'同步暂未完成，已有索引仍可浏览'})
        finally:
            self.state.update(running=False,last_sync=int(time.time()*1000))
            self._stats_cache = None
            self.sync_lock.release()

    def status(self):
        if self._stats_cache is None or time.monotonic()-self._stats_at>15:
            with self.connect() as db:
                self._stats_cache = {'posts':db.execute('SELECT count(*) FROM posts').fetchone()[0],
                    'comments':db.execute('SELECT count(*) FROM comments').fetchone()[0],
                    'media':db.execute('SELECT count(*) FROM media').fetchone()[0],
                    'platforms':[r[0] for r in db.execute('SELECT DISTINCT platform FROM posts')],
                    'keywords':[r[0] for r in db.execute('SELECT DISTINCT keyword FROM keywords ORDER BY keyword LIMIT 500')]}
            self._stats_at = time.monotonic()
        return {**self.state,**self._stats_cache}

    def filters(self, f):
        where, args = ['1=1'], []
        for word in f.get('q','').strip().split():
            if len(word)>=3:
                where.append('p.rowid IN (SELECT rowid FROM posts_fts WHERE posts_fts MATCH ?)')
                args.append('"'+word.replace('"','""')+'"')
            else:
                if len(word)==2 and word.isalnum():
                    where.append('p.rowid IN (SELECT rowid FROM posts_pairs WHERE posts_pairs MATCH ?)')
                    args.append('"'+word.replace('"','""')+'"')
                where.append("(p.title||' '||p.body||' '||p.tag_text) LIKE ? ESCAPE '\\'")
                term = '%'+word.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
                args.append(term)
        for word in f.get('exclude','').strip().split():
            where.append("instr(p.title||' '||p.body||' '||p.tag_text,?)=0"); args.append(word)
        for key,column in [('platform','p.platform'),('kind','p.kind')]:
            if f.get(key):
                where.append(column+'=?'); args.append(f[key])
        if f.get('keyword'):
            where.append('EXISTS(SELECT 1 FROM keywords k WHERE k.platform=p.platform AND k.post_id=p.post_id AND k.keyword=?)'); args.append(f['keyword'])
        for key,column in [('min_likes','likes'),('min_collects','collects'),('min_comments','comments')]:
            if f.get(key) is not None:
                where.append('p.'+column+'>=?'); args.append(max(0,int(f[key])))
        for key,op,days in [('since','>=',0),('until','<',1)]:
            if f.get(key):
                dt = datetime.fromisoformat(f[key]).replace(tzinfo=timezone(timedelta(hours=8)))+timedelta(days=days)
                where.append('p.published_at'+op+'?'); args.append(int(dt.timestamp()*1000))
        if f.get('images'):
            where.append("EXISTS(SELECT 1 FROM media m WHERE m.platform=p.platform AND m.post_id=p.post_id AND m.kind='image')")
        if f.get('saved'):
            where.append('a.saved=1')
        if not f.get('show_hidden'):
            where.append('coalesce(a.hidden,0)=0')
        if f.get('tag'):
            where.append('instr(coalesce(a.tags,\'\'),?)>0'); args.append(f['tag'])
        return ' AND '.join(where),args

    SORTS = {'recent':'published_at','likes':'likes','collects':'collects','comments':'comments','updated':'modified_at'}
    SELECT = '''SELECT p.rowid AS _rowid,p.*,coalesce(a.saved,0) AS saved,coalesce(a.hidden,0) AS hidden,
        coalesce(a.tags,'') AS user_tags,coalesce(a.note,'') AS note,
        (SELECT m.name FROM media m WHERE m.platform=p.platform AND m.post_id=p.post_id AND m.kind='image'
         ORDER BY CASE WHEN m.name LIKE 'cover.%' THEN 0 ELSE 1 END,m.name LIMIT 1) AS cover
        FROM posts p LEFT JOIN annotations a ON a.platform=p.platform AND a.post_id=p.post_id'''

    def list_posts(self, f, cursor='', limit=24):
        where,args = self.filters(f)
        sort = f.get('sort','recent')
        column = self.SORTS.get(sort,'published_at')
        signature = hashlib.sha256(json.dumps(f,sort_keys=True).encode()).hexdigest()[:16]
        with self.connect() as db:
            total = db.execute('SELECT count(*) FROM posts p LEFT JOIN annotations a ON a.platform=p.platform AND a.post_id=p.post_id WHERE '+where,args).fetchone()[0] if not cursor else None
            if cursor:
                position = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if position['signature'] != signature:
                    raise ValueError('Cursor does not match filters')
                where += f' AND (p.{column}<? OR (p.{column}=? AND p.rowid<?))'
                args += [int(position['value']),int(position['value']),int(position['rowid'])]
            rows = [dict(r) for r in db.execute(self.SELECT+' WHERE '+where+f' ORDER BY p.{column} DESC,p.rowid DESC LIMIT ?',args+[limit+1])]
            more = len(rows)>limit
            rows = rows[:limit]
            next_cursor = ''
            if more:
                last = rows[-1]
                next_cursor = base64.urlsafe_b64encode(json.dumps({'signature':signature,'value':last[column],'rowid':last['_rowid']}).encode()).decode()
            for row in rows:
                row['excerpt'] = row.pop('body')[:240]
            return {'items':rows,'total':total,'next_cursor':next_cursor}

    def detail(self, platform, pid, after=0):
        with self.connect() as db:
            row = db.execute(self.SELECT+' WHERE p.platform=? AND p.post_id=?',(platform,pid)).fetchone()
            if not row:
                return None
            item = dict(row)
            item['media'] = [dict(r) for r in db.execute('SELECT name,kind FROM media WHERE platform=? AND post_id=? ORDER BY name',(platform,pid))]
            item['keywords'] = [r[0] for r in db.execute('SELECT keyword FROM keywords WHERE platform=? AND post_id=?',(platform,pid))]
            comments = [dict(r) for r in db.execute('SELECT rowid AS cursor,* FROM comments WHERE platform=? AND post_id=? AND rowid>? ORDER BY rowid LIMIT 51',(platform,pid,after))]
            item['next_comment'] = comments[49]['cursor'] if len(comments)>50 else None
            item['comment_items'] = comments[:50]
            item['indexed_comments'] = db.execute('SELECT count(*) FROM comments WHERE platform=? AND post_id=?',(platform,pid)).fetchone()[0]
            return item

    def annotate(self, platform, pid, values):
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM posts WHERE platform=? AND post_id=?',(platform,pid)).fetchone():
                return False
            db.execute('INSERT OR IGNORE INTO annotations(platform,post_id) VALUES(?,?)',(platform,pid))
            for key,value in values.items():
                if key in {'saved','hidden','tags','note'}:
                    db.execute('UPDATE annotations SET '+key+'=? WHERE platform=? AND post_id=?',(value,platform,pid))
        return True
