package lt.duobiuradaras.recording

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationManager
import android.os.Looper
import androidx.core.content.ContextCompat
import androidx.core.location.LocationListenerCompat
import androidx.core.location.LocationManagerCompat
import androidx.core.location.LocationRequestCompat
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/** Keeps the latest GPS fix (SPEC.md 3.2). */
class LocationSource(private val context: Context) {

    private val locationManager: LocationManager? = context.getSystemService(LocationManager::class.java)

    private val _latest = MutableStateFlow<GeoPoint?>(null)

    /** Null until the first fix after [start]; the first non-null value marks the first fix. */
    val latest: StateFlow<GeoPoint?> = _latest.asStateFlow()

    private val _lastFixAtMillis = MutableStateFlow<Long?>(null)

    /**
     * `SystemClock.elapsedRealtime()` of the latest fix. Emits for every fix,
     * unlike [latest], which skips fixes with unchanged coordinates.
     */
    val lastFixAtMillis: StateFlow<Long?> = _lastFixAtMillis.asStateFlow()

    private val listener = object : LocationListenerCompat {
        override fun onLocationChanged(location: Location) {
            _latest.value = GeoPoint(location.latitude, location.longitude)
            _lastFixAtMillis.value = location.elapsedRealtimeNanos / 1_000_000
        }
    }

    /**
     * Requests GPS updates as fast as the hardware delivers them. Returns false if
     * `ACCESS_FINE_LOCATION` isn't granted or the device has no GPS provider.
     */
    fun start(): Boolean {
        val manager = locationManager ?: return false
        if (!LocationManagerCompat.hasProvider(manager, LocationManager.GPS_PROVIDER)) return false
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_FINE_LOCATION)
            != PackageManager.PERMISSION_GRANTED
        ) return false

        val request = LocationRequestCompat.Builder(0L)
            .setMinUpdateIntervalMillis(0L)
            .setMinUpdateDistanceMeters(0f)
            .setQuality(LocationRequestCompat.QUALITY_HIGH_ACCURACY)
            .build()
        LocationManagerCompat.requestLocationUpdates(
            manager,
            LocationManager.GPS_PROVIDER,
            request,
            listener,
            Looper.getMainLooper(),
        )
        return true
    }

    // removeUpdates() needs no permission; the compat annotation is overly broad.
    @SuppressLint("MissingPermission")
    fun stop() {
        locationManager?.let { LocationManagerCompat.removeUpdates(it, listener) }
    }
}
