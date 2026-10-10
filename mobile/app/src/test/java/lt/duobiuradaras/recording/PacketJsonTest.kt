package lt.duobiuradaras.recording

import java.time.Instant
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.double
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PacketJsonTest {

    @Test
    fun startedAtHasMillisecondsAndZ() {
        assertEquals("2026-10-09T17:44:00.123Z", formatStartedAt(Instant.parse("2026-10-09T17:44:00.123Z")))
    }

    @Test
    fun startedAtKeepsZeroMillis() {
        assertEquals("2026-10-09T17:44:00.000Z", formatStartedAt(Instant.parse("2026-10-09T17:44:00Z")))
        assertEquals("2026-01-02T03:04:05.070Z", formatStartedAt(Instant.parse("2026-01-02T03:04:05.070Z")))
    }

    @Test
    fun startedAtTruncatesSubMillisecondPrecision() {
        assertEquals("2026-10-09T17:44:00.123Z", formatStartedAt(Instant.parse("2026-10-09T17:44:00.123999999Z")))
    }

    @Test
    fun jsonShapeMatchesSpec() {
        val packet = Packet(
            startedAt = "2026-10-09T17:44:00.123Z",
            samples = listOf(
                Sample(x = 0.1f, y = 0.2f, z = 9.8f, latitude = 54.6872, longitude = 25.2797),
                Sample(x = 0.2f, y = 0.1f, z = 9.9f, latitude = 54.6872, longitude = 25.2797),
            ),
        )

        val root = Json.parseToJsonElement(Json.encodeToString(Packet.serializer(), packet)).jsonObject

        assertEquals(setOf("started_at", "samples"), root.keys)
        assertEquals(JsonPrimitive("2026-10-09T17:44:00.123Z"), root["started_at"])

        val samples = root.getValue("samples").jsonArray
        assertEquals(2, samples.size)
        val first = samples[0].jsonObject
        assertEquals(setOf("x", "y", "z", "latitude", "longitude"), first.keys)
        first.values.forEach { assertFalse("numbers, not strings", it.jsonPrimitive.isString) }
        assertEquals(0.1, first.getValue("x").jsonPrimitive.double, 1e-6)
        assertEquals(9.8, first.getValue("z").jsonPrimitive.double, 1e-6)
        assertEquals(54.6872, first.getValue("latitude").jsonPrimitive.double, 0.0)
        assertEquals(25.2797, first.getValue("longitude").jsonPrimitive.double, 0.0)
        assertTrue(samples[1].jsonObject.keys.containsAll(first.keys))
    }
}
