package lt.duobiuradaras.data

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class EndpointUrlTest {

    @Test
    fun acceptsHttpAndHttpsUrls() {
        listOf(
            SettingsRepository.DEFAULT_ENDPOINT_URL,
            "http://10.0.2.2:8080/packets",
            "https://example.com",
            "https://example.com/",
            "https://api.example.com:8443/v1/packets?source=app",
            "HTTPS://EXAMPLE.COM/packets",
            "http://localhost:8080",
            "http://[::1]:8080/packets",
        ).forEach { assertTrue(it, isValidEndpointUrl(it)) }
    }

    @Test
    fun ignoresSurroundingWhitespace() {
        assertTrue(isValidEndpointUrl("  https://example.com/packets \n"))
    }

    @Test
    fun rejectsOtherSchemes() {
        listOf(
            "ftp://example.com/packets",
            "file:///sdcard/packets",
            "ws://example.com/packets",
            "mailto:someone@example.com",
        ).forEach { assertFalse(it, isValidEndpointUrl(it)) }
    }

    @Test
    fun rejectsUrlsWithoutSchemeOrHost() {
        listOf(
            "",
            "   ",
            "example.com/packets",
            "//example.com/packets",
            "/packets",
            "http://",
            "https://",
            "http:example.com",
            "http:///packets",
        ).forEach { assertFalse(it, isValidEndpointUrl(it)) }
    }

    @Test
    fun rejectsMalformedUrls() {
        listOf(
            "http://exa mple.com/packets",
            "https://example.com/pa ckets",
            "http://example.com:port/packets",
        ).forEach { assertFalse(it, isValidEndpointUrl(it)) }
    }
}
