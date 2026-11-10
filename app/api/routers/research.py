"""Research workflow endpoints; all model requests require a claimed estimate."""
import asyncio
import csv
import json
from urllib.parse import urlparse
from functools import lru_cache
from io import StringIO
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from ..schemas import CrawlerStartRequest
from ..services.crawler_manager import crawler_manager
from ..services.research_store import ResearchStore, PLATFORMS, ROOT
from ..services.research_ai import ResearchAI, AISettings, PRESETS
from tools import research_login

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
    max_posts: int = Field(50, ge=1, le=1000000)
    max_comments: int = Field(50, ge=1, le=1000000)
    enable_comments: bool = True
    headless: bool = False
    request_interval: float = Field(5, ge=1, le=300, allow_inf_nan=False)
    login_type: Literal['qrcode', 'cookie', 'phone'] = 'qrcode'
    save_option: Literal['sqlite', 'jsonl', 'json', 'csv', 'excel', 'db', 'mongodb'] = 'sqlite'
    enable_sub_comments: bool = False
    enable_media: bool = False
    start_page: int = Field(1, ge=1, le=1000000)
    stop_condition: Literal['count', 'exhausted'] = 'count'
    rate_mode: Literal['auto', 'gentle'] = 'gentle'
    rest_every: int = Field(100, ge=10, le=10000)
    rest_seconds: float = Field(60, ge=10, le=3600, allow_inf_nan=False)


class CapturePreferences(CaptureSettings):
    cookies: dict[Platform, str] = Field(default_factory=dict)


CAPTURE_FILE = ROOT / 'config/capture.local.json'


def capture_preferences():
    return CapturePreferences.model_validate_json(CAPTURE_FILE.read_text(encoding='utf-8')) if CAPTURE_FILE.exists() else CapturePreferences()


def capture_public(settings):
    return settings.model_dump(exclude={'cookies'}) | {'has_cookies': {p: bool(settings.cookies.get(p)) for p in PLATFORMS}}


def target_type(platform, values):
    kinds = set()
    hosts = {'xhs': ('xiaohongshu.com', 'xhslink.com'), 'dy': ('douyin.com', 'iesdouyin.com'), 'bili': ('bilibili.com', 'b23.tv')}
    for value in values:
        if '://' not in value:
            continue
        url = urlparse(value)
        if url.scheme not in ('https', 'http') or not any(url.hostname == h or (url.hostname or '').endswith('.' + h) for h in hosts[platform]):
            raise ValueError('目标链接与所选平台不匹配')
        kinds.add('creator' if '/user/' in url.path or url.hostname == 'space.bilibili.com' else 'detail')
    if len(kinds) > 1:
        raise ValueError('同一平台的内容链接和创作者主页请分开采集')
    return next(iter(kinds), 'detail')


class RunRequest(KeywordRequest):
    name: str = Field('', max_length=100)
    description: str = Field(default='从采集内容发现使用问题和改进需求', min_length=2, max_length=10000)
    keyword_job_id: str = ''
    keywords: dict[Platform, list[str]] = Field(default_factory=dict)
    crawler_type: Literal['auto', 'search', 'detail', 'creator'] = 'auto'
    targets: dict[Platform, list[str]] = Field(default_factory=dict)
    config: CaptureSettings = Field(default_factory=CaptureSettings)

    @model_validator(mode='after')
    def words(self):
        if set(self.keywords) - set(self.platforms) or set(self.targets) - set(self.platforms):
            raise ValueError('关键词和目标平台必须与所选平台一致')
        for platform, words in self.keywords.items():
            words = list(dict.fromkeys(w.strip() for w in words if w.strip()))
            if len(words) > 100 or any(len(w) > 100 or ',' in w or '，' in w or '\n' in w for w in words):
                raise ValueError('每个平台最多100组关键词，每组最多100字且不能含逗号或换行')
            self.keywords[platform] = words
        for platform, values in self.targets.items():
            values = list(dict.fromkeys(v.strip() for v in values if v.strip()))
            if len(values) > 1000 or any(len(v) > 2000 or ',' in v or '\n' in v for v in values):
                raise ValueError('目标数量或格式无效')
            target_type(platform, values)
            self.targets[platform] = values
        return self


