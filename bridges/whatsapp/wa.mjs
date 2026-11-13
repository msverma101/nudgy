// Read-only WhatsApp bridge for Nudgy. It never sends messages or read receipts.
//
//   node wa.mjs link          print {"qr": ...} lines until the phone scans one, then {"linked": true}
//   node wa.mjs run           stay connected and keep whatsapp-chats.json up to date
//   node wa.mjs logout        unlink this device
//   node wa.mjs qr TEXT FILE  write TEXT as a QR code PNG (Nudgy uses this for Signal too)
//
// Files live in $NUDGY_DATA (default ~/.local/share/nudgy).
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const DATA = process.env.NUDGY_DATA || path.join(os.homedir(), '.local/share/nudgy')
const AUTH = path.join(DATA, 'whatsapp-auth')
const STATE = path.join(DATA, 'whatsapp-chats.json')
const KEEP_DAYS = 14
const say = (obj) => process.stdout.write(JSON.stringify(obj) + '\n')
// Diagnostics go to stderr; Nudgy keeps them in whatsapp-bridge.log.
const log = (...parts) => process.stderr.write(`${new Date().toISOString()} ${parts.join(' ')}\n`)

async function connect ({ onQr, onOpen, onChange }) {
  const { default: makeWASocket, useMultiFileAuthState, DisconnectReason, Browsers, fetchLatestWaWebVersion } = await import('baileys')
  const { default: pino } = await import('pino')
  fs.mkdirSync(AUTH, { recursive: true, mode: 0o700 })
  const { state, saveCreds } = await useMultiFileAuthState(AUTH)
  // The library's built-in WhatsApp Web version goes stale and WhatsApp then refuses to pair,
  // so ask WhatsApp for the current one. Without network the built-in one is used.
  const latest = await fetchLatestWaWebVersion().catch(() => null)
  const version = latest?.version
  log('connecting with WhatsApp Web version', version ? version.join('.') : 'built-in')
  return new Promise((resolve, reject) => {
    const start = () => {
      const sock = makeWASocket({
        ...(version ? { version } : {}),
        auth: state,
        logger: pino({ level: 'silent' }),
        browser: Browsers.ubuntu('Nudgy'),
        markOnlineOnConnect: false,   // keep phone notifications working
        syncFullHistory: false,
        fireInitQueries: true
      })
      sock.ev.on('creds.update', saveCreds)
      if (onChange) onChange(sock)
      sock.ev.on('connection.update', (u) => {
        if (u.qr && onQr) onQr(u.qr)
        if (u.connection === 'open' && onOpen) onOpen(sock)
        if (u.connection === 'close') {
          const code = u.lastDisconnect?.error?.output?.statusCode
          log('connection closed', code, u.lastDisconnect?.error?.message || '')
          if (code === DisconnectReason.loggedOut) return reject(new Error('logged_out'))
          // 515 after pairing, network drops and server restarts: reconnect after a pause.
          setTimeout(start, code === DisconnectReason.restartRequired ? 0 : 5000)
        }
      })
      resolve(sock)
    }
    start()
  })
}

// ---- chat state -------------------------------------------------------------------------------
const num = (x) => (x && typeof x === 'object' && x.toNumber ? x.toNumber() : Number(x || 0))
const ignored = (jid) => !jid || jid === 'status@broadcast' || jid.endsWith('@newsletter') || jid.endsWith('@broadcast')

function load () {
  try { return JSON.parse(fs.readFileSync(STATE, 'utf8')) } catch { return { chats: {}, names: {} } }
}

function save (db) {
  const cutoff = Date.now() / 1000 - KEEP_DAYS * 86400
  for (const [id, c] of Object.entries(db.chats)) if ((c.ts || 0) < cutoff) delete db.chats[id]
  db.updated = Date.now() / 1000
  const tmp = STATE + '.tmp'
  fs.writeFileSync(tmp, JSON.stringify(db), { mode: 0o600 })
  fs.renameSync(tmp, STATE)
}

function text (m) {
  const msg = m.message || {}
  const inner = msg.ephemeralMessage?.message || msg.viewOnceMessage?.message || msg
  return inner.conversation || inner.extendedTextMessage?.text || inner.imageMessage?.caption ||
    inner.videoMessage?.caption || inner.documentMessage?.fileName ||
    (inner.imageMessage && '[photo]') || (inner.videoMessage && '[video]') ||
    (inner.audioMessage && '[voice message]') || (inner.stickerMessage && '[sticker]') || ''
}

