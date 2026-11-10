import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ArrowUpRight, BookOpen, ChevronLeft, ChevronRight, Download, EyeOff, Image as ImageIcon, RefreshCw, Search, Star } from 'lucide-react'

type Post = { platform:string; post_id:string; kind:string; title:string; excerpt:string; body?:string; author:string; published_at:number; likes:number; collects:number; comments:number; original_url:string; tag_text:string; cover:string|null; saved:number; hidden:number; user_tags:string; note:string }
type Comment = { comment_id:string; body:string; author:string; published_at:number; likes:number }
type Detail = Post & { body:string; keywords:string[]; media:{name:string;kind:string}[]; comment_items:Comment[]; indexed_comments:number; next_comment:number|null }
type Status = { posts:number; comments:number; media:number; running:boolean; last_sync:number|null; errors:{file:string;message:string}[]; platforms:string[]; keywords:string[] }
type Batches = { items:{id:string;name:string;created_at:number;status:string}[];latest_id:string }
type Filters = { q:string; exclude:string; platform:string; keyword:string; kind:string; min_likes:string; min_collects:string; min_comments:string; since:string; until:string; images:boolean; saved:boolean; show_hidden:boolean; tag:string; sort:string;run_id:string }
const initialParams=new URLSearchParams(location.search)
const defaults:Filters = {q:'',exclude:'',platform:'',keyword:'',kind:'',min_likes:'',min_collects:'',min_comments:'',since:'',until:'',images:false,saved:false,show_hidden:false,tag:'',sort:'recent',run_id:initialParams.get('run_id')||''}
const names:Record<string,string> = {xhs:'小红书',bili:'B 站',dy:'抖音',ks:'快手',wb:'微博',tieba:'贴吧',zhihu:'知乎'}
const fmt = (n:number) => new Intl.NumberFormat('zh-CN',{notation:n>=10000?'compact':'standard'}).format(n || 0)
const date = (n:number) => n ? new Date(n).toLocaleDateString('zh-CN') : '时间未知'
const path = (p:Post) => `/api/library/posts/${encodeURIComponent(p.platform)}/${encodeURIComponent(p.post_id)}`
const mediaUrl = (p:Post,name:string,thumb=false) => `/api/library/media/${encodeURIComponent(p.platform)}/${encodeURIComponent(p.post_id)}/${encodeURIComponent(name)}${thumb?'?thumb=true':''}`
function params(f:Filters) { const out=new URLSearchParams(); Object.entries(f).forEach(([k,v])=>{if(v!=='' && v!==false) out.set(k,String(v))}); return out }
async function api<T>(url:string, options?:RequestInit):Promise<T> {
  const response=await fetch(url,options)
  if(!response.ok) { const body=await response.json().catch(()=>({})); throw new Error(typeof body.detail==='string'?body.detail:'操作未完成，请稍后重试') }
  return response.json()
}

function Photo({post}:{post:Post}) {
  const [failed,setFailed]=useState(false)
  return post.cover&&!failed ? <img src={mediaUrl(post,post.cover,true)} loading="lazy" decoding="async" alt={post.title||'帖子图片'} onError={()=>setFailed(true)} /> : <div className="lib-no-photo"><ImageIcon size={32}/><span>暂无本地图片</span></div>
}

