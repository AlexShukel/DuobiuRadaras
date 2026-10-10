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
import lt.duobiuradaras.recording.RecordingService
import lt.duobiuradaras.recording.RecordingState
import lt.duobiuradaras.recording.RecordingStatus

data class MainUiState(
    /** False until the saved endpoint URL has been loaded into the text field. */
    val isLoaded: Boolean = false,
    val isUrlValid: Boolean = false,
    val recording: RecordingState = RecordingState(),
) {
    val isUrlEditable: Boolean get() = isLoaded && !recording.isRecording
    val showUrlError: Boolean get() = isLoaded && !isUrlValid
    val canStart: Boolean get() = isLoaded && isUrlValid
}

class MainViewModel(
    application: Application,
    private val settings: SettingsRepository,
) : AndroidViewModel(application) {

    /**
     * Endpoint URL being edited. Owned here (not as a flow) so the text field
     * is updated synchronously and the cursor never jumps.
     */
    val endpointUrlField = TextFieldState()

    // Snapshot state, like the field, so both are observed in one consistent snapshot.
    private var isLoaded by mutableStateOf(false)
    private val endpointText = snapshotFlow { endpointUrlField.text.toString() }

    val uiState: StateFlow<MainUiState> = combine(
        snapshotFlow { isLoaded to isValidEndpointUrl(endpointUrlField.text.toString()) },
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
            endpointUrlField.setTextAndPlaceCursorAtEnd(settings.endpointUrl.first())
            isLoaded = true
            persistValidEdits()
        }
    }

    @OptIn(FlowPreview::class)
    private suspend fun persistValidEdits() {
        endpointText
            .drop(1) // the value just loaded from settings
            .debounce(500)
            .map { it.trim() }
            .filter(::isValidEndpointUrl)
            .collect { settings.setEndpointUrl(it) }
    }

    /** Saves the URL and starts recording. Caller must already hold location permission. */
    fun startRecording() {
        val url = endpointUrlField.text.toString().trim()
        if (!isLoaded || !isValidEndpointUrl(url)) return
        if (url != endpointUrlField.text.toString()) {
            endpointUrlField.setTextAndPlaceCursorAtEnd(url)
        }
        viewModelScope.launch { settings.setEndpointUrl(url) }
        RecordingService.start(getApplication<Application>(), url)
    }

    fun stopRecording() {
        RecordingService.stop(getApplication<Application>())
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
