import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useOperatorStream } from '@/composables/useOperatorStream'

class FakeWebSocket {
  static instances = []

  constructor(url) {
    this.url = url
    this.close = vi.fn()
    FakeWebSocket.instances.push(this)
  }
}

describe('useOperatorStream', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('connects to the campaign stream endpoint', () => {
    const stream = useOperatorStream({
      campaignId: 'campaign-1',
      baseUrl: 'wss://dashboard.test/'
    })

    stream.connect()

    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.instances[0].url).toBe(
      'wss://dashboard.test/api/campaigns/campaign-1/stream'
    )
  })

  it('supports a campaignId getter and skips connect without one', () => {
    const stream = useOperatorStream({
      campaignId: () => 'campaign-9',
      baseUrl: 'ws://localhost:8443/'
    })

    stream.connect()
    expect(FakeWebSocket.instances[0].url).toBe(
      'ws://localhost:8443/api/campaigns/campaign-9/stream'
    )

    const idle = useOperatorStream({ campaignId: null })
    idle.connect()
    expect(FakeWebSocket.instances).toHaveLength(1)
  })

  it('dispatches module_data payloads and ignores other messages', () => {
    const onModuleData = vi.fn()
    const stream = useOperatorStream({
      campaignId: 'campaign-1',
      onModuleData,
      baseUrl: 'ws://localhost/'
    })

    stream.connect()
    const socket = FakeWebSocket.instances[0]
    socket.onopen()
    expect(stream.status.value).toBe('open')

    socket.onmessage({ data: 'not json' })
    socket.onmessage({ data: JSON.stringify({ type: 'other' }) })
    expect(onModuleData).not.toHaveBeenCalled()

    const payload = { type: 'module_data', victim_id: 'v1', metadata: { code: '123456' } }
    socket.onmessage({ data: JSON.stringify(payload) })
    expect(onModuleData).toHaveBeenCalledWith(payload)
  })

  it('reconnects with backoff after the socket closes', () => {
    const stream = useOperatorStream({
      campaignId: 'campaign-1',
      baseUrl: 'ws://localhost/'
    })

    stream.connect()
    const first = FakeWebSocket.instances[0]
    first.onopen()
    first.onclose()
    expect(stream.status.value).toBe('closed')

    vi.advanceTimersByTime(1000)
    expect(FakeWebSocket.instances).toHaveLength(2)

    // Backoff doubles for the next failure.
    FakeWebSocket.instances[1].onclose()
    vi.advanceTimersByTime(1000)
    expect(FakeWebSocket.instances).toHaveLength(2)
    vi.advanceTimersByTime(1000)
    expect(FakeWebSocket.instances).toHaveLength(3)

    // A successful open resets the delay.
    FakeWebSocket.instances[2].onopen()
    FakeWebSocket.instances[2].onclose()
    vi.advanceTimersByTime(1000)
    expect(FakeWebSocket.instances).toHaveLength(4)
  })

  it('stops reconnecting after disconnect', () => {
    const stream = useOperatorStream({
      campaignId: 'campaign-1',
      baseUrl: 'ws://localhost/'
    })

    stream.connect()
    const socket = FakeWebSocket.instances[0]
    stream.disconnect()

    expect(socket.close).toHaveBeenCalledOnce()
    expect(stream.status.value).toBe('closed')

    vi.advanceTimersByTime(60000)
    expect(FakeWebSocket.instances).toHaveLength(1)
  })
})
