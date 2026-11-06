import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowUpRight, BookOpen, FlaskConical, Settings, Search, History } from 'lucide-react'
import { Dialog, DialogContent, DialogTitle, DialogDescription } from '@/components/ui/dialog'

type Platform = 'xhs'|'dy'|'bili'
type Counts = {hits:number;checked:number;new_posts:number;old_posts:number;new_comments:number;duplicate_comments:number;failures:number}
type Evidence = {id:string;platform:Platform;kind:string;post_id:string;item_id:string;quote:string;original_url:string}
type Demand = {name:string;problem:string;scene:string;explicit:string;suggestion:string;mentions:number;posts:number;platforms:Record<string,number>;evidence:Evidence[]}
type Usage = {prompt_tokens?:number;completion_tokens?:number;total_tokens?:number;estimated_calls?:number}
type KeywordResult = {understanding:string;product:string;audience:string;scenes:string[];questions:string[];keywords:Partial<Record<Platform,{text:string;reason:string}[]>>}
type Budget = {provider:string;model:string;documents:number;input_tokens:number;output_tokens:number;calls:number;limit_tokens:number;cost:number|null;method:string;additional_tokens?:number;retry_note?:string}
type Job = {id:string;kind:string;status:string;error:string;created_at:number;budget:Budget;progress?:{chunk_index:number;merge_index:number;calls:number};usage?:Usage;result:Partial<KeywordResult>&{summary?:string;demands?:Demand[];usage?:Usage}}
type Task = Counts & {platform:Platform;status:string;reason:string;started_at:number;ended_at:number}
type Run = {id:string;name:string;description:string;status:string;created_at:number;started_at:number;ended_at:number;error:string;counts:Counts;tasks:Task[];jobs?:Job[];keywords:Record<Platform,string[]>}
type SettingsData = {provider:string;base_url:string;api_key:string;model:string;context_tokens:number;max_output_tokens:number;input_price:number|null;output_price:number|null;has_key?:boolean}
const platforms:Platform[] = ['xhs','dy','bili']
const names:Record<string,string> = {xhs:'小红书',dy:'抖音',bili:'B站',deepseek:'DeepSeek',kimi:'Kimi',doubao:'豆包',custom:'自定义接口'}
const states:Record<string,string> = {waiting:'等待',estimated:'待确认',running:'运行中',stopping:'停止中',completed:'完成',partial:'部分失败',failed:'失败',stopped:'已停止',interrupted:'已中断',needs_budget:'预算不足，已暂停'}
const terminal = (s:string) => !['waiting','running','stopping'].includes(s)
const date = (n:number) => n?new Date(n).toLocaleString('zh-CN'):'—'
const fmt = (n=0) => n.toLocaleString('zh-CN')
const library = `${location.protocol}//${location.hostname}:${Number(location.port||8080)+1}`
async function api<T>(url:string, body?:unknown, method='POST'):Promise<T> {
  const response = await fetch('/api/research'+url, body===undefined?undefined:{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
  const data=await response.json()
  if(!response.ok) throw new Error(typeof data.detail==='string'?data.detail:'输入无效，请检查关键词、数量或AI设置')
  return data
}

export function ResearchPage() {
  const client=useQueryClient()
  const [tab,setTab]=useState('new'),[error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false)
  const [description,setDescription]=useState(''),[name,setName]=useState(''),[selectedPlatforms,setPlatforms]=useState<Platform[]>(platforms)
  const [words,setWords]=useState<Record<Platform,string>>({xhs:'',dy:'',bili:''}),[keywordJob,setKeywordJob]=useState<Job|null>(null)
  const [maxPosts,setMaxPosts]=useState(50),[maxComments,setMaxComments]=useState(50),[comments,setComments]=useState(true)
  const [pending,setPending]=useState<Job|null>(null),[activeJob,setActiveJob]=useState(''),[selectedRun,setSelectedRun]=useState('')
  const [selectedReport,setSelectedReport]=useState('')
  const running=useRef(new Set<string>()),applied=useRef('')
  const runs=useQuery({queryKey:['research-runs'],queryFn:()=>api<Run[]>('/runs'),refetchInterval:3000})
  const detail=useQuery({queryKey:['research-run',selectedRun],queryFn:()=>api<Run>('/runs/'+selectedRun),enabled:!!selectedRun,refetchInterval:3000})
  const job=useQuery({queryKey:['research-job',activeJob],queryFn:()=>api<Job>('/jobs/'+activeJob),enabled:!!activeJob,refetchInterval:q=>q.state.data?.status==='running'?1000:false})
  useEffect(()=>{
    for(const run of runs.data||[]) {
      if(!terminal(run.status)) running.current.add(run.id)
      else if(running.current.delete(run.id)&&sessionStorage.getItem('research-notified-'+run.id)!=='1') {
        sessionStorage.setItem('research-notified-'+run.id,'1')
        setNotice(`${run.name}：${states[run.status]}。新增帖子 ${fmt(run.counts.new_posts)} 条、评论 ${fmt(run.counts.new_comments)} 条，跳过旧帖 ${fmt(run.counts.old_posts)} 条。`)
      }
    }
  },[runs.data])
  useEffect(()=>{
    const current=job.data
    if(current?.status==='completed'&&current.kind==='keywords'&&applied.current!==current.id) {
      applied.current=current.id;setKeywordJob(current)
      setWords({xhs:(current.result.keywords?.xhs||[]).map(w=>w.text).join('\n'),dy:(current.result.keywords?.dy||[]).map(w=>w.text).join('\n'),bili:(current.result.keywords?.bili||[]).map(w=>w.text).join('\n')})
    }
  },[job.data])
  async function act(fn:()=>Promise<void>) {setBusy(true);setError('');try{await fn()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  function changedDescription(value:string) {setDescription(value);setKeywordJob(null)}
  async function launch() {
    if(!keywordJob)return
    await act(async()=>{
      const run=await api<Run>('/runs',{name,description,platforms:selectedPlatforms,keyword_job_id:keywordJob.id,
        keywords:Object.fromEntries(selectedPlatforms.map(p=>[p,words[p].split('\n').map(w=>w.trim()).filter(Boolean)])),
        config:{max_posts:maxPosts,max_comments:maxComments,enable_comments:comments,headless:false,request_interval:5}})
      setSelectedRun(run.id);setSelectedReport('');setTab('runs');running.current.add(run.id)
      await client.invalidateQueries({queryKey:['research-runs']})
    })
  }
  const reports=detail.data?.jobs?.filter(j=>j.kind==='analysis')||[]
  const reportMeta=reports.find(j=>j.id===selectedReport)||reports.find(j=>j.status==='completed')||reports[0]
  const reportQuery=useQuery({queryKey:['research-report',reportMeta?.id],queryFn:()=>api<Job>('/jobs/'+reportMeta!.id),enabled:!!reportMeta,refetchInterval:q=>q.state.data?.status==='running'?1500:false})
  useEffect(()=>{if(reportMeta?.id)void client.invalidateQueries({queryKey:['research-report',reportMeta.id]})},[client,reportMeta?.id,reportMeta?.status])
  const report=reportQuery.data||reportMeta
  const keywordBusy=job.data?.kind==='keywords'&&job.data.status==='running'
  return <div className="lib-page research-page">
    <header className="lib-header"><a className="lib-brand" href="/research"><FlaskConical size={23}/><strong>需求研究</strong><span>MediaCrawler</span></a><div className="research-actions"><a className="lib-link" href="/">采集控制台</a><a className="lib-link" href={library} target="_blank" rel="noreferrer"><BookOpen size={16}/>资料库<ArrowUpRight size={14}/></a></div></header>
    <div className="lib-intro"><div><span className="lib-eyebrow">LISTEN · DISCOVER · BUILD</span><h1>找到大家反复提到的需求。</h1><p>还不知道研究什么？先看看大家在吐槽哪些物品，再用原文证据核对每一次提及。</p></div></div>
    <main className="research-main">
      <nav className="research-tabs" aria-label="研究功能">{[['new','开始研究',Search],['runs','批次与报告',History],['settings','AI设置',Settings]].map(([key,label,Icon])=><button key={String(key)} className={tab===key?'active':''} aria-current={tab===key?'page':undefined} onClick={()=>setTab(String(key))}>{typeof Icon!=='string'&&<Icon size={17}/>} {String(label)}</button>)}</nav>
      {(error||runs.error||detail.error||reportQuery.error)&&<div role="alert" className="lib-alert">{error||(runs.error as Error)?.message||(detail.error as Error)?.message||(reportQuery.error as Error)?.message}</div>}
      {notice&&<div className="research-notice" role="status">{notice}<button aria-label="关闭完成提示" onClick={()=>setNotice('')}>×</button></div>}
      {tab==='settings'&&<SettingsPanel onSaved={()=>{setPending(null);setNotice('AI配置已保存。原有未执行预算需要重新预估。')}} onError={setError}/>}
      {tab==='new'&&<div className="research-grid"><section className="research-panel"><h2>1. 描述研究目标</h2><p className="research-muted">可以指定产品或场景，也可以只说“想发现大家在吐槽哪些物品”，不必先知道具体需求。</p>
        <label>批次名称（可选）<input value={name} maxLength={100} onChange={e=>setName(e.target.value)} placeholder="留空自动生成北京时间批次号：年月日-时分秒"/></label>
        <label>想了解什么？<textarea rows={6} maxLength={10000} value={description} onChange={e=>changedDescription(e.target.value)} placeholder="例如：我还不知道要做什么产品，想看看大家最近在吐槽哪些日常物品、有什么使用烦恼或希望改进的地方。也可以指定某种产品或生活场景。"/></label>
        <fieldset><legend>采集平台</legend><div className="research-actions">{platforms.map(p=><label className="research-check" key={p}><input type="checkbox" checked={selectedPlatforms.includes(p)} onChange={e=>{setPlatforms(old=>e.target.checked?[...old,p]:old.filter(v=>v!==p));setKeywordJob(null)}}/>{names[p]}</label>)}</div></fieldset>
        <button className="lib-button lib-primary" disabled={busy||keywordBusy||description.trim().length<2||!selectedPlatforms.length} onClick={()=>void act(async()=>setPending(await api<Job>('/keywords/estimate',{description,platforms:selectedPlatforms})))}>{keywordBusy?'正在解析关键词…':'预估关键词解析用量'}</button>
        {job.data?.kind==='keywords'&&['failed','interrupted','needs_budget'].includes(job.data.status)&&<div role="alert" className="lib-alert">{job.data.error}<button className="lib-button" onClick={()=>void act(async()=>setPending(await api<Job>('/jobs/'+job.data!.id+'/reestimate',{})))}>重新预估</button></div>}
      </section><section className="research-panel"><h2>2. 检查关键词并采集</h2>
        {!keywordJob?<p className="research-muted">先解析描述。AI返回的搜索词会显示在这里，检查后再启动采集。</p>:<><p>{keywordJob.result.understanding}</p><p className="research-muted">研究范围：{keywordJob.result.product} · 人群：{keywordJob.result.audience}</p>
          {!!keywordJob.result.questions?.length&&<div className="research-notice"><strong>需要补充的信息</strong>{keywordJob.result.questions.map(q=><p key={q}>{q}</p>)}<p>补充左侧描述后重新预估解析，暂不能启动。</p></div>}
          {selectedPlatforms.map(p=><label key={p}>{names[p]}关键词（每行一组，最多三组）<textarea aria-label={`${names[p]}关键词（每行一组，最多三组）`} rows={3} value={words[p]} onChange={e=>setWords(old=>({...old,[p]:e.target.value}))}/><small>{keywordJob.result.keywords?.[p]?.map(k=>`${k.text}：${k.reason}`).join('；')}</small></label>)}
          <div className="research-two"><label>每平台检查帖子上限<input type="number" min={1} max={10000} value={maxPosts} onChange={e=>setMaxPosts(Number(e.target.value))}/></label><label>每帖一级评论上限<input type="number" min={1} max={10000} value={maxComments} onChange={e=>setMaxComments(Number(e.target.value))}/></label></div>
          <label className="research-check"><input type="checkbox" checked={comments} onChange={e=>setComments(e.target.checked)}/>采集一级评论</label>
          <p className="research-muted">多关键词共用检查限额。旧帖跳过详情与评论；不下载媒体。平台浏览器打开后，按需完成扫码登录。</p>
          <button className="lib-button lib-primary" disabled={busy||!!keywordJob.result.questions?.length||selectedPlatforms.some(p=>!words[p].trim())} onClick={()=>void launch()}>检查完毕，启动本次采集</button>
        </>}
      </section></div>}
      {tab==='runs'&&<div className="research-grid research-history"><aside className="research-panel"><h2>采集批次</h2>{runs.isPending?<p>加载中…</p>:!runs.data?.length?<p className="research-muted">还没有研究批次。</p>:runs.data.map(r=><button className={`research-run ${selectedRun===r.id?'selected':''}`} key={r.id} onClick={()=>{setSelectedRun(r.id);setSelectedReport('')}}><strong>{r.name}</strong><span>{date(r.created_at)} · {states[r.status]||r.status}</span><small>帖子 +{fmt(r.counts.new_posts)} · 评论 +{fmt(r.counts.new_comments)} · 旧帖 {fmt(r.counts.old_posts)}</small></button>)}</aside>
        <section className="research-panel">{!detail.data?<p className="research-muted">选择批次，查看统计、原文和需求报告。</p>:<>
          <div className="research-heading"><div><h2>{detail.data.name}</h2><p className="research-muted">{detail.data.id} · {states[detail.data.status]}</p></div>{!terminal(detail.data.status)&&<button className="lib-button" disabled={busy} onClick={()=>void act(async()=>{await api('/runs/'+selectedRun+'/stop',{});await detail.refetch();await runs.refetch()})}>停止采集</button>}</div>
          <p>{detail.data.description}</p><p className="research-muted">开始 {date(detail.data.started_at)} · 结束 {date(detail.data.ended_at)}{detail.data.ended_at&&detail.data.started_at?` · 耗时 ${Math.round((detail.data.ended_at-detail.data.started_at)/1000)} 秒`:''}</p>
          <div className="research-counts">{([['检查帖子','checked'],['新增帖子','new_posts'],['新增评论','new_comments'],['跳过旧帖','old_posts'],['失败','failures']] as const).map(([label,key])=><div key={key}><strong>{fmt(detail.data!.counts[key])}</strong><span>{label}</span></div>)}</div>
          <details><summary>平台统计与搜索词</summary><div className="research-table-wrap"><table><thead><tr><th>平台</th><th>状态</th><th>命中</th><th>检查</th><th>新帖</th><th>旧帖</th><th>新评论</th><th>重复评论</th><th>失败</th></tr></thead><tbody>{detail.data.tasks.map(t=><tr key={t.platform}><td>{names[t.platform]}</td><td>{states[t.status]}</td>{(['hits','checked','new_posts','old_posts','new_comments','duplicate_comments','failures'] as const).map(k=><td key={k}>{fmt(t[k])}</td>)}</tr>)}</tbody></table></div>{detail.data.tasks.map(t=><p key={t.platform}>{names[t.platform]}：{detail.data!.keywords[t.platform].join('、')}<br/><small>{t.reason} · {date(t.started_at)} → {date(t.ended_at)}</small></p>)}</details>
          {detail.data.error&&<p role="alert" className="lib-alert">{detail.data.error}</p>}
          <div className="research-actions"><a className="lib-button" href={`${library}/?run_id=${selectedRun}`} target="_blank" rel="noreferrer">查看本批内容 / 导出 CSV</a><button className="lib-button lib-primary" disabled={busy||!terminal(detail.data.status)||reports.some(j=>j.status==='running')} onClick={()=>void act(async()=>setPending(await api<Job>('/runs/'+selectedRun+'/estimate',{})))}>预估本次需求分析用量</button></div>
          <Contents rid={selectedRun}/>
          <hr/><h2>需求报告</h2>{reports.length>0&&<label>分析版本<select value={report?.id||''} onChange={e=>setSelectedReport(e.target.value)}>{reports.map(j=><option key={j.id} value={j.id}>{date(j.created_at)} · {states[j.status]} · {names[j.budget.provider]} / {j.budget.model}</option>)}</select></label>}
          {!report?<p className="research-muted">采集结束后，先预估并确认，再生成需求报告。</p>:<><p className="research-muted">{states[report.status]} · {names[report.budget.provider]} / {report.budget.model} · 用量 {fmt(report.result.usage?.total_tokens||report.usage?.total_tokens||0)} token</p>{report.status==='running'&&<p role="status">后台分析中，可以离开此页，结果会保存到本批次。</p>}{report.error&&<div role="alert" className="lib-alert">{report.error}</div>}{['failed','interrupted','needs_budget'].includes(report.status)&&<button className="lib-button" disabled={busy} onClick={()=>void act(async()=>setPending(await api<Job>('/jobs/'+report.id+'/reestimate',{})))}>重新预估并继续</button>}{report.status==='completed'&&<Report job={report} run={detail.data} onError={setError}/>}</>}
        </>}</section></div>}
    </main>
    <Dialog open={!!pending} onOpenChange={open=>{if(!open)setPending(null)}}><DialogContent className="research-budget"><DialogTitle>确认 AI 用量预算</DialogTitle><DialogDescription>确认后，材料才会发送到下方服务商。整次分析预算包含分块提取、归类和最终汇总。</DialogDescription>{pending&&<><dl><dt>服务商 / 模型</dt><dd>{names[pending.budget.provider]} / {pending.budget.model}</dd><dt>分析文字条数</dt><dd>{pending.kind==='keywords'?'一段研究描述':fmt(pending.budget.documents)}</dd><dt>预计输入</dt><dd>{fmt(pending.budget.input_tokens)} token</dd><dt>输出预算</dt><dd>{fmt(pending.budget.output_tokens)} token</dd><dt>预计请求次数</dt><dd>{pending.budget.calls} 次（超预算会暂停）</dd><dt>确认总预算</dt><dd>{fmt(pending.budget.limit_tokens)} token</dd><dt>估算金额</dt><dd>{pending.budget.cost==null?'未配置单价，仅展示 token':`约 ¥${pending.budget.cost.toFixed(4)}`}</dd></dl><p>{pending.budget.method}</p>{pending.budget.retry_note&&<p>{pending.budget.retry_note} 本次追加可用预算约 {fmt(pending.budget.additional_tokens)} token。</p>}<div className="research-actions"><button className="lib-button" disabled={busy} onClick={()=>setPending(null)}>取消</button><button className="lib-button lib-primary" disabled={busy} onClick={()=>void act(async()=>{const j=await api<Job>('/jobs/'+pending.id+'/confirm',{});setActiveJob(j.id);setPending(null);await client.invalidateQueries({queryKey:['research-run']})})}>确认发送并执行</button></div></>}</DialogContent></Dialog>
  </div>
}

function SettingsPanel({onSaved,onError}:{onSaved:()=>void;onError:(s:string)=>void}) {
  const query=useQuery({queryKey:['research-settings'],queryFn:()=>api<{settings:SettingsData;presets:Record<string,{base_url:string;model:string}>}>('/settings')})
  const [form,setForm]=useState<SettingsData|null>(null),[saving,setSaving]=useState(false)
  useEffect(()=>{if(query.data)setForm({...query.data.settings,api_key:''})},[query.data])
  if(query.error)return <div className="lib-alert" role="alert">{(query.error as Error).message}</div>
  if(!form)return <p>加载配置…</p>
  const patch=(value:Partial<SettingsData>)=>setForm(old=>old?{...old,...value}:old)
  return <form className="research-panel research-settings" onSubmit={e=>{e.preventDefault();setSaving(true);onError('');const {has_key,...body}=form;void api<SettingsData>('/settings',body,'PUT').then(result=>{setForm({...result,api_key:''});onSaved()}).catch(e=>onError(e.message)).finally(()=>setSaving(false))}}><h2>本地 AI 配置</h2><p className="research-muted">模型名称按服务商控制台填写。保存不触发模型调用；API Key 只保存在本机后端。</p>
    <div className="research-two"><label>服务商<select value={form.provider} onChange={e=>{const provider=e.target.value;patch({provider,base_url:query.data!.presets[provider].base_url,model:'',api_key:'',has_key:false})}}>{Object.keys(names).filter(k=>!platforms.includes(k as Platform)).map(p=><option value={p} key={p}>{names[p]}</option>)}</select></label><label>模型名称<input required value={form.model} onChange={e=>patch({model:e.target.value})} placeholder="填写模型ID或已开通的接入点ID"/></label></div>
    <label>API 基础地址<input required type="url" value={form.base_url} onChange={e=>patch({base_url:e.target.value})}/></label><label>API Key<input type="password" autoComplete="new-password" value={form.api_key} onChange={e=>patch({api_key:e.target.value})} placeholder={form.has_key?'已配置；留空保留同一服务地址的密钥':'填写密钥'}/></label>
    <div className="research-two"><label>模型上下文长度（token）<input required type="number" min={4096} max={2000000} value={form.context_tokens} onChange={e=>patch({context_tokens:Number(e.target.value)})}/></label><label>每次最大输出（token）<input required type="number" min={512} max={65536} value={form.max_output_tokens} onChange={e=>patch({max_output_tokens:Number(e.target.value)})}/></label></div>
    <div className="research-two"><label>输入单价（元 / 百万 token，可选）<input type="number" min={0} step="any" value={form.input_price??''} onChange={e=>patch({input_price:e.target.value===''?null:Number(e.target.value)})}/></label><label>输出单价（元 / 百万 token，可选）<input type="number" min={0} step="any" value={form.output_price??''} onChange={e=>patch({output_price:e.target.value===''?null:Number(e.target.value)})}/></label></div><button className="lib-button lib-primary" disabled={saving}>{saving?'保存中…':'保存本地配置'}</button>
  </form>
}

function Contents({rid}:{rid:string}) {
  const [offset,setOffset]=useState(0)
  const result=useQuery({queryKey:['research-contents',rid,offset],queryFn:()=>api<{items:{platform:Platform;post_id:string;title:string;excerpt:string;is_new:number;similar_titles:number}[];next:number|null}>(`/runs/${rid}/contents?offset=${offset}`),refetchInterval:3000})
  useEffect(()=>setOffset(0),[rid])
  return <details className="research-contents"><summary>本批帖子（含旧帖命中）</summary>{result.error?<p role="alert">{(result.error as Error).message}</p>:result.data?.items.map(p=><article key={p.platform+p.post_id}><a className="lib-link" href={`${library}/?run_id=${rid}&platform=${p.platform}&post_id=${encodeURIComponent(p.post_id)}`} target="_blank" rel="noreferrer">{p.title||p.excerpt.slice(0,40)||'无标题'} ↗</a><small>{names[p.platform]} · {p.is_new?'新增，纳入分析':'旧内容，仅关联，不纳入本次分析'}{p.similar_titles?` · 有 ${p.similar_titles} 条同标题内容，仅提示，已保留`:''}</small></article>)}<div className="research-actions"><button className="lib-button" disabled={!offset} onClick={()=>setOffset(o=>Math.max(0,o-24))}>上一页</button><button className="lib-button" disabled={result.data?.next==null} onClick={()=>setOffset(result.data!.next!)}>下一页</button></div></details>
}

function Report({job,run,onError}:{job:Job;run:Run;onError:(s:string)=>void}) {
  const demands=job.result.demands||[]
  return <div className="research-report"><p className="research-muted">分析新增文字 {fmt(job.budget.documents)} 条 · 批次采集状态：{states[run.status]} · 各需求可能共享证据，提及次数不能相加当作人数。{job.result.usage?.estimated_calls?'部分调用未返回 usage，用量含保守估算。':''}</p>
    <div className="research-actions"><a className="lib-button" href={`/api/research/jobs/${job.id}/export`}>导出需求和证据 CSV</a><button className="lib-button" onClick={()=>{const text=`批次：${run.name}（${run.id}）\n报告：${job.id}\n${job.result.summary}\n\n`+demands.map(d=>`${d.name}：提及 ${d.mentions} 次，涉及 ${d.posts} 篇帖子\n明确诉求：${d.explicit}\nAI建议：${d.suggestion}`).join('\n\n');void navigator.clipboard.writeText(text).catch(()=>onError('复制失败，请使用CSV导出'))}}>复制报告</button></div>
    <p className="research-summary">{job.result.summary}</p>{demands.map((d,index)=><article className="research-demand" key={index}><div className="research-heading"><h3>{index+1}. {d.name}</h3><strong>{fmt(d.mentions)} 次提及</strong></div><p>{d.problem}</p><p className="research-muted">涉及 {fmt(d.posts)} 篇帖子 · {Object.entries(d.platforms).map(([p,n])=>`${names[p]} ${n}次`).join(' / ')}{d.scene?` · ${d.scene}`:''}</p>{d.explicit&&<p><strong>用户明确诉求：</strong>{d.explicit}</p>}{d.suggestion&&<p><strong>AI推测建议：</strong>{d.suggestion}</p>}<details><summary>核对 {d.evidence.length} 份原文证据</summary>{d.evidence.map(e=><blockquote key={e.id}><p>“{e.quote}”</p><small>{names[e.platform]} · {e.kind==='post'?'帖子正文':'评论'} · {e.item_id}</small><div className="research-actions"><a className="lib-link" href={`${library}/?run_id=${run.id}&platform=${e.platform}&post_id=${encodeURIComponent(e.post_id)}`} target="_blank" rel="noreferrer">查看关联帖子</a>{e.original_url&&/^https?:\/\//.test(e.original_url)&&<a className="lib-link" href={e.original_url} target="_blank" rel="noreferrer">平台原文 ↗</a>}</div></blockquote>)}</details></article>)}</div>
}
