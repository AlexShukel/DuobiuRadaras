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

    val endpointUrl: Flow<String> = dataStore.data
        .catch { e -> if (e is IOException) emit(emptyPreferences()) else throw e }
        .map { prefs -> prefs[KEY_ENDPOINT_URL] ?: DEFAULT_ENDPOINT_URL }
        .distinctUntilChanged()

    suspend fun setEndpointUrl(url: String) {
        dataStore.edit { prefs -> prefs[KEY_ENDPOINT_URL] = url }
    }

    companion object {
        /** Backend readings route on the host machine, as seen from the emulator. */
        const val DEFAULT_ENDPOINT_URL = "http://10.0.2.2:3000/api/readings"

        private val KEY_ENDPOINT_URL = stringPreferencesKey("endpoint_url")
    }
}
