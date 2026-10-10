package lt.duobiuradaras.recording

import java.time.Instant
import kotlin.time.Duration.Companion.milliseconds
import kotlin.time.Duration.Companion.nanoseconds
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.filterNotNull
import kotlinx.coroutines.flow.first

/** Sampling period (SPEC.md 3). */
val SAMPLE_INTERVAL = 50.milliseconds

/**
 * Takes one sample per 50 ms tick from the latest [acceleration] and [location]
 * and feeds them into packets (SPEC.md 3.1, 3.2).
 */
class Sampler(
    private val acceleration: StateFlow<Acceleration?>,
    private val location: StateFlow<GeoPoint?>,
    private val wallClock: () -> Instant = Instant::now,
    private val monotonicNanos: () -> Long = System::nanoTime,
) {

    /**
     * Waits for the first location fix (and accelerometer reading), then samples until cancelled,
     * calling [onPacket] for each full packet. Cancelling discards the partial packet.
     */
    suspend fun run(onPacket: (Packet) -> Unit) {
        location.filterNotNull().first()
        acceleration.filterNotNull().first()

        val assembler = PacketAssembler()
        val intervalNanos = SAMPLE_INTERVAL.inWholeNanoseconds
        val maxLatenessNanos = MAX_LATENESS.inWholeNanoseconds
        try {
            // Ticks are origin + n * interval, so delays don't accumulate drift.
            var origin = monotonicNanos()
            var tick = 0L
            while (true) {
                val target = origin + tick * intervalNanos
                val now = monotonicNanos()
                if (now < target) {
                    delay((target - now).nanoseconds)
                } else if (now - target > maxLatenessNanos) {
                    // Stalled for a long time (e.g. the CPU slept): catching up would emit a burst
                    // of identical samples, so drop the partial packet and restart the grid.
                    assembler.reset()
                    origin = now
                    tick = 0L
                }

                val accel = checkNotNull(acceleration.value)
                val fix = checkNotNull(location.value)
                val sample = Sample(
                    x = accel.x,
                    y = accel.y,
                    z = accel.z,
                    latitude = fix.latitude,
                    longitude = fix.longitude,
                )
                assembler.add(sample, wallClock())?.let(onPacket)
                tick++
            }
        } finally {
            assembler.reset()
        }
    }

    private companion object {
        val MAX_LATENESS = 1000.milliseconds
    }
}
