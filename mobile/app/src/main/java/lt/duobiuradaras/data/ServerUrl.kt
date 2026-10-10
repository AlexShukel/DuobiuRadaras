package lt.duobiuradaras.data

import java.net.URI
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import java.net.URISyntaxException

/**
 * Returns true if [url] (ignoring surrounding whitespace) is an absolute
 * `http://` or `https://` URL with a host.
 */
fun isValidServerUrl(url: String): Boolean {
    val uri = try {
        URI(url.trim())
    } catch (e: URISyntaxException) {
        return false
    }
    val scheme = uri.scheme ?: return false
    if (!scheme.equals("http", ignoreCase = true) && !scheme.equals("https", ignoreCase = true)) {
        return false
    }
    return !uri.host.isNullOrBlank()
}

/** Raw data route under [serverUrl] (SPEC.md 5). Null if [serverUrl] isn't a valid URL. */
fun rawDataUrlFor(serverUrl: String): HttpUrl? = routeUnder(serverUrl, "api/raw")

/** Manual label route under [serverUrl] (SPEC.md 5.1). Null if [serverUrl] isn't a valid URL. */
fun labelUrlFor(serverUrl: String): HttpUrl? = routeUnder(serverUrl, "api/label")

/**
 * Appends [route] to the path of [serverUrl], so `http://host:3000` and `http://host:3000/`
 * both give `http://host:3000/api/raw`, and `http://host/prefix` gives `http://host/prefix/api/raw`.
 */
private fun routeUnder(serverUrl: String, route: String): HttpUrl? {
    val base = serverUrl.trim().toHttpUrlOrNull() ?: return null
    val segments = base.pathSegments.filter { it.isNotEmpty() } + route.split('/')
    return base.newBuilder()
        .encodedPath("/")
        .apply { segments.forEach(::addPathSegment) }
        .query(null)
        .fragment(null)
        .build()
}
