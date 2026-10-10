package lt.duobiuradaras.data

import java.net.URI
import java.net.URISyntaxException

/**
 * Returns true if [url] (ignoring surrounding whitespace) is an absolute
 * `http://` or `https://` URL with a host.
 */
fun isValidEndpointUrl(url: String): Boolean {
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
