// Dependency-free regression harness: fixtures never contact a provider or use real credentials.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'

const root = new URL('../', import.meta.url)
const core = fs.readFileSync(new URL('console-ui/src/core.js', root), 'utf8')
const shell = fs.readFileSync(new URL('console-ui/src/shell-hermes.js', root), 'utf8')
const React = {
  createElement(type, props, ...children) {
    return typeof type === 'function' ? type({ ...props, children }) : { type, props, children }
  },
  useState: initial => [typeof initial === 'function' ? initial() : initial, () => {}],
  useEffect: () => {}, useCallback: f => f, useMemo: f => f(), useRef: current => ({ current }),
}
const ctx = vm.createContext({ console, React, navigator: { language: 'fr' }, setTimeout, clearTimeout,
  window: { confirm: () => true } })
vm.runInContext(core.replace('export function createConsole', 'function createConsole') + '\nglobalThis.createConsole = createConsole', ctx)
const account = (tag, priority, state) => ({ provider: 'anthropic', tag, priority, state, key: tag,
  windows: [], source: 'manual', model_cooldowns: [], requests_30d: 0, tokens_30d: 0 })
const fixture = { host: { name: 'test' }, sections: { accounts: { active: {}, providers: [],
  accounts: [account('perso', 0, 'limited'), account('pro', 1, 'ready')] } }, errors: {} }
const walk = n => !n || typeof n !== 'object' ? [] : [n, ...(n.children || []).flat(Infinity).flatMap(walk)]
const text = n => typeof n === 'string' ? n : (n?.children || []).flat(Infinity).map(text).join('')
const FR_STORE = { get: (k, d) => (k === 'lang' ? 'fr' : d), set: () => {} }  // French is opt-in
let picks = []
for (const proState of ['ready', 'limited']) {
  fixture.sections.accounts.accounts[1].state = proState
  const render = ctx.createConsole({ React, initial: fixture, api: async () => fixture, storage: FR_STORE,
    post: async () => {}, switchAccount: async tag => { picks.push(tag); return 'ok' } })
  const tree = walk(render())
  const proCard = tree.find(n => n.type === 'section' && /cc-acct/.test(n.props?.className || '') && text(n).startsWith('Pro'))
  const buttons = walk(proCard).filter(n => n.type === 'button' && /^Passer sur ce compte$/.test(text(n)))
  assert.ok(buttons.length === 1, `Pro card must expose its own switch button, including state=${proState}`)
  assert.equal(buttons[0].props['aria-label'], 'Passer sur Pro')
  assert.ok(walk(proCard).filter(n => n.type === 'button').every(b => text(b).length <= 22), 'Card button labels must stay short enough for a 280px card')
  buttons[0].props.onClick()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(picks.at(-1), 'pro')
}

// Each card carries exactly the two actions the user asked for: switch, and use-as-primary (not on the current primary).
{
  fixture.sections.accounts.accounts[1].state = 'ready'
  const posted = []
  const render = ctx.createConsole({ React, initial: fixture, api: async () => fixture, storage: FR_STORE,
    post: async (path, body) => { posted.push([path, body]); return {} }, switchAccount: async () => 'ok' })
  const tree = walk(render())
  const labels = name => walk(tree.find(n => n.type === 'section' && /cc-acct/.test(n.props?.className || '') && text(n).startsWith(name)))
    .filter(n => n.type === 'button').map(text)
  assert.deepEqual(labels('Perso'), ['Passer sur ce compte'], 'primary card: switch only')
  assert.deepEqual(labels('Pro'), ['Passer sur ce compte', 'Mettre en principal'], 'other card: switch + use as primary')
  const primary = tree.find(n => n.type === 'button' && text(n) === 'Mettre en principal')
  primary.props.onClick()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(JSON.stringify(posted[0]), JSON.stringify(['/accounts/first', { intent: 'make-first', tag: 'pro' }]))
  assert.ok(tree.some(n => n.type === 'button' && /^\+ Ajouter un compte$/.test(text(n))), 'A single add-account button must be on the overview')
  assert.ok(!tree.some(n => n.type === 'button' && /Claude/.test(text(n)) && /Ajouter/.test(text(n))), 'No Claude-only add button')
}

