package lt.duobiuradaras.ui

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.annotation.StringRes
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.consumeWindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.input.TextFieldLineLimits
import androidx.compose.foundation.text.input.TextFieldState
import androidx.compose.foundation.text.input.rememberTextFieldState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import kotlinx.coroutines.launch
import lt.duobiuradaras.R
import lt.duobiuradaras.recording.Acceleration
import lt.duobiuradaras.recording.GeoPoint
import lt.duobiuradaras.recording.RecordingMode
import lt.duobiuradaras.recording.RecordingState
import lt.duobiuradaras.ui.theme.DuobiuRadarasTheme

/** Location permissions must be requested together; only fine location is required to record. */
private val LOCATION_PERMISSIONS = arrayOf(
    Manifest.permission.ACCESS_FINE_LOCATION,
    Manifest.permission.ACCESS_COARSE_LOCATION,
)

private fun recordingPermissions(): Array<String> =
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
        LOCATION_PERMISSIONS + Manifest.permission.POST_NOTIFICATIONS
    } else {
        LOCATION_PERMISSIONS
    }

private fun Context.isGranted(permission: String): Boolean =
    ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED

/** Connects [MainScreen] to [MainViewModel] and handles runtime permissions (SPEC.md 6.2). */
@Composable
fun MainRoute(viewModel: MainViewModel = viewModel(factory = MainViewModel.Factory)) {
    val state by viewModel.uiState.collectAsStateWithLifecycle()
    val context = LocalContext.current
    val snackbarHostState = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()
    val locationDeniedMessage = stringResource(R.string.location_permission_denied)
    // Mode whose Start button triggered the permission request; survives configuration changes.
    var pendingStartMode by rememberSaveable { mutableStateOf<RecordingMode?>(null) }

    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions(),
    ) { results ->
        val mode = pendingStartMode ?: return@rememberLauncherForActivityResult
        pendingStartMode = null
        val fineGranted = results[Manifest.permission.ACCESS_FINE_LOCATION]
            ?: context.isGranted(Manifest.permission.ACCESS_FINE_LOCATION)
        // A denied notification permission only hides the notification; recording still works.
        if (fineGranted) {
            viewModel.onModeButton(mode)
        } else {
            scope.launch { snackbarHostState.showSnackbar(locationDeniedMessage) }
        }
    }

    MainScreen(
        state = state,
        urlFields = viewModel.urlFields,
        snackbarHostState = snackbarHostState,
        onModeButton = { mode ->
            val missing = recordingPermissions().filterNot(context::isGranted)
            if (state.action(mode) == ModeAction.START && missing.isNotEmpty()) {
                pendingStartMode = mode
                permissionLauncher.launch(missing.toTypedArray())
            } else {
                viewModel.onModeButton(mode)
            }
        },
    )
}

@Composable
fun MainScreen(
    state: MainUiState,
    urlFields: Map<RecordingMode, TextFieldState>,
    snackbarHostState: SnackbarHostState,
    onModeButton: (RecordingMode) -> Unit,
    modifier: Modifier = Modifier,
) {
    Scaffold(
        modifier = modifier.fillMaxSize(),
        snackbarHost = { SnackbarHost(snackbarHostState) },
    ) { innerPadding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(innerPadding)
                .consumeWindowInsets(innerPadding)
                .imePadding()
                .verticalScroll(rememberScrollState())
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            Text(
                text = stringResource(R.string.app_name),
                style = MaterialTheme.typography.headlineMedium,
            )
            for (mode in RecordingMode.entries) {
                ModeSection(
                    mode = mode,
                    state = state,
                    field = urlFields.getValue(mode),
                    onClick = { onModeButton(mode) },
                )
            }
            LiveStatusCard(recording = state.recording)
        }
    }
}

/** URL field plus its Start / Switch / Stop button, for one [RecordingMode] (SPEC.md 6.1, 6.2). */
@Composable
private fun ModeSection(
    mode: RecordingMode,
    state: MainUiState,
    field: TextFieldState,
    onClick: () -> Unit,
) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        EndpointUrlField(mode = mode, state = state, field = field)
        ModeButton(
            mode = mode,
            action = state.action(mode),
            enabled = state.isButtonEnabled(mode),
            onClick = onClick,
        )
    }
}

@Composable
private fun EndpointUrlField(mode: RecordingMode, state: MainUiState, field: TextFieldState) {
    val showError = state.showUrlError(mode)
    val supportingTextRes = when {
        showError -> R.string.endpoint_url_invalid
        state.recording.isRecording -> R.string.endpoint_url_locked
        else -> null
    }
    OutlinedTextField(
        state = field,
        modifier = Modifier.fillMaxWidth(),
        enabled = state.areUrlsEditable,
        label = {
            Text(
                stringResource(
                    when (mode) {
                        RecordingMode.NORMAL -> R.string.endpoint_url_label
                        RecordingMode.CALIBRATION -> R.string.calibration_url_label
                    },
                ),
            )
        },
        supportingText = if (supportingTextRes != null) {
            { Text(stringResource(supportingTextRes)) }
        } else {
            null
        },
        isError = showError,
        keyboardOptions = KeyboardOptions(
            autoCorrectEnabled = false,
            keyboardType = KeyboardType.Uri,
            imeAction = ImeAction.Done,
        ),
        lineLimits = TextFieldLineLimits.SingleLine,
    )
}

