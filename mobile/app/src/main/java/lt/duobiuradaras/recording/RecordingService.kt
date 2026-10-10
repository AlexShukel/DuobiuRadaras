package lt.duobiuradaras.recording

import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.PowerManager
import android.util.Log
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleService
import androidx.lifecycle.lifecycleScope
import kotlin.time.Duration.Companion.milliseconds
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.filterNotNull
import kotlinx.coroutines.flow.mapNotNull
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/** Foreground service that samples sensors and uploads packets (SPEC.md 3–5, 7). */
class RecordingService : LifecycleService() {

    private lateinit var notifications: RecordingNotifications

    private var accelerometer: AccelerometerSource? = null
    private var location: LocationSource? = null
    private var session: Job? = null
    private var wakeLock: PowerManager.WakeLock? = null

    /** Replaced on a mode switch; null when not recording. */
    @Volatile
    private var sender: PacketSender? = null

    override fun onCreate() {
        super.onCreate()
        notifications = RecordingNotifications(this)
        notifications.createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        val mode = intent?.getStringExtra(EXTRA_MODE)?.let(RecordingMode::valueOf)
        val endpoint = intent?.getStringExtra(EXTRA_ENDPOINT)
        when {
            intent?.action == ACTION_START && mode != null -> startRecording(mode, endpoint)
            intent?.action == ACTION_SWITCH && mode != null -> switchMode(mode, endpoint)
            else -> stopRecording() // ACTION_STOP, or anything unexpected
        }
        // A restart without the activity can't satisfy foreground-location rules.
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        stopSession()
        RecordingStatus.update { it.copy(isRecording = false, mode = null) }
        super.onDestroy()
    }

    private fun startRecording(mode: RecordingMode, endpointUrl: String?) {
        // Must happen promptly after startForegroundService(), before anything that can fail.
        if (!enterForeground(mode)) {
            stopRecording()
            return
        }

        stopSession()
        val endpoint = endpointUrl?.toHttpUrlOrNull()
        if (endpoint == null) {
            Log.e(TAG, "Invalid endpoint URL: $endpointUrl")
            stopRecording()
            return
        }

        RecordingStatus.update { RecordingState(isRecording = true, mode = mode) }

        val accelerometer = AccelerometerSource(this).also { this.accelerometer = it }
        val location = LocationSource(this).also { this.location = it }
        if (!accelerometer.start()) {
            Log.e(TAG, "No accelerometer available")
            stopRecording()
            return
        }
        if (!location.start()) {
            Log.e(TAG, "GPS unavailable or ACCESS_FINE_LOCATION not granted")
            stopRecording()
            return
        }

        // Keeps the CPU running with the screen off so the 25 ms sampling doesn't stall (SPEC.md 7).
        wakeLock = getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, WAKE_LOCK_TAG)
            .apply { acquire() }

        sender = PacketSender(endpoint)
        val sampler = Sampler(accelerometer.latest, location.latest)

        session = lifecycleScope.launch {
            launch(Dispatchers.Default) {
                sampler.run { packet ->
                    // Each upload runs on its own so a slow server never delays sampling.
                    // Read when the packet completes, so it goes to the URL active at that moment.
                    sender?.let { target ->
                        launch {
                            if (target.send(packet)) {
                                RecordingStatus.update { it.copy(sentPackets = it.sentPackets + 1) }
                            }
                        }
                    }
                }
            }
            launch {
                location.latest.filterNotNull().collect { point ->
                    RecordingStatus.update { it.copy(location = point) }
                }
            }
            launch {
                // Sensor events arrive at ~100 Hz; the UI only needs ~10 Hz.
                while (isActive) {
                    accelerometer.latest.value?.let { reading ->
                        RecordingStatus.update { it.copy(acceleration = reading) }
                    }
                    delay(UI_ACCELERATION_PERIOD)
                }
            }
            launch {
                RecordingStatus.state
                    .mapNotNull { state -> state.mode?.let { it to state.sentPackets } }
                    .distinctUntilChanged()
                    .collect { (mode, sentPackets) -> notifications.update(mode, sentPackets) }
            }
        }
    }

    /**
     * Sends further packets, including the one in progress, to [endpointUrl] (SPEC.md 6.2).
     * Sampling continues uninterrupted.
     */
    private fun switchMode(mode: RecordingMode, endpointUrl: String?) {
        val endpoint = endpointUrl?.toHttpUrlOrNull()
        if (session == null || endpoint == null) {
            Log.e(TAG, "Ignoring switch to $mode: not recording or invalid URL $endpointUrl")
            return
        }
        sender = PacketSender(endpoint)
        RecordingStatus.update { it.copy(mode = mode) }
    }

    /** Returns false if the system refused to start the foreground service. */
    private fun enterForeground(mode: RecordingMode): Boolean {
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
        } else {
            0
        }
        return try {
            ServiceCompat.startForeground(
                this,
                RecordingNotifications.NOTIFICATION_ID,
                notifications.build(mode, sentPackets = 0),
                type,
            )
            true
        } catch (e: RuntimeException) {
            // SecurityException without location permission; ForegroundServiceStartNotAllowedException
            // (an IllegalStateException) when started from the background on Android 12+.
            Log.e(TAG, "Could not start foreground service", e)
            false
        }
    }

    private fun stopRecording() {
        stopSession()
        RecordingStatus.update { it.copy(isRecording = false, mode = null) }
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    /** Stops sampling and uploads; the partial packet is discarded with the sampler. */
    private fun stopSession() {
        session?.cancel()
        session = null
        sender = null
        accelerometer?.stop()
        accelerometer = null
        location?.stop()
        location = null
        wakeLock?.takeIf { it.isHeld }?.release()
        wakeLock = null
    }

    companion object {
        private const val TAG = "RecordingService"
        private const val WAKE_LOCK_TAG = "DuobiuRadaras:recording"
        private val UI_ACCELERATION_PERIOD = 100.milliseconds

        private const val ACTION_START = "lt.duobiuradaras.action.START"
        private const val ACTION_SWITCH = "lt.duobiuradaras.action.SWITCH"
        private const val ACTION_STOP = "lt.duobiuradaras.action.STOP"
        private const val EXTRA_MODE = "lt.duobiuradaras.extra.MODE"
        private const val EXTRA_ENDPOINT = "lt.duobiuradaras.extra.ENDPOINT"

        /** Starts recording. Caller must already hold location permission and pass a valid URL. */
        fun start(context: Context, mode: RecordingMode, endpointUrl: String) {
            ContextCompat.startForegroundService(context, intent(context, ACTION_START, mode, endpointUrl))
        }

        /** While recording, sends packets to [endpointUrl] from now on, without stopping sampling. */
        fun switchTo(context: Context, mode: RecordingMode, endpointUrl: String) {
            context.startService(intent(context, ACTION_SWITCH, mode, endpointUrl))
        }

        private fun intent(context: Context, action: String, mode: RecordingMode, endpointUrl: String) =
            Intent(context, RecordingService::class.java)
                .setAction(action)
                .putExtra(EXTRA_MODE, mode.name)
                .putExtra(EXTRA_ENDPOINT, endpointUrl)

        fun stop(context: Context) {
            context.startService(Intent(context, RecordingService::class.java).setAction(ACTION_STOP))
        }
    }
}