class Workflow:
    def __init__(self, store=None):
        self.store = store or ResearchStore()
        self.ai = ResearchAI(self.store)
        self.task = None
        self.stopping = False
        self.login_path = self.store.library.home / '.research-login.json'

    async def launch(self, body):
        plan = {}
        if body.keyword_job_id:
            job = self.store.job(body.keyword_job_id)
            if job['kind'] != 'keywords' or job['status'] != 'completed' or job['result'].get('questions'):
                raise ValueError('请先完成目标解析并解决澄清问题；也可清除描述后手动填写')
            if job['payload']['description'] != body.description or set(job['payload']['platforms']) != set(body.platforms):
                raise ValueError('目标描述或平台已变化，请重新解析或改用手动采集')
            plan = job['result'].get('crawl_plan', {})
        preferences = capture_preferences()
        config = CaptureSettings.model_validate(preferences.model_dump(exclude={'cookies'}) | body.config.model_dump(exclude_unset=True))
        modes = {}
        for platform in body.platforms:
            targets = body.targets.get(platform, [])
            mode = body.crawler_type
            if mode == 'auto':
                if targets and any('://' in value for value in targets):
                    mode = target_type(platform, targets)
                else:
                    mode = plan.get(platform, {}).get('crawler_type', 'detail' if targets else 'search')
            if mode == 'search' and not body.keywords.get(platform):
                raise ValueError(f'{PLATFORMS[platform]}需要填写关键词')
            if mode not in ('search', 'detail', 'creator'):
                raise ValueError('采集类型无效')
            if mode != 'search' and targets and any('://' in v for v in targets) and target_type(platform, targets) != mode:
                raise ValueError('目标链接与采集类型不匹配，请检查所选类型')
            if mode != 'search' and not targets:
                raise ValueError(f'{PLATFORMS[platform]}需要填写内容或创作者链接/ID')
            if config.stop_condition == 'exhausted' and (platform != 'xhs' or mode != 'search'):
                raise ValueError('无更多内容停止仅支持小红书搜索，请调整通用设置')
            if config.login_type == 'cookie' and not preferences.cookies.get(platform):
                raise ValueError(f'请在通用设置中填写{PLATFORMS[platform]}的Cookie')
            modes[platform] = mode
        snapshot = body.model_dump()
        snapshot['config'] = config.model_dump() | {'crawler_types': modes, 'targets': body.targets}
        snapshot['keywords'] = {p: body.keywords.get(p, []) for p in body.platforms}
        async with crawler_manager._lock:
            if crawler_manager.research_owner or (crawler_manager.process and crawler_manager.process.poll() is None):
                raise ValueError('已有采集任务正在运行，请先等待结束或停止')
            # Finish importing historic IDs before enabling persistent deduplication.
            await asyncio.to_thread(self.store.library.sync)
            if self.store.library.state['errors']:
                raise ValueError('历史数据尚未同步完整，请在资料库检查来源错误并重新同步后再启动，避免把旧内容误算为新增')
            run = self.store.create_run(snapshot)
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
                previous = next(t for t in run['tasks'] if t['platform'] == platform)
                if previous['status'] in ('completed', 'skipped'):
                    continue
                failures_before = previous['failures']
                self.login_path.unlink(missing_ok=True)
                self.login_path.with_name(self.login_path.name + '.action').unlink(missing_ok=True)
                self.store.set_platform(rid, platform, 'running')
                mode = config.get('crawler_types', {}).get(platform, 'search')
                targets = ','.join(config.get('targets', {}).get(platform, []))
                request = CrawlerStartRequest(platform=platform, crawler_type=mode, keywords=','.join(run['keywords'][platform]),
                    specified_ids=targets if mode == 'detail' else '', creator_ids=targets if mode == 'creator' else '',
                    login_type=config.get('login_type', 'qrcode'), cookies=capture_preferences().cookies.get(platform, '') if config.get('login_type') == 'cookie' else '',
                    enable_comments=config['enable_comments'], enable_sub_comments=config.get('enable_sub_comments', False), enable_media=config.get('enable_media', False),
                    max_notes_count=config['max_posts'], max_comments_count=config['max_comments'], headless=config['headless'],
                    save_option=config.get('save_option', 'sqlite'), rate_mode=config.get('rate_mode', 'gentle'), request_interval=config['request_interval'],
                    start_page=config.get('start_page', 1), stop_condition=config.get('stop_condition', 'count'),
                    rest_every=config.get('rest_every', 100), rest_seconds=config.get('rest_seconds', 60))
                try:
                    success = await crawler_manager.start(request, {'MC_RESEARCH_ID': rid, 'MC_RESEARCH_HOME': str(self.store.library.home), 'MC_RESEARCH_LOGIN': str(self.login_path)})
                    if not success:
                        raise ValueError('采集进程无法启动')
                    await crawler_manager._read_task
                    if self.stopping:
                        raise asyncio.CancelledError()
                    code = crawler_manager.process.poll()
                    task = next(t for t in self.store.run(rid, jobs=False)['tasks'] if t['platform'] == platform)
                    if research_login.read(self.login_path).get('phase') == 'skipped':
                        self.store.set_platform(rid, platform, 'skipped', '用户跳过此平台登录')
                    elif code != 0 or crawler_manager.status == 'error':
                        if task['status'] != 'failed':
                            self.store.failure(rid, platform)
                        self.store.set_platform(rid, platform, 'failed', task['reason'] or f'平台进程异常结束（退出码 {code}），请查看采集日志并处理登录或平台限制')
                    else:
                        with self.store.connect() as db:
                            unfinished = db.execute("SELECT count(*) FROM research_work WHERE run_id=? AND platform=? AND status!='completed'", (rid, platform)).fetchone()[0]
                        incomplete = task['failures'] > failures_before or unfinished > 0
                        self.store.set_platform(rid, platform, 'partial' if incomplete else 'completed', '部分内容未能获取，可继续采集' if incomplete else '正常结束；达到检查限额、分页安全上限或平台无更多结果')
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.store.failure(rid, platform)
                    self.store.set_platform(rid, platform, 'failed', '平台执行失败，请检查采集日志')
            result = self.store.run(rid, jobs=False)
            failed = any(t['status'] != 'completed' for t in result['tasks'])
            useful = result['counts']['new_posts'] + result['counts']['old_posts'] > 0 or any(t['status'] == 'completed' for t in result['tasks'])
            if all(t['status'] == 'skipped' for t in result['tasks']):
                self.store.set_run(rid, 'stopped', '全部平台登录均已跳过，未执行采集')
            else:
                self.store.set_run(rid, 'partial' if failed and useful else 'failed' if failed else 'completed', '部分平台或内容未完成，请查看平台详情' if failed else '')
        except asyncio.CancelledError:
            await crawler_manager.stop()
            for task in self.store.run(rid, jobs=False)['tasks']:
                if task['status'] in ('running', 'waiting'):
                    self.store.set_platform(rid, task['platform'], 'stopped', '用户停止或服务关闭')
            self.store.set_run(rid, 'stopped', '采集已停止，已保存内容可分析')
        except Exception:
            self.store.set_run(rid, 'failed', '批次执行异常，已保存内容保留')
        finally:
            self.login_path.unlink(missing_ok=True)
            self.login_path.with_name(self.login_path.name + '.action').unlink(missing_ok=True)
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

    async def resume(self, rid):
        async with crawler_manager._lock:
            if crawler_manager.research_owner or (crawler_manager.process and crawler_manager.process.poll() is None):
                raise ValueError('已有采集任务正在运行，请等待结束后再继续')
            self.store.resume_run(rid)
            crawler_manager.research_owner = rid
            self.stopping = False
            self.task = asyncio.create_task(self.capture(rid))
            return self.store.run(rid)


