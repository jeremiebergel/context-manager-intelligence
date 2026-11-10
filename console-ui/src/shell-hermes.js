// Hermes Desktop shell for the CMI Console. Built into cmi_console/desktop/plugin.js by
// console-ui/build.py (the core is inlined: disk plugins may only import the SDK and React).
import { ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA, host } from '@hermes/plugin-sdk'
import * as React from 'react'

/*@@CORE@@*/

const PATH = '/cmi-console'

// Map Hermes theme tokens onto the console's --cc-* variables (dark/light follow the app theme).
const THEME = {
  '--cc-fg': 'var(--ui-text-primary)',
  '--cc-fg2': 'var(--ui-text-secondary)',
  '--cc-fg3': 'var(--ui-text-tertiary)',
  '--cc-line': 'var(--ui-stroke-secondary)',
  '--cc-card': 'var(--ui-bg-card, transparent)',
  '--cc-accent': 'var(--ui-accent)',
  '--cc-ok': 'var(--ui-green)',
  '--cc-warn': 'var(--ui-yellow)',
  '--cc-bad': 'var(--ui-red)',
  height: '100%',
}

export default {
  id: 'cmi_console',
  name: 'CMI Console',
  defaultEnabled: false, // opt-in: inert until the user enables it in Capabilities → Plugins
  register(ctx) {
    const ControlCenter = createConsole({
      React,
      api: path => ctx.rest(path, { timeoutMs: 20000 }),
      post: (path, body) => ctx.rest(path, { method: 'POST', body, timeoutMs: 20000 }),
      // Re-selecting the same Claude model resolves fresh credentials without replacing the chat.
      // Stop the retry loop first; Hermes may still queue the switch while the worker winds down.
      switchAccount: async tag => {
        const sid = host.state.focusedSessionId.get()
        const owner = host.state.focusedSessionOwner.get()
        if (sid && (!owner || owner.profile !== host.state.profile.get() || owner.connectionId !== host.state.connectionId.get())) {
          throw new Error(String(ctx.storage?.get?.('lang', '') || '').toLowerCase() === 'fr' ? 'Ouvre cette conversation sur sa connexion et son profil avant de changer de compte.' : 'Open this conversation on its own connection and profile before switching account.')
        }
        // session.info is an event, not an RPC; read the already focused live session.
        const live = sid ? await host.request('session.activate', { session_id: sid, omit_messages: true }) : null
        const info = live && live.info
        if (sid && (!info || live.session_id !== sid)) throw new Error('Missing session route')
        const fr = String(ctx.storage?.get?.('lang', '') || '').toLowerCase() === 'fr'
        if (info?.provider === 'anthropic' && !info.model) throw new Error('Missing session model')
        const selection = await ctx.rest('/accounts/first', { method: 'POST', body: { tag, intent: 'make-first', retry: true }, timeoutMs: 20000 })
        if (!selection?.moved || selection.error) throw new Error(fr ? 'Impossible de sélectionner ce compte.' : 'Could not select this account.')
        if (!info || info.provider !== 'anthropic') {
          return fr ? 'Compte choisi pour Claude. Le modèle actuel reste inchangé ; sélectionne Claude dans le sélecteur de modèle.' : 'Account selected for Claude. Current model unchanged; select Claude in the model picker.'
        }
        if (live.running) await host.request('session.interrupt', { session_id: sid })
        const result = await host.request('config.set', { session_id: sid, key: 'model', value: `${info.model} --provider anthropic --session` })
        if (result.confirm_required) throw new Error(fr ? 'Ordre modifié, mais bascule à confirmer dans le sélecteur de modèle.' : 'Order updated; confirm the switch in the model picker.')
        return result.deferred
          ? (fr ? 'Arrêt demandé. Bascule programmée pour le prochain message ; attends la fin de l’arrêt, puis envoie « continue ».' : 'Stop requested. Switch queued for the next message; wait for the stop to finish, then send ‘continue’.')
          : (fr ? 'Compte sélectionné pour cette conversation. Renvoie ton message pour reprendre.' : 'Account selected for this conversation. Resend your message to resume.')
      },
      storage: ctx.storage,
      notify: message => host.notify({ kind: 'info', message }),
    })
    const Page = () => React.createElement('div', { style: THEME }, React.createElement(ControlCenter))
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: PATH }, render: () => React.createElement(Page) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: PATH, label: 'Control Center', codicon: 'dashboard' } },
      { id: 'open', area: PALETTE_AREA, data: { id: 'cmi-console.open', label: 'CMI: Open Control Center', keywords: ['cmi', 'quota', 'accounts', 'control'], run: () => host.navigate(PATH) } },
    ])
  },
}
