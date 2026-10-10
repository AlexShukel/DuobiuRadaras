package lt.duobiuradaras.recording

import kotlinx.serialization.json.Json
import okhttp3.HttpUrl
import okhttp3.OkHttpClient

/** Uploads manual labels to the label endpoint (SPEC.md 5.1). No retries. */
class LabelSender(
    private val endpoint: HttpUrl,
    private val client: OkHttpClient = sharedHttpClient,
) {

    /** POSTs [label] as JSON. Returns true on a 2xx response; see [postJson]. */
    suspend fun send(label: Label): Boolean =
        client.postJson(endpoint, Json.encodeToString(Label.serializer(), label))
}