export function applyChats (db, chats) {
  for (const c of chats) {
    if (ignored(c.id)) continue
    const old = db.chats[c.id] || { unread: 0, last: '', ts: 0 }
    const next = { ...old, group: c.id.endsWith('@g.us') }
    if (c.name || c.subject) db.names[c.id] = c.name || c.subject
    if (c.unreadCount !== undefined && c.unreadCount !== null) next.unread = Math.max(0, c.unreadCount)
    if (c.conversationTimestamp) next.ts = Math.max(old.ts, num(c.conversationTimestamp))
    if (c.archived !== undefined && c.archived !== null) next.archived = !!c.archived
    if (c.muteEndTime !== undefined && c.muteEndTime !== null) next.muted = num(c.muteEndTime) * (num(c.muteEndTime) > 1e12 ? 0.001 : 1)
    db.chats[c.id] = next
  }
}

export function applyMessages (db, messages, live) {
  for (const m of messages) {
    const jid = m.key?.remoteJid
    if (ignored(jid) || !m.message) continue
    const ts = num(m.messageTimestamp)
    const chat = db.chats[jid] || { unread: 0, last: '', ts: 0, group: jid.endsWith('@g.us') }
    if (m.key.fromMe) {
      chat.unread = 0           // you replied, from the phone or elsewhere
    } else {
      if (m.pushName && !chat.group) db.names[jid] = db.names[jid] || m.pushName
      if (ts >= chat.ts) {
        chat.last = text(m) || chat.last
        chat.from = chat.group ? (m.pushName || '') : ''
      }
      if (live) chat.unread += 1
    }
    chat.ts = Math.max(chat.ts, ts)
    db.chats[jid] = chat
  }
}

export function applyContacts (db, contacts) {
  for (const c of contacts) {
    const name = c.name || c.notify || c.verifiedName
    if (c.id && name) db.names[c.id] = name
  }
}

// ---- commands ---------------------------------------------------------------------------------
async function link () {
  const sock = await connect({
    onQr: (qr) => say({ qr }),
    onOpen: (s) => { say({ linked: true, name: s.user?.name || '' }); setTimeout(() => process.exit(0), 3000) }
  })
  return sock
}

async function run () {
  const db = load()
  let timer = null
  const later = () => { clearTimeout(timer); timer = setTimeout(() => save(db), 1500) }
  await connect({
    onQr: () => { say({ error: 'not_linked' }); process.exit(3) },
    onOpen: () => { say({ connected: true }); later() },
    onChange: (sock) => {
      sock.ev.on('messaging-history.set', ({ chats, contacts, messages }) => {
        applyContacts(db, contacts || []); applyChats(db, chats || []); applyMessages(db, messages || [], false); later()
      })
      sock.ev.on('chats.upsert', (c) => { applyChats(db, c); later() })
      sock.ev.on('chats.update', (c) => { applyChats(db, c); later() })
      sock.ev.on('contacts.upsert', (c) => { applyContacts(db, c); later() })
      sock.ev.on('contacts.update', (c) => { applyContacts(db, c); later() })
      sock.ev.on('messages.upsert', ({ messages, type }) => { applyMessages(db, messages, type === 'notify'); later() })
    }
  }).catch((e) => { say({ error: e.message }); process.exit(e.message === 'logged_out' ? 3 : 1) })
  setInterval(() => save(db), 60000)   // also proves to Nudgy that the bridge is alive
}

async function logout () {
  await connect({
    onQr: () => { fs.rmSync(AUTH, { recursive: true, force: true }); process.exit(0) },
    onOpen: async (s) => { await s.logout().catch(() => {}); fs.rmSync(AUTH, { recursive: true, force: true }); fs.rmSync(STATE, { force: true }); process.exit(0) }
  }).catch(() => { fs.rmSync(AUTH, { recursive: true, force: true }); process.exit(0) })
}

async function qr (value, file) {
  const { default: QRCode } = await import('qrcode')
  await QRCode.toFile(file, value, { width: 300, margin: 2 })
}

const [cmd, ...args] = process.argv.slice(2)
async function qrTerminal (value) {
  const { default: QRCode } = await import('qrcode')
  process.stderr.write(await QRCode.toString(value, { type: 'terminal', small: true }))
}

const commands = { link, run, logout, qr: () => qr(...args), 'qr-terminal': () => qrTerminal(args[0]) }
import { fileURLToPath } from 'node:url'
if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  if (!commands[cmd]) { console.error('usage: wa.mjs link | run | logout | qr TEXT FILE | qr-terminal TEXT'); process.exit(2) }
  await commands[cmd]()
}
