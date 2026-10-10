package lt.duobiuradaras.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ServerUrlTest {

    @Test
    fun acceptsHttpAndHttpsUrls() {
        listOf(
            SettingsRepository.DEFAULT_SERVER_URL,
            "http://10.0.2.2:8080/packets",
            "https://example.com",
            "https://example.com/",
            "https://api.example.com:8443/v1/packets?source=app",
            "HTTPS://EXAMPLE.COM/packets",
            "http://localhost:8080",
            "http://[::1]:8080/packets",
        ).forEach { assertTrue(it, isValidServerUrl(it)) }
    }

    @Test
    fun ignoresSurroundingWhitespace() {
        assertTrue(isValidServerUrl("  https://example.com/packets \n"))
    }

    @Test
    fun rejectsOtherSchemes() {
        listOf(
            "ftp://example.com/packets",
            "file:///sdcard/packets",
            "ws://example.com/packets",
            "mailto:someone@example.com",
        ).forEach { assertFalse(it, isValidServerUrl(it)) }
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
        ).forEach { assertFalse(it, isValidServerUrl(it)) }
    }

    @Test
    fun rejectsMalformedUrls() {
        listOf(
            "http://exa mple.com/packets",
            "https://example.com/pa ckets",
            "http://example.com:port/packets",
        ).forEach { assertFalse(it, isValidServerUrl(it)) }
    }

    @Test
    fun appendsRoutesToServerUrl() {
        listOf(
            "http://100.72.8.35:3000",
            "http://100.72.8.35:3000/",
            "  http://100.72.8.35:3000?x=1#frag \n",
        ).forEach {
            assertEquals(it, "http://100.72.8.35:3000/api/raw", rawDataUrlFor(it).toString())
            assertEquals(it, "http://100.72.8.35:3000/api/label", labelUrlFor(it).toString())
        }
    }

    @Test
    fun keepsServerPathPrefix() {
        assertEquals("https://example.com/v1/api/raw", rawDataUrlFor("https://example.com/v1/").toString())
        assertEquals("https://example.com/v1/api/label", labelUrlFor("https://example.com/v1").toString())
    }

    @Test
    fun invalidServerUrlGivesNoRoutes() {
        assertNull(rawDataUrlFor("not a url"))
        assertNull(labelUrlFor("ftp://example.com"))
    }
}
