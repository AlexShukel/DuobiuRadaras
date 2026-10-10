package lt.duobiuradaras.recording

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Road feature the user marked by hand (SPEC.md 6.4). */
@Serializable
enum class RoadLabel {
    @SerialName("pothole") POTHOLE,
    @SerialName("bump") BUMP,
}

/** One manual label upload (SPEC.md 5.1). */
@Serializable
data class Label(
    /** Unix epoch milliseconds when the button was pressed. */
    val timestamp: Long,
    val latitude: Double,
    val longitude: Double,
    val label: RoadLabel,
)
