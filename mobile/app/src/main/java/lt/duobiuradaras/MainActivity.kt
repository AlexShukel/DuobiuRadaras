package lt.duobiuradaras

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import lt.duobiuradaras.ui.MainRoute
import lt.duobiuradaras.ui.theme.DuobiuRadarasTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            DuobiuRadarasTheme {
                MainRoute()
            }
        }
    }
}
