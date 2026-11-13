// Tests for the chat bookkeeping only; no network. Run: node --test test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { applyChats, applyMessages, applyContacts } from './wa.mjs'

const fresh = () => ({ chats: {}, names: {} })
const msg = (jid, text, ts, extra = {}) => ({ key: { remoteJid: jid, fromMe: false, ...extra.key }, message: { conversation: text }, messageTimestamp: ts, pushName: extra.pushName })

test('history sets unread and names, live messages add to it', () => {
  const db = fresh()
  applyChats(db, [{ id: 'a@s.whatsapp.net', unreadCount: 2, conversationTimestamp: 100, name: 'Ana' }])
  applyMessages(db, [msg('a@s.whatsapp.net', 'old', 90)], false)
  assert.equal(db.chats['a@s.whatsapp.net'].unread, 2)
  applyMessages(db, [msg('a@s.whatsapp.net', 'are you free?', 200)], true)
  assert.deepEqual([db.chats['a@s.whatsapp.net'].unread, db.chats['a@s.whatsapp.net'].last], [3, 'are you free?'])
  assert.equal(db.names['a@s.whatsapp.net'], 'Ana')
})

test('reading on the phone or replying clears unread', () => {
  const db = fresh()
  applyMessages(db, [msg('a@s.whatsapp.net', 'hi', 1)], true)
  applyChats(db, [{ id: 'a@s.whatsapp.net', unreadCount: 0 }])
  assert.equal(db.chats['a@s.whatsapp.net'].unread, 0)
  applyMessages(db, [msg('b@s.whatsapp.net', 'hi', 1)], true)
  applyMessages(db, [msg('b@s.whatsapp.net', 'ok', 2, { key: { fromMe: true } })], true)
  assert.equal(db.chats['b@s.whatsapp.net'].unread, 0)
})

test('status and newsletters are ignored, groups keep the sender', () => {
  const db = fresh()
  applyMessages(db, [msg('status@broadcast', 'x', 1), msg('1@newsletter', 'x', 1)], true)
  assert.deepEqual(db.chats, {})
  applyMessages(db, [msg('g@g.us', 'meeting at 5', 3, { pushName: 'Ben' })], true)
  assert.deepEqual([db.chats['g@g.us'].group, db.chats['g@g.us'].from], [true, 'Ben'])
  applyContacts(db, [{ id: 'c@s.whatsapp.net', notify: 'Cleo' }])
  assert.equal(db.names['c@s.whatsapp.net'], 'Cleo')
})
