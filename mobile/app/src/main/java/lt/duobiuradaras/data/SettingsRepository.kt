package lt.duobiuradaras.data

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.emptyPreferences
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import java.io.IOException

private val Context.settingsDataStore: DataStore<Preferences> by preferencesDataStore(name = "settings")

/** Persistent user settings (SPEC.md 6.1). */
class SettingsRepository(private val dataStore: DataStore<Preferences>) {

    constructor(context: Context) : this(context.applicationContext.settingsDataStore)

    val serverUrl: Flow<String> = dataStore.data
        .catch { e -> if (e is IOException) emit(emptyPreferences()) else throw e }
        .map { prefs -> prefs[KEY_SERVER_URL] ?: DEFAULT_SERVER_URL }
        .distinctUntilChanged()

    suspend fun setServerUrl(url: String) {
        dataStore.edit { prefs -> prefs[KEY_SERVER_URL] = url }
    }

    companion object {
        /** Backend server; raw data and labels go to routes under it (SPEC.md 5). */
        const val DEFAULT_SERVER_URL = "http://100.72.8.35:3000"

        private val KEY_SERVER_URL = stringPreferencesKey("server_url")
    }
}
