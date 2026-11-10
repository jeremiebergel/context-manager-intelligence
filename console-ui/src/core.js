// CMI Console UI: host-neutral. Plain ES module, no JSX, no imports.
// A shell (Hermes Desktop, VS Code webview, standalone page) calls
//   createConsole({ React, api, storage, notify })
// and mounts the returned component. `api(path)` resolves the backend JSON.
//
// Security rules for this file (enforced by tests/test_console_ui.py):
// no dangerouslySetInnerHTML, no eval/Function, no network access other than `api`,
// every value rendered as React text (auto-escaped).

export function createConsole({ React, api, storage, notify, initial, post, switchAccount }) {
  // post (optional, shell-provided): post(path, body) for the single action 'make first'.
  // Without it the console is strictly read-only (no button).
  const { createElement: h, useState, useEffect, useCallback, useMemo, useRef } = React
  const store = storage || { get: (_k, d) => d, set: () => {} }

  const FR = String(store.get('lang', '') || '').toLowerCase() === 'fr'  // English by default; French is opt-in (stored setting 'lang' = 'fr')
  const T = FR ? {
    title: 'Control Center', overview: "Vue d'ensemble", accounts: 'Comptes', projects: 'Projets', savings: 'Économies CMI', security: 'Sécurité',
    refresh: 'Actualiser', auto: 'Live', privacy: 'Mode capture', synced: 'Synchronisé', never: 'jamais',
    loading: 'Chargement des données réelles…', disconnected: 'Backend déconnecté',
    disconnectedHelp: "Le backend Python du plugin ne répond pas. Activez-le : `hermes plugins enable cmi_console`, puis redémarrez Hermes.",
    leak: 'Réponse bloquée par le garde-fou anti-fuite', leakHelp: "Le backend a détecté un identifiant dans la réponse et l'a refusée. Rien n'a été affiché.",
    unavailable: 'Indisponible', notExposed: 'non exposé par le fournisseur', unknown: 'inconnu', noWindow: 'Quota en attente de synchro',
    active: 'Actif', kAccounts: 'Comptes', kLimited: 'limités', kSessions: 'Sessions (12 mois)', kCost: 'Coût estimé (12 mois)', kSaved: 'Tokens économisés (30 j)',
    fleet: 'Santé du parc', models: 'modèles', strategy: 'Stratégie', priority: 'Priorité', requests: 'requêtes', lastError: 'Dernière erreur',
    cooldown: 'Cooldown', resets: 'reset', auth: 'Auth', source: 'Source', email: 'Email', emailNone: 'non stocké par Hermes',
    project: 'Projet', sessions: 'Sessions', tokens: 'Tokens', cost: 'Coût', lastActive: 'Dernière activité', providers: 'Fournisseurs',
    noProject: 'Hors projet', recent: 'Sessions récentes', attribution: "Hermes enregistre le fournisseur de chaque session, pas le compte exact du pool qui l'a servie. Attribution par compte : prévue (journal CMI).",
    period: 'Période', today: "Aujourd'hui", d7: '7 jours', d30: '30 jours', raw: 'Sans CMI', saved: 'Économisés', costCmp: 'Coût sans → avec CMI',
    partial: 'partiel', recalls: 'Rappels 7 j', anticipated: 'anticipés', daily: 'Tokens économisés par jour (30 j)', byModel: 'Par modèle',
    notInstalled: 'Ledger CMI absent sur cet hôte.', costNote: "Coût d'entrée simulé au tarif public, cache inclus. Sur un forfait, l'écart est du quota en plus.",
    secTitle: 'Garanties de sécurité', secLive: 'Dernière réponse vérifiée par le garde-fou', models_: 'Modèles disponibles', show: 'voir', hide: 'masquer',
    sec: [
      ['Lecture seule', "Aucune route d'écriture. Le plugin ne modifie ni auth.json, ni config.yaml, ni state.db, ni aucun compte."],
      ['Zéro secret sur le fil', 'Sérialisation par liste blanche : seuls des champs nommés sortent du backend. Jamais de token, refresh token ou clé.'],
      ['Garde-fou anti-fuite', 'Chaque réponse est comparée à tous les identifiants stockés et aux formats connus (sk-, JWT, ghp_…). Une correspondance bloque la réponse entière.'],
      ['Textes assainis', 'Labels, titres et erreurs : emails masqués, secrets caviardés, caractères de contrôle et bidi supprimés, longueur bornée.'],
      ['Identifiants opaques', 'Comptes, sessions et projets sont désignés par un hash non réversible, jamais par un id de compte.'],
      ['Double opt-in', 'Le backend Python ne se charge que via plugins.enabled ; la moitié desktop reste désactivée tant que vous ne l\'activez pas.'],
      ['Appels réseau', "Aucun appel propre. Les quotas viennent du rafraîchissement natif de Hermes (lecture seule, au plus toutes les 120 s par compte)."],
      ['Mode capture', 'Floute labels, projets et titres pour partager un écran sans rien exposer.'],
    ],
  } : {
    title: 'Control Center', overview: 'Overview', accounts: 'Accounts', projects: 'Projects', savings: 'CMI savings', security: 'Security',
    refresh: 'Refresh', auto: 'Live', privacy: 'Capture mode', synced: 'Synced', never: 'never',
    loading: 'Loading real data…', disconnected: 'Backend disconnected',
    disconnectedHelp: 'The plugin backend is not answering. Enable it: `hermes plugins enable cmi_console`, then restart Hermes.',
    leak: 'Response blocked by the leak guard', leakHelp: 'The backend found a credential in the response and refused it. Nothing was displayed.',
    unavailable: 'Unavailable', notExposed: 'not exposed by provider', unknown: 'unknown', noWindow: 'Quota awaiting sync',
    active: 'Active', kAccounts: 'Accounts', kLimited: 'limited', kSessions: 'Sessions (12 mo)', kCost: 'Estimated cost (12 mo)', kSaved: 'Tokens saved (30 d)',
    fleet: 'Fleet health', models: 'models', strategy: 'Strategy', priority: 'Priority', requests: 'requests', lastError: 'Last error',
    cooldown: 'Cooldown', resets: 'reset', auth: 'Auth', source: 'Source', email: 'Email', emailNone: 'not stored by Hermes',
    project: 'Project', sessions: 'Sessions', tokens: 'Tokens', cost: 'Cost', lastActive: 'Last active', providers: 'Providers',
    noProject: 'No project', recent: 'Recent sessions', attribution: 'Hermes records the provider of each session, not which pooled account served it. Per-account attribution: planned (CMI ledger).',
    period: 'Period', today: 'Today', d7: '7 days', d30: '30 days', raw: 'Without CMI', saved: 'Saved', costCmp: 'Cost without → with CMI',
    partial: 'partial', recalls: 'Recalls 7 d', anticipated: 'anticipated', daily: 'Tokens saved per day (30 d)', byModel: 'By model',
    notInstalled: 'No CMI ledger on this host.', costNote: 'Input cost simulated at public prices, cache included. On a subscription the gap is extra quota.',
    secTitle: 'Security guarantees', secLive: 'Last response checked by the leak guard', models_: 'Available models', show: 'show', hide: 'hide',
    sec: [
      ['Read-only', 'No write route. The plugin never modifies auth.json, config.yaml, state.db or any account.'],
      ['No secret on the wire', 'Allowlist serialisation: only named fields leave the backend. Never a token, refresh token or key.'],
      ['Leak guard', 'Every response is checked against every stored credential and known formats (sk-, JWT, ghp_…). A hit blocks the whole response.'],
      ['Sanitised text', 'Labels, titles, errors: emails masked, secrets redacted, control/bidi chars stripped, bounded length.'],
      ['Opaque ids', 'Accounts, sessions and projects are keyed by a non-reversible hash, never an account id.'],
      ['Double opt-in', 'The Python backend loads only via plugins.enabled; the desktop half stays off until you enable it.'],
      ['Network', "No calls of its own. Quotas come from Hermes's native refresh (read-only, at most every 120 s per account)."],
      ['Capture mode', 'Blurs labels, projects and titles so you can share a screen safely.'],
    ],
  }

  // ---------- formatting ----------
  const fmtTok = n => n == null ? '–' : n >= 1e9 ? (n / 1e9).toFixed(2) + ' B' : n >= 1e6 ? (n / 1e6).toFixed(1) + ' M' : n >= 1e3 ? Math.round(n / 1e3) + ' k' : String(n)
  const fmtUsd = n => n == null ? '–' : '$' + (n >= 100 ? Math.round(n).toLocaleString() : n.toFixed(2))
  const rel = ts => {
    if (!ts) return T.never
    const s = Math.round(Date.now() / 1000 - ts)
    if (s < 60) return FR ? `il y a ${s} s` : `${s}s ago`
    if (s < 3600) return FR ? `il y a ${Math.round(s / 60)} min` : `${Math.round(s / 60)}m ago`
    if (s < 86400) return FR ? `il y a ${Math.round(s / 3600)} h` : `${Math.round(s / 3600)}h ago`
    return FR ? `il y a ${Math.round(s / 86400)} j` : `${Math.round(s / 86400)}d ago`
  }
  const dur = s => s == null ? '' : s < 60 ? `${s}s` : s < 3600 ? `${Math.round(s / 60)} min` : s < 86400 ? `${(s / 3600).toFixed(1)} h` : `${Math.round(s / 86400)} j`
  const until = iso => { if (!iso) return ''; const t = Date.parse(iso); return isNaN(t) ? '' : dur(Math.max(0, Math.round((t - Date.now()) / 1000))) }
  const pct = v => v == null ? '–' : `${Math.round(v)}%`

  // ---------- primitives ----------
  const cls = (...a) => a.filter(Boolean).join(' ')
  const Dot = ({ state }) => h('span', { className: cls('cc-dot', 'cc-' + (state || 'unknown')), title: state })
  const Pill = ({ children, tone }) => h('span', { className: cls('cc-pill', tone && 'cc-' + tone) }, children)
  const S = ({ children }) => h('span', { className: 'cc-sensitive' }, children)
  const Card = ({ title, right, children, className }) => h('section', { className: cls('cc-card', className) },
    (title || right) && h('header', { className: 'cc-card-h' }, h('h3', null, title), right), children)
  const Kpi = ({ label, value, sub, tone }) => h('div', { className: 'cc-kpi' },
    h('div', { className: 'cc-kpi-l' }, label), h('div', { className: cls('cc-kpi-v', tone && 'cc-t-' + tone) }, value),
    sub && h('div', { className: 'cc-kpi-s' }, sub))
  const Bar = ({ value, label, reset, scope }) => {
    const v = value == null ? null : Math.max(0, Math.min(100, value))
    const tone = v == null ? 'unknown' : v >= 90 ? 'bad' : v >= 70 ? 'warn' : 'ok'
    return h('div', { className: 'cc-bar' },
      h('div', { className: 'cc-bar-top' }, h('span', null, label, scope === 'model' ? h('em', null, ' · model') : null),
        h('span', { className: 'cc-mono' }, pct(v), reset ? ` · ${T.resets} ${reset}` : '')),
      h('div', { className: 'cc-bar-track' }, h('div', { className: cls('cc-bar-fill', 'cc-bg-' + tone), style: { width: (v || 0) + '%' } })))
  }
  const Ring = ({ value, size = 44 }) => {
    const r = (size - 6) / 2, c = 2 * Math.PI * r
    const v = value == null ? null : Math.max(0, Math.min(100, value))
    const tone = v == null ? 'unknown' : v >= 90 ? 'bad' : v >= 70 ? 'warn' : 'ok'
    return h('svg', { width: size, height: size, viewBox: `0 0 ${size} ${size}`, className: 'cc-ring' },
      h('circle', { cx: size / 2, cy: size / 2, r, className: 'cc-ring-bg' }),
      v != null && h('circle', { cx: size / 2, cy: size / 2, r, className: 'cc-ring-fg cc-st-' + tone, strokeDasharray: `${(v / 100) * c} ${c}`, transform: `rotate(-90 ${size / 2} ${size / 2})` }),
      h('text', { x: '50%', y: '54%', textAnchor: 'middle', className: 'cc-ring-t' }, v == null ? '?' : Math.round(v)))
  }
  const Spark = ({ values, height = 56 }) => {
    const max = Math.max(1, ...values)
    const w = 100 / Math.max(1, values.length)
    return h('svg', { className: 'cc-spark', viewBox: `0 0 100 ${height}`, preserveAspectRatio: 'none', height },
      values.map((v, i) => h('rect', { key: i, x: i * w + w * 0.15, width: w * 0.7, y: height - (v / max) * height, height: (v / max) * height, rx: 0.6 })))
  }
  const Empty = ({ title, body, tone }) => h('div', { className: cls('cc-empty', tone && 'cc-' + tone) }, h('strong', null, title), body && h('p', null, body))
  const SectionErr = ({ name, err }) => err ? h(Empty, { title: `${T.unavailable} · ${name}`, body: String(err), tone: 'warn' }) : null

  // ---------- views ----------
  const worstUsed = acc => acc.windows.filter(w => w.scope === 'account' && w.used_percent != null).reduce((m, w) => Math.max(m, w.used_percent), -1)

  function Overview({ d }) {
    const A = d.sections.accounts, Ss = d.sections.sessions, Sv = d.sections.savings
    const accounts = A ? A.accounts : []
    const limited = accounts.filter(a => a.state === 'limited' || a.model_cooldowns.length).length
    const cost = Ss ? Ss.projects.reduce((s, p) => s + p.cost_usd, 0) : null
    const sv = Sv && Sv.installed ? Sv.windows['30d'] : null
    return h('div', { className: 'cc-stack' },
      h('div', { className: 'cc-kpis' },
        h(Kpi, { label: T.kAccounts, value: A ? accounts.length : '–', sub: A ? `${limited} ${T.kLimited}` : T.unavailable, tone: limited ? 'warn' : null }),
        h(Kpi, { label: T.kSessions, value: Ss ? Ss.total : '–' }),
        h(Kpi, { label: T.kCost, value: fmtUsd(cost), sub: FR ? 'estimation Hermes' : 'Hermes estimate' }),
        h(Kpi, { label: T.kSaved, value: sv ? fmtTok(sv.tokens_saved) : '–', sub: sv && sv.saved_pct != null ? `${sv.saved_pct}%` : null, tone: 'ok' })),
      h(Card, { title: T.fleet },
        A ? h('div', { className: 'cc-fleet' }, A.providers.filter(p => p.authenticated).map(p => {
          const accs = accounts.filter(a => a.provider === p.slug)
          const used = accs.filter(a => a.state !== 'unavailable').map(worstUsed).filter(v => v >= 0)
          const pw = p.windows.filter(w => w.used_percent != null).map(w => w.used_percent)
          const all = used.concat(pw)
          // Headroom view: the ring shows the least-used account (what the next request can still get).
          const value = all.length ? Math.min(...all) : null
          const nLimited = accs.filter(a => a.state === 'limited' || a.model_cooldowns.length).length
          return h('div', { key: p.slug, className: cls('cc-prov', p.is_current && 'cc-current') },
            h(Ring, { value }),
            h('div', { className: 'cc-prov-b' },
              h('div', { className: 'cc-prov-n' }, p.name, p.is_current && h(Pill, { tone: 'accent' }, T.active)),
              h('div', { className: 'cc-prov-m' }, `${accs.length || 1} · ${p.total_models} ${T.models}`, nLimited ? ` · ${nLimited} ${T.kLimited}` : ''),
              h('div', { className: 'cc-dots' }, accs.map(a => h(Dot, { key: a.key, state: a.model_cooldowns.length ? 'limited' : a.state }))),
              h('div', { className: 'cc-prov-q' }, value == null ? (p.usage_supported ? T.noWindow : T.notExposed) : '')))
        })) : h(SectionErr, { name: T.accounts, err: d.errors.accounts })),
      Sv && Sv.installed && h(Card, { title: T.daily }, h(Spark, { values: Sv.daily.map(x => x.tokens_saved) })))
  }

  function Projects({ d }) {
    const Ss = d.sections.sessions
    if (!Ss) return h(SectionErr, { name: T.projects, err: d.errors.sessions || T.unavailable })
    return h('div', { className: 'cc-stack' },
      h('div', { className: 'cc-note' }, T.attribution),
      h(Card, { title: `${T.projects} · 30 j` }, h('table', { className: 'cc-table' },
        h('thead', null, h('tr', null, [T.project, T.sessions, T.tokens, T.cost, T.providers, T.lastActive].map(x => h('th', { key: x }, x)))),
        h('tbody', null, Ss.projects.map(p => h('tr', { key: p.key },
          h('td', { title: p.path }, h(S, null, p.name || T.noProject)), h('td', { className: 'cc-mono' }, p.sessions),
          h('td', { className: 'cc-mono' }, fmtTok(p.tokens)), h('td', { className: 'cc-mono' }, fmtUsd(p.cost_usd)),
          h('td', null, p.providers.map(x => h(Pill, { key: x.provider }, `${x.provider} ${x.sessions}`))),
          h('td', { className: 'cc-muted' }, rel(p.last_active))))))),
      h(Card, { title: T.recent }, h('table', { className: 'cc-table' },
        h('tbody', null, Ss.recent.map(s => h('tr', { key: s.key },
          h('td', { className: 'cc-title' }, h(S, null, s.title || '—')), h('td', null, h(S, null, s.project || T.noProject)),
          h('td', null, h(Pill, null, s.provider)), h('td', { className: 'cc-muted' }, s.model),
          h('td', { className: 'cc-mono' }, fmtTok(s.tokens)), h('td', { className: 'cc-mono' }, fmtUsd(s.cost_usd)),
          h('td', { className: 'cc-muted' }, rel(s.last_active))))))))
  }

  function Savings({ d }) {
    const Sv = d.sections.savings
    if (!Sv) return h(SectionErr, { name: T.savings, err: d.errors.savings || T.unavailable })
    if (!Sv.installed) return h(Empty, { title: T.notInstalled })
    const rows = [['today', T.today], ['7d', T.d7], ['30d', T.d30]]
    return h('div', { className: 'cc-stack' },
      h(Card, { title: T.savings }, h('table', { className: 'cc-table' },
        h('thead', null, h('tr', null, [T.period, T.requests, T.raw, T.saved, T.costCmp].map(x => h('th', { key: x }, x)))),
        h('tbody', null, rows.map(([k, l]) => { const w = Sv.windows[k]; return h('tr', { key: k },
          h('td', null, l), h('td', { className: 'cc-mono' }, w.requests), h('td', { className: 'cc-mono' }, fmtTok(w.tokens_raw)),
          h('td', { className: 'cc-mono cc-t-ok' }, `${fmtTok(w.tokens_saved)} (${w.saved_pct ?? '–'}%)`),
          h('td', { className: 'cc-mono' }, w.cost_raw_usd == null ? '–' : `${fmtUsd(w.cost_raw_usd)} → ${fmtUsd(w.cost_cmi_usd)}${w.cost_partial ? ` (${T.partial})` : ''}`)) }))),
        h('p', { className: 'cc-muted cc-small' }, T.costNote, ` ${T.recalls}: ${Sv.recalls_7d} cmi_recall, ${Sv.anticipated_7d} ${T.anticipated}.`)),
      h(Card, { title: T.daily }, h(Spark, { values: Sv.daily.map(x => x.tokens_saved), height: 80 })),
      h(Card, { title: T.byModel }, h('table', { className: 'cc-table' }, h('tbody', null, Sv.models.map(m => h('tr', { key: m.model },
        h('td', null, m.model), h('td', { className: 'cc-mono' }, `${m.requests} ${T.requests}`),
        h('td', { className: 'cc-mono cc-t-ok' }, fmtTok(m.saved)), h('td', { className: 'cc-mono cc-muted' }, m.raw ? `${Math.round(m.saved / m.raw * 100)}%` : '–')))))))
  }

  function Security({ d, at }) {
    return h('div', { className: 'cc-stack' },
      h(Card, { title: T.secTitle }, h('div', { className: 'cc-sec' }, T.sec.map(([t, b]) => h('div', { key: t, className: 'cc-sec-i' },
        h('span', { className: 'cc-check' }, '✓'), h('div', null, h('strong', null, t), h('p', null, b)))))),
      h('div', { className: 'cc-note' }, `${T.secLive}: ${at ? new Date(at).toLocaleTimeString() : T.never} · schema v${d.schema} · ${d.host.name} ${d.host.version || ''}`))
  }


  const H = FR ? {
    tab: 'Mes comptes', adv: 'Avancé', back: 'Retour', perso: 'Perso', pro: 'Pro', pro2: 'Pro 2', session: 'Session (5 h)', week: 'Semaine', usedWord: 'consommé', blockedFirst: (a, b) => `${a} est bloqué. Rien ne bascule tout seul depuis cet écran : tu décides.`, switchTo: b => `Passer sur ${b}`, switchBtn: 'Passer sur ce compte', primaryBtn: 'Mettre en principal', primaryDone: 'Compte principal mis à jour', served: (r, t) => `${r} requêtes · ${t} tokens (30 j)`, ledgerWait: 'Suivi par compte activé : les requêtes sont comptées à partir de maintenant.', worth: 'Ce que tu gagnes (30 j)', paid: 'Tu paies', apiVal: 'Au tarif API', gain: 'Gain', priceLbl: 'Abonnement / mois (€)', noPrice: 'Saisis le prix de chaque abonnement pour voir le gain.', rateNote: 'Taux indicatif 1 € ≈ 1,15 $. Le coût API est une estimation ; la part de chaque compte n’est pas connue, la comparaison porte sur tout Anthropic.', connected: 'Connecté', noQuota: 'Quota non exposé par le fournisseur', full: 'Session pleine (à confirmer)', details: 'Détails techniques (modèles, connexions)', openai: 'OpenAI (ChatGPT)', logins: n => `${n} connexions`,
    ready: 'Disponible', blocked: 'Bloqué', unknownS: 'Inconnu', resets: 'Remise à zéro', fullWarn: n => `« ${n} » est à 100 % de sa limite : Claude va refuser les requêtes tant qu'elle n'est pas remise à zéro. Basculer quand même ?`, fullAfter: n => `Attention : « ${n} » est encore à sa limite.`, resetIn: 'Remise à zéro dans', resetDone: 'Remise à zéro en cours…', first: 'Mettre en premier', isFirst: 'Prioritaire pour Claude',
    next: 'Hermes utilisera', noTags: "Aucun compte nommé. Dis à l'assistant lequel est ton perso.",
    confirm: n => `Utiliser « ${n} » pour Claude ? Réessaie ce compte malgré sa pause locale. La tentative en cours sera arrêtée. La conversation est conservée et la bascule s'applique au prochain message. Aucun quota Anthropic n'est rechargé.`,
    projs: 'Projets sur ce fournisseur (7 j)', allBlocked: 'Les comptes sont marqués en pause. Tu peux réessayer un compte manuellement ; une vraie limite Anthropic reste applicable.', someBlocked: 'Un compte est bloqué, Hermes bascule sur l’autre.', costT: 'Coût estimé · 12 mois', costNote: 'Équivalent au tarif API : pour un abonnement, ce n’est pas ce que tu paies.', byProv: 'Par fournisseur', byProj: 'Par projet', saved: 'Économisé par CMI (30 j)', tokensSaved: 'tokens', nothing: 'Aucun projet récent',
    dups: n => `${n} connexions enregistrées pour ce compte (les reconnexions s’ajoutent)`, unnamed: 'Compte non nommé', fresh: 'Nouvelle connexion détectée : à quel compte correspond-elle ?', nameIt: 'C’est mon',
    addBtn: 'Ajouter un compte', addWho: 'Quel nom pour ce compte ?', addNone: 'Les trois noms (Perso, Pro, Pro 2) sont déjà utilisés.', addStep: 'Connecte-toi avec le compte à ajouter, puis copie le code affiché par Anthropic et colle-le ici.', addOpen: 'Ouvrir la page de connexion', addCopy: 'Copier le lien', addCopied: 'Lien copié', addPrivate: 'Déjà connecté à un autre compte Claude dans ton navigateur ? Copie le lien et ouvre-le dans une fenêtre privée.', addCode: 'Code de connexion', addOk: 'Valider', addCancel: 'Annuler', addAdded: n => `Compte « ${n} » ajouté.`, addUnchecked: ' Je n’ai pas pu vérifier s’il était déjà connecté : compare ses jauges avec les autres cartes.', addDup: n => `Ce compte est déjà connecté${n ? ` (${n})` : ''}. Rien n’a été ajouté. Dans ton navigateur, déconnecte-toi de claude.ai ou utilise une fenêtre privée, puis recommence avec l’autre compte.`, addFail: 'Connexion refusée : code incorrect ou expiré. Recommence.', provBtn: 'Ajouter un autre fournisseur', provName: 'Nom du compte (facultatif)', provConnect: 'Se connecter', provWho: 'Quel fournisseur ?', provLink: 'Ouvre la page de connexion, connecte-toi et autorise Hermes. Cette fenêtre se met à jour toute seule.', provCode: 'Code à saisir sur la page :', provWait: 'En attente de ta connexion…', provKey: 'Clé API', provKeyHelp: 'Colle la clé API du fournisseur. Elle est transmise à Hermes et n’est jamais affichée.', provOk: n => `Fournisseur « ${n} » ajouté.`, provFail: 'Connexion échouée ou expirée. Recommence.', provKeyFail: 'Clé refusée ou fournisseur injoignable. Vérifie la clé.',
    done: 'Compte prioritaire mis à jour', fail: 'Échec du changement', note: "Rien ne bouge tout seul : l'ordre ne change que si tu cliques.",
  } : {
    tab: 'My accounts', adv: 'Advanced', back: 'Back', perso: 'Personal', pro: 'Work', pro2: 'Work 2', session: 'Session (5 h)', week: 'Week', usedWord: 'used', blockedFirst: (a, b) => `${a} is blocked. Nothing switches by itself from this screen: you decide.`, switchTo: b => `Switch to ${b}`, switchBtn: 'Switch to this account', primaryBtn: 'Use as primary', primaryDone: 'Primary account updated', served: (r, t) => `${r} requests · ${t} tokens (30 d)`, ledgerWait: 'Per-account tracking is on: requests are counted from now on.', worth: 'What you gain (30 d)', paid: 'You pay', apiVal: 'At API price', gain: 'Gain', priceLbl: 'Subscription / month (€)', noPrice: 'Enter each subscription price to see the gain.', rateNote: 'Indicative rate 1 € ≈ $1.15. API cost is an estimate; per-account share is unknown, so the comparison covers all of Anthropic.', connected: 'Connected', noQuota: 'Quota not exposed by the provider', full: 'Session full (unconfirmed)', details: 'Technical details (models, logins)', openai: 'OpenAI (ChatGPT)', logins: n => `${n} logins`,
    ready: 'Available', blocked: 'Blocked', unknownS: 'Unknown', resets: 'Resets', fullWarn: n => `"${n}" is at 100% of its limit: Claude will reject requests until it resets. Switch anyway?`, fullAfter: n => `Warning: "${n}" is still at its limit.`, resetIn: 'Resets in', resetDone: 'Resetting…', first: 'Make first', isFirst: 'Preferred for Claude',
    next: 'Hermes will use', noTags: 'No account is named yet. Tell the assistant which one is your personal account.',
    confirm: n => `Use "${n}" for Claude? Retries this account despite its local backoff. The current attempt will be stopped. Conversation is preserved and the switch applies to the next message. Anthropic quota is not reset.`,
    projs: 'Projects on this provider (7 d)', allBlocked: 'Accounts are marked paused. You can manually retry one; an actual Anthropic quota limit still applies.', someBlocked: 'One account is blocked, Hermes falls back to the other.', costT: 'Estimated cost · 12 months', costNote: 'API-price equivalent: for a subscription this is not what you pay.', byProv: 'By provider', byProj: 'By project', saved: 'Saved by CMI (30 d)', tokensSaved: 'tokens', nothing: 'No recent project',
    dups: n => `${n} logins stored for this account (re-logins add new ones)`, unnamed: 'Unnamed account', fresh: 'New login detected: which account is it?', nameIt: 'This is my',
    addBtn: 'Add an account', addWho: 'What should this account be called?', addNone: 'All three names (Personal, Work, Work 2) are already used.', addStep: 'Sign in with the account to add, then copy the code Anthropic shows and paste it here.', addOpen: 'Open the sign-in page', addCopy: 'Copy link', addCopied: 'Link copied', addPrivate: 'Already signed in to another Claude account in your browser? Copy the link and open it in a private window.', addCode: 'Sign-in code', addOk: 'Confirm', addCancel: 'Cancel', addAdded: n => `Account "${n}" added.`, addUnchecked: ' Could not check whether it was already connected: compare its gauges with the other cards.', addDup: n => `This account is already connected${n ? ` (${n})` : ''}. Nothing was added. In your browser, sign out of claude.ai or use a private window, then retry with the other account.`, addFail: 'Sign-in refused: wrong or expired code. Try again.', provBtn: 'Add another provider', provName: 'Account name (optional)', provConnect: 'Connect', provWho: 'Which provider?', provLink: 'Open the sign-in page, sign in and authorize Hermes. This panel updates by itself.', provCode: 'Code to enter on the page:', provWait: 'Waiting for you to sign in…', provKey: 'API key', provKeyHelp: 'Paste the provider API key. It is handed to Hermes and never displayed.', provOk: n => `Provider "${n}" added.`, provFail: 'Sign-in failed or expired. Try again.', provKeyFail: 'Key refused or provider unreachable. Check the key.',
    done: 'Order updated', fail: 'Could not change', note: 'Nothing moves by itself: the order only changes when you click.',
  }

  // One shared 1 s clock: every countdown re-renders from it, nothing is fetched.
  function useNow() {
    const [now, setNow] = useState(() => Date.now())
    useEffect(() => {
      const id = setInterval(() => { if (typeof document === 'undefined' || !document.hidden) setNow(Date.now()) }, 1000)
      return () => clearInterval(id)
    }, [])
    return now
  }
  // Exact time left: 4 h 12 min 07 s / 3 j 4 h 12 min (seconds only under a day, they would be noise above).
  const exact = ms => {
    const t = Math.max(0, Math.floor(ms / 1000)), d = Math.floor(t / 86400), hh = Math.floor((t % 86400) / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60
    const p2 = n => String(n).padStart(2, '0')
    return d ? `${d} j ${hh} h ${p2(m)} min` : hh ? `${hh} h ${p2(m)} min ${p2(sec)} s` : m ? `${m} min ${p2(sec)} s` : `${sec} s`
  }
  const clock = at => {
    const day = new Date(at).toDateString() === new Date().toDateString() ? '' : new Date(at).toLocaleDateString(undefined, { weekday: 'short' }) + ' '
    return day + new Date(at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  }
  function Gauge({ label, win }) {
    const now = useNow()
    const used = win ? win.used_percent : null
    const u = used == null ? null : Math.round(used)
    const tone = u == null ? '' : u >= 90 ? 'cc-bad' : u >= 70 ? 'cc-warn' : 'cc-ok'
    const at = win && win.resets_at ? Date.parse(win.resets_at) : NaN
    return h('div', { className: 'cc-g' },
      h('div', { className: 'cc-g-h' }, h('span', null, label), h('strong', null, u == null ? '–' : `${u} % ${H.usedWord}`)),
      h('div', { className: 'cc-gbar' }, h('i', { className: tone, style: { width: `${u == null ? 0 : u}%` } })),
      isNaN(at) ? null : h('div', { className: 'cc-muted cc-small cc-mono' },
        at - now > 0 ? `${H.resetIn} ${exact(at - now)} · ${clock(at)}` : H.resetDone))
  }

  // One row per real account: logins with identical live quota are the same account (re-logins).
  const groupAccounts = list => {
    const sig = a => a.windows.length ? a.provider + JSON.stringify(a.windows.map(w => [w.label, w.used_percent])) : null
    const out = [], seen = {}
    list.forEach(a => {
      const k = sig(a)
      if (k && seen[k]) { seen[k].n += 1; seen[k].entries.push(a); return }
      const g = { rep: a, n: 1, entries: [a] }
      if (k) seen[k] = g
      out.push(g)
    })
    return out
  }

  const TAGS = ['perso', 'pro', 'pro2']
  // The user's own name for a slot (set when adding the account), else the built-in label.
  let SLOT_NAMES = {}
  const nm = t => SLOT_NAMES[t] || H[t] || ''

  // Claude sign-in (the one provider where the user pastes a code): Hermes' own login runs in the backend;
  // this step shows its link and relays the one-time code. A login for an account already connected is refused.
  function ClaudeLogin({ tag, name, id, url, onDone, onCancel }) {
    const [code, setCode] = useState('')
    const [busy, setBusy] = useState(false)
    const [error, setError] = useState('')
    const finish = () => {
      setBusy(true)
      post('/accounts/add/finish', { intent: 'add-account-code', id, code: code.trim() }).then(r => {
        setBusy(false)
        if (r.status === 'added') return onDone(H.addAdded(name || nm(tag)) + (r.checked ? '' : H.addUnchecked), '')
        if (r.status === 'duplicate') return onDone('', H.addDup(r.of ? nm(r.of) : ''))
        setError(H.addFail + (r.detail ? ' (' + r.detail + ')' : ''))
      }).catch(() => { setBusy(false); setError(H.addFail) })
    }
    const open = e => { e.preventDefault(); post('/login/open', { intent: 'open-login', id }).catch(() => {}) }
    const copy = () => { try { navigator.clipboard.writeText(url).then(() => notify && notify(H.addCopied)) } catch (e) { /* the link stays visible */ } }
    return h('div', { className: 'cc-note' }, `${name || nm(tag)} · ${H.addStep}`,
      error ? h('div', { className: 'cc-n-bad', role: 'alert' }, error) : null,
      h('div', { className: 'cc-acct-f' },
        h('a', { className: 'cc-btn cc-primary', href: url, target: '_blank', rel: 'noopener noreferrer', onClick: open }, H.addOpen),
        h('button', { className: 'cc-btn', onClick: copy }, H.addCopy)),
      h('div', { className: 'cc-muted cc-small' }, H.addPrivate),
      h('div', { className: 'cc-acct-f' },
        h('input', { className: 'cc-sel', type: 'text', autoComplete: 'off', spellCheck: false, 'aria-label': H.addCode, placeholder: H.addCode, value: code, onChange: e => setCode(e.target.value) }),
        h('button', { className: 'cc-btn cc-primary', disabled: busy || code.trim().length < 8, onClick: finish }, H.addOk),
        h('button', { className: 'cc-btn', disabled: busy, onClick: onCancel }, H.addCancel)))
  }

  // 'Add an account' (Claude, OpenAI, Nous, OpenRouter, ...): pick the provider, name the account, Connect.
  // Hermes' own `auth add` runs in the backend. OAuth providers: the sign-in page opens in the browser
  // (opened by Hermes itself on this machine, like in the terminal; the link stays as a fallback,
  // + the code to type there, if any) and we poll until Hermes has saved the login. Others: an API key.
  const LINK_PROVIDERS = ['nous', 'openai-codex', 'xai-oauth', 'qwen-oauth', 'minimax-oauth']
  // Only ever open an https page without embedded credentials, whatever the backend returned.
  const safeUrl = u => { try { const x = new URL(u); return x.protocol === 'https:' && !x.username && !x.password ? x.href : null } catch (e) { return null } }
  function AddProvider({ providers, free, reload }) {
    const [st, setSt] = useState({ step: 'idle' })
    const [slug, setSlug] = useState('')
    const [label, setLabel] = useState('')
    const [key, setKey] = useState('')
    const [busy, setBusy] = useState(false)
    const sid = st.step === 'link' ? st.id : null
    const cur = providers.find(p => p.slug === slug)
    const nameOf = p => (p && (p.slug === 'anthropic' ? 'Claude' : (p.name || p.slug))) || ''
    const isClaude = slug === 'anthropic'
    const tagNow = free[0] || ''
    const isLink = LINK_PROVIDERS.indexOf(slug) >= 0
    const finishClaude = (ok, err) => { if (ok) notify && notify(ok); setSlug(''); setLabel(''); setSt({ step: 'idle', ok, error: err }); return reload() }
    const done = (name) => { const m = H.provOk(label.trim() || name); notify && notify(m); setSlug(''); setLabel(''); setKey(''); setSt({ step: 'idle', ok: m }); return reload() }
    const fail = msg => { setBusy(false); setKey(''); setSt({ step: 'form', error: msg }) }
    useEffect(() => {
      if (!sid) return undefined
      let stop = false
      const tick = () => post('/providers/add/status', { intent: 'add-provider-status', id: sid }).then(r => {
        if (stop || r.status === 'waiting') return
        if (r.status === 'added') return done(st.name)
        fail(H.provFail)
      }).catch(() => { if (!stop) fail(H.provFail) })
      const t = setInterval(tick, 3000)
      return () => { stop = true; clearInterval(t) }
    }, [sid])
    const connect = () => {
      setBusy(true)
      const name = nameOf(cur)
      if (isClaude) {
        post('/accounts/add/begin', { intent: 'add-account', tag: tagNow, name: label.trim() }).then(r => {
          setBusy(false)
          const url = safeUrl(r.url)
          if (!url) return fail(H.provFail)
          setSt({ step: 'claude', id: r.id, url, tag: tagNow, name: label.trim() })
        }).catch(() => fail(H.provFail))
      } else if (isLink) {
        post('/providers/add/begin', { intent: 'add-provider', provider: slug, label: label.trim() }).then(r => {
          setBusy(false)
          const url = safeUrl(r.url)
          if (!url) return fail(H.provFail)
          setSt({ step: 'link', id: r.id, url, userCode: r.user_code, name })
        }).catch(() => fail(H.provFail))
      } else {
        post('/providers/add/key', { intent: 'add-provider-key', provider: slug, key: key.trim(), label: label.trim() }).then(r => {
          setBusy(false)
          if (r.status === 'added') return done(name)
          fail(H.provKeyFail)
        }).catch(() => fail(H.provKeyFail))
      }
    }
    const back = () => { setKey(''); setSlug(''); setLabel(''); setSt({ step: 'idle' }) }
    if (st.step === 'idle') return h('div', { className: 'cc-stack' },
      st.error ? h('div', { className: 'cc-note cc-n-bad', role: 'alert' }, st.error) : null,
      st.ok ? h('div', { className: 'cc-note', role: 'status' }, st.ok) : null,
      h('div', null, h('button', { className: 'cc-btn cc-primary', onClick: () => setSt({ step: 'form' }) }, '+ ' + H.addBtn)))
    if (st.step === 'form') return h('div', { className: 'cc-note' },
      st.error ? h('div', { className: 'cc-n-bad', role: 'alert' }, st.error) : null,
      h('div', { className: 'cc-acct-f' },
        h('select', { className: 'cc-sel', 'aria-label': H.provWho, value: slug, onChange: e => setSlug(e.target.value) },
          h('option', { value: '' }, H.provWho),
          providers.map(p => h('option', { key: p.slug, value: p.slug }, nameOf(p)))),
        isClaude && !free.length ? h('span', { className: 'cc-muted' }, H.addNone)
          : h('input', { className: 'cc-sel', type: 'text', maxLength: 40, autoComplete: 'off', 'aria-label': H.provName, placeholder: H.provName, value: label, onChange: e => setLabel(e.target.value) }),
        slug && !isLink && !isClaude ? h('input', { className: 'cc-sel', type: 'password', autoComplete: 'off', spellCheck: false, 'aria-label': H.provKey, placeholder: H.provKey, value: key, onChange: e => setKey(e.target.value) }) : null,
        h('button', { className: 'cc-btn cc-primary', disabled: busy || !slug || (isClaude && !tagNow) || (!isLink && !isClaude && key.trim().length < 8), onClick: connect }, H.provConnect),
        h('button', { className: 'cc-btn', disabled: busy, onClick: back }, H.addCancel)),
      slug && !isLink && !isClaude ? h('div', { className: 'cc-muted cc-small' }, H.provKeyHelp) : null)
    if (st.step === 'claude') return h(ClaudeLogin, { tag: st.tag, name: st.name, id: st.id, url: st.url, onDone: finishClaude, onCancel: back })
    return h('div', { className: 'cc-note' }, `${st.name} · ${H.provLink}`,
      h('div', { className: 'cc-acct-f' },
        h('a', { className: 'cc-btn cc-primary', href: st.url, target: '_blank', rel: 'noopener noreferrer', onClick: e => { e.preventDefault(); post('/login/open', { intent: 'open-login', id: st.id }).catch(() => {}) } }, H.addOpen),
        st.userCode ? h('span', null, H.provCode, ' ', h('strong', { className: 'cc-mono' }, st.userCode)) : null),
      h('div', { className: 'cc-muted cc-small' }, H.provWait),
      h('div', { className: 'cc-acct-f' }, h('button', { className: 'cc-btn', onClick: back }, H.addCancel)))
  }

  function Home({ d, reload, actions }) {
    const A = d.sections.accounts
    const [busy, setBusy] = useState(false)
    const [actionMessage, setActionMessage] = useState('')
    const canPost = actions && post
    if (!A) return h(SectionErr, { name: H.tab, err: d.errors.accounts || T.unavailable })
    SLOT_NAMES = A.names || {}
    const list = A.accounts.filter(a => a.provider === 'anthropic').sort((x, y) => x.priority - y.priority)
    const firstTag = list[0] && list[0].tag
    const isFull = tag => { const es = list.filter(a => a.tag === tag); return es.length > 0 && es.every(a => a.state === 'limited' || (a.windows || []).some(w => w.scope !== 'model' && w.used_percent != null && w.used_percent >= 100)) }
    const selectAccount = tag => {
      const full = isFull(tag)
      if (typeof window !== 'undefined' && !window.confirm(full ? H.fullWarn(nm(tag)) : H.confirm(nm(tag)))) return
      setBusy(true)
      setActionMessage('')
      Promise.resolve().then(() => switchAccount ? switchAccount(tag) : post('/accounts/first', { intent: 'make-first', tag })).then(message => { const text = (full ? H.fullAfter(nm(tag)) + ' ' : '') + (typeof message === 'string' ? message : H.done); setActionMessage(text); notify && notify(text); return reload() }).catch(e => { setActionMessage(e.message || H.fail); notify && notify(e.message || H.fail) }).finally(() => setBusy(false))
    }
    const makePrimary = tag => {
      setBusy(true); setActionMessage('')
      post('/accounts/first', { intent: 'make-first', tag }).then(() => { setActionMessage(H.primaryDone); notify && notify(H.primaryDone); return reload() })
        .catch(() => { setActionMessage(H.fail); notify && notify(H.fail) }).finally(() => setBusy(false))
    }
    const cards = TAGS.map(tag => {
      const es = list.filter(a => a.tag === tag)
      if (!es.length) return null
      const blocked = es.every(a => a.state === 'limited')
      const paused = es.some(a => a.last_error_code || (a.model_cooldowns || []).length)
      const w = (es.find(a => a.windows.length) || es[0]).windows
      const pick = re => w.find(x => re.test(x.label))
      const resets = es.map(a => a.last_error_reset_in_s).filter(Boolean)[0]
      return h(Card, { key: tag, className: 'cc-acct', title: nm(tag), right: h(Pill, { tone: blocked ? (paused ? 'bad' : 'warn') : 'ok' }, blocked ? (paused ? H.blocked : H.full) : H.ready) },
        h(Gauge, { label: H.session, win: pick(/session/i) }), h(Gauge, { label: H.week, win: pick(/week/i) }),
        resets ? h('div', { className: 'cc-muted cc-small' }, `${H.resets}: ${dur(resets)}`) : null,
        (() => { const L = (A.ledger || {}).accounts || {}; const r = es.reduce((t, a) => t + ((L[a.key] || {}).requests || 0), 0), k = es.reduce((t, a) => t + ((L[a.key] || {}).tokens || 0), 0); return A.ledger && A.ledger.installed ? h('div', { className: 'cc-muted cc-small' }, H.served(r, fmtTok(k))) : h('div', { className: 'cc-muted cc-small' }, H.ledgerWait) })(),
        canPost ? h('label', { className: 'cc-muted cc-small' }, H.priceLbl, ' ', h('input', { className: 'cc-sel', type: 'number', min: 0, style: { width: 80 }, defaultValue: (A.prices || {})[tag] ?? '', onBlur: e => { const v = parseFloat(e.target.value); if (!isNaN(v)) post('/accounts/price', { intent: 'set-price', tag, eur: v }).then(() => reload()).catch(() => notify && notify(H.fail)) } })) : null,
        h('div', { className: 'cc-acct-f' },
          firstTag === tag ? h('div', { className: 'cc-acct-st' }, h(Pill, { tone: 'ok' }, `✓ ${H.isFirst}`)) : null,
          canPost ? h('div', { className: 'cc-acct-act' },
            h('button', { className: 'cc-btn cc-primary', disabled: busy, title: H.switchTo(nm(tag)), 'aria-label': H.switchTo(nm(tag)), onClick: () => selectAccount(tag) }, H.switchBtn),
            firstTag !== tag ? h('button', { className: 'cc-btn', disabled: busy, onClick: () => makePrimary(tag) }, H.primaryBtn) : null) : null))
    }).filter(Boolean)
    // An Anthropic login without a perso/pro name must still be visible (with its quota), never silently hidden.
    list.filter(a => !a.tag && a.state !== 'unavailable' && !/^env:/.test(a.source || '')).forEach(a => {
      const pick = re => (a.windows || []).find(x => re.test(x.label))
      cards.push(h(Card, { key: 'u-' + a.key, className: 'cc-acct', title: `${H.unnamed} · #${a.priority + 1}`, right: h(Pill, { tone: a.state === 'limited' ? 'bad' : 'warn' }, a.state === 'limited' ? H.blocked : H.ready) },
        h(Gauge, { label: H.session, win: pick(/session/i) }), h(Gauge, { label: H.week, win: pick(/week/i) })))
    })
    const oa = groupAccounts(A.accounts.filter(a => a.provider === 'openai-codex'))
    if (oa.length) {
      const g = oa[0], a = g.rep, w = a.windows
      const pick = re => w.find(x => re.test(x.label))
      cards.push(h(Card, { key: 'openai', className: 'cc-acct', title: H.openai, right: h(Pill, { tone: a.state === 'limited' ? 'bad' : 'ok' }, a.state === 'limited' ? H.blocked : H.ready) },
        h(Gauge, { label: H.session, win: pick(/session/i) }), h(Gauge, { label: H.week, win: pick(/week/i) }),
        g.n > 1 ? h('div', { className: 'cc-muted cc-small' }, H.logins(g.n)) : null))
    }
    const NAMES = { zai: 'GLM (Z.AI)', copilot: 'GitHub Copilot' }
    Object.keys(NAMES).forEach(slug => {
      const g = groupAccounts(A.accounts.filter(a => a.provider === slug))
      if (!g.length) return
      cards.push(h(Card, { key: slug, className: 'cc-acct', title: NAMES[slug], right: h(Pill, { tone: 'ok' }, H.connected) },
        h('div', { className: 'cc-muted' }, H.noQuota),
        h('div', { className: 'cc-muted cc-small' }, `${g[0].rep.auth_type} · ${g[0].rep.request_count} ${T.requests}`)))
    })
    if (!cards.length) return h(Empty, { title: H.noTags })
    const fresh = list.filter(a => !a.tag && a.state !== 'unavailable' && a.source !== 'env:ANTHROPIC_TOKEN' && !/^env:/.test(a.source || ''))
    const name = (key, tag) => { setBusy(true); post('/accounts/name', { intent: 'name-account', key, tag }).then(() => reload()).catch(() => notify && notify(H.fail)).finally(() => setBusy(false)) }
    const nBlocked = list.filter(a => a.state === 'limited').length
    const live = list.filter(a => a.state !== 'unavailable')
    const status = !live.length ? null : live.every(a => a.state === 'limited') ? ['bad', H.allBlocked] : nBlocked ? ['warn', H.someBlocked] : null
    const Ss = d.sections.sessions, Sv = d.sections.savings
    const onAnth = Ss ? Ss.projects.filter(p => p.providers.some(x => x.provider === 'anthropic' && x.active_7d)) : []
    return h('div', { className: 'cc-stack' },
      actionMessage ? h('div', { className: 'cc-note', role: 'status' }, actionMessage) : null,
      status ? h('div', { className: cls('cc-note', 'cc-n-' + status[0]) }, status[1]) : null,
      (() => {
        const lead = list[0]; if (!canPost || !lead || lead.state !== 'limited' || !lead.tag) return null
        const other = TAGS.find(t => t !== lead.tag && list.some(a => a.tag === t && a.state !== 'limited' && a.state !== 'unavailable'))
        if (!other) return null
        return h('div', { className: 'cc-note cc-n-bad' }, H.blockedFirst(nm(lead.tag), nm(other)),
          h('div', { className: 'cc-acct-f' }, h('button', { className: 'cc-btn cc-primary', disabled: busy, onClick: () => selectAccount(other) }, H.switchTo(nm(other)))))
      })(),
      canPost ? h(AddProvider, { providers: A.providers, free: TAGS.filter(t => !list.some(a => a.tag === t)), reload }) : null,
      fresh.length && canPost ? h('div', { className: 'cc-note cc-n-warn' }, H.fresh, fresh.map(a => h('div', { key: a.key, className: 'cc-acct-f' }, h('span', { className: 'cc-muted' }, `#${a.priority + 1}`), ['perso', 'pro'].map(t => h('button', { key: t, className: 'cc-btn', disabled: busy, onClick: () => name(a.key, t) }, `${H.nameIt} ${H[t].toLowerCase()}`))))) : null,
      h('div', { className: 'cc-cards2' }, cards),
      canPost ? h('div', { className: 'cc-note' }, H.note) : null,
      h('div', { className: 'cc-stack' },
      Ss ? h(Card, { title: `${H.projs} · anthropic` }, onAnth.length ? h('div', { className: 'cc-chips' }, onAnth.map(p => h(Pill, { key: p.key, tone: status && status[0] === 'bad' ? 'bad' : undefined }, h(S, null, p.name || T.noProject)))) : h('div', { className: 'cc-muted' }, H.nothing)) : null,
      Ss ? (() => {
        const pr = A.prices || {}, paidEur = TAGS.reduce((t, k) => t + (pr[k] || 0), 0)
        const api = (Ss.cost_30d || {}).anthropic || 0, gainUsd = api - paidEur * 1.15
        return h(Card, { title: H.worth }, paidEur > 0 ? h('div', { className: 'cc-cost-g' },
          h('div', null, h('h4', null, H.paid), h('div', { className: 'cc-big' }, `${paidEur.toFixed(0)} €`)),
          h('div', null, h('h4', null, H.apiVal), h('div', { className: 'cc-big' }, fmtUsd(api))),
          h('div', null, h('h4', null, H.gain), h('div', { className: cls('cc-big', gainUsd >= 0 ? 'cc-t-ok' : 'cc-t-bad') }, `${gainUsd >= 0 ? '+' : '−'}${fmtUsd(Math.abs(gainUsd))}`, paidEur ? h('span', { className: 'cc-muted cc-small' }, `  ×${(api / (paidEur * 1.15)).toFixed(1)}`) : null)))
          : h('div', { className: 'cc-muted' }, H.noPrice), h('p', { className: 'cc-muted cc-small' }, H.rateNote))
      })() : null,
      Ss ? h(Card, { title: H.costT },
        h('p', { className: 'cc-muted cc-small' }, H.costNote),
        h('div', { className: 'cc-cost-g' },
          h('div', null, h('h4', null, H.byProv), h('table', { className: 'cc-table' }, h('tbody', null, Ss.providers.map(v => h('tr', { key: v.provider }, h('td', null, v.provider), h('td', { className: 'cc-mono' }, fmtUsd(v.cost_usd)), h('td', { className: 'cc-mono cc-muted' }, fmtTok(v.tokens))))))),
          h('div', null, h('h4', null, H.byProj), h('table', { className: 'cc-table' }, h('tbody', null, Ss.projects.filter(p => p.cost_usd > 0).sort((a, b) => b.cost_usd - a.cost_usd).slice(0, 10).map(p => h('tr', { key: p.key },
            h('td', null, h(S, null, p.name || T.noProject)), h('td', { className: 'cc-mono' }, fmtUsd(p.cost_usd)),
            h('td', null, p.providers.filter(x => x.cost_usd > 0).map(x => h(Pill, { key: x.provider }, `${x.provider} ${fmtUsd(x.cost_usd)}`))))))))) ) : null,
      Sv && Sv.installed ? h(Card, { title: H.saved }, h('div', { className: 'cc-big cc-t-ok' }, `${fmtTok(Sv.windows['30d'].tokens_saved)} ${H.tokensSaved}`,
        Sv.windows['30d'].cost_raw_usd == null ? null : h('span', { className: 'cc-muted cc-small' }, `  ${fmtUsd(Sv.windows['30d'].cost_raw_usd)} → ${fmtUsd(Sv.windows['30d'].cost_cmi_usd)}`))) : null))
  }


  // ---------- root ----------
  const TABS = [['overview', T.overview], ['projects', T.projects], ['savings', T.savings], ['security', T.security]]

  return function ControlCenter() {
    const [tab, setTab] = useState(() => { const t = store.get('tab', 'overview'); return TABS.some(x => x[0] === t) ? t : 'overview' })
    const [auto, setAuto] = useState(() => store.get('auto', true))
    const [priv, setPriv] = useState(() => store.get('privacy', false))
    // `initial` (a snapshot) lets a shell or a test render without waiting on the first fetch.
    const [state, setState] = useState(initial ? { status: 'ready', data: initial, error: null, at: Date.now() } : { status: 'loading', data: null, error: null, at: 0 })
    const busy = useRef(false)

    const load = useCallback(async force => {
      if (busy.current) return
      busy.current = true
      try {
        const data = await api(force ? '/snapshot?refresh=1' : '/snapshot')
        if (!data || data.error) throw Object.assign(new Error(data && data.error || 'empty'), { code: data && data.error })
        setState({ status: 'ready', data, error: null, at: Date.now() })
      } catch (e) {
        const code = e && (e.code || (String(e.message || '').includes('leak_guard') ? 'leak_guard' : null))
        setState(s => ({ ...s, status: s.data ? 'stale' : (code === 'leak_guard' ? 'leak' : 'disconnected'), error: code || String(e && e.message || e) }))
        if (force && notify) notify(code === 'leak_guard' ? T.leak : T.disconnected)
      } finally { busy.current = false }
    }, [])

    useEffect(() => { load(false) }, [load])
    useEffect(() => {
      if (!auto) return undefined
      const id = setInterval(() => { if (typeof document === 'undefined' || !document.hidden) load(false) }, 15000)
      const onVis = () => { if (!document.hidden) load(false) }
      if (typeof document !== 'undefined') document.addEventListener('visibilitychange', onVis)
      return () => { clearInterval(id); if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVis) }
    }, [auto, load])

    const pick = k => { setTab(k); store.set('tab', k) }
    const d = state.data
    const active = d && d.sections.accounts && d.sections.accounts.active
    const body = useMemo(() => {
      if (state.status === 'loading') return h('div', { className: 'cc-skel' }, [0, 1, 2].map(i => h('div', { key: i })), h('p', { className: 'cc-muted' }, T.loading))
      if (state.status === 'disconnected') return h(Empty, { title: T.disconnected, body: T.disconnectedHelp, tone: 'bad' })
      if (state.status === 'leak') return h(Empty, { title: T.leak, body: T.leakHelp, tone: 'bad' })
      const V = { overview: p => h(Home, { ...p, reload: () => load(true), actions: true }), projects: Projects, savings: Savings, security: Security }[tab] || Overview
      return h(V, { d, at: state.at })
    }, [state, tab, post])

    return h('div', { className: cls('cc-root', priv && 'cc-private') },
      h('style', null, CSS),
      h('header', { className: 'cc-top' },
        h('div', null,
          h('h1', null, 'CMI ', h('span', null, T.title)),
          h('div', { className: 'cc-muted cc-small' },
            d ? `${d.host.name} ${d.host.version || ''} · ${d.host.profile || ''}` : '',
            active && active.provider ? ` · ${T.active}: ${active.provider} / ${active.model || '?'}` : '')),
        h('div', { className: 'cc-actions' },
          state.status === 'stale' && h(Pill, { tone: 'warn' }, T.disconnected),
          h('span', { className: 'cc-muted cc-small' }, `${T.synced} ${state.at ? rel(state.at / 1000) : T.never}`),
          h('label', { className: 'cc-toggle' }, h('input', { type: 'checkbox', checked: priv, onChange: e => { setPriv(e.target.checked); store.set('privacy', e.target.checked) } }), T.privacy),
          h('label', { className: 'cc-toggle' }, h('input', { type: 'checkbox', checked: auto, onChange: e => { setAuto(e.target.checked); store.set('auto', e.target.checked) } }), T.auto),
          h('button', { className: 'cc-btn', onClick: () => load(true) }, '↻ ', T.refresh))),
      h('nav', { className: 'cc-tabs' }, TABS.map(([k, l]) => h('button', { key: k, className: cls('cc-tab', tab === k && 'cc-on'), onClick: () => pick(k) }, l))),
      h('main', { className: 'cc-main' }, body))
  }
}

// Theme: shells define --cc-* (Hermes maps them to --ui-*). Fallbacks keep a standalone page readable.
const CSS = `
.cc-root{--fg:var(--cc-fg,CanvasText);--fg2:var(--cc-fg2,GrayText);--fg3:var(--cc-fg3,GrayText);--line:var(--cc-line,color-mix(in srgb,CanvasText 12%,transparent));--card:var(--cc-card,color-mix(in srgb,CanvasText 3%,transparent));--acc:var(--cc-accent,LinkText);--ok:var(--cc-ok,#3fb950);--warn:var(--cc-warn,#d29922);--bad:var(--cc-bad,#f85149);
 color:var(--fg);height:100%;overflow:auto;padding:20px 24px 40px;font-size:13px;line-height:1.45;box-sizing:border-box}
.cc-root *{box-sizing:border-box}
.cc-top{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:14px}
.cc-top h1{margin:0;font-size:20px;font-weight:650;letter-spacing:-.02em}.cc-top h1 span{color:var(--fg2);font-weight:500}
.cc-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.cc-btn{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:7px;padding:5px 11px;cursor:pointer;font:inherit;white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis;min-height:30px}.cc-btn:disabled{opacity:.55;cursor:default}.cc-btn.cc-primary{border-color:color-mix(in srgb,var(--acc) 55%,transparent);background:color-mix(in srgb,var(--acc) 16%,transparent);font-weight:600}
.cc-btn:hover{border-color:var(--acc)}
.cc-toggle{display:flex;gap:5px;align-items:center;color:var(--fg2);font-size:12px;cursor:pointer}
.cc-cards2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(280px,100%),1fr));gap:16px}.cc-acct .cc-g{margin:14px 0}.cc-g-h{display:flex;justify-content:space-between;font-size:13px;margin-bottom:6px}.cc-gbar{height:10px;border-radius:6px;background:var(--line)}.cc-gbar i{display:block;height:100%;border-radius:6px;background:var(--acc)}.cc-gbar i.cc-ok{background:var(--ok)}.cc-gbar i.cc-warn{background:var(--warn)}.cc-gbar i.cc-bad{background:var(--bad)}.cc-acct{display:flex;flex-direction:column}.cc-acct-f{display:flex;flex-direction:column;gap:8px;margin-top:auto;padding-top:16px;min-width:0}.cc-note .cc-acct-f{flex-direction:row;flex-wrap:wrap;align-items:center;margin-top:10px;padding-top:0}.cc-acct-st{display:flex}.cc-acct-act{display:flex;flex-wrap:wrap;gap:8px}.cc-acct-act .cc-btn{flex:1 1 auto}
.cc-sel{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:3px 6px;font:inherit}.cc-sel:focus{outline:none;border-color:var(--acc)}
.cc-chips{display:flex;gap:6px;flex-wrap:wrap}.cc-cost-g{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr));gap:20px}.cc-cost-g h4{margin:6px 0;font-size:12px;color:var(--fg2);text-transform:uppercase;letter-spacing:.04em}.cc-big{font-size:26px;font-weight:600}.cc-n-bad{border-color:var(--bad);color:var(--bad)}.cc-n-warn{border-color:var(--warn)}
.cc-acct h2,.cc-acct h3{color:var(--fg)!important;font-size:16px!important;font-weight:600}
.cc-tabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin-bottom:18px}
.cc-tab{background:none;border:0;border-bottom:2px solid transparent;color:var(--fg2);padding:8px 12px;cursor:pointer;font:inherit;margin-bottom:-1px}
.cc-tab:hover{color:var(--fg)}.cc-tab.cc-on{color:var(--fg);border-bottom-color:var(--acc)}
.cc-stack{display:flex;flex-direction:column;gap:16px}
.cc-card{border:1px solid var(--line);background:var(--card);border-radius:10px;padding:14px 16px}
.cc-card-h{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px}
.cc-card-h h3{margin:0;font-size:13px;font-weight:600}
.cc-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(170px,100%),1fr));gap:12px}
.cc-kpi{border:1px solid var(--line);background:var(--card);border-radius:10px;padding:12px 14px}
.cc-kpi-l{color:var(--fg2);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.cc-kpi-v{font-size:24px;font-weight:650;letter-spacing:-.02em;margin-top:4px;font-variant-numeric:tabular-nums}
.cc-kpi-s{color:var(--fg3);font-size:12px}
.cc-fleet{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(230px,100%),1fr));gap:10px}
.cc-prov{display:flex;gap:12px;align-items:center;border:1px solid var(--line);border-radius:9px;padding:10px 12px}
.cc-prov.cc-current{border-color:color-mix(in srgb,var(--acc) 55%,transparent)}
.cc-prov-b{min-width:0;flex:1}.cc-prov-n{font-weight:600;display:flex;gap:6px;align-items:center}
.cc-prov-m,.cc-prov-q{color:var(--fg3);font-size:12px}
.cc-dots{display:flex;gap:4px;margin:4px 0}
.cc-dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:none;background:var(--fg3)}
.cc-dot.cc-ready{background:var(--ok)}.cc-dot.cc-limited{background:var(--warn)}.cc-dot.cc-unavailable{background:var(--bad)}
.cc-ring-bg{fill:none;stroke:var(--line);stroke-width:5}.cc-ring-fg{fill:none;stroke-width:5;stroke-linecap:round}
.cc-st-ok{stroke:var(--ok)}.cc-st-warn{stroke:var(--warn)}.cc-st-bad{stroke:var(--bad)}
.cc-ring-t{fill:var(--fg2);font-size:11px;font-weight:600;dominant-baseline:middle}
.cc-pill{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:0 7px;font-size:11px;color:var(--fg2);white-space:nowrap;margin-right:4px}
.cc-pill.cc-accent{color:var(--acc);border-color:color-mix(in srgb,var(--acc) 45%,transparent)}
.cc-pill.cc-warn{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 45%,transparent)}
.cc-pill.cc-bad{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 45%,transparent)}
.cc-acc{border-top:1px solid var(--line);padding:10px 0 4px}
.cc-acc-h{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.cc-grow{flex:1}
.cc-bars{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(220px,100%),1fr));gap:8px 18px;margin:8px 0 4px}
.cc-bar-top{display:flex;justify-content:space-between;font-size:12px;color:var(--fg2)}.cc-bar-top em{color:var(--fg3);font-style:normal}
.cc-bar-track{height:6px;border-radius:3px;background:var(--line);overflow:hidden;margin-top:3px}
.cc-bar-fill{height:100%;border-radius:3px;transition:width .4s}
.cc-bg-ok{background:var(--ok)}.cc-bg-warn{background:var(--warn)}.cc-bg-bad{background:var(--bad)}.cc-bg-unknown{background:var(--fg3)}
.cc-chips{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
.cc-err{color:var(--bad);font-size:12px;margin-top:6px}
.cc-models{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px}.cc-models code{font-size:11px;border:1px solid var(--line);border-radius:5px;padding:1px 5px;color:var(--fg2)}
.cc-table{width:100%;border-collapse:collapse}.cc-table th{text-align:left;color:var(--fg3);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.05em;padding:4px 8px;border-bottom:1px solid var(--line)}
.cc-table td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:middle}.cc-table tr:last-child td{border-bottom:0}
.cc-title{max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cc-mono{font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.cc-muted{color:var(--fg2)}.cc-small{font-size:12px}.cc-pad{padding:4px 0}
.cc-t-ok{color:var(--ok)}.cc-t-warn{color:var(--warn)}
.cc-link{background:none;border:0;color:var(--acc);cursor:pointer;font:inherit;padding:0}
.cc-note{border:1px dashed var(--line);border-radius:9px;padding:9px 12px;color:var(--fg2);font-size:12px}
.cc-empty{border:1px solid var(--line);border-radius:10px;padding:22px;text-align:center}.cc-empty p{color:var(--fg2);margin:6px 0 0}
.cc-empty.cc-bad{border-color:color-mix(in srgb,var(--bad) 45%,transparent)}.cc-empty.cc-warn{border-color:color-mix(in srgb,var(--warn) 45%,transparent)}
.cc-spark{width:100%;display:block}.cc-spark rect{fill:color-mix(in srgb,var(--ok) 70%,transparent)}
.cc-sec{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(280px,100%),1fr));gap:12px}
.cc-sec-i{display:flex;gap:10px}.cc-sec-i p{margin:2px 0 0;color:var(--fg2);font-size:12px}.cc-check{color:var(--ok);font-weight:700}
.cc-skel{display:flex;flex-direction:column;gap:10px}.cc-skel div{height:64px;border-radius:10px;background:var(--card);border:1px solid var(--line);animation:cc-pulse 1.4s ease-in-out infinite}
@keyframes cc-pulse{50%{opacity:.5}}
.cc-private .cc-sensitive{filter:blur(5px);user-select:none}
`
