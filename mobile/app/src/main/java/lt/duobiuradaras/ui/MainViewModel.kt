package lt.duobiuradaras.ui

import android.app.Application
import androidx.compose.foundation.text.input.TextFieldState
import androidx.compose.foundation.text.input.setTextAndPlaceCursorAtEnd
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.ViewModelProvider.AndroidViewModelFactory.Companion.APPLICATION_KEY
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import kotlinx.coroutines.FlowPreview
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.debounce
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import lt.duobiuradaras.data.SettingsRepository
import lt.duobiuradaras.data.isValidEndpointUrl
import lt.duobiuradaras.recording.RecordingMode
import lt.duobiuradaras.recording.RecordingService
import lt.duobiuradaras.recording.RecordingState
import lt.duobiuradaras.recording.RecordingStatus

/** What a mode's button does when pressed (SPEC.md 6.2). */
enum class ModeAction { START, SWITCH, STOP }

data class MainUiState(
    /** False until the saved endpoint URLs have been loaded into the text fields. */
    val isLoaded: Boolean = false,
    val validUrls: Set<RecordingMode> = emptySet(),
    val recording: RecordingState = RecordingState(),
) {
    /** Both fields are locked while recording, since either may be switched to (SPEC.md 6.1). */
    val areUrlsEditable: Boolean get() = isLoaded && !recording.isRecording

    fun showUrlError(mode: RecordingMode): Boolean = isLoaded && mode !in validUrls

    fun action(mode: RecordingMode): ModeAction = when {
        !recording.isRecording -> ModeAction.START
        recording.mode == mode -> ModeAction.STOP
        else -> ModeAction.SWITCH
    }

    /** Stop is always allowed; starting or switching needs a valid URL for that mode. */
    fun isButtonEnabled(mode: RecordingMode): Boolean =
        action(mode) == ModeAction.STOP || (isLoaded && mode in validUrls)
}

class MainViewModel(
    application: Application,
    private val settings: SettingsRepository,
) : AndroidViewModel(application) {

    /**
     * Endpoint URLs being edited, one per mode. Owned here (not as a flow) so the text
     * fields are updated synchronously and the cursor never jumps.
     */
    val urlFields: Map<RecordingMode, TextFieldState> =
        RecordingMode.entries.associateWith { TextFieldState() }

    // Snapshot state, like the fields, so all are observed in one consistent snapshot.
    private var isLoaded by mutableStateOf(false)

    val uiState: StateFlow<MainUiState> = combine(
        snapshotFlow {
            isLoaded to urlFields.filterValues { isValidEndpointUrl(it.text.toString()) }.keys
        },
        RecordingStatus.state,
    ) { (loaded, validUrls), recording ->
        MainUiState(isLoaded = loaded, validUrls = validUrls, recording = recording)
    }.stateIn(
        scope = viewModelScope,
        started = SharingStarted.WhileSubscribed(5_000),
        initialValue = MainUiState(recording = RecordingStatus.state.value),
    )

    init {
        viewModelScope.launch {
            for ((mode, field) in urlFields) {
                field.setTextAndPlaceCursorAtEnd(settings.endpointUrl(mode).first())
            }
            isLoaded = true
            for ((mode, field) in urlFields) {
                launch { persistValidEdits(mode, field) }
            }
        }
    }

    @OptIn(FlowPreview::class)
    private suspend fun persistValidEdits(mode: RecordingMode, field: TextFieldState) {
        snapshotFlow { field.text.toString() }
            .drop(1) // the value just loaded from settings
            .debounce(500)
            .map { it.trim() }
            .filter(::isValidEndpointUrl)
            .collect { settings.setEndpointUrl(mode, it) }
    }

    /**
     * Handles a press of [mode]'s button: starts recording to its URL, switches the running
     * recording to it, or stops if it is already the active mode. Starting requires the caller
     * to already hold location permission.
     */
    fun onModeButton(mode: RecordingMode) {
        val app = getApplication<Application>()
        when (uiState.value.action(mode)) {
            ModeAction.STOP -> RecordingService.stop(app)
            ModeAction.SWITCH -> validUrl(mode)?.let { RecordingService.switchTo(app, mode, it) }
            ModeAction.START -> validUrl(mode)?.let { url ->
                viewModelScope.launch { settings.setEndpointUrl(mode, url) }
                RecordingService.start(app, mode, url)
            }
        }
    }

    /** The trimmed URL for [mode], written back to its field, or null if it isn't valid. */
    private fun validUrl(mode: RecordingMode): String? {
        val field = urlFields.getValue(mode)
        val url = field.text.toString().trim()
        if (!isLoaded || !isValidEndpointUrl(url)) return null
        if (url != field.text.toString()) field.setTextAndPlaceCursorAtEnd(url)
        return url
    }

    companion object {
        val Factory = viewModelFactory {
            initializer {
                val application = checkNotNull(this[APPLICATION_KEY])
                MainViewModel(application, SettingsRepository(application))
            }
        }
    }
}