@Composable
private fun ModeButton(mode: RecordingMode, action: ModeAction, enabled: Boolean, onClick: () -> Unit) {
    val colors = when (action) {
        ModeAction.STOP -> ButtonDefaults.buttonColors(
            containerColor = MaterialTheme.colorScheme.error,
            contentColor = MaterialTheme.colorScheme.onError,
        )
        ModeAction.SWITCH -> ButtonDefaults.filledTonalButtonColors()
        ModeAction.START -> ButtonDefaults.buttonColors()
    }
    val textRes = when (action) {
        ModeAction.STOP -> R.string.recording_stop
        ModeAction.SWITCH -> when (mode) {
            RecordingMode.NORMAL -> R.string.recording_switch_normal
            RecordingMode.CALIBRATION -> R.string.recording_switch_calibration
        }
        ModeAction.START -> when (mode) {
            RecordingMode.NORMAL -> R.string.recording_start
            RecordingMode.CALIBRATION -> R.string.recording_start_calibration
        }
    }
    Button(
        onClick = onClick,
        modifier = Modifier.fillMaxWidth(),
        enabled = enabled,
        colors = colors,
    ) {
        Text(stringResource(textRes))
    }
}

@Composable
private fun LiveStatusCard(recording: RecordingState) {
    val placeholder = stringResource(R.string.status_placeholder)
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            StatusRow(
                label = stringResource(R.string.status_state),
                value = stringResource(
                    when (recording.mode) {
                        RecordingMode.NORMAL -> R.string.status_state_normal
                        RecordingMode.CALIBRATION -> R.string.status_state_calibration
                        null -> R.string.status_state_off
                    },
                ),
            )
            HorizontalDivider()
            StatusRow(
                label = stringResource(R.string.status_sent_packets),
                value = if (recording.isRecording) recording.sentPackets.toString() else placeholder,
            )
            HorizontalDivider()
            val location = recording.location
            StatusRow(
                label = stringResource(R.string.status_location),
                value = when {
                    !recording.isRecording -> placeholder
                    location == null -> stringResource(R.string.status_waiting_for_gps)
                    else -> stringResource(
                        R.string.status_location_value,
                        location.latitude,
                        location.longitude,
                    )
                },
            )
            HorizontalDivider()
            Text(
                text = stringResource(R.string.status_accelerometer),
                style = MaterialTheme.typography.bodyMedium,
            )
            val acceleration = recording.acceleration.takeIf { recording.isRecording }
            AccelerationRow(R.string.status_axis_x, acceleration?.x, placeholder)
            AccelerationRow(R.string.status_axis_y, acceleration?.y, placeholder)
            AccelerationRow(R.string.status_axis_z, acceleration?.z, placeholder)
        }
    }
}

@Composable
private fun AccelerationRow(@StringRes axisLabel: Int, value: Float?, placeholder: String) {
    StatusRow(
        label = stringResource(axisLabel),
        value = if (value == null) {
            placeholder
        } else {
            stringResource(R.string.status_acceleration_value, value)
        },
        labelModifier = Modifier.padding(start = 16.dp),
    )
}

@Composable
private fun StatusRow(label: String, value: String, labelModifier: Modifier = Modifier) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = label,
            modifier = labelModifier.weight(1f),
            style = MaterialTheme.typography.bodyMedium,
        )
        Spacer(Modifier.width(16.dp))
        Text(
            text = value,
            style = MaterialTheme.typography.bodyLarge,
            // Monospace so live values don't shift around as digits change.
            fontFamily = FontFamily.Monospace,
            textAlign = TextAlign.End,
        )
    }
}

@Preview(showBackground = true)
@Composable
private fun MainScreenStoppedPreview() {
    DuobiuRadarasTheme {
        MainScreen(
            state = MainUiState(isLoaded = true, validUrls = RecordingMode.entries.toSet()),
            urlFields = previewUrlFields(),
            snackbarHostState = remember { SnackbarHostState() },
            onModeButton = {},
        )
    }
}

@Preview(showBackground = true)
@Composable
private fun MainScreenRecordingPreview() {
    DuobiuRadarasTheme {
        MainScreen(
            state = MainUiState(
                isLoaded = true,
                validUrls = RecordingMode.entries.toSet(),
                recording = RecordingState(
                    isRecording = true,
                    mode = RecordingMode.CALIBRATION,
                    sentPackets = 42,
                    location = GeoPoint(latitude = 54.687157, longitude = 25.279652),
                    acceleration = Acceleration(x = 0.12f, y = -0.31f, z = 9.81f),
                ),
            ),
            urlFields = previewUrlFields(),
            snackbarHostState = remember { SnackbarHostState() },
            onModeButton = {},
        )
    }
}

@Composable
private fun previewUrlFields(): Map<RecordingMode, TextFieldState> = mapOf(
    RecordingMode.NORMAL to rememberTextFieldState("http://10.0.2.2:8080/packets"),
    RecordingMode.CALIBRATION to rememberTextFieldState("http://10.0.2.2:8080/calibration"),
)
