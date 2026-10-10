package lt.duobiuradaras.ui

import lt.duobiuradaras.recording.RecordingMode.CALIBRATION
import lt.duobiuradaras.recording.RecordingMode.NORMAL
import lt.duobiuradaras.recording.RecordingState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class MainUiStateTest {

    private val bothValid = setOf(NORMAL, CALIBRATION)

    @Test
    fun `both buttons start when not recording`() {
        val state = MainUiState(isLoaded = true, validUrls = bothValid)
        assertEquals(ModeAction.START, state.action(NORMAL))
        assertEquals(ModeAction.START, state.action(CALIBRATION))
        assertTrue(state.areUrlsEditable)
    }

    @Test
    fun `active mode stops and the other switches while recording`() {
        val state = MainUiState(
            isLoaded = true,
            validUrls = bothValid,
            recording = RecordingState(isRecording = true, mode = CALIBRATION),
        )
        assertEquals(ModeAction.SWITCH, state.action(NORMAL))
        assertEquals(ModeAction.STOP, state.action(CALIBRATION))
        assertFalse(state.areUrlsEditable)
    }

    @Test
    fun `invalid url disables start and switch but never stop`() {
        val stopped = MainUiState(isLoaded = true, validUrls = setOf(NORMAL))
        assertTrue(stopped.isButtonEnabled(NORMAL))
        assertFalse(stopped.isButtonEnabled(CALIBRATION))
        assertTrue(stopped.showUrlError(CALIBRATION))

        val recording = stopped.copy(recording = RecordingState(isRecording = true, mode = NORMAL))
        assertFalse(recording.isButtonEnabled(CALIBRATION))
        assertTrue(recording.isButtonEnabled(NORMAL))

        val activeInvalid = MainUiState(
            isLoaded = true,
            validUrls = emptySet(),
            recording = RecordingState(isRecording = true, mode = CALIBRATION),
        )
        assertTrue(activeInvalid.isButtonEnabled(CALIBRATION))
    }

    @Test
    fun `nothing is enabled before settings load`() {
        val state = MainUiState(isLoaded = false, validUrls = bothValid)
        assertFalse(state.isButtonEnabled(NORMAL))
        assertFalse(state.areUrlsEditable)
    }
}
