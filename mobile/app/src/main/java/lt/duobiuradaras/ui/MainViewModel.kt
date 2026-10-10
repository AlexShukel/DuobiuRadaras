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
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.debounce
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import lt.duobiuradaras.data.SettingsRepository
import lt.duobiuradaras.data.isValidServerUrl
import lt.duobiuradaras.data.labelUrlFor
import lt.duobiuradaras.data.rawDataUrlFor
import lt.duobiuradaras.recording.Label
import lt.duobiuradaras.recording.LabelSender
import lt.duobiuradaras.recording.RecordingService
import lt.duobiuradaras.recording.RecordingState
import lt.duobiuradaras.recording.RecordingStatus
import lt.duobiuradaras.recording.RoadLabel

data class MainUiState(
    /** False until the saved server URL has been loaded into the text field. */
    val isLoaded: Boolean = false,
    val isUrlValid: Boolean = false,
    val recording: RecordingState = RecordingState(),
) {
    val isUrlEditable: Boolean get() = isLoaded && !recording.isRecording
    val showUrlError: Boolean get() = isLoaded && !isUrlValid
    val canStart: Boolean get() = isLoaded && isUrlValid

    /** Labels need a GPS fix, which only exists while recording. */
    val canLabel: Boolean get() = recording.isRecording && recording.location != null
}

/** Outcome of one label button press, shown once as a snackbar. */
data class LabelResult(val label: RoadLabel, val success: Boolean)

class MainViewModel(
    application: Application,
    private val settings: SettingsRepository,
) : AndroidViewModel(application) {

    /**
     * Server URL being edited. Owned here (not as a flow) so the text field
     * is updated synchronously and the cursor never jumps.
     */
    val serverUrlField = TextFieldState()

    // Snapshot state, like the field, so both are observed in one consistent snapshot.
    private var isLoaded by mutableStateOf(false)

    private val labelResults = Channel<LabelResult>(Channel.BUFFERED)
    val labelResultEvents: Flow<LabelResult> = labelResults.receiveAsFlow()

    val uiState: StateFlow<MainUiState> = combine(
        snapshotFlow { isLoaded to isValidServerUrl(serverUrlField.text.toString()) },
        RecordingStatus.state,
    ) { (loaded, urlValid), recording ->
        MainUiState(isLoaded = loaded, isUrlValid = urlValid, recording = recording)
    }.stateIn(
        scope = viewModelScope,
        started = SharingStarted.WhileSubscribed(5_000),
        initialValue = MainUiState(recording = RecordingStatus.state.value),
    )

    init {
        viewModelScope.launch {
            serverUrlField.setTextAndPlaceCursorAtEnd(settings.serverUrl.first())
            isLoaded = true
            persistValidEdits()
        }
    }

    @OptIn(FlowPreview::class)
    private suspend fun persistValidEdits() {
        snapshotFlow { serverUrlField.text.toString() }
            .drop(1) // the value just loaded from settings
            .debounce(500)
            .map { it.trim() }
            .filter(::isValidServerUrl)
            .collect { settings.setServerUrl(it) }
    }

    /**
     * Saves the server URL and starts recording to its raw data route.
     * Caller must already hold location permission.
     */
    fun startRecording() {
        val url = serverUrlField.text.toString().trim()
        if (!isLoaded || !isValidServerUrl(url)) return
        val rawDataUrl = rawDataUrlFor(url) ?: return
        if (url != serverUrlField.text.toString()) {
            serverUrlField.setTextAndPlaceCursorAtEnd(url)
        }
        viewModelScope.launch { settings.setServerUrl(url) }
        RecordingService.start(getApplication<Application>(), rawDataUrl.toString())
    }

    fun stopRecording() {
        RecordingService.stop(getApplication<Application>())
    }

    /**
     * Sends [label] at the current time and GPS fix to the server's label route.
     * Does nothing without a fix. The URL field is locked while recording, so it's the one in use.
     */
    fun sendLabel(label: RoadLabel) {
        val timestamp = System.currentTimeMillis()
        val location = RecordingStatus.state.value.location ?: return
        val url = labelUrlFor(serverUrlField.text.toString()) ?: return
        viewModelScope.launch {
            val success = LabelSender(url).send(
                Label(timestamp, location.latitude, location.longitude, label),
            )
            labelResults.send(LabelResult(label, success))
        }
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
