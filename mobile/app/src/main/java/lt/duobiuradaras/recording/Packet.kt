package lt.duobiuradaras.recording

import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** One 50 ms sample (SPEC.md 4.1). */
@Serializable
data class Sample(
    val x: Float,
    val y: Float,
    val z: Float,
    val latitude: Double,
    val longitude: Double,
)

/** One upload: [SAMPLES_PER_PACKET] samples, 50 ms apart, starting at [startedAt] (SPEC.md 4). */
@Serializable
data class Packet(
    @SerialName("started_at") val startedAt: String,
    val samples: List<Sample>,
)

// Instant.toString() drops the fraction when millis are zero, so use a fixed pattern.
private val STARTED_AT_FORMATTER: DateTimeFormatter =
    DateTimeFormatter.ofPattern("uuuu-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.ROOT)
        .withZone(ZoneOffset.UTC)

/** Formats [instant] as ISO 8601 UTC with exactly three fraction digits, e.g. `2026-10-09T17:44:00.123Z`. */
internal fun formatStartedAt(instant: Instant): String = STARTED_AT_FORMATTER.format(instant)
