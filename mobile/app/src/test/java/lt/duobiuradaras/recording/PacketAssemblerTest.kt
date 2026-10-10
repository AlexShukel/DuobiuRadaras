package lt.duobiuradaras.recording

import java.time.Duration
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PacketAssemblerTest {

    private val start = Instant.parse("2026-10-09T17:44:00.123Z")

    private fun sample(i: Int) = Sample(x = i.toFloat(), y = 0f, z = 9.8f, latitude = 54.6872, longitude = 25.2797)

    private fun timeOf(i: Int): Instant = start.plus(Duration.ofMillis(50L * i))

    @Test
    fun emitsPacketEvery200Samples() {
        val assembler = PacketAssembler()
        val packets = mutableListOf<Packet>()
        for (i in 0 until 3 * SAMPLES_PER_PACKET + 17) {
            assembler.add(sample(i), timeOf(i))?.let(packets::add)
        }

        assertEquals(3, packets.size)
        packets.forEach { assertEquals(SAMPLES_PER_PACKET, it.samples.size) }
        assertEquals(17, assembler.pendingSamples)
    }

    @Test
    fun packetKeepsSamplesInOrderAndStartsAtFirstSampleTime() {
        val assembler = PacketAssembler()
        val packets = mutableListOf<Packet>()
        for (i in 0 until 2 * SAMPLES_PER_PACKET) {
            assembler.add(sample(i), timeOf(i))?.let(packets::add)
        }

        assertEquals("2026-10-09T17:44:00.123Z", packets[0].startedAt)
        assertEquals((0 until 200).map { it.toFloat() }, packets[0].samples.map { it.x })
        // SPEC.md 4.2: the next packet starts at 17:44:10.123Z.
        assertEquals("2026-10-09T17:44:10.123Z", packets[1].startedAt)
        assertEquals((200 until 400).map { it.toFloat() }, packets[1].samples.map { it.x })
    }

    @Test
    fun emittedPacketIsNotMutatedByLaterSamples() {
        val assembler = PacketAssembler(samplesPerPacket = 2)
        assembler.add(sample(0), timeOf(0))
        val packet = assembler.add(sample(1), timeOf(1))!!
        assembler.add(sample(2), timeOf(2))

        assertEquals(listOf(0f, 1f), packet.samples.map { it.x })
    }

    @Test
    fun resetDiscardsPartialPacket() {
        val assembler = PacketAssembler()
        for (i in 0 until 150) {
            assertNull(assembler.add(sample(i), timeOf(i)))
        }
        assembler.reset()
        assertEquals(0, assembler.pendingSamples)

        val restart = 1000
        var packet: Packet? = null
        for (i in restart until restart + SAMPLES_PER_PACKET) {
            val result = assembler.add(sample(i), timeOf(i))
            if (i < restart + SAMPLES_PER_PACKET - 1) assertNull(result) else packet = result
        }

        val emitted = packet!!
        assertEquals(formatStartedAt(timeOf(restart)), emitted.startedAt)
        assertEquals(restart.toFloat(), emitted.samples.first().x)
        assertEquals(SAMPLES_PER_PACKET, emitted.samples.size)
    }
}
