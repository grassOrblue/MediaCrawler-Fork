"""Independent local library service: remains available while crawlers run."""
import csv
from io import StringIO
import hashlib
from pathlib import Path
import threading
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image, ImageOps

from .services.library_store import LibraryStore

APP_ROOT = Path(__file__).resolve().parent.parent
HOME = APP_ROOT.parent / 'library'
store = LibraryStore(APP_ROOT/'data',HOME,APP_ROOT/'database/sqlite_tables.db')
app = FastAPI(title='MediaCrawler 资料库')
wake, stopped = threading.Event(), threading.Event()
media_requested = threading.Event()


def worker():
    while not stopped.is_set():
        wake.clear()
        force_media = media_requested.is_set()
        media_requested.clear()
        store.sync(force_media=force_media)
        wake.wait(60)


@app.on_event('startup')
def start_worker():
    threading.Thread(target=worker,daemon=True,name='library-indexer').start()


@app.on_event('shutdown')
def stop_worker():
    stopped.set()
    wake.set()


@app.get('/api/health')
def health():
    return {'status':'ok'}


@app.get('/')
def home():
    return FileResponse(APP_ROOT/'api/webui/library.html')


@app.get('/api/library/status')
def status():
    return store.status()


@app.post('/api/library/sync',status_code=202)
def sync():
    media_requested.set()
    wake.set()
    return {'queued':True}


def filters(q='',exclude='',platform='',kind='',keyword='',min_likes=0,min_collects=0,min_comments=0,since='',until='',images=False,saved=False,show_hidden=False,tag='',sort='recent'):
    return dict(q=q,exclude=exclude,platform=platform,kind=kind,keyword=keyword,min_likes=min_likes,min_collects=min_collects,
                min_comments=min_comments,since=since,until=until,images=images,saved=saved,show_hidden=show_hidden,tag=tag,sort=sort)


@app.get('/api/library/posts')
def posts(q:str=Query('',max_length=200),exclude:str=Query('',max_length=200),platform:str='',kind:str='',keyword:str='',
          min_likes:int=Query(0,ge=0),min_collects:int=Query(0,ge=0),min_comments:int=Query(0,ge=0),since:str='',until:str='',
          images:bool=False,saved:bool=False,show_hidden:bool=False,tag:str='',sort:str='recent',cursor:str='',limit:int=Query(24,ge=1,le=100)):
    try:
        return store.list_posts(filters(q,exclude,platform,kind,keyword,min_likes,min_collects,min_comments,since,until,images,saved,show_hidden,tag,sort),cursor,limit)
    except (ValueError,KeyError,TypeError):
        raise HTTPException(400,'筛选条件或翻页位置无效，请重新筛选')


@app.get('/api/library/posts/{platform}/{pid}')
def detail(platform:str,pid:str,after:int=Query(0,ge=0)):
    item = store.detail(platform,pid,after)
    if not item:
        raise HTTPException(404,'这条内容不在资料库中')
    return item


class Annotation(BaseModel):
    saved: bool | None = None
    hidden: bool | None = None
    tags: str | None = Field(None,max_length=500)
    note: str | None = Field(None,max_length=5000)


@app.patch('/api/library/posts/{platform}/{pid}')
def annotate(platform:str,pid:str,body:Annotation):
    if not store.annotate(platform,pid,body.model_dump(exclude_none=True)):
        raise HTTPException(404,'这条内容不在资料库中')
    return {'ok':True}


@app.get('/api/library/media/{platform}/{pid}/{name}')
def media(platform:str,pid:str,name:str,thumb:bool=False):
    if any(x in {'.','..'} for x in (platform,pid,name)):
        raise HTTPException(403,'无效路径')
    base = (store.data_dir/platform/'media').resolve()
    file = (base/pid/name).resolve()
    try:
        base.relative_to(store.data_dir)
        file.relative_to(base)
    except ValueError:
        raise HTTPException(403,'无效路径')
    if not file.is_file() or file.suffix.lower() not in {'.jpg','.jpeg','.png','.webp','.gif','.mp4','.webm','.mov'}:
        raise HTTPException(404,'本地媒体文件不存在')
    if thumb and file.suffix.lower() not in {'.mp4','.webm','.mov'}:
        key = hashlib.sha256((str(file)+str(file.stat().st_mtime_ns)).encode()).hexdigest()
        folder = HOME/'thumbnails'
        folder.mkdir(exist_ok=True)
        target = folder/(key+'.webp')
        if not target.exists():
            try:
                with Image.open(file) as original:
                    image = ImageOps.exif_transpose(original).convert('RGB')
                    image.thumbnail((600,600))
                    temporary = folder/(key+'.'+str(threading.get_ident())+'.tmp')
                    image.save(temporary,format='WEBP',quality=78)
                    temporary.replace(target)
            except (OSError,ValueError):
                raise HTTPException(422,'图片暂时无法预览')
        file = target
    media_types={'.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png','.webp':'image/webp','.gif':'image/gif',
                 '.mp4':'video/mp4','.webm':'video/webm','.mov':'video/quicktime'}
    return FileResponse(file,media_type=media_types[file.suffix.lower()],headers={'Cache-Control':'private, max-age=3600'})


@app.get('/api/library/export')
def export(q:str='',exclude:str='',platform:str='',kind:str='',keyword:str='',min_likes:int=0,min_collects:int=0,min_comments:int=0,
           since:str='',until:str='',images:bool=False,saved:bool=False,show_hidden:bool=False,tag:str='',sort:str='recent'):
    try:
        where,args = store.filters(filters(q,exclude,platform,kind,keyword,min_likes,min_collects,min_comments,since,until,images,saved,show_hidden,tag,sort))
    except ValueError:
        raise HTTPException(400,'日期或数量条件无效')
    def generate():
        buf = StringIO()
        writer = csv.writer(buf)
        columns = ['platform','post_id','title','body','author','published_at','likes','collects','comments','original_url','user_tags','note']
        writer.writerow(columns)
        yield '\ufeff'+buf.getvalue()
        with store.connect() as db:
            result = db.execute(store.SELECT+' WHERE '+where+' ORDER BY p.rowid DESC',args)
            while True:
                batch = result.fetchmany(500)
                if not batch:
                    break
                buf.seek(0); buf.truncate(0)
                for row in batch:
                    values = [row[key] for key in columns]
                    writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v for v in values])
                yield buf.getvalue()
    return StreamingResponse(generate(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote('筛选结果.csv')})


app.mount('/assets',StaticFiles(directory=APP_ROOT/'api/webui/assets'),name='assets')
