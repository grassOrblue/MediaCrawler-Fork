import { useState } from 'react'
import { Toaster } from 'sonner'
import { Sidebar } from '@/components/layout/Sidebar'
import { MainContent } from '@/components/layout/MainContent'
import { BookOpen, ArrowUpRight } from 'lucide-react'
import { CrawlerConfigPanel } from '@/components/config/CrawlerConfigPanel'
import { EnvironmentCheck, isEnvChecked } from '@/components/env/EnvironmentCheck'
import { LicenseDisclaimer, isLicenseAccepted } from '@/components/license/LicenseDisclaimer'

function App() {
  // Initialize by checking localStorage if license has been accepted
  const [licenseAccepted, setLicenseAccepted] = useState(() => isLicenseAccepted())
  // Initialize by checking localStorage if env check has passed
  const [envChecked, setEnvChecked] = useState(() => isEnvChecked())
  // State for showing disclaimer manually
  const [showDisclaimer, setShowDisclaimer] = useState(false)

  const handleEnvCheckComplete = () => {
    setEnvChecked(true)
  }

  const handleLicenseAccept = () => {
    setLicenseAccepted(true)
    setShowDisclaimer(false)
  }

  const handleShowDisclaimer = () => {
    setShowDisclaimer(true)
  }

  return (
    <div className="flex flex-col h-dvh cyber-grid overflow-hidden relative">
      {/* License Disclaimer Modal - Shows first or when triggered */}
      {(!licenseAccepted || showDisclaimer) && (
        <LicenseDisclaimer onAccept={handleLicenseAccept} />
      )}

      {/* Environment Check Modal - Shows after license accepted */}
      {licenseAccepted && !showDisclaimer && !envChecked && (
        <EnvironmentCheck onCheckComplete={handleEnvCheckComplete} />
      )}

      {/* Header Bar */}
      <Sidebar onShowDisclaimer={handleShowDisclaimer} />

      {/* Main Area */}
      <div className="flex-1 flex flex-col gap-4 p-4 overflow-y-auto overflow-x-hidden min-h-0 min-w-0">
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-cyan-500/30 bg-cyan-500/5 p-4 flex-shrink-0">
          <div><h2 className="font-semibold text-cyber-text-primary">数据资料库</h2><p className="text-xs text-cyber-text-secondary mt-1">筛选已采集内容，浏览图片，收藏、备注并导出结果。</p></div>
          <a href={`${location.protocol}//${location.hostname}:${Number(location.port||8080)+1}`} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-2 rounded-lg bg-cyan-600 hover:bg-cyan-700 px-5 py-3 font-semibold text-white"><BookOpen size={18}/>打开数据资料库<ArrowUpRight size={16}/></a>
        </div>
        {/* Config Panel - Primary Action Area (Always Expanded) */}
        <div className="flex-shrink-0">
          <CrawlerConfigPanel />
        </div>

        {/* Console - Collapsible Terminal */}
        <MainContent />
      </div>

      {/* Toast notifications - Theme-aware style */}
      <Toaster
        position="top-right"
        toastOptions={{
          className: 'glass-panel font-mono text-cyber-text-primary',
          style: {
            fontFamily: 'JetBrains Mono, monospace',
          },
        }}
      />
    </div>
  )
}

export default App
