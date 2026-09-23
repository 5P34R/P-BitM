import { ref } from 'vue'

const INITIAL_RETRY_MS = 1000
const MAX_RETRY_MS = 30000

function defaultStreamBase() {
  const base = import.meta.env.VITE_API_BASE_URL || `${location.protocol}//${location.host}/`
  return base.replace(/^http/, 'ws')
}

/**
 * Subscribe to the operator event stream of one campaign.
 *
 * The browser sends the dashboard session cookie with the WebSocket
 * handshake, so no extra credentials are needed. On close/error the
 * composable schedules a reconnect with exponential backoff; callers keep
 * their REST polling as the source of truth while `status` is not 'open'.
 */
export function useOperatorStream({ campaignId, onModuleData, baseUrl } = {}) {
  const status = ref('closed')
  let socket = null
  let retryDelay = INITIAL_RETRY_MS
  let reconnectTimer = null
  let manuallyClosed = false

  function resolveCampaignId() {
    return typeof campaignId === 'function' ? campaignId() : campaignId
  }

  function clearReconnect() {
    if (reconnectTimer !== null) {
      window.clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
  }

  function scheduleReconnect() {
    if (manuallyClosed || reconnectTimer !== null) return
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null
      connect()
    }, retryDelay)
    retryDelay = Math.min(retryDelay * 2, MAX_RETRY_MS)
  }

  function connect() {
    const id = resolveCampaignId()
    if (!id) return
    manuallyClosed = false
    const base = baseUrl || defaultStreamBase()
    try {
      socket = new WebSocket(`${base}api/campaigns/${id}/stream`)
    } catch (error) {
      console.error('Operator stream connect failed:', error)
      status.value = 'closed'
      scheduleReconnect()
      return
    }
    socket.onopen = () => {
      status.value = 'open'
      retryDelay = INITIAL_RETRY_MS
    }
    socket.onmessage = (event) => {
      let payload
      try {
        payload = JSON.parse(event.data)
      } catch {
        return
      }
      if (payload?.type === 'module_data') {
        onModuleData?.(payload)
      }
    }
    socket.onclose = () => {
      status.value = 'closed'
      socket = null
      scheduleReconnect()
    }
  }

  function disconnect() {
    manuallyClosed = true
    clearReconnect()
    if (socket) {
      socket.onclose = null
      socket.close()
      socket = null
    }
    status.value = 'closed'
  }

  return { status, connect, disconnect }
}
