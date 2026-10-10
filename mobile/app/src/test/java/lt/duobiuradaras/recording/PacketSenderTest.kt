package lt.duobiuradaras.recording

import java.util.concurrent.TimeUnit
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class PacketSenderTest {

    private lateinit var server: MockWebServer

    private val client = OkHttpClient.Builder()
        .connectTimeout(2, TimeUnit.SECONDS)
        .readTimeout(2, TimeUnit.SECONDS)
        .build()

    private val packet = Packet(
        startedAt = "2026-10-09T17:44:00.123Z",
        samples = List(SAMPLES_PER_PACKET) {
            Sample(x = 0.1f, y = 0.2f, z = 9.8f, latitude = 54.6872, longitude = 25.2797)
        },
    )

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    private fun sender() = PacketSender(server.url("/packets"), client)

    @Test
    fun postsPacketAsJson() = runBlocking<Unit> {
        server.enqueue(MockResponse().setResponseCode(200))

        assertTrue(sender().send(packet))

        val request = server.takeRequest(2, TimeUnit.SECONDS)!!
        assertEquals("POST", request.method)
        assertEquals("/packets", request.path)
        assertEquals("application/json", request.getHeader("Content-Type"))
        val body = Json.decodeFromString(Packet.serializer(), request.body.readUtf8())
        assertEquals(packet, body)
    }

    @Test
    fun countsOnly2xxAsSuccess() = runBlocking<Unit> {
        val codes = listOf(200, 201, 204, 299, 400, 404, 500, 503)
        codes.forEach { server.enqueue(MockResponse().setResponseCode(it)) }

        val sender = sender()
        val sent = codes.count { sender.send(packet) }

        assertEquals(4, sent)
        assertEquals(codes.size, server.requestCount)
    }

    @Test
    fun keepsGoingAfterFailure() = runBlocking<Unit> {
        server.enqueue(MockResponse().setResponseCode(500))
        server.enqueue(MockResponse().setResponseCode(200))

        val sender = sender()
        assertFalse(sender.send(packet))
        assertTrue(sender.send(packet))
        // No retry: one request per send.
        assertEquals(2, server.requestCount)
    }

    @Test
    fun networkErrorIsFailure() = runBlocking<Unit> {
        // Grab a port nothing listens on any more.
        val closed = MockWebServer().apply { start() }
        val url = closed.url("/packets")
        closed.shutdown()

        assertFalse(PacketSender(url, client).send(packet))
    }
}
