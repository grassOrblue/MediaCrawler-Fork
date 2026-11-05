import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Database, Download, Eye, RefreshCw } from 'lucide-react'
import { dataApi } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'

const urlPath = (path: string) => path.replace(/\\/g, '/').split('/').map(encodeURIComponent).join('/')

export function DataExplorerDialog() {
  const { t } = useTranslation('data')
  const [open, setOpen] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const files = useQuery({
    queryKey: ['data-files'],
    queryFn: async () => (await dataApi.getFiles()).data.files,
    enabled: open,
  })
  const preview = useQuery({
    queryKey: ['data-preview', selected],
    queryFn: async () => (await dataApi.getFileContent(urlPath(selected!))).data,
    enabled: open && selected !== null,
  })

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="sm" className="h-7 px-2 text-cyber-neon-cyan">
          <Database className="mr-2 h-4 w-4" />{t('dialog.button')}
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-4xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{t('dialog.title')}</DialogTitle>
          <DialogDescription>{t('explorer.title')}</DialogDescription>
        </DialogHeader>
        <Button variant="outline" size="sm" className="justify-self-start" onClick={() => { void files.refetch() }} disabled={files.isFetching}>
          <RefreshCw className="mr-2 h-4 w-4" />{t('explorer.rescan')}
        </Button>
        {files.isPending && <p role="status">{t('explorer.loading')}</p>}
        {files.isError && <p role="alert">{t('preview.error')}</p>}
        {files.data?.length === 0 && (
          <div className="py-8 text-center text-cyber-text-secondary">
            <p>{t('explorer.noData')}</p><p className="mt-2 text-sm">{t('explorer.noDataHint')}</p>
          </div>
        )}
        <ul className="space-y-2">
          {files.data?.map(file => (
            <li key={file.path} className="flex items-center justify-between gap-4 rounded border border-cyber-border-subtle p-3">
              <div className="min-w-0"><p className="break-all text-sm">{file.path}</p><p className="text-xs text-cyber-text-secondary">{(file.size / 1024).toFixed(1)} KB</p></div>
              <div className="flex shrink-0 gap-2">
                <Button variant="ghost" size="sm" onClick={() => setSelected(file.path)}><Eye className="mr-1 h-4 w-4" />{t('file.preview')}</Button>
                <Button variant="ghost" size="sm" asChild><a href={dataApi.getDownloadUrl(urlPath(file.path))} download><Download className="mr-1 h-4 w-4" />{t('file.extract')}</a></Button>
              </div>
            </li>
          ))}
        </ul>
        {selected && (
          <section className="min-w-0 space-y-2">
            <h3 className="break-all text-sm">{t('preview.title')}: {selected}</h3>
            {preview.isPending && <p role="status">{t('preview.loading')}</p>}
            {preview.isError && <p role="alert">{t('preview.error')}</p>}
            {preview.data && <><p className="text-xs">{t('preview.records', { count: preview.data.total })}</p><pre className="max-h-64 overflow-auto rounded bg-black/30 p-3 text-xs">{JSON.stringify(preview.data.data, null, 2)}</pre></>}
          </section>
        )}
      </DialogContent>
    </Dialog>
  )
}