// Execute the real desktop-shell callback with a mocked SDK transport.
let options, calls = [], provider = 'anthropic', deferred = false, fail = false, running = false
let owner = { profile: 'default', connectionId: 'local' }
const atom = get => ({ get })
const host = {
  state: { focusedSessionId: atom(() => 'session-1'), focusedSessionOwner: atom(() => owner),
    profile: atom(() => 'default'), connectionId: atom(() => 'local') },
  request: async (method, params) => {
    calls.push([method, params])
    if (method === 'session.activate') return { session_id: 'session-1', info: { model: 'claude-test', provider }, running }
    if (method === 'config.set') { if (fail) throw new Error('switch failed'); return { deferred } }
    if (method === 'session.interrupt') return {}
    throw new Error(`unknown method: ${method}`)
  },
  notify() {}, navigate() {},
}
const shellCtx = vm.createContext({ React, host, navigator: { language: 'fr' },
  ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'sidebar', PALETTE_AREA: 'palette',
  createConsole: opts => { options = opts; return () => null } })
vm.runInContext(shell.replace(/^import .*$/gm, '').replace('export default', 'globalThis.plugin ='), shellCtx)
shellCtx.plugin.register({ rest: async (path, params) => { calls.push([path, params]); return { moved: 1 } },
  storage: FR_STORE, registerMany() {} })
let message = await options.switchAccount('pro')
assert.match(message, /sélectionné pour cette conversation/)
assert.equal(calls[1][0], '/accounts/first')
assert.equal(calls[1][1].body.tag, 'pro')
assert.equal(calls[1][1].body.retry, true)
assert.equal(calls[2][0], 'config.set')
assert.equal(calls[2][1].session_id, 'session-1')
assert.equal(calls[2][1].value, 'claude-test --provider anthropic --session')
running = true; calls = []
await options.switchAccount('pro')
assert.equal(calls[2][0], 'session.interrupt', 'Stop the current retry loop before switching')
assert.equal(calls[2][1].session_id, 'session-1')
assert.equal(calls[3][0], 'config.set')
running = false; deferred = true
assert.match(await options.switchAccount('pro'), /prochain message/)
provider = 'openai-codex'; calls = []
assert.match(await options.switchAccount('pro'), /modèle actuel reste inchangé/)
assert.ok(!calls.some(([method]) => method === 'config.set'), 'Never silently switch away from Codex')
owner = { profile: 'other', connectionId: 'local' }; calls = []
await assert.rejects(options.switchAccount('pro'), /profil/)
assert.equal(calls.length, 0, 'Foreign focused profile must fail before any write')
owner = { profile: 'default', connectionId: 'remote' }
await assert.rejects(options.switchAccount('pro'), /connexion/)
owner = { profile: 'default', connectionId: 'local' }; provider = 'anthropic'; fail = true
await assert.rejects(options.switchAccount('pro'), /switch failed/)
console.log('PASS: overview buttons + ready/paused Pro + click routing + session reselection + deferred notice + Codex unchanged + profile/connection isolation + RPC failure')

// English by default, even on a French browser: French needs the stored setting.
{
  const render = ctx.createConsole({ React, initial: fixture, api: async () => fixture,
    post: async () => {}, switchAccount: async () => 'ok' })
  const all = walk(render()).map(n => typeof n === 'string' ? n : '').join(' ') + text({ children: [render()] })
  assert.ok(!/Passer sur ce compte|Vue d'ensemble/.test(all), 'console must default to English')
  assert.ok(/Switch to this account|Overview/.test(all), 'English labels expected by default')
}