export function LibraryPage() {
  const queryClient=useQueryClient()
  const [draft,setDraft]=useState<Filters>(defaults),[filters,setFilters]=useState<Filters>(defaults)
  const [cursors,setCursors]=useState(['']),[page,setPage]=useState(0),[total,setTotal]=useState<number|null>(null)
  const [selected,setSelected]=useState<{platform:string;post_id:string}|null>(()=>initialParams.get('platform')&&initialParams.get('post_id')?{platform:initialParams.get('platform')!,post_id:initialParams.get('post_id')!}:null)
  const [error,setError]=useState(''),[busy,setBusy]=useState('')
  const [batchChoice,setBatchChoice]=useState<string|null>(()=>initialParams.has('run_id')?initialParams.get('run_id')||'':null)
  const batches=useQuery({queryKey:['library-batches'],queryFn:()=>api<Batches>('/api/library/batches'),refetchInterval:3000})
  const batchId=batchChoice??batches.data?.latest_id??''
  const effectiveFilters={...filters,run_id:batchId}
  const pageCursor=filters.run_id===batchId?cursors[page]:''
  const status=useQuery({queryKey:['library-status'],queryFn:()=>api<Status>('/api/library/status'),refetchInterval:3000})
  const list=useQuery({queryKey:['library-posts',effectiveFilters,pageCursor],queryFn:()=>api<{items:Post[];total:number|null;next_cursor:string}>(`/api/library/posts?${params(effectiveFilters)}&cursor=${encodeURIComponent(pageCursor)}`),enabled:batchChoice!==null||batches.isSuccess,placeholderData:(previous,query)=>(query?.queryKey[1] as Filters|undefined)?.run_id===batchId?previous:undefined})
  useEffect(()=>{setFilters(old=>({...old,run_id:batchId}));setDraft(old=>({...old,run_id:batchId}));setCursors(['']);setPage(0);setTotal(null)},[batchId])
  useEffect(()=>{if(list.data?.total!=null)setTotal(list.data.total)},[list.data])
  useEffect(()=>{if(status.data?.last_sync)void queryClient.invalidateQueries({queryKey:['library-posts']})},[status.data?.last_sync,queryClient])
  function apply(next:Filters) { setFilters({...next});setCursors(['']);setPage(0);setTotal(null);setSelected(null) }
  function selectBatch(value:string) {
    setBatchChoice(value);setDraft(old=>({...old,run_id:value}));apply({...filters,run_id:value})
    const url=new URL(location.href);url.searchParams.set('run_id',value);url.searchParams.delete('platform');url.searchParams.delete('post_id');history.replaceState(null,'',url)
  }
  async function mark(post:Post, values:Record<string,unknown>) {
    setBusy(post.post_id);setError('')
    try { await api(path(post),{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)}); await queryClient.invalidateQueries({queryKey:['library-posts']}); await queryClient.invalidateQueries({queryKey:['library-detail',post.platform,post.post_id]}) }
    catch(e) {setError((e as Error).message);throw e} finally {setBusy('')}
  }
  async function sync() {
    setError('')
    try {await api('/api/library/sync',{method:'POST'});await status.refetch()} catch(e){setError((e as Error).message)}
  }
  const rootPort=Number(location.port||8081)-1
  const crawlUrl=`${location.protocol}//${location.hostname}:${rootPort}`
  function field(key:keyof Filters,value:string|boolean) {setDraft(old=>({...old,[key]:value}))}
  return <div className="lib-page">
    <header className="lib-header"><a className="lib-brand" href="/" onClick={e=>{e.preventDefault();setSelected(null)}}><BookOpen size={22}/><strong>数据资料库</strong><span>MediaCrawler</span></a><a className="lib-link" href={`${crawlUrl}/research`} target="_blank" rel="noreferrer">需求研究 / 批次报告 <ArrowUpRight size={15}/></a><a className="lib-link" href={crawlUrl} target="_blank" rel="noreferrer">开始研究 <ArrowUpRight size={15}/></a></header>
    <div className="lib-batch-bar"><label htmlFor="library-batch">采集批次<select id="library-batch" value={batchId} disabled={batches.isPending} onChange={e=>selectBatch(e.target.value)}><option value="">全部批次（含历史未分批）</option><option value="__legacy__">历史未分批内容</option>{batchId&&batchId!=='__legacy__'&&!batches.data?.items.some(b=>b.id===batchId)&&<option value={batchId}>指定批次：{batchId}</option>}{batches.data?.items.map((batch,index)=><option key={batch.id} value={batch.id}>{batch.id}{batch.name!==batch.id?` · ${batch.name}`:''}{index===0?'（最新）':''}</option>)}</select></label><span>{batches.isPending?'正在加载批次…':batchChoice===null&&batchId?'已默认选中最新采集批次':'切换批次后立即显示对应内容；导出也使用当前批次。'}</span></div>
    <div className="lib-intro"><div><span className="lib-eyebrow">YOUR LOCAL COLLECTION</span><h1>把采集结果，变成可用的资料。</h1><p>筛选、浏览图片，留下真正需要的内容。</p></div><div className="lib-top-actions"><button className="lib-button" onClick={()=>void sync()} disabled={status.data?.running}><RefreshCw size={16} className={status.data?.running?'animate-spin':''}/>{status.data?.running?'正在同步…':'同步新数据'}</button><a className="lib-button lib-primary" href={`/api/library/export?${params(effectiveFilters)}`}><Download size={16}/>导出筛选结果</a></div></div>
    <div className="lib-stats"><span>全库：<strong>{fmt(status.data?.posts||0)}</strong> 篇内容</span><span><strong>{fmt(status.data?.comments||0)}</strong> 条评论</span><span><strong>{fmt(status.data?.media||0)}</strong> 个本地媒体</span><span className="lib-sync-state">{status.data?.running?'正在整理新数据，已有内容可继续浏览':status.data?.last_sync?`最近同步 ${new Date(status.data.last_sync).toLocaleTimeString('zh-CN')}`:'正在准备资料库…'}</span></div>
    {(error||status.error||list.error||batches.error)&&<div role="alert" className="lib-alert">{error||(status.error as Error)?.message||(list.error as Error)?.message||(batches.error as Error)?.message}</div>}
    {!!status.data?.errors.length&&<div className="lib-notice">{status.data.errors.length} 个来源暂未完成同步（可能仍在写入），会自动重试；已有数据可正常使用。</div>}
    {selected?<PostDetail identity={selected} onBack={()=>setSelected(null)} onMark={mark} busy={busy}/>:<div className="lib-workspace">
      <aside className="lib-filters"><div className="lib-filter-title"><h2><Search size={17}/>筛选内容</h2><button onClick={()=>{const next={...defaults,run_id:batchId};setDraft(next);apply(next)}}>重置</button></div>
        <form onSubmit={e=>{e.preventDefault();apply(draft)}}>
          <label>包含关键词<input value={draft.q} onChange={e=>field('q',e.target.value)} placeholder="标题 / 正文 / 标签，空格表示同时满足"/></label>
          <label>排除关键词<input value={draft.exclude} onChange={e=>field('exclude',e.target.value)} placeholder="不想看到的内容，空格分隔"/></label>
          <div className="lib-two"><label>平台<select value={draft.platform} onChange={e=>field('platform',e.target.value)}><option value="">全部平台</option>{status.data?.platforms.map(v=><option key={v} value={v}>{names[v]||v}</option>)}</select></label><label>内容类型<select value={draft.kind} onChange={e=>field('kind',e.target.value)}><option value="">全部类型</option><option value="normal">图文</option><option value="video">视频</option><option value="note">其他内容</option></select></label></div>
          <label>采集来源关键词<select value={draft.keyword} onChange={e=>field('keyword',e.target.value)}><option value="">全部来源</option>{status.data?.keywords.map(v=><option key={v}>{v}</option>)}</select></label>
          <div className="lib-two"><label>发布日期起<input type="date" value={draft.since} onChange={e=>field('since',e.target.value)}/></label><label>发布日期止<input type="date" value={draft.until} onChange={e=>field('until',e.target.value)}/></label></div>
          <div className="lib-two"><label>最低点赞<input type="number" min="0" value={draft.min_likes} onChange={e=>field('min_likes',e.target.value)} placeholder="不限"/></label><label>最低收藏<input type="number" min="0" value={draft.min_collects} onChange={e=>field('min_collects',e.target.value)} placeholder="不限"/></label></div>
          <label>最低评论数<input type="number" min="0" value={draft.min_comments} onChange={e=>field('min_comments',e.target.value)} placeholder="不限"/></label>
          <label>我的标签<input value={draft.tag} onChange={e=>field('tag',e.target.value)} placeholder="例如：待联系、案例、灵感"/></label>
          <div className="lib-checks">{([['images','只看有本地图片'],['saved','只看我的收藏'],['show_hidden','包含已隐藏内容']] as const).map(([key,label])=><label key={key}><input type="checkbox" checked={draft[key]} onChange={e=>field(key,e.target.checked)}/>{label}</label>)}</div>
          <button type="submit" className="lib-button lib-primary lib-full"><Search size={16}/>应用筛选</button>
        </form>
      </aside>
      <main className="lib-results"><div className="lib-results-head"><div><h2>内容一览</h2><span>{list.isFetching?'正在筛选…':total!=null?`匹配 ${total.toLocaleString()} 篇内容`:'正在加载…'} · 每页 24 篇</span></div><label className="lib-sort">排序<select aria-label="排序" value={filters.sort} onChange={e=>{const next={...filters,sort:e.target.value};setDraft(old=>({...old,sort:next.sort}));apply(next)}}><option value="recent">最新发布</option><option value="likes">点赞最多</option><option value="collects">收藏最多</option><option value="comments">评论最多</option><option value="updated">最近采集</option></select></label></div>
        {list.isPending?<div className="lib-empty">正在打开资料库…</div>:list.data?.items.length===0?<div className="lib-empty"><ImageIcon size={36}/><h3>{status.data?.running?'正在导入现有内容':'没有匹配的内容'}</h3><p>可减少筛选条件，或点击“同步新数据”。</p></div>:<div className="lib-cards">{list.data?.items.map(post=><article className={`lib-card ${post.hidden?'lib-hidden':''}`} key={`${post.platform}/${post.post_id}`}>
          <button className="lib-cover" onClick={()=>setSelected(post)} aria-label={`查看 ${post.title||'内容'} 详情`}><Photo post={post}/><span className="lib-platform">{names[post.platform]||post.platform}{post.kind==='video'?' · 视频':''}</span></button>
          <div className="lib-card-body"><button className="lib-title" onClick={()=>setSelected(post)}>{post.title||post.excerpt.slice(0,50)||'无标题内容'}</button><p className="lib-excerpt">{post.excerpt}</p><div className="lib-byline"><span>{post.author||'未知作者'}</span><time>{date(post.published_at)}</time></div><div className="lib-counts"><span>赞 {fmt(post.likes)}</span><span>收藏 {fmt(post.collects)}</span><span>评论 {fmt(post.comments)}</span></div>{post.user_tags&&<p className="lib-user-tags">{post.user_tags}</p>}
          <div className="lib-card-actions"><button onClick={()=>setSelected(post)}>查看详情 <ChevronRight size={14}/></button><button aria-label={post.saved?'取消收藏':'收藏内容'} className={post.saved?'is-saved':''} disabled={busy===post.post_id} onClick={()=>void mark(post,{saved:!post.saved}).catch(()=>{})}><Star size={17} fill={post.saved?'currentColor':'none'}/></button></div></div>
        </article>)}</div>}
        <nav className="lib-pagination" aria-label="分页"><button className="lib-button" disabled={page===0||list.isFetching} onClick={()=>setPage(p=>p-1)}><ChevronLeft size={16}/>上一页</button><span>第 {page+1} 页</span><button className="lib-button" disabled={!list.data?.next_cursor||list.isFetching} onClick={()=>{setCursors(old=>[...old.slice(0,page+1),list.data!.next_cursor]);setPage(p=>p+1)}}>下一页<ChevronRight size={16}/></button></nav>
      </main>
    </div>}
  </div>
}

