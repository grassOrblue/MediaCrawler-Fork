import { Terminal } from '@/components/console/Terminal'
import { useLogWebSocket } from '@/hooks/useWebSocket'

export function MainContent() {
  // Connect to WebSocket for logs
  useLogWebSocket()

  return (
    <main className="flex-[1_0_20rem] min-h-[20rem] flex flex-col overflow-hidden min-w-0 relative z-10">
      <Terminal />
    </main>
  )
}
