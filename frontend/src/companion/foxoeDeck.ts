/** FoxOE's documented page bridge, compatible with sdk/foxoe.js in v0.1.0-pre2.
 * https://github.com/tarobits/foxoe/blob/v0.1.0-pre2/docs/API.md
 * No credentials or PIN values cross this interface. The extension authenticates
 * the selected tab; station actions still use the companion's normal session.
 */
import { randomUUID } from '../shared/uuid'

export interface DeckButton {
  id: string
  label: string
  icon: 'window' | 'printer' | 'keypad' | 'play' | 'backspace' | 'check' | 'close' | 'back'
  disabled?: boolean
  run: () => void
}
export interface DeckScreen {
  context: string
  buttons: (DeckButton | null)[]
}

export function createFoxoeDeck(target: Window = window) {
  let connected = false
  let disposed = false
  let signature = ''
  let desired: { id: string; buttons: (Omit<DeckButton, 'run'> | null)[] } | undefined
  let handlers = new Map<string, DeckButton>()
  let active = ''
  let pending: { request: string; layout: string; timer: ReturnType<typeof setTimeout> } | undefined
  let retry: ReturnType<typeof setTimeout> | undefined
  let attempts = 0
  const prefix = `companion-${randomUUID().slice(0, 8)}`
  let revision = 0

  function send(message: object) {
    target.postMessage({ source: 'foxoe:app', ...message }, target.location.origin)
  }
  function cancelPending() {
    clearTimeout(pending?.timer)
    clearTimeout(retry)
    pending = undefined
    retry = undefined
    active = ''
  }
  function publish() {
    if (disposed || !connected || !desired || pending || active === desired.id) return
    const request = randomUUID()
    const layout = desired.id
    pending = {
      request,
      layout,
      timer: setTimeout(() => finish(request, false), 5000),
    }
    send({ type: 'layout', request, operation: 'show', layout: desired })
  }
  function finish(request: string, success: boolean) {
    if (!pending || pending.request !== request) return
    const layout = pending.layout
    clearTimeout(pending.timer)
    pending = undefined
    if (success) active = layout
    if (layout !== desired?.id) {
      attempts = 0
      publish()
    } else if (!success && attempts++ === 0) {
      // One bounded retry handles a transient bridge failure without a request
      // loop. A reconnect or later screen change can always recover normally.
      retry = setTimeout(publish, 250)
    }
  }
  function receive(event: MessageEvent) {
    if (disposed || event.source !== target || event.origin !== target.location.origin) return
    const message = event.data
    if (!message || message.source !== 'foxoe:bridge') return
    if (message.type === 'connection') {
      cancelPending()
      connected = message.connected === true
      attempts = 0
      if (connected && desired) desired = { ...desired, id: `${prefix}-${++revision}` }
      publish()
    } else if (message.type === 'ack' || message.type === 'error') {
      finish(message.request, message.type === 'ack')
    } else if (
      message.type === 'action' &&
      connected &&
      !target.document.hidden &&
      active &&
      active === desired?.id &&
      message.layout === active
    ) {
      const button = handlers.get(message.id)
      if (button && !button.disabled) button.run()
    }
  }
  target.addEventListener('message', receive)
  send({ type: 'hello' })
  return {
    setScreen(screen: DeckScreen) {
      if (disposed) return
      // Key 15 belongs to FoxOE Home. Never send application controls for it.
      const buttons = screen.buttons.slice(0, 14)
      handlers = new Map(buttons.filter((b): b is DeckButton => !!b).map((b) => [b.id, b]))
      const wire = buttons.map(
        (button) =>
          button && {
            id: button.id,
            label: Array.from(button.label).slice(0, 32).join(''),
            icon: button.icon,
            disabled: !!button.disabled,
          },
      )
      const next = JSON.stringify([screen.context, wire])
      if (signature === next) return
      signature = next
      active = ''
      attempts = 0
      clearTimeout(retry)
      desired = { id: `${prefix}-${++revision}`, buttons: wire }
      publish()
    },
    dispose() {
      if (disposed) return
      if (connected)
        send({
          type: 'layout',
          request: randomUUID(),
          operation: 'show',
          layout: { id: `${prefix}-closed`, buttons: [] },
        })
      disposed = true
      connected = false
      cancelPending()
      handlers.clear()
      target.removeEventListener('message', receive)
    },
  }
}
