package com.voxbuddy.app;

import android.content.Intent;
import android.os.Build;
import androidx.core.content.ContextCompat;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * JS-facing control for ConversationForegroundService — called from
 * startMicCapture()/stopMicCapture() in app-preview.html so the service's
 * lifetime always matches an actual active conversation, never longer.
 */
@CapacitorPlugin(name = "ConversationService")
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
