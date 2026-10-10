package lt.duobiuradaras.recording

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/** Keeps the latest raw `TYPE_ACCELEROMETER` reading (SPEC.md 3.1). */
class AccelerometerSource(context: Context) {

    private val sensorManager: SensorManager? = context.getSystemService(SensorManager::class.java)

    private val _latest = MutableStateFlow<Acceleration?>(null)

    /** Null until the first sensor event after [start]. */
    val latest: StateFlow<Acceleration?> = _latest.asStateFlow()

    private val listener = object : SensorEventListener {
        override fun onSensorChanged(event: SensorEvent) {
            val values = event.values
            _latest.value = Acceleration(values[0], values[1], values[2])
        }

        override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) = Unit
    }

    /** Returns false if the device has no accelerometer or registration failed. */
    fun start(): Boolean {
        val manager = sensorManager ?: return false
        val sensor = manager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER) ?: return false
        // ~100 Hz, comfortably faster than the 40 Hz sampling grid
        // (SENSOR_DELAY_GAME is only ~50 Hz). Below 200 Hz, so no extra permission is needed.
        return manager.registerListener(listener, sensor, SENSOR_PERIOD_US)
    }

    fun stop() {
        sensorManager?.unregisterListener(listener)
    }
}

private const val SENSOR_PERIOD_US = 10_000