@lru_cache(maxsize=1)
def workflow():
    return Workflow()


def public_job(job):
    return {k: v for k, v in job.items() if k not in ('payload', 'state', 'config_hash') } | {
        'progress': {k: job['state'].get(k, 0) for k in ('chunk_index', 'merge_index', 'calls')},
        'usage': job['state'].get('usage', {}),
    }


@router.get('/capabilities')
def capabilities():
    return {'platforms': [{'id': p, 'label': name} for p, name in PLATFORMS.items()]}


@router.get('/login')
def login_status():
    current = workflow()
    if not crawler_manager.research_owner:
        return None
    state = research_login.read(current.login_path)
    return state if state.get('phase') in ('preparing', 'scanning', 'failed') else None


class LoginAction(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    action: Literal['refresh', 'skip']


@router.post('/login/action')
def login_action(body: LoginAction):
    state = login_status()
    if not state or state.get('id') != body.id:
        raise HTTPException(409, '登录状态已变化，请等待二维码更新')
    research_login.write(str(workflow().login_path) + '.action', body.model_dump())
    return {'accepted': True}


@router.get('/capture-settings')
def get_capture_settings():
    return capture_public(capture_preferences())


@router.put('/capture-settings')
def save_capture_settings(body: CapturePreferences):
    if crawler_manager.research_owner or (crawler_manager.process and crawler_manager.process.poll() is None):
        raise HTTPException(409, '请等待当前采集结束再修改通用设置')
    previous = capture_preferences()
    for platform in PLATFORMS:
        if not body.cookies.get(platform):
            body.cookies[platform] = previous.cookies.get(platform, '')
    temporary = CAPTURE_FILE.with_suffix('.json.tmp')
    temporary.write_text(body.model_dump_json(), encoding='utf-8')
    temporary.replace(CAPTURE_FILE)
    return capture_public(body)


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


@router.post('/runs/{rid}/resume')
async def resume(rid: str):
    return await workflow().resume(rid)


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
        writer.writerow(['批次', '报告', '需求', '提及次数', '涉及帖子数', '平台', '内容类型', '帖子ID', '内容ID', '原文证据', '原文链接', '归类说明'])
        yield '\ufeff' + buffer.getvalue()
        for demand in job['result']['demands']:
            for evidence in demand['evidence']:
                buffer.seek(0)
                buffer.truncate(0)
                row = [job['run_id'], jid, demand['name'], demand['mentions'], demand['posts'], evidence['platform'], evidence['kind'], evidence['post_id'], evidence['item_id'], evidence['quote'], evidence['original_url'], '\n'.join(job['result'].get('warnings', []))]
                writer.writerow(["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v for v in row])
                yield buffer.getvalue()
    return StreamingResponse(generate(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': f'attachment; filename="research-{job["run_id"]}-{jid}.csv"'})
