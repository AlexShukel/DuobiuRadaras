package lt.duobiuradaras.recording

import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.serialization.json.Json
import okhttp3.Call
import okhttp3.Callback
import okhttp3.HttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response

/** Uploads packets to the endpoint (SPEC.md 5). No retries, no offline storage. */
class PacketSender(
    private val endpoint: HttpUrl,
    private val client: OkHttpClient = sharedClient,
) {

    /**
     * POSTs [packet] as JSON. Returns true on a 2xx response, false on any other response
     * or network error. Runs on OkHttp's dispatcher, so it never blocks the caller's thread;
     * cancelling the coroutine cancels the call.
     */
    suspend fun send(packet: Packet): Boolean {
        // ByteArray body keeps the header exactly "application/json" (a String body adds a charset).
        val body = Json.encodeToString(Packet.serializer(), packet)
            .encodeToByteArray()
            .toRequestBody(JSON_MEDIA_TYPE)
        val request = Request.Builder().url(endpoint).post(body).build()
        val call = client.newCall(request)

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

    companion object {
        private val JSON_MEDIA_TYPE = "application/json".toMediaType()

        /** One client per process so connections and threads are pooled across sessions. */
        private val sharedClient: OkHttpClient by lazy {
            OkHttpClient.Builder()
                .connectTimeout(5, TimeUnit.SECONDS)
                .writeTimeout(10, TimeUnit.SECONDS)
                .readTimeout(10, TimeUnit.SECONDS)
                // Keep a stuck upload from outliving the next packet by much.
                .callTimeout(15, TimeUnit.SECONDS)
                .build()
        }
    }
}
