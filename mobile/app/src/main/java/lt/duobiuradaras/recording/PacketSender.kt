package lt.duobiuradaras.recording

import kotlinx.serialization.json.Json
import okhttp3.HttpUrl
import okhttp3.OkHttpClient

/** Uploads packets to the endpoint (SPEC.md 5). No retries, no offline storage. */
class PacketSender(
    private val endpoint: HttpUrl,
    private val client: OkHttpClient = sharedHttpClient,
) {

    /** POSTs [packet] as JSON. Returns true on a 2xx response; see [postJson]. */
    suspend fun send(packet: Packet): Boolean =
        client.postJson(endpoint, Json.encodeToString(Packet.serializer(), packet))
}
