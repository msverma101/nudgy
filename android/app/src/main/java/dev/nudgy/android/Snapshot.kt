package dev.nudgy.android

import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract
import org.json.JSONArray
import org.json.JSONObject
import java.time.OffsetDateTime

/** One thing to show: the same fields the laptop writes into nudgy-now.json. */
data class Entry(val title: String, val whenText: String, val source: String, val url: String, val id: String)

/** What needs you, as the laptop last saw it. */
data class Snapshot(
    val pressing: Int,
    val alarm: Boolean,
    val now: List<Entry>,
    val soon: List<Entry>,
    val updated: OffsetDateTime?,
) {
    /** True when the laptop hasn't written for a while (asleep, off, or the sync stopped). */
    fun stale(): Boolean = updated == null || updated.plusHours(STALE_HOURS).isBefore(OffsetDateTime.now())

    companion object {
        const val FILE = "nudgy-now.json"
        const val STALE_HOURS = 3L
        val EMPTY = Snapshot(0, false, emptyList(), emptyList(), null)

        fun parse(text: String): Snapshot {
            val json = JSONObject(text)
            fun list(name: String): List<Entry> {
                val array = json.optJSONArray(name) ?: JSONArray()
                return (0 until array.length()).mapNotNull { index ->
                    val item = array.optJSONObject(index) ?: return@mapNotNull null
                    val url = item.optString("url")
                    Entry(item.optString("title").take(140), item.optString("when"), item.optString("source"),
                        if (url.startsWith("https://") || url.startsWith("http://")) url else "",
                        item.optString("id", item.optString("title")))
                }
            }
            val updated = runCatching { OffsetDateTime.parse(json.optString("updated")) }.getOrNull()
            return Snapshot(json.optInt("pressing"), json.optBoolean("alarm"), list("now"), list("soon"), updated)
        }

        /** Reads nudgy-now.json from the folder you picked. Null when it can't be read. */
        fun read(context: Context, tree: Uri): Snapshot? = runCatching {
            val file = find(context, tree) ?: return null
            context.contentResolver.openInputStream(file)?.use { parse(it.readBytes().decodeToString()) }
        }.getOrNull()

        private fun find(context: Context, tree: Uri): Uri? {
            val children = DocumentsContract.buildChildDocumentsUriUsingTree(
                tree, DocumentsContract.getTreeDocumentId(tree))
            val columns = arrayOf(DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                DocumentsContract.Document.COLUMN_DISPLAY_NAME)
            context.contentResolver.query(children, columns, null, null, null)?.use { cursor ->
                while (cursor.moveToNext()) {
                    if (cursor.getString(1) == FILE) {
                        return DocumentsContract.buildDocumentUriUsingTree(tree, cursor.getString(0))
                    }
                }
            }
            return null
        }
    }
}

/** Your choices, kept on the phone. */
class Prefs(context: Context) {
    private val prefs = context.getSharedPreferences("nudgy", Context.MODE_PRIVATE)

    var folder: Uri?
        get() = prefs.getString("folder", null)?.let(Uri::parse)
        set(value) = prefs.edit().putString("folder", value?.toString()).apply()

    var running: Boolean
        get() = prefs.getBoolean("running", false)
        set(value) = prefs.edit().putBoolean("running", value).apply()

    var x: Int
        get() = prefs.getInt("x", -1)
        set(value) = prefs.edit().putInt("x", value).apply()

    var y: Int
        get() = prefs.getInt("y", 300)
        set(value) = prefs.edit().putInt("y", value).apply()

    var lastReminder: Long
        get() = prefs.getLong("last_reminder", 0L)
        set(value) = prefs.edit().putLong("last_reminder", value).apply()

    /** Urgent items the pet has already told you about. */
    var told: Set<String>
        get() = prefs.getStringSet("told", emptySet()) ?: emptySet()
        set(value) = prefs.edit().putStringSet("told", value.toSet()).apply()
}
