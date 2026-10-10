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
import lt.duobiuradaras.recording.RecordingMode

private val Context.settingsDataStore: DataStore<Preferences> by preferencesDataStore(name = "settings")

/** Persistent user settings: one endpoint URL per [RecordingMode] (SPEC.md 6.1). */
class SettingsRepository(private val dataStore: DataStore<Preferences>) {

    constructor(context: Context) : this(context.applicationContext.settingsDataStore)

    fun endpointUrl(mode: RecordingMode): Flow<String> = dataStore.data
        .catch { e -> if (e is IOException) emit(emptyPreferences()) else throw e }
        .map { prefs -> prefs[mode.key] ?: mode.defaultUrl }
        .distinctUntilChanged()

    suspend fun setEndpointUrl(mode: RecordingMode, url: String) {
        dataStore.edit { prefs -> prefs[mode.key] = url }
    }

    companion object {
        // Test server on the host machine, as seen from the emulator.
        const val DEFAULT_ENDPOINT_URL = "http://10.0.2.2:8080/packets"
        const val DEFAULT_CALIBRATION_URL = "http://10.0.2.2:8080/calibration"

        private val KEY_ENDPOINT_URL = stringPreferencesKey("endpoint_url")
        private val KEY_CALIBRATION_URL = stringPreferencesKey("calibration_url")

        private val RecordingMode.key
            get() = when (this) {
                RecordingMode.NORMAL -> KEY_ENDPOINT_URL
                RecordingMode.CALIBRATION -> KEY_CALIBRATION_URL
            }

        private val RecordingMode.defaultUrl
            get() = when (this) {
                RecordingMode.NORMAL -> DEFAULT_ENDPOINT_URL
                RecordingMode.CALIBRATION -> DEFAULT_CALIBRATION_URL
            }
    }
}
