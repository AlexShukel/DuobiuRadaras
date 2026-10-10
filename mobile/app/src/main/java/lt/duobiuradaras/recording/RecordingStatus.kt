package lt.duobiuradaras.recording

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

/** Latest location fix, in WGS 84 degrees. */
data class GeoPoint(val latitude: Double, val longitude: Double)

/** Latest raw accelerometer reading, in m/s², device axes. */
data class Acceleration(val x: Float, val y: Float, val z: Float)

data class RecordingState(
    val isRecording: Boolean = false,
    /** Packets that got a 2xx response since recording was switched on. */
    val sentPackets: Int = 0,
    /** Null until the first fix after recording was switched on. */
    val location: GeoPoint? = null,
    val acceleration: Acceleration? = null,
)

/**
 * Process-wide recording state. Written only by [RecordingService],
 * observed by the UI and the service notification.
 */
object RecordingStatus {
    private val _state = MutableStateFlow(RecordingState())
    val state: StateFlow<RecordingState> = _state.asStateFlow()

    internal fun update(transform: (RecordingState) -> RecordingState) = _state.update(transform)
}
