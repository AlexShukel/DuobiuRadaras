package lt.duobiuradaras.recording

import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlinx.coroutines.suspendCancellableCoroutine
import okhttp3.Call
import okhttp3.Callback
import okhttp3.HttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response

private val JSON_MEDIA_TYPE = "application/json".toMediaType()

/** One client per process so connections and threads are pooled across sessions. */
internal val sharedHttpClient: OkHttpClient by lazy {
    OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .writeTimeout(10, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        // Keep a stuck upload from piling up behind the next packets (one every 2 s).
        .callTimeout(10, TimeUnit.SECONDS)
        .build()
}

/**
 * POSTs [json] to [url]. Returns true on a 2xx response, false on any other response
 * or network error. Runs on OkHttp's dispatcher, so it never blocks the caller's thread;
 * cancelling the coroutine cancels the call.
 */
internal suspend fun OkHttpClient.postJson(url: HttpUrl, json: String): Boolean {
    // ByteArray body keeps the header exactly "application/json" (a String body adds a charset).
    val body = json.encodeToByteArray().toRequestBody(JSON_MEDIA_TYPE)
    val request = Request.Builder().url(url).post(body).build()
    val call = newCall(request)

    return suspendCancellableCoroutine { continuation ->
        continuation.invokeOnCancellation { call.cancel() }
        call.enqueue(object : Callback {
            override fun onResponse(call: Call, response: Response) {
                val success = response.use { it.isSuccessful }
                continuation.resume(success)
            }

            override fun onFailure(call: Call, e: IOException) {
                continuation.resume(false)
            }
        })
    }
}