function PostDetail({identity,onBack,onMark,busy}:{identity:{platform:string;post_id:string};onBack:()=>void;onMark:(p:Post,v:Record<string,unknown>)=>Promise<void>;busy:string}) {
  const result=useQuery({queryKey:['library-detail',identity.platform,identity.post_id],queryFn:()=>api<Detail>(`/api/library/posts/${encodeURIComponent(identity.platform)}/${encodeURIComponent(identity.post_id)}`)})
  return <main className="lib-detail-wrap"><button className="lib-link" onClick={onBack}><ArrowLeft size={17}/>返回筛选结果</button>{result.error?<div role="alert" className="lib-alert">{(result.error as Error).message}</div>:result.data?<LoadedDetail key={identity.platform+'/'+identity.post_id} item={result.data} onMark={onMark} busy={busy}/>:<div className="lib-empty">正在加载详情…</div>}</main>
}

function LoadedDetail({item,onMark,busy}:{item:Detail;onMark:(p:Post,v:Record<string,unknown>)=>Promise<void>;busy:string}) {
  const [tags,setTags]=useState(item.user_tags),[note,setNote]=useState(item.note),[message,setMessage]=useState('')
  const [comments,setComments]=useState(item.comment_items),[next,setNext]=useState(item.next_comment),[loading,setLoading]=useState(false)
  async function save() {try{await onMark(item,{tags,note});setMessage('标签和备注已保存')}catch{setMessage('保存失败，请稍后重试')}}
  async function more() {setLoading(true);try{const data=await api<Detail>(`${path(item)}?after=${next}`);setComments(old=>[...old,...data.comment_items]);setNext(data.next_comment)}catch{setMessage('评论加载失败')}finally{setLoading(false)}}
  return <div className="lib-detail-grid"><article className="lib-detail-content"><div className="lib-detail-meta">{names[item.platform]||item.platform} · {date(item.published_at)} · {item.author}</div><h1>{item.title||'内容详情'}</h1><div className="lib-detail-actions"><button className={`lib-button ${item.saved?'is-saved':''}`} onClick={()=>void onMark(item,{saved:!item.saved}).catch(()=>{})} disabled={busy===item.post_id}><Star size={16}/>{item.saved?'已收藏':'收藏内容'}</button>{item.original_url&&<a className="lib-button" href={item.original_url} target="_blank" rel="noreferrer">查看原文<ArrowUpRight size={15}/></a>}<button className="lib-button" onClick={()=>void onMark(item,{hidden:!item.hidden}).catch(()=>{})}><EyeOff size={16}/>{item.hidden?'取消隐藏':'隐藏内容'}</button></div><div className="lib-counts"><span>点赞 {fmt(item.likes)}</span><span>收藏 {fmt(item.collects)}</span><span>评论 {fmt(item.comments)}</span></div><p className="lib-full-body">{item.body}</p>{item.tag_text&&<p className="lib-original-tags">{item.tag_text}</p>}
    <div className="lib-detail-media">{item.media.map(file=>file.kind==='image'?<a href={mediaUrl(item,file.name)} target="_blank" rel="noreferrer" key={file.name}><img src={mediaUrl(item,file.name)} alt={file.name} loading="lazy" decoding="async"/></a>:<video key={file.name} src={mediaUrl(item,file.name)} controls preload="none"/>)}</div>
    {item.media.length===0&&<p className="lib-notice">这篇内容没有下载到本地的图片或视频。</p>}
    <section className="lib-comments"><h2>已采集评论 <span>{item.indexed_comments.toLocaleString()}</span></h2>{comments.map(c=><article key={c.comment_id}><div><strong>{c.author||'用户'}</strong><time>{date(c.published_at)}</time><span>赞 {fmt(c.likes)}</span></div><p>{c.body}</p></article>)}{comments.length===0&&<p>暂未导入相关评论。</p>}{next!=null&&<button className="lib-button" onClick={()=>void more()} disabled={loading}>{loading?'加载中…':'加载更多评论'}</button>}</section></article>
    <aside className="lib-detail-notes"><h2>我的整理</h2><p>标签与备注独立保存，重新同步不会覆盖。</p><label>自定义标签<input value={tags} onChange={e=>setTags(e.target.value)} placeholder="用逗号分隔，例如：案例, 待整理" maxLength={500}/></label><label>备注<textarea rows={7} value={note} onChange={e=>setNote(e.target.value)} placeholder="记录筛选理由、下一步计划…" maxLength={5000}/></label><button className="lib-button lib-primary lib-full" onClick={()=>void save()} disabled={busy===item.post_id}>保存整理</button>{message&&<p role="status">{message}</p>}<div className="lib-source"><h3>采集来源</h3>{item.keywords.map(k=><span key={k}>{k}</span>)}<small>内容 ID：{item.post_id}</small></div></aside>
  </div>
}
