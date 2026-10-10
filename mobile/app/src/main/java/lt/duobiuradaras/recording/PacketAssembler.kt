package lt.duobiuradaras.recording

import java.time.Instant

/** 10 s of samples at 50 ms (SPEC.md 3). */
const val SAMPLES_PER_PACKET = 200

/**
 * Groups samples into fixed-size packets. Not thread-safe: feed it from one coroutine.
 */
class PacketAssembler(private val samplesPerPacket: Int = SAMPLES_PER_PACKET) {

    init {
        require(samplesPerPacket > 0) { "samplesPerPacket must be positive" }
    }

    private var samples = ArrayList<Sample>(samplesPerPacket)
    private var startedAt: Instant? = null

    /** Number of samples in the packet being assembled. */
    val pendingSamples: Int get() = samples.size

    /**
     * Adds [sample] taken at [takenAt]. Returns the completed packet when this sample fills it,
     * otherwise null. The packet's `started_at` is the time of its first sample.
     */
    fun add(sample: Sample, takenAt: Instant): Packet? {
        val firstAt = startedAt ?: takenAt.also { startedAt = it }
        samples.add(sample)
        if (samples.size < samplesPerPacket) return null

        val packet = Packet(startedAt = formatStartedAt(firstAt), samples = samples)
        samples = ArrayList(samplesPerPacket)
        startedAt = null
        return packet
    }

    /** Discards the partial packet; partial packets are never sent (SPEC.md 6.2). */
    fun reset() {
        samples.clear()
        startedAt = null
    }
}
