"""Integration checks in an isolated fixture directory; no production annotations."""
import asyncio
import csv
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch
import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parent
os.environ['TEMP'] = os.environ['TMP'] = str(ROOT/'tmp')
sys.path.insert(0,str(ROOT/'app'))
from api.services.library_store import LibraryStore, number


def sample(i):
    return {'note_id':str(i),'title':f'前端开发案例 {i}','desc':('需要编程协助' if i%2==0 else '设计作品')+' 正文测试 '*8,
            'tag_list':'接单' if i==1 else '', 'time':1700000000000,'last_modify_ts':1700000000000,
            'liked_count':'1.2万' if i==0 else str(i),'collected_count':str(i),'comment_count':'99',
            'source_keyword':'前端','note_url':'https://example.invalid/post/'+str(i)}


async def api_checks(store, folder):
    from api import library_app as module
    with patch.object(module,'store',store),patch.object(module,'HOME',folder/'home'):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),base_url='http://test') as client:
            response=await client.get('/api/library/posts',params={'q':'接单','images':'false'})
            print('API search completed',flush=True)
            assert response.status_code==200 and response.json()['total']==1,response.text
            response=await client.get('/api/library/media/xhs/0/001.png',params={'thumb':'true'})
            print('API thumbnail completed',response.status_code,response.headers.get('content-type'),flush=True)
            assert response.status_code==200 and response.headers['content-type']=='image/webp'
            response=await client.get('/api/library/media/xhs/0/secret.txt')
            assert response.status_code==404
            response=await client.get('/api/library/posts',params={'since':'invalid date'})
            assert response.status_code==400
            response=await client.patch('/api/library/posts/xhs/1',json={'saved':True,'tags':'待整理,案例','note':'测试备注'})
            assert response.status_code==200
            response=await client.get('/api/library/export',params={'q':'接单','saved':'true'})
            assert response.status_code==200
            rows=list(csv.DictReader(StringIO(response.content.decode('utf-8-sig'))))
            assert len(rows)==1 and rows[0]['post_id']=='1' and rows[0]['note']=='测试备注'
            response=await client.get('/api/library/posts/xhs/0')
            detail=response.json()
            assert len(detail['comment_items'])==50 and detail['next_comment'] is not None
            tail=(await client.get('/api/library/posts/xhs/0',params={'after':detail['next_comment']})).json()
            assert len(tail['comment_items'])==1 and tail['next_comment'] is None


def run():
    started=time.monotonic()
    # TemporaryDirectory performs recursive cleanup only on this verified workspace child.
    with tempfile.TemporaryDirectory(prefix='library-check-',dir=ROOT/'tmp') as temp:
        folder=Path(temp).resolve()
        assert folder.is_relative_to((ROOT/'tmp').resolve())
        data=folder/'data'; home=folder/'home'
        source=data/'xhs/jsonl/search_contents.jsonl'
        source.parent.mkdir(parents=True)
        with source.open('w',encoding='utf-8') as stream:
            for i in range(5000):
                stream.write(json.dumps(sample(i),ensure_ascii=False)+'\n')
        json_source=data/'xhs/json/search_contents.json'
        json_source.parent.mkdir()
        with json_source.open('w',encoding='utf-8') as stream:
            stream.write('[')
            for i in range(5000):
                if i:stream.write(',')
                stream.write(json.dumps(sample(i),ensure_ascii=False))
            stream.write(']')
        comments=source.with_name('search_comments.jsonl')
        comments.write_text('\n'.join(json.dumps({'comment_id':str(i),'note_id':'0','content':f'合成评论{i}'}) for i in range(51))+'\n',encoding='utf-8')
        media=data/'xhs/media/0';media.mkdir(parents=True)
        Image.new('RGB',(900,600),'teal').save(media/'001.png')
        (media/'secret.txt').write_text('must not be served')
        db_source=folder/'crawl.sqlite'
        with sqlite3.connect(db_source) as db:
            db.execute('CREATE TABLE xhs_note(note_id TEXT,title TEXT,desc TEXT,last_modify_ts INTEGER)')
            db.execute('INSERT INTO xhs_note VALUES(?,?,?,?)',('0',sample(0)['title'],sample(0)['desc'],1))
        db.close()
        store=LibraryStore(data,home,db_source)
        store.sync(force_media=True)
        assert not store.state['errors'],store.state
        assert store.status()['posts']==5000 and store.status()['comments']==51
        assert number('1.2万')==12000
        assert store.list_posts({'q':'编程'})['total']==2500
        assert store.list_posts({'q':'前端开发'})['total']==5000
        assert store.list_posts({'q':'接单'})['total']==1 # tags are searchable too
        assert store.list_posts({'q':'编程','exclude':'协助'})['total']==0
        assert store.list_posts({'images':True})['total']==1
        assert store.list_posts({'min_likes':10000})['total']==1
        assert store.list_posts({'sort':'likes'})['items'][0]['post_id']=='0'
        ids=[];cursor=''
        while True:
            result=store.list_posts({},cursor,100)
            ids.extend(item['post_id'] for item in result['items'])
            cursor=result['next_cursor']
            if not cursor:break
        assert len(ids)==len(set(ids))==5000
        with source.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(sample(5000),ensure_ascii=False)+'\n'+json.dumps(sample(5001),ensure_ascii=False)[:30])
        store.sync()
        assert store.status()['posts']==5001
        with source.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(sample(5001),ensure_ascii=False)[30:]+'\n')
        store.sync()
        assert store.status()['posts']==5002
        store.annotate('xhs','1',{'saved':True,'tags':'手工标签','note':'不会被导入覆盖'})
        store.sync()
        assert store.detail('xhs','1')['note']=='不会被导入覆盖'
        assert store.list_posts({'saved':True})['total']==1
        store.annotate('xhs','1',{'hidden':True})
        assert store.list_posts({'q':'接单'})['total']==0
        store.annotate('xhs','1',{'hidden':False})
        print('Store checks completed; checking API',flush=True)
        asyncio.run(asyncio.wait_for(api_checks(store,folder),timeout=30))
        print(f'PASS: 5000-post streaming import/dedup, Chinese 2/4-character search, numeric normalization, image association, keyset pages, append checkpoints, annotations, comment pages, thumbnails, filtered CSV and path restrictions ({time.monotonic()-started:.1f}s)')


run()
