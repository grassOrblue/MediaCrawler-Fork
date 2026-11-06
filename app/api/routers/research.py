"""Research workflow endpoints; all model requests require a claimed estimate."""
import asyncio
import csv
from functools import lru_cache
from io import StringIO
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from ..schemas import CrawlerStartRequest
from ..services.crawler_manager import crawler_manager
from ..services.research_store import ResearchStore, PLATFORMS
from ..services.research_ai import ResearchAI, AISettings, PRESETS

router = APIRouter(prefix='/research', tags=['research'])
Platform = Literal['xhs', 'dy', 'bili']


class KeywordRequest(BaseModel):
    description: str = Field(min_length=2, max_length=10000)
    platforms: list[Platform] = Field(min_length=1, max_length=3)

    @field_validator('platforms')
    @classmethod
    def unique(cls, value):
        return list(dict.fromkeys(value))


class CaptureSettings(BaseModel):
    max_posts: int = Field(50, ge=1, le=10000)
    max_comments: int = Field(50, ge=1, le=10000)
    enable_comments: bool = True
    headless: bool = False
    request_interval: float = Field(5, ge=1, le=300, allow_inf_nan=False)


class RunRequest(KeywordRequest):
    name: str = Field('', max_length=100)
    keyword_job_id: str
    keywords: dict[Platform, list[str]]
    config: CaptureSettings = Field(default_factory=CaptureSettings)

    @model_validator(mode='after')
    def words(self):
        if set(self.keywords) != set(self.platforms):
            raise ValueError('关键词平台必须与选中平台一致')
        for platform, words in self.keywords.items():
            words = list(dict.fromkeys(w.strip() for w in words))
            if not 1 <= len(words) <= 3 or any(not w or len(w) > 100 or ',' in w or '，' in w or '\n' in w for w in words):
                raise ValueError('每个平台需要1至3组非空关键词，每组最多100字且不能含逗号或换行')
            self.keywords[platform] = words
        return self


class Workflow:
    def __init__(self, store=None):
        self.store = store or ResearchStore()
        self.ai = ResearchAI(self.store)
        self.task = None
        self.stopping = False

    async def launch(self, body):
        job = self.store.job(body.keyword_job_id)
        if job['kind'] != 'keywords' or job['status'] != 'completed' or job['result'].get('questions'):
            raise ValueError('请先完成关键词解析并解决澄清问题')
        if job['payload']['description'] != body.description or set(job['payload']['platforms']) != set(body.platforms):
            raise ValueError('目标描述或平台已变化，请重新解析关键词')
        async with crawler_manager._lock:
            if crawler_manager.research_owner or (crawler_manager.process and crawler_manager.process.poll() is None):
                raise ValueError('已有采集任务正在运行，请先等待结束或停止')
            # Finish importing historic IDs before enabling persistent deduplication.
            await asyncio.to_thread(self.store.library.sync)
            if self.store.library.state['errors']:
                raise ValueError('历史数据尚未同步完整，请在资料库检查来源错误并重新同步后再启动，避免把旧内容误算为新增')
            run = self.store.create_run(body.model_dump())
            crawler_manager.research_owner = run['id']
            self.stopping = False
            self.store.set_run(run['id'], 'running')
            self.task = asyncio.create_task(self.capture(run['id']))
            return self.store.run(run['id'])

    async def capture(self, rid):
        run = self.store.run(rid)
        config = run['config']
        try:
            for platform in run['platforms']:
                self.store.set_platform(rid, platform, 'running')
                request = CrawlerStartRequest(platform=platform, keywords=','.join(run['keywords'][platform]),
                    enable_comments=config['enable_comments'], enable_sub_comments=False, enable_media=False,
                    max_notes_count=config['max_posts'], max_comments_count=config['max_comments'], headless=config['headless'],
                    save_option='jsonl', rate_mode='gentle', request_interval=config['request_interval'])
                try:
                    success = await crawler_manager.start(request, {'MC_RESEARCH_ID': rid, 'MC_RESEARCH_HOME': str(self.store.library.home)})
                    if not success:
                        raise ValueError('采集进程无法启动')
                    await crawler_manager._read_task
                    if self.stopping:
                        raise asyncio.CancelledError()
                    code = crawler_manager.process.poll()
                    task = next(t for t in self.store.run(rid, jobs=False)['tasks'] if t['platform'] == platform)
                    if code != 0 or crawler_manager.status == 'error':
                        if task['status'] != 'failed':
                            self.store.failure(rid, platform)
                        self.store.set_platform(rid, platform, 'failed', task['reason'] or f'平台进程异常结束（退出码 {code}），请查看采集日志并处理登录或平台限制')
                    else:
                        self.store.set_platform(rid, platform, 'partial' if task['failures'] else 'completed', '部分内容未能获取' if task['failures'] else '正常结束；达到检查限额、分页安全上限或平台无更多结果')
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.store.failure(rid, platform)
                    self.store.set_platform(rid, platform, 'failed', '平台执行失败，请检查采集日志')
            result = self.store.run(rid, jobs=False)
            failed = any(t['status'] != 'completed' for t in result['tasks'])
            useful = result['counts']['new_posts'] + result['counts']['old_posts'] > 0 or any(t['status'] == 'completed' for t in result['tasks'])
            self.store.set_run(rid, 'partial' if failed and useful else 'failed' if failed else 'completed', '部分平台或内容失败，请查看平台详情' if failed else '')
        except asyncio.CancelledError:
            await crawler_manager.stop()
            for task in self.store.run(rid, jobs=False)['tasks']:
                if task['status'] in ('running', 'waiting'):
                    self.store.set_platform(rid, task['platform'], 'stopped', '用户停止或服务关闭')
            self.store.set_run(rid, 'stopped', '采集已停止，已保存内容可分析')
        except Exception:
            self.store.set_run(rid, 'failed', '批次执行异常，已保存内容保留')
        finally:
            crawler_manager.research_owner = None

    async def stop(self, rid):
        if crawler_manager.research_owner != rid or not self.task or self.task.done():
            raise ValueError('该批次未在运行')
        self.stopping = True
        self.store.set_run(rid, 'stopping')
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        if crawler_manager.research_owner == rid:
            # A task cancelled before its first turn never enters capture's finally block.
            await crawler_manager.stop()
            for task in self.store.run(rid, jobs=False)['tasks']:
                if task['status'] in ('running', 'waiting'):
                    self.store.set_platform(rid, task['platform'], 'stopped', '用户停止或服务关闭')
            self.store.set_run(rid, 'stopped', '采集已停止，已保存内容可分析')
            crawler_manager.research_owner = None
        return self.store.run(rid)


