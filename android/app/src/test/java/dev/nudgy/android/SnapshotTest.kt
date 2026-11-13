package dev.nudgy.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SnapshotTest {
    private val file = """
        {"version": 1, "pressing": 2, "alarm": true, "updated": "2026-11-13T09:00:00+01:00",
         "now": [{"id": "note:1", "title": "Pay rent", "when": "Today", "source": "note", "url": ""},
                 {"id": "note:2", "title": "Pay rent", "when": "", "source": "note", "url": "javascript:alert(1)"}],
         "soon": [{"id": "task:1", "title": "Book flat viewing", "when": "", "source": "task",
                   "url": "https://tasks.google.com/x"}]}
    """.trimIndent()

    @Test fun readsItemsWithTheirIds() {
        val snapshot = Snapshot.parse(file)
        assertEquals(2, snapshot.pressing)
        assertTrue(snapshot.alarm)
        assertEquals(listOf("note:1", "note:2"), snapshot.now.map { it.id })       // same title, different ids
        assertEquals("https://tasks.google.com/x", snapshot.soon[0].url)
        assertFalse(snapshot.newer)
    }

    @Test fun onlyWebLinksAreKept() {
        assertEquals("", Snapshot.parse(file).now[1].url)
    }

    @Test fun newerFileIsFlagged() {
        assertTrue(Snapshot.parse("""{"version": 2, "now": []}""").newer)
    }

    @Test fun oldFileWithoutIdsFallsBackToTitles() {
        val snapshot = Snapshot.parse("""{"now": [{"title": "Call mum"}]}""")
        assertEquals("Call mum", snapshot.now[0].id)
        assertTrue(snapshot.stale())                                              // no "updated" time
    }
}
