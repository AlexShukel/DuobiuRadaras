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
        // GAME is ~50 Hz, faster than the 20 Hz sampling grid.
        return manager.registerListener(listener, sensor, SensorManager.SENSOR_DELAY_GAME)
    }

    fun stop() {
        sensorManager?.unregisterListener(listener)
    }
}
