import { useEffect } from 'react'
import { connectWebSocket, useJarvisStore } from '@/store/jarvisStore'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { OrbConsole } from '@/pages/OrbConsole'
import { DiagnosticsPage } from '@/pages/Diagnostics'
import { MobileControl } from '@/pages/MobileControl'
import { initPersonaEarly } from '@/lib/cosmo'
import '@/components/ProjectsPanel'
import '@/components/SubagentsPanel'
import '@/components/TasksPanel'
import '@/components/LogsPanel'
import '@/components/SettingsPanel'

function App() {
  const setPersona = useJarvisStore((s) => s.setPersona)
  const isDiagnostics = window.location.pathname === '/diagnostics'
  const isMobileControl = window.location.pathname === '/mobile' || window.location.pathname === '/mobile-control'

  useEffect(() => {
    const persona = initPersonaEarly()
    setPersona(persona)
    connectWebSocket()
  }, [setPersona])

  return (
    <ErrorBoundary>
      {isMobileControl ? (
        <MobileControl />
      ) : isDiagnostics ? (
        <DiagnosticsPage />
      ) : (
        <OrbConsole />
      )}
    </ErrorBoundary>
  )
}

export default App
