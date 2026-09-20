package com.voxbuddy.voiceai;

import android.Manifest;
import android.content.Intent;
import android.os.Build;
import androidx.core.content.ContextCompat;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;

/**
 * JS-facing control for ConversationForegroundService — called from
 * startMicCapture()/stopMicCapture() in app-preview.html so the service's
 * lifetime always matches an actual active conversation, never longer.
 *
 * Also owns the RECORD_AUDIO runtime-permission request. Previously
 * nothing in this codebase ever called ActivityCompat/requestPermissions
 * for RECORD_AUDIO — the app relied entirely on the WebView's implicit
 * onPermissionRequest handling of getUserMedia() to somehow trigger the
 * native permission dialog. It never did (Android never registers the
 * dangerous permission as "used" until an app explicitly requests it,
 * which is also why it wasn't listed under Settings > Apps > VoxBuddy >
 * Permissions), so getUserMedia() failed instantly with no dialog ever
 * shown, and app-preview.html's catch block printed the generic
 * "Microphone permission denied." for what was actually "never asked."
 * The @Permission alias below makes Capacitor's Plugin base class
 * auto-implement checkPermissions()/requestPermissions() as real,
 * callable PluginMethods that go through ActivityCompat.requestPermissions
 * — see requestMicPermissionUpfront() and startMicCapture() in
 * app-preview.html for where these are now called from JS.
 */
@CapacitorPlugin(
    name = "ConversationService",
    permissions = {
        @Permission(strings = { Manifest.permission.RECORD_AUDIO }, alias = "microphone")
    }
)
public class ConversationServicePlugin extends Plugin {

    @PluginMethod
    public void start(PluginCall call) {
        Intent intent = new Intent(getContext(), ConversationForegroundService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            ContextCompat.startForegroundService(getContext(), intent);
        } else {
            getContext().startService(intent);
        }
        call.resolve(new JSObject());
    }

    @PluginMethod
    public void stop(PluginCall call) {
        getContext().stopService(new Intent(getContext(), ConversationForegroundService.class));
        call.resolve(new JSObject());
    }
}