@lru_cache(maxsize=1)
def workflow():
    return Workflow()


def public_job(job):
    return {k: v for k, v in job.items() if k not in ('payload', 'state', 'config_hash') } | {
        'progress': {k: job['state'].get(k, 0) for k in ('chunk_index', 'merge_index', 'calls')},
        'usage': job['state'].get('usage', {}),
    }


@router.get('/settings')
def settings():
    return {'settings': workflow().ai.settings.public(), 'presets': PRESETS}


@router.put('/settings')
def save_settings(body: AISettings):
    current = workflow()
    if current.ai.tasks:
        raise HTTPException(409, 'AI任务运行期间不能修改配置')
    return current.ai.settings.save(body)


@router.post('/keywords/estimate')
def estimate_keywords(body: KeywordRequest):
    return public_job(workflow().ai.estimate_keywords(body.description, body.platforms))


@router.get('/jobs/{jid}')
def get_job(jid: str):
    return public_job(workflow().store.job(jid))


@router.post('/jobs/{jid}/confirm')
async def confirm_job(jid: str):
    return public_job(workflow().ai.confirm(jid))


@router.post('/jobs/{jid}/reestimate')
def reestimate_job(jid: str):
    return public_job(workflow().ai.reestimate(jid))


@router.get('/runs')
def runs():
    return workflow().store.runs()


@router.post('/runs')
async def launch(body: RunRequest):
    return await workflow().launch(body)


@router.get('/runs/{rid}')
def run(rid: str):
    return workflow().store.run(rid)


@router.post('/runs/{rid}/stop')
async def stop(rid: str):
    return await workflow().stop(rid)


@router.post('/runs/{rid}/estimate')
def estimate_analysis(rid: str):
    return public_job(workflow().ai.estimate_analysis(rid))


@router.get('/runs/{rid}/contents')
def contents(rid: str, offset: int = Query(0, ge=0), limit: int = Query(24, ge=1, le=100)):
    store = workflow().store
    store.run(rid)
    with store.connect() as db:
        rows = db.execute('''SELECT i.platform,i.item_id AS post_id,i.is_new,p.title,substr(p.body,1,240) AS excerpt,p.original_url,
            (SELECT count(*) FROM research_titles t JOIN research_titles own ON own.platform=p.platform AND own.post_id=p.post_id
             WHERE own.title_key<>'' AND t.title_key=own.title_key AND NOT(t.platform=p.platform AND t.post_id=p.post_id)) AS similar_titles
            FROM research_items i JOIN posts p ON p.platform=i.platform AND p.post_id=i.post_id
            WHERE i.run_id=? AND i.kind='post' ORDER BY i.is_new DESC,i.platform,i.item_id LIMIT ? OFFSET ?''', (rid, limit + 1, offset)).fetchall()
        return {'items': [dict(r) for r in rows[:limit]], 'next': offset + limit if len(rows) > limit else None}


@router.get('/jobs/{jid}/export')
def export_report(jid: str):
    job = workflow().store.job(jid)
    if job['kind'] != 'analysis' or job['status'] != 'completed':
        raise ValueError('报告尚未完成')
    def generate():
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(['批次', '报告', '需求', '提及次数', '涉及帖子数', '平台', '内容类型', '帖子ID', '内容ID', '原文证据', '原文链接'])
        yield '\ufeff' + buffer.getvalue()
        for demand in job['result']['demands']:
            for evidence in demand['evidence']:
                buffer.seek(0)
                buffer.truncate(0)
                row = [job['run_id'], jid, demand['name'], demand['mentions'], demand['posts'], evidence['platform'], evidence['kind'], evidence['post_id'], evidence['item_id'], evidence['quote'], evidence['original_url']]
                writer.writerow(["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v for v in row])
                yield buffer.getvalue()
    return StreamingResponse(generate(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': f'attachment; filename="research-{job["run_id"]}-{jid}.csv"'})
